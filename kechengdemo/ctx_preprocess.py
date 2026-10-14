# -*- coding: utf-8 -*-
"""上下文预处理层：把「消息 → 文本」的逻辑收敛到一处。

取代原先散落在 4 处的重复实现：
  · ``backend/scoring_service.py::build_conversation``
  · ``analyze.py::messages_to_text``
  · ``wb_chat_export.py::extract``
  · ``wb_staff_export.py`` 的内联拼装

设计原则
  · **纯函数**：所有 stage 只吃 dict，不碰数据库、不调模型 → 可独立单测
  · **单一事实源**：msgtype 中文名一律来自 ``ww_chatdata.TYPE_NAME``，不再新增副本
  · **保守降噪**：只去掉「确定无意义」的内容；表情保留 ``[表情]`` 占位，
    因为「态度差 −2」「配合度低 −1」的信号可能藏在短消息与表情里
  · **可回链**：每条消息带 ``#编号``，并给出 编号 → message_id 索引，供证据回链

管道
  S1 ``classify``         归型：角色 / msgtype / 正文（plain 优先，content 回退）
  S2 ``should_drop``      降噪：撤回 / 名片 / 纯 URL / 系统消息 / 空文本
  S3 ``select_window``    取最新 N 条（标记是否截断；``_pinned`` 行不受上限约束）
  S4 ``annotate``         编号 #001 + 绝对时间 + 相对天数
  S5 ``render_lines``     渲染 ``#001 [10-08 09:12｜6天前] 客户: 内容``
  S6 ``build_llm_context`` 组装（主入口）

用法::

    from ctx_preprocess import build_llm_context, CtxOptions
    ctx = build_llm_context(rows)       # rows 按时间升序（本模块内部仍会再排序）
    ctx["text"]                         # 直接喂大模型
    ctx["index"]                        # {"#001": "<message_id>"} → 证据回链

消息条目（dict）字段来自 ``chat_message`` 表：
  message_id / msgid(=original_id) / direction(send|recv) / msg_time(秒) /
  content / msgtype(企微数值) / msg_type(微伴字符串) / plain(企微明文 JSONB) /
  content_state(pending|ok|empty|fail)
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ww_chatdata import TYPE_NAME, plain_text as _plain_text

__all__ = [
    "CtxOptions", "MEDIA_KINDS", "DROP_REASON_LABELS",
    "parse_msgtypes", "type_label", "role_of", "is_media", "media_of", "classify",
    "should_drop", "select_window", "annotate", "render_lines",
    "fmt_time_abs_rel", "fmt_relative", "resolve_evidence", "display_text",
    "build_llm_context",
]

# 需要按「类型占位」呈现的媒体消息（与前端原有 MEDIA_TYPES 一致）
MEDIA_KINDS = frozenset({2, 3, 6, 7, 8})
# 正文是裸文件名 / 裸地址、需要补「[类型名] 」前缀的类型
_BARE_MEDIA = frozenset({2, 3, 7, 8})

# 丢弃原因 → 中文说明（写进 scoring_run.detail，便于排查）
DROP_REASON_LABELS = {
    "empty": "无正文",
    "pure_url": "纯链接",
    "system": "系统消息",
    "revoke": "撤回消息",
    "card": "名片",
}

_URL_RE = re.compile(r"https?://\S+", re.I)
_WS_RE = re.compile(r"[ \t\u3000]+")

# 系统消息识别：只做「高置信度」匹配，避免误杀正常聊天
_SYSTEM_PATTERNS = (
    re.compile(r"^\[系统消息\]"),
    re.compile(r"^你已添加了.+为好友"),
    re.compile(r"^对方已通过你的好友申请"),
    re.compile(r"^以上是打招呼的内容"),
)


# ============================================================ 选项

@dataclass(frozen=True)
class CtxOptions:
    """预处理层选项。默认值即「保守规范化」：补时间 + 编号，只去确定无意义的。"""

    window: int = 60                                  # 保留最新多少条
    drop_msgtypes: frozenset = frozenset({31, 9})     # 整行丢弃：撤回 / 名片
    drop_pure_url: bool = True                        # 丢弃「纯链接」的文本消息
    drop_system: bool = True                          # 丢弃系统消息
    display_relative: bool = True                     # 时间是否附「N天前」


def parse_msgtypes(value: Any) -> frozenset:
    """把 "31,9" / "31，9" / [31, 9] / None 解析成 frozenset({31, 9})。"""
    if value is None or value == "":
        return frozenset()
    if isinstance(value, (set, frozenset, list, tuple)):
        out = {n for n in (_to_int(v) for v in value) if n is not None}
        return frozenset(out)
    out = set()
    for part in str(value).replace("，", ",").split(","):
        n = _to_int(part.strip())
        if n is not None:
            out.add(n)
    return frozenset(out)


# ============================================================ 小工具

def _to_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_plain(row: dict) -> Optional[dict]:
    """取行上的 plain 明文条目（DB 里是 JSONB，脚本里可能是 dict 或 JSON 字符串）。"""
    p = (row or {}).get("plain")
    if isinstance(p, dict):
        return p
    if isinstance(p, str) and p.strip():
        try:
            v = json.loads(p)
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


def _strip_urls(text: str) -> str:
    """去掉 http(s) 链接：CDN 地址带签名会过期，对语义判断也无价值。"""
    if not text:
        return ""
    out = _URL_RE.sub("", text)
    out = _WS_RE.sub(" ", out).strip()
    # 清掉去掉链接后残留的孤立标点（如「请看 https://x ，」→「请看」）
    out = re.sub(r"^[\s，。、；：,.;:!?！？]+", "", out)
    out = re.sub(r"[\s，。、；：,.;:]+$", "", out)
    return out.strip()


def _is_pure_url(text: str) -> bool:
    parts = [p for p in (text or "").split() if p]
    if not parts:
        return False
    return all(_URL_RE.fullmatch(p) for p in parts)


def _is_system(text: str) -> bool:
    s = (text or "").strip()
    return any(p.match(s) for p in _SYSTEM_PATTERNS)


# ============================================================ 统一映射（供接口层 / 前端 / 导出）

def type_label(msgtype: Any, msg_type: Any = None) -> str:
    """msgtype → 中文名。企微数值优先，回退微伴 type 字符串，再回退「其它」。"""
    mt = _to_int(msgtype)
    if mt is not None:
        name = TYPE_NAME.get(mt)
        if name:
            return name
    if msg_type:
        return str(msg_type)
    return "其它"


def role_of(direction: Any) -> str:
    """企微 direction → 自然语言角色。"""
    return "销售" if str(direction or "").strip().lower() == "send" else "客户"


def is_media(msgtype: Any) -> bool:
    return _to_int(msgtype) in MEDIA_KINDS


def media_of(row: dict) -> dict:
    """从企微明文里抽出媒体地址（与 api_chat._media_of 同源，供前端渲染）。"""
    plain = _as_plain(row) or {}
    mt = _to_int(plain.get("msgtype"))
    if mt is None:
        mt = _to_int((row or {}).get("msgtype"))
    url, name = "", ""
    if mt == 2:
        url = ((plain.get("image") or {}).get("media") or {}).get("url", "") or ""
        name = "图片"
    elif mt == 3:
        url = (plain.get("emotion") or {}).get("url", "") or ""
        name = "表情"
    elif mt == 6:
        v = plain.get("voice") or {}
        url = (v.get("media") or {}).get("url", "") or ""
        name = f"语音 {((v.get('attr') or {}).get('playtime') or '')}s".strip()
    elif mt == 7:
        url = ((plain.get("video") or {}).get("media") or {}).get("url", "") or ""
        name = "视频"
    elif mt == 8:
        f = plain.get("file") or {}
        url = (f.get("media") or {}).get("url", "") or ""
        name = (f.get("attr") or {}).get("filename") or "文件"
    return {"media_url": url, "media_name": name}


def display_text(row: dict) -> str:
    """单条消息的规范化展示文本（供后端接口下发给前端）。"""
    return classify(row)["text"]


# ============================================================ S1 归型

def _display_body(msgtype: Optional[int], text: str) -> str:
    """把原始正文规范化成「一行可读文本」。

    · 一律去掉 http(s) 链接
    · 媒体消息（图片/表情/视频/文件）给 ``[类型名]`` 占位，
      有可读文件名的保留（如 ``[文件] 报名表.pdf``）
    · 链接 / 小程序 / 名片 / 合并记录等，``plain_text`` 已自带 ``[类型]`` 前缀，原样保留
    · 文本消息（msgtype=1）没有正文时返回空串，交给降噪阶段按「无正文」丢弃，
      不能渲染成 ``[文本]`` 这种噪声占位
    """
    def _placeholder() -> str:
        return "" if (msgtype is None or msgtype == 1) else f"[{type_label(msgtype)}]"

    if not text:
        return _placeholder()
    cleaned = _strip_urls(text)
    if not cleaned:
        return _placeholder()
    # 只有「正文本身就是裸文件名/裸地址」的类型才需要补类型前缀
    if msgtype in _BARE_MEDIA and not cleaned.startswith("["):
        return f"[{type_label(msgtype)}] {cleaned}"
    return cleaned


def classify(row: dict) -> dict:
    """把一行 chat_message 归一成预处理层内部条目。

    · msgtype 以 ``plain.msgtype`` 为准，回退 ``row.msgtype``
    · 正文以 ``plain_text(plain)`` 为准，回退 ``row.content``
    """
    row = row or {}
    plain = _as_plain(row)

    mt = _to_int(plain.get("msgtype")) if plain else None
    if mt is None:
        mt = _to_int(row.get("msgtype"))

    raw = ""
    if plain:
        try:
            raw = (_plain_text(plain) or "").strip()
        except Exception:
            raw = ""
    if not raw:
        raw = (row.get("content") or "").strip()

    direction = str(row.get("direction") or "").strip().lower()
    return {
        "message_id": row.get("message_id"),
        "msgid": row.get("msgid") or row.get("original_id"),
        "direction": direction,
        "role": role_of(direction),
        "msg_time": _to_int(row.get("msg_time")) or 0,
        "msgtype": mt,
        "type_name": type_label(mt, row.get("msg_type")),
        "raw_text": raw,
        "text": _display_body(mt, raw),
        "content_state": row.get("content_state"),
    }


# ============================================================ S2 降噪

def should_drop(item: dict, opts: Optional[CtxOptions] = None) -> Optional[str]:
    """判断是否丢弃；返回丢弃原因，``None`` 表示保留。

    判定顺序有讲究：文本类消息先判「纯链接 / 系统消息」，再判「无正文」——
    否则一条只有 URL 的消息会被笼统记成 ``empty``，丢掉有用的诊断信息。
    """
    opts = opts or CtxOptions()
    mt = _to_int(item.get("msgtype"))

    if mt is not None and mt in opts.drop_msgtypes:
        if mt == 31:
            return "revoke"
        if mt == 9:
            return "card"
        return f"msgtype{mt}"

    text = (item.get("text") or "").strip()

    # 纯链接 / 系统消息只在「文本类」消息上判，否则会误杀媒体占位
    if mt is None or mt == 1:
        probe = (item.get("raw_text") or text or "").strip()
        if probe:
            if opts.drop_pure_url and _is_pure_url(probe):
                return "pure_url"
            if opts.drop_system and _is_system(probe):
                return "system"
        if not text:
            return "empty"
        return None

    return None if text else "empty"


# ============================================================ S3 窗口 / S4 编号时间

def select_window(items: List[dict], opts: Optional[CtxOptions] = None) -> Tuple[List[dict], bool]:
    """按时间升序取最新 ``opts.window`` 条，返回 (窗口条目, 是否被截断)。

    ``item["_pinned"]`` 的条目（架构 §4.4 的「关键历史消息」）**不受窗口上限约束**：
    它们恰恰因为是「最早被采纳过的证据」而会被窗口切掉——那样检索就白做了。
    ``_pinned`` 为空的常规输入下，行为与改造前**逐字一致**（既有 25 条单测不变）。
    """
    opts = opts or CtxOptions()
    key = lambda x: (x.get("msg_time") or 0, str(x.get("message_id") or ""))  # noqa: E731
    ordered = sorted(items, key=key)
    pinned = [x for x in ordered if x.get("_pinned")]
    rest = [x for x in ordered if not x.get("_pinned")]
    w = _to_int(opts.window) or 0
    truncated = False
    if w > 0 and len(rest) > w:
        rest = rest[-w:]
        truncated = True
    if pinned:
        return sorted(pinned + rest, key=key), truncated
    return rest, truncated


def fmt_relative(msg_time: Any, now: int) -> str:
    """相对时间：刚刚 / N分钟前 / N小时前 / 昨天 / N天前。"""
    t = _to_int(msg_time)
    if not t:
        return ""
    diff = max(0, int(now) - t)
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{diff // 60}分钟前"
    if diff < 86400:
        return f"{diff // 3600}小时前"
    days = diff // 86400
    return "昨天" if days == 1 else f"{days}天前"


def fmt_time_abs_rel(msg_time: Any, now: int, display_relative: bool = True) -> str:
    """绝对 + 相对时间：``10-08 09:12｜6天前``。绝对时间供人回查，相对天数供模型算时效。"""
    t = _to_int(msg_time)
    if not t:
        return ""
    abs_s = datetime.fromtimestamp(t).strftime("%m-%d %H:%M")
    if not display_relative:
        return abs_s
    rel = fmt_relative(t, now)
    return f"{abs_s}｜{rel}" if rel else abs_s


def annotate(items: List[dict], *, now: int = None,
             display_relative: bool = True, start: int = 1) -> List[dict]:
    """给窗口内条目编号并附上时间标签。编号即证据引用用的 ``#编号``。"""
    now = int(now if now is not None else time.time())
    out = []
    for i, it in enumerate(items, start):
        row = dict(it)
        row["index"] = i
        row["code"] = "#%03d" % i
        row["time_label"] = fmt_time_abs_rel(row.get("msg_time"), now, display_relative)
        out.append(row)
    return out


# ============================================================ S5 渲染

def render_lines(items: List[dict]) -> List[str]:
    """渲染成 ``#001 [10-08 09:12｜6天前] 客户: 内容``。"""
    lines = []
    for it in items:
        code = it.get("code") or ""
        tl = it.get("time_label") or ""
        head = f"{code} [{tl}] {it.get('role')}: " if tl else f"{code} {it.get('role')}: "
        lines.append(head + (it.get("text") or ""))
    return lines


# ============================================================ 证据回链

def normalize_code(raw: Any) -> Optional[str]:
    """公开别名：把模型可能写出的 "#12" / "12" / "#012" 统一成 "#012"。"""
    return _norm_code(raw)


def _norm_code(raw: Any) -> Optional[str]:
    """把模型可能写出的 "#12" / "12" / "#012" 统一成 "#012"。"""
    s = str(raw or "").strip()
    if not s:
        return None
    if s.startswith("#"):
        s = s[1:]
    n = _to_int(s)
    if n is None or n <= 0:
        return None
    return "#%03d" % n


def resolve_evidence(ids: Iterable[Any], index: Dict[str, str]) -> dict:
    """把模型输出的编号列表映射回 ``message_id``。

    返回 ``{"codes": [...], "message_ids": [...], "invalid": [...]}``。
    映射不到的编号进 ``invalid``，由调用方决定丢弃还是留痕。
    """
    index = index or {}
    codes, mids, invalid = [], [], []
    for raw in ids or []:
        code = _norm_code(raw)
        mid = index.get(code) if code else None
        if mid:
            codes.append(code)
            mids.append(mid)
        else:
            invalid.append(raw)
    return {"codes": codes, "message_ids": mids, "invalid": invalid}


# ============================================================ S6 组装（主入口）

def build_llm_context(rows: Iterable[dict], *, session_meta: dict = None,
                      opts: CtxOptions = None, now: int = None) -> dict:
    """把原始消息行组装成「可直接喂大模型 + 可回链证据」的上下文。

    返回::

        {
          "current_date": "2026-10-14",
          "text":          "#001 [10-08 09:12｜6天前] 客户: ...\\n#002 ...",
          "lines":         [...],
          "messages":      [{code, index, message_id, msgid, role, direction,
                             msg_time, msgtype, type_name, text}, ...],
          "index":         {"#001": "<message_id>", ...},
          "labels":        {"#001": "#001 [10-08 09:12｜6天前] 客户: ...", ...},
          "stats":         {...},
          "session_meta":  {...}
        }
    """
    opts = opts or CtxOptions()
    now = int(now if now is not None else time.time())
    rows = list(rows or [])

    # ``_pinned``（关键历史）是行级标记，classify 只认业务字段，故在这里补挂上去。
    classified = []
    for r in rows:
        it = classify(r)
        if (r or {}).get("_pinned"):
            it["_pinned"] = True
        classified.append(it)

    kept: List[dict] = []
    reasons: Counter = Counter()
    for it in classified:
        reason = should_drop(it, opts)
        if reason:
            reasons[reason] += 1
        else:
            kept.append(it)

    window_items, truncated = select_window(kept, opts)
    messages = annotate(window_items, now=now, display_relative=opts.display_relative)
    lines = render_lines(messages)

    return {
        "current_date": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
        "text": "\n".join(lines),
        "lines": lines,
        "messages": messages,
        "index": {m["code"]: m["message_id"] for m in messages},
        "labels": {c: ln for c, ln in zip((m["code"] for m in messages), lines)},
        "stats": {
            "total": len(rows),
            "kept": len(messages),
            "available": len(kept),
            "dropped": sum(reasons.values()),
            "truncated": truncated,
            "drop_reasons": dict(reasons),
            "window": opts.window,
            # 关键历史（_pinned）最终留下了几条 → 架构 §4.6 的 ctx_seg_key
            "key": sum(1 for m in messages if m.get("_pinned")),
            # B4（架构 §2.3）：有效信息量的**三个口径**。
            # 改造前只有「规范化后保留条数」(kept)，一个口径容易被钻空子：
            # 销售单方面刷屏也会让 kept 变大，但客户一个字没说 —— 那不是有效信息。
            "customer_msgs": sum(1 for m in messages if m.get("role") == "客户"),
            "sales_msgs": sum(1 for m in messages if m.get("role") == "销售"),
            "text_len": sum(len(m.get("text") or "") for m in messages),
            "first_msg_time": messages[0]["msg_time"] if messages else None,
            "last_msg_time": messages[-1]["msg_time"] if messages else None,
        },
        "session_meta": session_meta or {},
    }
