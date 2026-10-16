# -*- coding: utf-8 -*-
"""角色与权限（RBAC 最小集）验收 —— 架构 §6.1 / ADR-003。

    .venv\\Scripts\\python.exe tests/test_rbac.py

不需要数据库、不调用大模型：全部是纯函数 + 依赖函数级测试。

为什么必须测"权限配错"这一类
    权限判断写错了，系统**不会报错**，只会安静地放行 —— 直到某天有人用
    viewer 账号发布了规则、或清理了数据。这类缺陷没有运行期症状，只能靠穷举断言。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t0"

import base64
import os
import sys

from _harness import FAILED, PASSED, ROOT, check  # noqa: E402

import api_guard      # noqa: E402
import security       # noqa: E402





def basic(user, pwd):
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


class _Env:
    """临时改环境变量并还原（`config.get` 优先读 os.environ）。"""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        security._resolved.clear()
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        security._resolved.clear()
        return False


def test_single_account_default():
    print("\n--- 1. 未配 API_USERS 时：单账号 = admin（向后兼容）---")
    with _Env(API_USERS=None, API_USER="boss", API_PASSWORD="pw"):
        accs = security.accounts()
        check("只有一个账号", len(accs) == 1, str(accs))
        check("角色是 admin", accs[0][2] == "admin", str(accs[0]))
        check("verify 认这个账号", security.verify(basic("boss", "pw")) == "boss")
        check("role_of 正确", security.role_of("boss") == "admin")


def test_multi_account():
    print("\n--- 2. 多账号：不同的人拿到不同角色 ---")
    users = "zhang:pw1:reviewer,li:pw2:operator,root:pw3:admin"
    with _Env(API_USERS=users, API_USER=None, API_PASSWORD=None):
        accs = security.accounts()
        check("解析出 3 个账号", len(accs) == 3, str(len(accs)))
        check("★ 每个账号的角色正确",
              [r for _u, _p, r in accs] == ["reviewer", "operator", "admin"],
              str([r for _u, _p, r in accs]))
        check("verify 认每一个账号",
              all(security.verify(basic(u, p)) == u for u, p, _r in accs))
        check("未知账号被拒", security.verify(basic("hacker", "pw")) is None)
        check("错口令被拒", security.verify(basic("zhang", "wrong")) is None)


def test_malformed_never_escalates():
    print("\n--- 3. ★配置写错**绝不**静默提权 ---")
    with _Env(API_USERS="bad:pw:superuser,ok:pw2:viewer",
              API_USER=None, API_PASSWORD=None):
        accs = security.accounts()
        check("非法角色被跳过（不是降级成 admin）",
              all(r in security.ROLES for _u, _p, r in accs), str(accs))
        check("合法的那条仍生效", any(u == "ok" for u, _p, _r in accs), str(accs))
        check("★ 非法账号无法登录", security.verify(basic("bad", "pw")) is None)
    with _Env(API_USERS="a:b", API_USER="fallback", API_PASSWORD="fp"):
        accs = security.accounts()
        check("★ 整串解析失败时回退到单账号（而不是无权限可配）",
              len(accs) == 1 and accs[0][0] == "fallback", str(accs))
    with _Env(API_USERS="x::admin", API_USER="fallback", API_PASSWORD="fp"):
        check("★ 空口令的账号被拒绝（不能变成空口令可登录）",
              security.verify(basic("x", "")) is None)


def test_role_gate():
    print("\n--- 4. 接口守卫：角色不够必须 403 ---")
    users = "view:pw1:viewer,rev:pw2:reviewer,op:pw3:operator,root:pw4:admin"
    with _Env(API_USERS=users, API_AUTH="on"):

        def call_dep(user, allowed):
            tok = security.set_principal(user)
            try:
                api_guard.require(*allowed)()
                return 200
            except Exception as e:                  # noqa: BLE001
                return getattr(e, "status_code", 500)
            finally:
                security.reset_principal(tok)

        check("viewer 访问 admin 接口 → 403", call_dep("view", ("admin",)) == 403)
        check("★ reviewer 不能发布规则（需要 operator/admin）",
              call_dep("rev", ("operator", "admin")) == 403)
        check("operator 可以发布规则", call_dep("op", ("operator", "admin")) == 200)
        check("admin 可以清理数据", call_dep("root", ("admin",)) == 200)
        check("★ viewer 不能提交人工复核（需要 reviewer+）",
              call_dep("view", ("reviewer", "operator", "admin")) == 403)
        check("reviewer 可以提交人工复核",
              call_dep("rev", ("reviewer", "operator", "admin")) == 200)
        check("权限逐级递增：admin 能做 reviewer 的事",
              call_dep("root", ("reviewer", "operator", "admin")) == 200)


def main():
    print("=== 角色与权限验收（RBAC 最小集）===")
    test_single_account_default()
    test_multi_account()
    test_malformed_never_escalates()
    test_role_gate()
    print(f"\n===== {len(PASSED)} passed / {len(FAILED)} failed =====")
    if FAILED:
        print("失败：" + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
