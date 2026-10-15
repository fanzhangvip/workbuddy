# -*- coding: utf-8 -*-
"""ctx_preprocess 单元测试（纯函数，不依赖数据库与模型）。

运行：
    python tests/test_ctx_preprocess.py
"""
# 统一输出编码（修 P2-2）：默认 GBK 控制台下，断言名里的 ↔ → 等字符会触发
# UnicodeEncodeError —— 看起来像测试失败，其实是环境问题。
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ctx_preprocess as cp  # noqa: E402

NOW = 1770000000          # 固定「当前时间」，让相对天数可精确断言
DAY = 86400


def _row(mid, direction="recv", msg_time=None, plain=None, content=None):
    return {
        "message_id": mid,
        "direction": direction,
        "msg_time": NOW if msg_time is None else msg_time,
        "plain": plain,
        "content": content,
    }


def _build(rows, **kw):
    return cp.build_llm_context(rows, now=NOW, **kw)


# ---------------------------------------------------------------- S1 归型

def test_classify_all_msgtypes():
    """12 种企微 msgtype 都能归成一行可读文本，且 URL 一律被抹掉。"""
    cases = [
        (1, {"msgtype": 1, "text": {"content": "你好"}}, "你好"),
        (2, {"msgtype": 2, "image": {"media": {"url": "https://cdn/a.jpg"}}}, "[图片]"),
        (3, {"msgtype": 3, "emotion": {"url": "https://cdn/e.png"}}, "[表情]"),
        (4, {"msgtype": 4, "link": {"title": "课程介绍", "url": "https://x.com/l"}}, "[链接] 课程介绍"),
        (5, {"msgtype": 5, "miniprogram": {"title": "小程序A"}}, "[小程序] 小程序A"),
        (6, {"msgtype": 6, "voice": {"attr": {"playtime": 12}, "media": {"url": "https://cdn/v"}}}, "[语音 12s]"),
        (7, {"msgtype": 7, "video": {"media": {"url": "https://cdn/v.mp4"}}}, "[视频]"),
        (8, {"msgtype": 8, "file": {"attr": {"filename": "报名表.pdf"}, "media": {"url": "https://cdn/f"}}}, "[文件] 报名表.pdf"),
        (9, {"msgtype": 9, "personal_card": {"name": "张三", "corp_name": "某公司"}}, "[名片] 张三 某公司"),
        (10, {"msgtype": 10, "forward": {"items": [{}, {}]}}, "[合并转发的聊天记录，共 2 条]"),
        (20, {"msgtype": 20, "mixed": {"msgitem": [{"text": {"content": "混合正文"}}]}}, "混合正文"),
        (21, {"msgtype": 21, "meeting_voice": {"voice": {"attr": {"filename": "通话"}}}}, "[语音通话] 通话"),
        (31, {"msgtype": 31, "revoke_msg": {"item": {"text": {"content": "撤回的原话"}}}}, "[撤回了一条消息]：撤回的原话"),
    ]
    for mt, plain, expected in cases:
        got = cp.classify(_row(f"m{mt}", plain=plain))["text"]
        assert got == expected, f"msgtype={mt} 期望 {expected!r}，实际 {got!r}"
        assert "http" not in got, f"msgtype={mt} 未去掉链接：{got!r}"


def test_plain_wins_over_content():
    row = _row("m1", plain={"msgtype": 1, "text": {"content": "来自 plain"}},
               content="来自 content")
    assert cp.classify(row)["text"] == "来自 plain"


def test_content_fallback_without_plain():
    row = _row("m1", plain=None, content="  只有 content  ")
    assert cp.classify(row)["text"] == "只有 content"


def test_msgtype_fallback_to_row_column():
    """plain 缺失时，msgtype 应从行上的列取，且裸链接退化成类型占位。"""
    item = cp.classify(_row("m1", plain=None, content="https://cdn/a.jpg") | {"msgtype": 2})
    assert item["msgtype"] == 2
    assert item["text"] == "[图片]"


def test_role_mapping():
    assert cp.role_of("send") == "销售"
    assert cp.role_of("recv") == "客户"
    assert cp.role_of(None) == "客户"


def test_type_label():
    assert cp.type_label(2) == "图片"
    assert cp.type_label(31) == "撤回消息"
    assert cp.type_label(None, "image") == "image"
    assert cp.type_label(999, None) == "其它"


# ---------------------------------------------------------------- S2 降噪

def test_drop_revoke_and_card():
    rows = [_row("a", plain={"msgtype": 31, "revoke_msg": {}}),
            _row("b", plain={"msgtype": 9, "personal_card": {"name": "李四"}}),
            _row("c", plain={"msgtype": 1, "text": {"content": "保留我"}})]
    ctx = _build(rows)
    assert ctx["stats"]["drop_reasons"] == {"revoke": 1, "card": 1}
    assert ctx["stats"]["kept"] == 1
    assert ctx["messages"][0]["message_id"] == "c"


def test_emotion_is_kept_as_placeholder():
    """表情保留 [表情] 占位——「态度差 −2」的信号可能就藏在一条表情里。"""
    ctx = _build([_row("a", plain={"msgtype": 3, "emotion": {"url": "https://cdn/e.png"}})])
    assert ctx["stats"]["kept"] == 1
    assert ctx["messages"][0]["text"] == "[表情]"


def test_pure_url_text_is_dropped_but_media_is_not():
    ctx = _build([
        _row("url", plain={"msgtype": 1, "text": {"content": "https://x.com/a"}}),
        _row("img", plain={"msgtype": 2, "image": {"media": {"url": "https://cdn/1.jpg"}}}),
    ])
    assert ctx["stats"]["drop_reasons"] == {"pure_url": 1}
    assert [m["message_id"] for m in ctx["messages"]] == ["img"]


def test_empty_text_is_dropped():
    ctx = _build([_row("a", plain={"msgtype": 1, "text": {"content": "   "}})])
    assert ctx["stats"]["drop_reasons"] == {"empty": 1}
    assert ctx["stats"]["kept"] == 0


def test_system_message_is_dropped():
    ctx = _build([_row("a", plain={"msgtype": 1, "text": {"content": "你已添加了张三为好友"}})])
    assert ctx["stats"]["drop_reasons"] == {"system": 1}


# ---------------------------------------------------------------- S3 窗口

def test_window_truncation_keeps_latest():
    rows = [_row(f"m{i}", msg_time=NOW - (10 - i) * DAY,
                 plain={"msgtype": 1, "text": {"content": f"第{i}条"}})
            for i in range(10)]
    ctx = _build(rows, opts=cp.CtxOptions(window=3))
    assert ctx["stats"]["available"] == 10
    assert ctx["stats"]["kept"] == 3
    assert ctx["stats"]["truncated"] is True
    assert [m["message_id"] for m in ctx["messages"]] == ["m7", "m8", "m9"]
    # 编号在窗口内从 #001 重新开始，保持紧凑可读
    assert [m["code"] for m in ctx["messages"]] == ["#001", "#002", "#003"]


def test_no_truncation_when_within_window():
    ctx = _build([_row("a", plain={"msgtype": 1, "text": {"content": "x"}})],
                 opts=cp.CtxOptions(window=60))
    assert ctx["stats"]["truncated"] is False


# ---------------------------------------------------------------- S4 编号与时间

def test_time_label_abs_and_relative():
    ctx = _build([_row("a", msg_time=NOW - 6 * DAY,
                       plain={"msgtype": 1, "text": {"content": "六天前说的话"}})])
    label = ctx["messages"][0]["time_label"]
    assert label.endswith("｜6天前"), label
    assert len(label.split("｜")[0]) == len("10-08 09:12")


def test_fmt_relative_buckets():
    assert cp.fmt_relative(NOW - 10, NOW) == "刚刚"
    assert cp.fmt_relative(NOW - 300, NOW) == "5分钟前"
    assert cp.fmt_relative(NOW - 7200, NOW) == "2小时前"
    assert cp.fmt_relative(NOW - DAY, NOW) == "昨天"
    assert cp.fmt_relative(NOW - 6 * DAY, NOW) == "6天前"
    assert cp.fmt_relative(0, NOW) == ""


def test_display_relative_can_be_disabled():
    ctx = _build([_row("a", msg_time=NOW - 6 * DAY,
                       plain={"msgtype": 1, "text": {"content": "x"}})],
                 opts=cp.CtxOptions(display_relative=False))
    assert "｜" not in ctx["messages"][0]["time_label"]


# ---------------------------------------------------------------- S5 渲染

def test_render_line_format():
    ctx = _build([
        _row("m1", direction="recv", msg_time=NOW - 2 * DAY,
             plain={"msgtype": 1, "text": {"content": "你好"}}),
        _row("m2", direction="send", msg_time=NOW - 2 * DAY,
             plain={"msgtype": 1, "text": {"content": "您好"}}),
    ])
    assert ctx["lines"][0].startswith("#001 [")
    assert ctx["lines"][0].endswith("] 客户: 你好")
    assert ctx["lines"][1].endswith("] 销售: 您好")
    assert ctx["text"] == "\n".join(ctx["lines"])


# ---------------------------------------------------------------- 契约与回链

def test_index_labels_are_consistent():
    rows = [_row(f"m{i}", msg_time=NOW - i * 60,
                 plain={"msgtype": 1, "text": {"content": f"第{i}条"}})
            for i in range(4)]
    ctx = _build(rows)
    assert set(ctx["index"]) == set(ctx["labels"]) == {"#001", "#002", "#003", "#004"}
    for code, line in ctx["labels"].items():
        assert line.startswith(code + " [")
    assert ctx["index"]["#001"] == "m3"        # 最早的在前
    assert ctx["index"]["#004"] == "m0"


def test_resolve_evidence_normalizes_codes():
    index = {"#001": "m1", "#012": "m12"}
    r = cp.resolve_evidence(["#012", "12", "  #1  "], index)
    assert r["codes"] == ["#012", "#012", "#001"]
    assert r["message_ids"] == ["m12", "m12", "m1"]
    assert r["invalid"] == []


def test_resolve_evidence_reports_invalid():
    index = {"#001": "m1"}
    r = cp.resolve_evidence(["#999", "", None, "#001"], index)
    assert r["message_ids"] == ["m1"]
    assert len(r["invalid"]) == 3


def test_stage_helpers_exposed():
    """每个 stage 都能单独调用（可单测性）。"""
    item = cp.classify(_row("m1", plain={"msgtype": 1, "text": {"content": "hi"}}))
    assert cp.should_drop(item, cp.CtxOptions()) is None
    picked, truncated = cp.select_window([item], cp.CtxOptions(window=5))
    assert truncated is False
    numbered = cp.annotate(picked, now=NOW)
    assert numbered[0]["code"] == "#001"
    assert cp.render_lines(numbered)[0].endswith("客户: hi")


def test_parse_msgtypes():
    assert cp.parse_msgtypes("31,9") == frozenset({31, 9})
    assert cp.parse_msgtypes("31，9，3") == frozenset({31, 9, 3})
    assert cp.parse_msgtypes(None) == frozenset()
    assert cp.parse_msgtypes("") == frozenset()
    assert cp.parse_msgtypes([1, "2"]) == frozenset({1, 2})


def test_media_of_and_is_media():
    row = _row("m1", plain={"msgtype": 8, "file": {"attr": {"filename": "a.pdf"},
                                                   "media": {"url": "https://cdn/f"}}})
    assert cp.is_media(8) is True
    assert cp.is_media(1) is False
    assert cp.media_of(row) == {"media_url": "https://cdn/f", "media_name": "a.pdf"}


def test_current_date_present():
    ctx = _build([_row("a", plain={"msgtype": 1, "text": {"content": "x"}})])
    assert len(ctx["current_date"]) == 10
    assert ctx["current_date"].count("-") == 2


def test_options_from_isolated_calls():
    """默认选项 = 保守规范化：不丢表情、不丢语气短句。"""
    opts = cp.CtxOptions()
    assert 3 not in opts.drop_msgtypes
    assert 31 in opts.drop_msgtypes and 9 in opts.drop_msgtypes
    ctx = _build([
        _row("a", plain={"msgtype": 3, "emotion": {"url": "https://cdn/e"}}),
        _row("b", plain={"msgtype": 1, "text": {"content": "哦"}}),
        _row("c", plain={"msgtype": 1, "text": {"content": "随便吧"}}),
    ])
    assert ctx["stats"]["kept"] == 3, "短语气消息不得被合并或丢弃"


# ---------------------------------------------------------------- 运行器

def _run_all():
    fns = sorted((n, f) for n, f in globals().items()
                 if n.startswith("test_") and callable(f))
    failed = []
    for name, fn in fns:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - len(failed)}/{len(fns)} 通过")
    if failed:
        print("失败用例：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
