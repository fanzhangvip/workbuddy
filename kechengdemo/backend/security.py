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
import db                          # 登录会话存库里（跨进程可见、可撤销、可过期）

#: 未启用鉴权时的审计主体（固定常量，非客户端可控）
LOCAL_ACTOR = "local"
#: 自动生成口令的长度
GEN_LEN = 20

#: 角色（权限由小到大）。顺序有意义：`>=` 比较用得上。
ROLES = ("viewer", "reviewer", "operator", "admin")

#: 各角色的职责边界（写在这里，供前端展示与文档引用）
ROLE_DESC = {
    "viewer":   "只读：看列表、看结论、看任务与审计",
    "reviewer": "可提交人工复核结论、标注评估集（不改配置、不写外部系统）",
    "operator": "可发起任务、调整运行时设置、执行需确认的回写动作",
    "admin":    "全部权限，含审计导出、数据清理、规则发布、建标签",
}

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
    # 逐个账号比对：口令为空的一律跳过（**配置缺失宁可拒绝，不能变成"空口令可登录"**）
    for exp_user, exp_pwd, _role in accounts():
        if not exp_pwd:
            continue
        # 定长比较，避免时序侧信道
        ok = (secrets.compare_digest(user, exp_user)
              & secrets.compare_digest(pwd, exp_pwd))
        if ok:
            return user
    return None


# ============================================================ 登录会话（Cookie）
#
# 背景（用户的直接反馈）：全站 Basic 的代价是**首屏弹浏览器原生登录框** —— 样式
# 不可控、不能「记住我」、看不出当前是谁、退出还要关浏览器。于是加一层服务端会话：
# 浏览器只拿一个**不透明 sid**（HttpOnly），口令不进前端存储；脚本/curl/测试
# 继续走 Basic（`verify` 那条路，两边并存、互不影响）。

#: Cookie 名。`HttpOnly`（JS 读不到）+ `SameSite=Lax`（跨站 POST 不带它，够挡 CSRF）。
COOKIE_NAME = "wb_session"
#: 勾选「记住我」的有效期（天）
REMEMBER_DAYS = 30
#: 未勾选时的有效期（小时）。**照样给服务端过期**，避免僵尸会话永远有效。
SESSION_HOURS = 12


def _session_ttl_seconds(remember: bool) -> int:
    if remember:
        days = int(config.get("SESSION_REMEMBER_DAYS") or REMEMBER_DAYS)
        return max(1, days) * 86400
    hours = float(config.get("SESSION_HOURS") or SESSION_HOURS)
    return max(0.1, hours) * 3600


def create_session(username: str, *, remember: bool = False,
                   user_agent: str = None, client_ip: str = None) -> str:
    """建一条登录会话，返回 sid（**只在不透明 token 里表达身份，不签名不塞用户信息**）。

    顺手清掉过期行：会话表只增不减会慢慢变成「谁都在」的垃圾堆，而清理逻辑放在这里
    最省事（每次登录一次 DELETE，代价可忽略）。
    """
    import secrets as _secrets

    sid = _secrets.token_urlsafe(32)
    db.execute("DELETE FROM login_session WHERE expires_at < now()")
    db.execute(
        """INSERT INTO login_session (sid, username, expires_at, user_agent, client_ip)
           VALUES (%s, %s, now() + make_interval(secs => %s), %s, %s)""",
        (sid, username, _session_ttl_seconds(remember),
         (user_agent or "")[:300] or None, (client_ip or "")[:64] or None))
    return sid


def resolve_session(sid: str | None) -> str | None:
    """sid → 用户名（过期/不存在 → None，并把过期行顺手删掉）。"""
    if not sid:
        return None
    try:
        row = db.query_one(
            """SELECT username, (expires_at <= now()) AS expired
                 FROM login_session WHERE sid=%s""", (sid,))
    except Exception:                              # noqa: BLE001  查库失败按未登录处理
        return None
    if not row:
        return None
    if row.get("expired"):
        revoke_session(sid)
        return None
    db.execute("UPDATE login_session SET last_seen_at=now() WHERE sid=%s", (sid,))
    return row.get("username")


def revoke_session(sid: str | None) -> None:
    """退出：删掉这条会话（幂等，不存在也不报错）。"""
    if not sid:
        return
    try:
        db.execute("DELETE FROM login_session WHERE sid=%s", (sid,))
    except Exception:                              # noqa: BLE001
        pass


def accounts() -> list:
    """全部可用账号：``[(user, password, role), ...]``。

    两种配置方式（**后者优先**）：

    · 单账号：`API_USER` + `API_PASSWORD` → 角色固定 `admin`
      （向后兼容，也是"只有一个人用"时的默认）
    · 多账号：`API_USERS=zhang:pwd:reviewer,li:pwd:operator,admin:pwd:admin`

    为什么用环境变量而不是建表：本系统是单机内部工具，账号数量是个位数；
    建 `actor` 表要配套增删改查、口令哈希、轮换界面 —— 那是给"几十人、要自助管理"
    的场景准备的（ARCHITECTURE §6.1 已把表设计留作将来）。**现在这层的价值是
    "不同的人拿到不同的权限"，不是"能自助注册"**。

    解析失败的条目**直接跳过**（并在启动时由 `startup_report()` 提示），
    绝不因为一行格式写错就让某个账号静默变成 admin。
    """
    raw = (config.get("API_USERS") or "").strip()
    out = []
    if raw:
        for part in raw.split(","):
            bits = part.strip().split(":")
            if len(bits) != 3:
                continue
            u, p, r = (x.strip() for x in bits)
            if u and p and r in ROLES:
                out.append((u, p, r))
    if out:
        return out
    user, pwd = credentials()
    return [(user, pwd, "admin")]


def role_of(user: str | None) -> str | None:
    """某账号的角色；查不到返回 None。"""
    if not user:
        return None
    for u, _p, r in accounts():
        if u == user:
            return r
    return None


def current_role() -> str | None:
    """当前请求主体的角色。

    · 已认证 → 该账号的角色
    · 鉴权关闭 → `admin`（`API_AUTH=off` 是显式逃生阀，启动会打印警告；
      既然允许任何人直接访问，就不该再假装拦得住 —— 拦不住的权限是坏权限）
    """
    if not enabled():
        return "admin"
    return role_of(current_principal())


def has_role(*allowed: str) -> bool:
    """当前主体是否具备 `allowed` 中任一角色。"""
    return current_role() in (allowed or ROLES)


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
    # 多账号时把「谁是什么角色」打出来 —— 权限配错是排查时最难想到的一类问题
    accs = accounts()
    if len(accs) > 1 or (config.get("API_USERS") or "").strip():
        who = "、".join(f"{u}({r})" for u, _p, r in accs) or "（解析失败，已回退单账号）"
        line += f"｜ 账号：{who}"
        raw = (config.get("API_USERS") or "").strip()
        if raw and not accs:
            line += "｜ ⚠️ API_USERS 格式应为 user:pwd:role（逗号分隔），当前解析不出任何账号"
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
