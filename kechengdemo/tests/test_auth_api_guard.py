# -*- coding: utf-8 -*-
"""`/api/auth/*` 的**权限边界**（源码级断言）—— 纯逻辑，不需要 PG。

    .venv\\Scripts\\python.exe tests\\test_auth_api_guard.py

## 守的是什么

`api_auth.py` 里混着两件**名字很像、权限完全不同**的事（模块 docstring 有说明）：

  · **登录本系统**（`/session`、`/me`）—— 必须**匿名可访问**（否则没人登得进来）；
  · **微伴/企微数据源凭据**（`/status`、`/login`、`/verify`）—— 会去
    「弹浏览器扫码拿 open_sid」，属于**操作类动作**，必须要求登录且角色足够。

改造前 `POST /login` 与 `POST /verify` **没有任何 guard**：未登录者（或匿名扫描器）
只要打到 8848 就能让服务端**弹一个浏览器窗口去扫码** —— 相当于把「在本机拉起 GUI 进程」
这个能力开放给了任何能连上端口的人。修法是加
`dependencies=[Depends(api_guard.require("operator", "admin"))]`。

## 为什么用"读源码"的方式测

这是**接线**问题，不是算法问题：接口能跑通、返回也对，问题在于"少挂了一个依赖"。
本项目没有前端/集成测试基建来覆盖「未登录请求被拒」，而 `GET /api/auth/status`
在 `auth_off`（本地开发默认）下**本来就不需要登录** —— 用真请求测会随环境飘。
所以钉住接线本身：guard 一旦被删，本文件立刻红。

⚠️ 断言的粒度是"关键语句存在"，不是逐字比对。
"""

#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t0"

import re
import sys
from pathlib import Path

from _harness import ROOT, check, summary  # noqa: E402

SRC = Path(ROOT) / "backend" / "api_auth.py"

#: 必须**带 guard** 的接口：会操作微伴/企微数据源凭据（弹浏览器、写 cookie）。
#: 值 = 允许的最低角色；断言用的是"出现了 `api_guard.require(`"这种接线存在性，
#: 角色清单另外单独断言，避免以后放宽角色时这里跟着静默放宽。
_GUARDED = ("/login", "/verify")

#: 必须**不带 guard** 的接口：登录本系统的入口。给它加 guard 会形成死锁
#: —— 没登录就进不去登录接口。
_PUBLIC = ("/session", "/me")


def _endpoint_src(text: str, path: str) -> str:
    """切出某个路由的装饰器 + 函数签名那一行（够断言 guard 的存在性）。"""
    i = text.find(f'@router.post("{path}"')
    if i < 0:
        i = text.find(f'@router.get("{path}"')
    if i < 0:
        return ""
    j = text.find("\n\n", i)          # 到该路由的签名/docstring 为止
    return text[i:j if j > 0 else i + 400]


def main():
    src = SRC.read_text(encoding="utf-8")

    check("导入了 api_guard（guard 才有可挂的东西）", "import api_guard" in src)
    check("导入了 Depends（FastAPI 依赖注入）",
          re.search(r"from fastapi import [^\n]*\bDepends\b", src) is not None)

    # ---- 数据源凭据类动作：必须要求登录 + 角色 ----
    for path in _GUARDED:
        seg = _endpoint_src(src, path)
        check(f"{path} 挂了 api_guard.require（不再匿名可触发扫码）",
              "api_guard.require(" in seg, seg[:160] or "没找到这个路由")
        check(f"{path} 只放行 operator / admin",
              '"operator"' in seg and '"admin"' in seg, seg[:160])

    # ---- 登录入口：必须保持匿名可访问 ----
    for path in _PUBLIC:
        seg = _endpoint_src(src, path)
        check(f"{path} 保持公开（没挂 guard，否则登录死锁）",
              seg != "" and "api_guard.require(" not in seg, seg[:160])

    # ---- 反向护栏：别用「给整个 router 挂依赖」的写法顺手把登录也锁了 ----
    head = src[:src.find("def _is_https")]
    check("router 是裸 APIRouter（guards 挂在单个路由上，不是一个全局依赖）",
          'router = APIRouter(prefix="/api/auth"' in head
          and "dependencies=" not in head, head[-200:])

    return summary()


if __name__ == "__main__":
    sys.exit(main())
