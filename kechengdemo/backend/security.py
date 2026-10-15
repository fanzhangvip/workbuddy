# -*- coding: utf-8 -*-
"""应用层最小鉴权（HTTP Basic）+ 审计主体的服务端推导。

## 这一层解决什么

`api_auth.py` 管的是**微伴登录态**（cookie / open_sid），回答「本系统能不能访问微伴」；
本模块回答的是**另一个问题**：「谁能调用本系统」。

改造前所有 `/api/*` 都没有应用层鉴权 → 任何能访问 8848 的进程都能发起全量重评、
**真回写微伴客户分数与标签**、篡改人工复核结论。而 `audit_log.actor` 直接取请求体里的
字符串（`payload.get("reviewer") or "工作人员"`）→ **审计不能作为追责依据**。

## 为什么选 HTTP Basic Auth

1. **浏览器原生支持**：首次访问返回 401 + `WWW-Authenticate` → 浏览器弹出登录框；
   之后**同源**的页面、静态资源、`fetch`/XHR 都会自动带上凭据
   → **前端零改动**（已核实 `frontend/src/api.js` 没有自定义 `Authorization` 头）
2. **用户名即审计主体** → 顺带修掉「actor 由客户端自报」
3. 无 session 表、无前端存储、无 token 轮换逻辑 —— 对「单机本地工具」是合适的成本

## 开关与默认值

| 配置 | 默认 | 说明 |
|---|---|---|
| `API_AUTH` | `on` | `off` 可完全关闭（应急用，会打印警告） |
| `API_USER` | `admin` | 登录用户名 |
| `API_PASSWORD` | **自动生成** | 为空则生成 20 位强随机口令并写入 `.env`，启动日志打印一次 |

⚠️ **默认就是安全的**（auth on + 自动口令），而不是再留一个「写好了但没接线」的开关 ——
后者正是代码评审里点名的反模式：看代码的人以为防住了，实际没生效。

## 未开鉴权时的审计语义

没有服务端身份可推导时，`actor` 落**固定常量** `local`，客户端自报值只进
`detail.reported_actor`（明确标记为不可信）—— 绝不把客户端字符串当 `actor`。
"""
import base64
import secrets
import sys
from contextvars import ContextVar
from pathlib import Path

import config

#: 未启用鉴权时的审计主体（固定常量，非客户端可控）
LOCAL_ACTOR = "local"
#: 认证失败时的 realm 名（浏览器登录框标题）
REALM = "weiban-scoring"
#: 自动生成口令的长度
GEN_LEN = 20

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

#: 当前请求的已认证主体（由 ASGI 中间件设置）
_principal: ContextVar[str] = ContextVar("principal", default=None)
#: 进程内解析出的口令（首次访问时算一次，避免重复读盘/生成）
_resolved: dict = {}
#: 本次进程自动生成的口令（供启动日志打印；None = 用的是 .env 里已有的）
generated_password: str | None = None

_OFF = ("0", "false", "no", "off")


def enabled() -> bool:
    """是否启用应用层鉴权（`API_AUTH`，默认 on）。"""
    v = config.get("API_AUTH")
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() not in _OFF


def _write_env_value(key: str, value: str) -> None:
    """把 KEY=VALUE 写进 .env（有则替换该行，无则追加）。"""
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    hit = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            hit = True
            break
    if not hit:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def credentials(create: bool = True) -> tuple:
    """返回 `(user, password)`。

    `API_PASSWORD` 为空时（且 `create=True`）**自动生成强随机口令并写入 .env**，
    同时记进 :data:`generated_password` 供启动日志打印一次。
    """
    if _resolved.get("creds"):
        return _resolved["creds"]

    user = (config.get("API_USER") or "admin").strip() or "admin"
    pwd = config.get("API_PASSWORD") or ""
    if not pwd and create:
        global generated_password
        pwd = secrets.token_urlsafe(GEN_LEN)[:GEN_LEN]
        generated_password = pwd
        try:
            _write_env_value("API_PASSWORD", pwd)
        except Exception as e:                  # noqa: BLE001  写盘失败不影响本次运行
            print(f"[security] 写入 API_PASSWORD 到 .env 失败（本次仍生效）：{e}")
    _resolved["creds"] = (user, pwd)
    return _resolved["creds"]


def verify(header: str | None) -> str | None:
    """校验 `Authorization: Basic ...` 头。成功返回用户名，失败返回 None。"""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
        user, _, pwd = raw.partition(":")
    except Exception:                           # noqa: BLE001  畸形头一律视为未认证
        return None
    exp_user, exp_pwd = credentials()
    if not exp_pwd:                             # 没口令 → 无法认证（配置缺失，宁可拒绝）
        return None
    # 定长比较，避免时序侧信道
    ok = secrets.compare_digest(user, exp_user) & secrets.compare_digest(pwd, exp_pwd)
    return user if ok else None


# ---------------------------------------------------------------- 主体传递

def set_principal(who: str | None):
    """设置当前请求主体，返回可用于 reset 的 token（供 ASGI 中间件用）。"""
    return _principal.set(who)


def reset_principal(token) -> None:
    try:
        _principal.reset(token)
    except Exception:                           # noqa: BLE001
        pass


def current_principal() -> str | None:
    """当前请求的已认证主体（未认证/鉴权关闭时为 None）。"""
    return _principal.get()


def actor(reported: str | None = None) -> str:
    """**审计主体**：一律由服务端推导，**不采信客户端自报值**。

    · 已认证 → 返回登录用户名（服务端权威）
    · 未认证（鉴权关闭）→ 返回固定常量 `local`
    · `reported`（客户端自报）**不参与** `actor`，需要留痕时请用 :func:`actor_detail`
    """
    return current_principal() or LOCAL_ACTOR


def actor_detail(reported: str | None = None, **extra) -> dict:
    """审计 detail 的公共构造：把客户端自报值**标记为不可信**单独存放。"""
    d = dict(extra)
    r = (reported or "").strip()
    if r and r != actor():
        d["reported_actor"] = r             # 仅作参考，明确不是 actor
    return d


# ---------------------------------------------------------------- 启动自检

def startup_report() -> str:
    """启动时打印鉴权状态（含首次生成的口令）。"""
    if not enabled():
        return ("[security] ⚠️ 应用层鉴权已**关闭**（API_AUTH=off）：任何能访问本端口的"
                "进程都可发起评分与回写。仅限本机排障时使用。")
    user, pwd = credentials()
    if not pwd:
        return ("[security] ⚠️ 鉴权已开启但 API_PASSWORD 为空且无法生成 → "
                "所有请求都会被拒（401）。请在 .env 里设置 API_PASSWORD。")
    line = f"[security] 应用层鉴权：开启（Basic Auth）｜ 用户名 {user}"
    if generated_password:
        line += f"｜ 🔑 本次自动生成口令：{generated_password}（已写入 .env）"
    else:
        line += "｜ 口令取自 .env（API_PASSWORD）"
    return line


if __name__ == "__main__":                       # 手动查看/重置口令
    if "--reset" in sys.argv:
        _resolved.clear()
        try:
            ENV_PATH.write_text(
                "\n".join(ln for ln in ENV_PATH.read_text(encoding="utf-8").splitlines()
                          if not ln.strip().startswith("API_PASSWORD=")) + "\n",
                encoding="utf-8")
        except Exception:
            pass
        print("已清除 .env 中的 API_PASSWORD，下次启动会重新生成")
    else:
        print(startup_report())
