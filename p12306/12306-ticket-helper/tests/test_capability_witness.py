"""F4 能力可用性证明：**证明「请求真的发出去了」**。

为什么这一层必须存在
--------------------
本项目吃过一次很典型的亏：云打码**看起来配好了**，实际

- 超级鹰的表单是 ``user=&pass2=&softid=0`` —— 账号空、代码里配的 key **没进请求**、
  **图片根本没上传**（它要 multipart 的 ``file``，而我们只有 ``data``）；
- 云码图片传了但缺 ``type``；
- ``available()`` **只看「有没有填 key」**就放行；
- 失败被 ``log.warning`` / ``return None`` **吞掉**。

结果：使用者只看到「验证码识别失败」，**永远不知道请求压根没发出去**。

这种「假实现」用常规测试抓不到 —— 断言返回值时，代码和测试会一起说「没事」。
唯一可靠的判据是：**看请求长什么样**。

四个断言（每个能力都要过）
--------------------------
1. **请求真的到达**了 —— 否则 ``available()`` 说的「可用」就是假的；
2. **必需字段在** —— 缺一个就必然被对方拒绝，而调用者看不出来；
3. **响应真的被解析** —— 传了不等于读了；
4. **失败不被吞** —— 失败必须留下可观测痕迹（返回值 / ``last_error`` / 日志级别）。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from mock_server.echo import EchoServer  # noqa: E402
from ticket_helper.notify.channels import (  # noqa: E402
    DingTalkChannel,
    ServerChanChannel,
    TelegramChannel,
    VoiceChannel,
    WeComChannel,
)
from ticket_helper.verify.service import FreeSolver  # noqa: E402


def cfg(**kw: Any) -> Any:
    """渠道只用 ``getattr`` 读配置 —— 所以测试用简单对象即可，不必构造真实 dataclass。"""
    kw.setdefault("enabled", True)
    return SimpleNamespace(**kw)


#: 每个 HTTP 能力：名字 / 从哪里造 / 让它判成功的响应 / 请求里必须有的字段 / 期望路径 / 是否 critical_only
CAPABILITIES: list[tuple[str, Callable[[str], Any], dict, tuple[str, ...], str, bool]] = [
    (
        "serverchan",
        lambda url: ServerChanChannel(cfg(key="", webhook=url)),
        {"code": 0},
        ("title", "desp"),
        "/sc",
        False,
    ),
    (
        "dingtalk",
        lambda url: DingTalkChannel(cfg(webhook=url)),
        {"errcode": 0},
        ("msgtype",),
        "/dd",
        False,
    ),
    (
        "wecom",
        lambda url: WeComChannel(cfg(webhook=url)),
        {"errcode": 0},
        ("msgtype",),
        "/wc",
        False,
    ),
    (
        "telegram",
        lambda url: TelegramChannel(cfg(bot_token="BOT", chat_id="CHAT", endpoint=url)),
        {"ok": True},
        ("chat_id", "text"),
        "/tg",
        False,
    ),
    (
        "voice",
        lambda url: VoiceChannel(cfg(appcode="APP", phone="13800000000", tts_code="TTS", endpoint=url)),
        {"code": "0"},
        ("mobile", "content"),
        "/voice",
        True,  # critical_only：只在重大事件（出票成功）触发
    ),
]

_IDS = [c[0] for c in CAPABILITIES]


def _fields(record: Any) -> dict[str, str]:
    """把一次请求的**全部可见参数**摊平成字段表：JSON body / form body / query string。

    三者都要看 —— 实测发现语音渠道用 `params=` 传参（参数在 query string 里），
    只读 body 会得出「请求缺字段」的**错误结论**。
    这类「验错地方」的测试比没有测试更糟：它会让人去改本来正确的代码。
    """
    out: dict[str, str] = {}
    payload = record.json
    if isinstance(payload, dict):
        out.update({k: str(v) for k, v in payload.items()})
    else:
        out.update(dict(record.form))
    for key, values in record.query.items():
        out.setdefault(key, values[0] if values else "")
    return out


@pytest.mark.parametrize("name,make,ok_response,required,path,critical", CAPABILITIES, ids=_IDS)
def test_capability_really_sends_the_request(name, make, ok_response, required, path, critical):
    """① 请求真的到达 ② 必需字段在 ③ 响应被解析 —— 三件事一次验。

    这条用例的判据是**请求长什么样**，不是「函数返回了什么」——
    因为假实现的本质就是**请求没发对**，而返回值在那种情况下看起来一切正常。
    """
    with EchoServer() as echo:
        echo.respond(ok_response)
        channel = make(echo.url(path))
        assert channel.send("标题", "正文", critical=critical) is True, (
            f"{name} 发送失败：{channel.last_error}"
        )

        assert not echo.empty, (
            f"{name} 报告「发送成功」，但**回声服务一个请求都没收到** —— "
            "这就是典型的假实现（配置看着对、请求没发出去）"
        )
        record = echo.records[-1]
        assert record.path == path, f"{name} 打到了 {record.path}"

        fields = _fields(record)
        missing = [f for f in required if f not in fields or fields[f] == ""]
        assert not missing, f"{name} 的请求缺必需字段 {missing}；实际字段：{sorted(fields)}"

        # ③ 响应被解析：正文里要能看出内容确实被带上了
        joined = " ".join(fields.values())
        assert "标题" in joined or "正文" in joined, f"{name} 没把内容带进请求：{joined[:120]}"


@pytest.mark.parametrize("name,make,ok_response,required,path,critical", CAPABILITIES, ids=_IDS)
def test_capability_failure_is_observable(name, make, ok_response, required, path, critical):
    """④ 失败不被吞 —— 对方回错误时，必须留下**可观测**的痕迹。

    防的是：失败被 ``log.warning`` + ``return False`` 吞掉，
    使用者只看到「没发出去」，不知道是**为什么**。

    首轮实测就抓到两处真缺陷：`Channel.send` 的两条 early return
    （未启用 / 非重大事件）只回 False 不写原因；Telegram 在 ``ok`` 为假时也丢了原因。
    """
    with EchoServer() as echo:
        echo.respond({"errcode": 40013, "code": 40013, "ok": False, "msg": "拒绝", "errmsg": "拒绝"})
        channel = make(echo.url(path))
        result = channel.send("标题", "正文", critical=critical)
        assert result is False, f"{name} 在对方报错时仍返回成功"
        assert channel.last_error, f"{name} 失败时没有留下任何原因（last_error 为空）—— 这就是「被吞掉」"


def test_skipped_send_is_not_silent():
    """**这条直接来自首轮 F4 实测**：语音渠道「只接受重大事件」时，
    原先静默返回 False、`last_error` 为空 —— 调用方看到失败却查不到为什么。

    一个「因为没到触发条件所以跳过」和一个「真的发失败了」，
    在调用方眼里长得一模一样，这是必须区分的两件事。
    """
    channel = VoiceChannel(cfg(appcode="APP", phone="138", tts_code="T", endpoint="http://127.0.0.1:1/x"))
    assert channel.send("标题", "正文") is False  # 未传 critical
    assert "重大事件" in channel.last_error, channel.last_error

    disabled = DingTalkChannel(cfg(enabled=False, webhook="http://127.0.0.1:1/x"))
    assert disabled.send("标题", "正文") is False
    assert "未启用" in disabled.last_error, disabled.last_error


def test_free_solver_really_uploads_the_image():
    """**这一条直接针对「假实现」**：图片必须真的在请求体里，且非空。

    老的云打码实现里，超级鹰那条**图片根本没上传**（它要 multipart 的 file，
    而当时的 HttpClient 只有 data/json_body）。所以这里断言的是
    「请求体里有一段非空 base64」，而不是「函数返回了 None 或答案」。
    """
    image = bytes(range(256)) * 4  # 1024 字节的假图
    with EchoServer() as echo:
        echo.respond({"msg": "success", "result": [3, 7]})  # 上游协议：返回格子编号
        solver = FreeSolver(echo.url("/check"))
        assert solver.available() is True

        solver.solve(image, hint="login", kind="login")

        assert not echo.empty, "打码请求**根本没有发出去** —— 这正是当年那个假实现的样子"
        fields = _fields(echo.records[-1])
        blob = fields.get("img") or fields.get("image") or ""
        assert len(blob) > 0, f"请求里没有图片字段；实际字段：{sorted(fields)}"
        assert base64.b64decode(blob) == image, "请求里的图片与传入的不一致（被截断或编码错）"


def test_free_solver_failure_leaves_a_visible_trace():
    """失败必须可见 —— 老实现把异常吞在 ``log.debug`` 里，默认日志级别下**完全看不见**。"""
    with EchoServer() as echo:  # 默认响应 `{}` → 不是 success
        solver = FreeSolver(echo.url("/check"))
        assert solver.solve(b"1234") is None  # noqa: WEAK-ASSERT - 真正的判据是下面两行
        assert echo.records, "请求应已到达"
        assert solver.last_error, "失败没有留下任何可观测的原因（被吞掉了）"


def test_free_solver_without_endpoint_reports_why():
    """没配 endpoint 时也要说清「为什么不可用」，而不是静默返回 None。"""
    solver = FreeSolver("")
    assert solver.available() is False
    assert solver.solve(b"1234") is None  # noqa: WEAK-ASSERT - 真正的判据是下面一行
    assert solver.last_error, "不可用时必须说明原因"
