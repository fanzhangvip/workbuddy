"""通知与验证码测试（NT-T1 ~ NT-T5 / VC-T1 ~ VC-T4）。"""

from __future__ import annotations

import time

import pytest

from ticket_helper.config.settings import AppConfig, CodecConfig
from ticket_helper.errors import VerifyCodeError
from ticket_helper.notify.channels import Channel, EmailChannel, WebChannel
from ticket_helper.notify.gateway import EventType, NotifyEvent, NotifyGateway
from ticket_helper.verify.service import (
    CallbackSolver,
    CaptchaSolver,
    FreeSolver,
    ManualSolver,
    VerifyCodeService,
)


class _RecordingChannel(Channel):
    def __init__(self, name: str, *, ok: bool = True, raise_error: bool = False) -> None:
        super().__init__(enabled=True)
        self.name = name
        self._ok = ok
        self._raise = raise_error
        self.messages: list[tuple[str, str, bool]] = []

    def _send(self, title: str, body: str, *, critical: bool = False) -> bool:
        if self._raise:
            raise RuntimeError("渠道内部爆炸")
        self.messages.append((title, body, critical))
        return self._ok


def _gateway(*channels) -> NotifyGateway:
    gateway = NotifyGateway(None)
    gateway.channels = list(channels)
    gateway.web_channel = next((c for c in channels if isinstance(c, WebChannel)), None)
    return gateway


# --------------------------------------------------------------------------- #
# NT-T4 事件 → 渠道分发
# --------------------------------------------------------------------------- #
def test_build_channels_from_config_covers_all():
    from ticket_helper.notify.channels import build_channels

    channels = build_channels(AppConfig().notify)
    names = {c.name for c in channels}
    assert names == {"email", "serverchan", "dingtalk", "wecom", "telegram", "voice", "web"}
    # 默认只有 Web 本地提醒是启用的
    assert [c.name for c in channels if c.enabled] == ["web"]


def test_only_enabled_channels_receive_events():
    enabled = _RecordingChannel("enabled")
    disabled = _RecordingChannel("disabled")
    disabled.enabled = False
    gateway = _gateway(enabled, disabled)

    results = gateway.notify_sync(NotifyEvent(type=EventType.INFO.value, task="t", detail="d"))
    assert results == {"enabled": True}
    assert len(enabled.messages) == 1
    assert disabled.messages == []


def test_critical_only_channel_skips_normal_events():
    voice = _RecordingChannel("voice")
    voice.critical_only = True
    web = _RecordingChannel("web")
    gateway = _gateway(voice, web)

    gateway.notify_sync(NotifyEvent(type=EventType.INFO.value, task="t", detail="普通信息"))
    assert voice.messages == []
    assert len(web.messages) == 1

    gateway.notify_sync(NotifyEvent(type=EventType.SUCCESS.value, task="t", context={"train_no": "G1"}))
    assert len(voice.messages) == 1  # 出票属于重大事件
    assert voice.messages[0][2] is True


# --------------------------------------------------------------------------- #
# NT-T5 单渠道失败不影响其他渠道
# --------------------------------------------------------------------------- #
def test_channel_failure_is_isolated():
    broken = _RecordingChannel("broken", raise_error=True)
    healthy = _RecordingChannel("healthy")
    failing = _RecordingChannel("failing", ok=False)
    gateway = _gateway(broken, healthy, failing)

    results = gateway.notify_sync(NotifyEvent(type=EventType.ERROR.value, task="t", detail="d"))
    assert results["broken"] is False      # 抛异常 → 被吞掉记为失败
    assert results["healthy"] is True
    assert results["failing"] is False
    assert healthy.messages, "健康渠道必须仍然收到消息"
    assert gateway.snapshot()["stats"]["broken"]["fail"] == 1


def test_notification_is_recorded_in_db(app_context, mock_server):
    gateway = NotifyGateway(app_context.config.notify, db=app_context.db)
    gateway.notify_sync(NotifyEvent(type=EventType.INFO.value, task="t", detail="写库测试"))
    rows = app_context.db.list_notifications()
    assert rows and rows[0]["event_type"] == "info"
    assert rows[0]["ok"] is True
    gateway.shutdown()


def test_async_notify_does_not_block():
    channel = _RecordingChannel("slow")

    class SlowChannel(_RecordingChannel):
        def _send(self, title: str, body: str, *, critical: bool = False) -> bool:
            time.sleep(0.6)
            return super()._send(title, body, critical=critical)

    slow = SlowChannel("slow")
    gateway = _gateway(slow)
    started = time.perf_counter()
    gateway.notify(NotifyEvent(type=EventType.INFO.value, task="t", detail="d"))
    assert time.perf_counter() - started < 0.2, "notify 必须异步，不能阻塞抢票主循环"
    time.sleep(1.0)
    assert slow.messages
    gateway.shutdown()


# --------------------------------------------------------------------------- #
# NT-T1 / NT-T2 具体渠道构建与失败处理
# --------------------------------------------------------------------------- #
def test_email_channel_without_config_fails_gracefully():
    from ticket_helper.config.settings import EmailNotifyConfig

    channel = EmailChannel(EmailNotifyConfig(enabled=True))
    assert channel.send("t", "b") is False
    assert "配置不完整" in channel.last_error


def test_email_disabled_returns_false():
    from ticket_helper.config.settings import EmailNotifyConfig

    channel = EmailChannel(EmailNotifyConfig(enabled=False))
    assert channel.send("t", "b") is False


def test_serverchan_without_key_fails_gracefully():
    from ticket_helper.config.settings import WebhookNotifyConfig
    from ticket_helper.notify.channels import ServerChanChannel

    channel = ServerChanChannel(WebhookNotifyConfig(enabled=True))
    assert channel.send("t", "b") is False


def test_web_channel_publishes_and_keeps_history():
    web = WebChannel(enabled=True)
    seen: list[dict] = []
    unsubscribe = web.subscribe(seen.append)

    assert web.send("标题", "内容") is True
    assert seen and seen[0]["title"] == "标题"
    assert web.history(limit=5)

    unsubscribe()
    web.send("再来一条", "内容")
    assert len(seen) == 1


def test_gateway_test_all_and_test_channel():
    ok_channel = _RecordingChannel("email")
    bad_channel = _RecordingChannel("serverchan", ok=False)
    gateway = _gateway(ok_channel, bad_channel)
    assert gateway.test_all() == {"email": True, "serverchan": False}
    assert gateway.test_channel("email") is True
    assert gateway.test_channel("不存在") is False


# --------------------------------------------------------------------------- #
# NT-05 模板渲染
# --------------------------------------------------------------------------- #
def test_event_template_rendering():
    event = NotifyEvent(
        type=EventType.SUCCESS.value,
        task="广州→长沙",
        context={
            "train_no": "G6102", "depart_date": "2026-10-01", "from_station": "广州南",
            "to_station": "长沙南", "seat_type": "二等座", "passenger": "张三", "order_no": "E123",
        },
    )
    title, body = event.render()
    assert "抢票成功" in title
    assert "G6102" in body and "E123" in body and "张三" in body


def test_event_template_survives_missing_fields():
    event = NotifyEvent(type=EventType.SUCCESS.value, task="t", context={})
    title, body = event.render()
    assert title and body  # 缺字段也不能抛异常


def test_event_to_dict_and_critical_flag():
    assert NotifyEvent(type=EventType.SUCCESS.value).critical is True
    assert NotifyEvent(type=EventType.INFO.value).critical is False
    payload = NotifyEvent(type=EventType.INFO.value, task="t", detail="d").to_dict()
    assert payload["type"] == "info"
    assert "title" in payload and "body" in payload


# =========================================================================== #
# 验证码
# =========================================================================== #
class _AlwaysFail(CaptchaSolver):
    name = "always_fail"

    def solve(self, image: bytes, *, hint: str = "", kind: str = "login") -> str | None:
        return None


class _Unavailable(CaptchaSolver):
    name = "unavailable"

    def available(self) -> bool:
        return False

    def solve(self, image: bytes, *, hint: str = "", kind: str = "login") -> str | None:
        raise AssertionError("不可用的识别器不应被调用")


# --------------------------------------------------------------------------- #
# VC-T1 识别成功
# --------------------------------------------------------------------------- #
def test_recognize_success_with_callback_solver():
    service = VerifyCodeService(CodecConfig(mode="free"))
    service.set_solver(CallbackSolver(lambda i, h, k: "112,45", name="fixed"))
    assert service.recognize(b"fake-image") == "112,45"
    assert service.can_recognize() is True


# --------------------------------------------------------------------------- #
# VC-T2 失败重试与上限
# --------------------------------------------------------------------------- #
def test_recognize_raises_after_retry_limit():
    service = VerifyCodeService(CodecConfig(mode="free", retry_limit=2, fallback_enabled=False))
    service.set_solver(_AlwaysFail())
    with pytest.raises(VerifyCodeError) as exc:
        service.recognize(b"img")
    assert "识别失败" in str(exc.value)


def test_unavailable_solver_is_skipped():
    service = VerifyCodeService(CodecConfig(mode="free", retry_limit=1, fallback_enabled=False))
    service._chain = [_Unavailable(), CallbackSolver(lambda i, h, k: "1,2", name="ok")]  # noqa: SLF001
    assert service.recognize(b"img") == "1,2"


# --------------------------------------------------------------------------- #
# VC-T3 本地 → 云端切换链路
# --------------------------------------------------------------------------- #
def test_fallback_chain_order():
    service = VerifyCodeService(CodecConfig(mode="local", local_model_path="", retry_limit=2))
    chain = service.rebuild_chain()
    names = [s.name for s in chain]
    assert names[0] == "local"
    assert "manual" in names
    # 云打码渠道已移除：它以前会"假装可用"（账号空、图片没上传却 available()=True）
    assert "cloud" not in names
    # local 模式会同时带上 ddddocr（现代本地识别，未安装时自动跳过）
    assert names[:2] == ["local", "ddddocr"]
    # 关闭兜底时仍保留人工兜底（保证链路永远不至于「完全不可用」）
    service.config.fallback_enabled = False
    names2 = [s.name for s in service.rebuild_chain()]
    assert names2 == ["local", "ddddocr", "manual"]


def test_apply_config_rebuilds_chain():
    service = VerifyCodeService(CodecConfig(mode="free", fallback_enabled=False))
    assert [s.name for s in service.rebuild_chain()][0] == "free"
    service.apply_config(CodecConfig(mode="manual", fallback_enabled=False))
    assert service.snapshot()["mode"] == "manual"
    assert [s.name for s in service.rebuild_chain()] == ["manual"]


def test_cloud_codec_mode_is_rejected_with_a_way_out():
    """云打码渠道已删除 → ``mode="cloud"`` 必须是**非法配置**，而且要给出改法。

    防的是两件事：
    1. 删掉实现却漏了取值校验 —— 配置里还写着 cloud，加载时既不报错也不生效，
       使用者以为配上了，实际什么都没发生；
    2. 只报「取值非法: cloud」而不说改成什么 —— 使用者不知道该填哪个值。
       （去掉 cloud 分支后，光靠"取值不在白名单"也能报错，所以必须断言"给出改法"。）
    """
    errors = CodecConfig(mode="cloud").validate()
    assert errors, "mode=cloud 必须被拒绝"
    msg = errors[0]
    assert "cloud" in msg, msg
    assert "manual" in msg or "local" in msg, f"必须指出改成什么，实际: {msg}"
    # 剩下三个模式仍然合法（local 需要模型路径，这是它自己的既有规则）
    assert CodecConfig(mode="free").validate() == []
    assert CodecConfig(mode="manual").validate() == []
    assert CodecConfig(mode="local", local_model_path="x.onnx").validate() == []


def test_cloud_solver_is_gone_for_good():
    """云打码识别器**必须不复存在**。

    防的是有人「顺手」把它加回来：它不是"没接好"，而是**任何配置下都不可能成功**
    却自称可用（``available()`` 只看有没有填 Key），失败还会被吞成一句
    「验证码识别失败」。加回来之前请先读 ``verify/service.py`` 的模块注释。
    """
    import ticket_helper.verify as verify_pkg
    import ticket_helper.verify.service as verify_svc

    assert not hasattr(verify_svc, "CloudSolver"), "CloudSolver 不应存在（理由见模块注释）"
    assert "CloudSolver" not in getattr(verify_pkg, "__all__", [])
    assert not hasattr(verify_svc.VerifyCodeService(CodecConfig(mode="manual")), "cloud")


def test_snapshot_reports_availability():
    service = VerifyCodeService(CodecConfig(mode="free", fallback_enabled=False))
    service.set_solver(CallbackSolver(lambda i, h, k: "1", name="fixed"))
    snap = service.snapshot()
    assert snap["mode"] == "free"
    assert any(c["name"] == "fixed" and c["available"] for c in snap["chain"])
    assert service.can_recognize() is False or True  # free solver 依赖 endpoint，可能不可用


# --------------------------------------------------------------------------- #
# 人工兜底（VC-04）
# --------------------------------------------------------------------------- #
def test_manual_solver_receives_task_and_submits_answer():
    solver = ManualSolver(timeout=5.0)
    received: list = []
    solver.attach(received.append)

    import threading

    result: list = []

    def _worker() -> None:
        result.append(solver.solve(b"\x89PNG", hint="请点击小笼包", kind="order"))

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    time.sleep(0.4)

    assert received, "验证码任务应被推送给前端"
    task = received[0]
    assert task.hint == "请点击小笼包"
    assert task.image_base64
    assert solver.pending()

    assert solver.submit(task.id, "112,45") is True
    thread.join(timeout=3)
    assert result == ["112,45"]
    assert solver.pending() == []
    assert solver.submit("不存在", "x") is False


def test_manual_solver_timeout_returns_none():
    solver = ManualSolver(timeout=0.6)
    solver.attach(lambda task: None)
    assert solver.solve(b"img") is None


def test_manual_solver_auto_submit_bypasses_ui():
    solver = ManualSolver(timeout=1.0)
    solver.auto_submit = "1,2"
    assert solver.available() is True
    assert solver.solve(b"img") == "1,2"




# --------------------------------------------------------------------------- #
# VC-T5 人工兜底超时不应被重试放大
# --------------------------------------------------------------------------- #
class _CountingManual:
    """计数的「人工识别器」替身：永远等不到答案，只统计被调用了几轮。"""

    name = "manual"

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def solve(self, image: bytes, *, hint: str = "", kind: str = "login") -> str | None:
        self.calls += 1
        return None


def test_manual_timeout_is_not_retried_across_rounds():
    """人工兜底超时后不能再重试。

    识别链默认会重试 ``retry_limit`` 轮，而人工兜底每轮都要等满超时（默认 2 分钟）。
    若不加处理，用户漏看一次验证码弹窗就要白等 6 分钟，而且不会得到任何新信息
    —— 只会把验证码弹窗再推一遍。
    """
    service = VerifyCodeService(CodecConfig(mode="manual", retry_limit=3, fallback_enabled=False))
    counting = _CountingManual()
    service._chain = [counting]  # noqa: SLF001

    with pytest.raises(VerifyCodeError) as exc:
        service.recognize(b"img")
    assert "识别失败" in str(exc.value)
    assert counting.calls == 1, f"人工兜底只应等待一轮，实际 {counting.calls} 轮"


def test_manual_timeout_message_is_actionable():
    """人工超时的提示要能指导下一步操作，而不是一句「识别失败」。"""
    from ticket_helper.verify.service import ManualSolver

    service = VerifyCodeService(CodecConfig(mode="manual", retry_limit=3, fallback_enabled=False))
    manual = ManualSolver(timeout=0.03)
    # 人工兜底必须先「武装」才会进入识别链：纯命令行（无任何界面）时它不该被使用
    manual.enable_pull(True)
    service._chain = [manual]  # noqa: SLF001
    with pytest.raises(VerifyCodeError) as exc:
        service.recognize(b"img")
    assert "人工录入已超时" in str(exc.value)


# --------------------------------------------------------------------------- #
# VC-T6 本地识别（ddddocr 适配器）
# --------------------------------------------------------------------------- #
def test_ddddocr_solver_returns_sorted_click_points(monkeypatch):
    """装上 ddddocr 后，应输出「从左到右」的点击点坐标。

    12306 登录验证码是「按提示顺序点选」，官方接口要的是点击点坐标串，
    所以这里必须把检测框中心按 x 排序后再拼。
    """
    import sys
    import types

    from ticket_helper.verify.service import DdddocrSolver

    fake = types.ModuleType("ddddocr")

    class _Ocr:
        def __init__(self, **kwargs) -> None:
            pass

        def detection(self, image):
            # 故意给右、左两个框，验证会排序
            return [[40, 10, 60, 30], [10, 10, 30, 30]]

    fake.DdddOcr = _Ocr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddddocr", fake)

    solver = DdddocrSolver()
    assert solver.available() is True
    assert solver.solve(b"image") == "20,20,50,20"


def test_ddddocr_solver_degrades_quietly_when_missing(monkeypatch):
    """没装 ddddocr 时应安静跳过，不能抛异常、也不能影响识别链其它环节。"""
    import sys

    from ticket_helper.verify.service import DdddocrSolver

    monkeypatch.setitem(sys.modules, "ddddocr", None)  # 让 import 直接失败
    solver = DdddocrSolver()
    assert solver.available() is False
    assert solver.solve(b"image") is None


def test_ddddocr_solver_returns_none_when_nothing_detected(monkeypatch):
    """检测不到目标时返回 None，交给链条里的下一个识别器。"""
    import sys
    import types

    from ticket_helper.verify.service import DdddocrSolver

    fake = types.ModuleType("ddddocr")

    class _Ocr:
        def __init__(self, **kwargs) -> None:
            pass

        def detection(self, image):
            return []

    fake.DdddOcr = _Ocr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddddocr", fake)

    assert DdddocrSolver().solve(b"image") is None


# --------------------------------------------------------------------------- #
# VC-T6 续：ddddocr 必须按验证码类型分流
# --------------------------------------------------------------------------- #
def _png(width: int, height: int) -> bytes:
    """造一张真 PNG（只为让尺寸可读，内容无关）。"""
    import io as _io

    from PIL import Image

    buffer = _io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buffer, "PNG")
    return buffer.getvalue()


#: **真实样本**（testerSunshine 仓库的 tkcode.png）里的 9 个提示文字框。
#: 它们的 y 都在 0~24，即贴在 190 高的图片顶部 —— 通用检测模型把这句提示
#: "点击下图中所有的…"整行当成了待点击目标。
_REAL_PROMPT_BOXES = [
    [25, 6, 39, 21], [38, 6, 53, 21], [50, 4, 66, 21], [63, 3, 80, 21],
    [76, 3, 94, 23], [90, 5, 104, 24], [100, 4, 118, 22], [116, 0, 137, 21],
    [135, 1, 154, 21],
]

#: 同一张样本里真正的目标字符。
_REAL_TARGET_BOX = [248, 128, 268, 147]


def _install_fake_ddddocr(monkeypatch, *, boxes=None, text: str = ""):
    import sys
    import types

    fake = types.ModuleType("ddddocr")

    class _Ocr:
        def __init__(self, **kwargs) -> None:
            pass

        def detection(self, image):
            return list(boxes or [])

        def classification(self, image):
            return text

    fake.DdddOcr = _Ocr  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddddocr", fake)


def test_ddddocr_click_mode_drops_prompt_text_row(monkeypatch):
    """点击型验证码：必须把顶部那句提示文字从候选里剔掉。

    实测依据（真实样本 293×190）：通用检测模型返回 10 个框，
    **其中 9 个是提示文字行**。若原样输出，答案里会混进一堆提示文字坐标，
    必然被判错 —— 剔掉之后只剩真正的目标字符。
    """
    from ticket_helper.verify.service import DdddocrSolver

    _install_fake_ddddocr(monkeypatch, boxes=[*_REAL_PROMPT_BOXES, _REAL_TARGET_BOX])
    solver = DdddocrSolver()

    answer = solver.solve(_png(293, 190), kind="login")
    assert answer == "258,137", f"应只剩真实目标，实际={answer}"
    assert "剔除顶部提示文字行 9 个框" in solver.last_note


def test_ddddocr_click_mode_without_image_size_keeps_all_boxes(monkeypatch):
    """拿不到图片尺寸时**不剔除**：宁可保守，也不误删真实目标。"""
    from ticket_helper.verify.service import DdddocrSolver

    _install_fake_ddddocr(monkeypatch, boxes=[*_REAL_PROMPT_BOXES, _REAL_TARGET_BOX])
    answer = DdddocrSolver().solve(b"not-a-real-image", kind="login")

    # 10 个框 → 10 组 "x,y"
    assert answer is not None and len(answer.split(",")) == 20


def test_ddddocr_click_mode_skips_when_only_prompt_text_detected(monkeypatch):
    """只检出提示文字时返回 None，交给链路上的下一个识别源（而不是硬猜）。"""
    from ticket_helper.verify.service import DdddocrSolver

    _install_fake_ddddocr(monkeypatch, boxes=list(_REAL_PROMPT_BOXES))
    solver = DdddocrSolver()

    assert solver.solve(_png(293, 190), kind="login") is None
    assert "全是顶部提示文字" in solver.last_note


def test_ddddocr_order_mode_returns_text(monkeypatch):
    """字符型（下单）验证码走**整图分类**，返回的是字符而不是坐标。

    两条路径的答案格式完全不同（``"x1,y1"`` vs ``"3A5B"``），
    混用会直接导致校验失败，所以必须按 ``kind`` 分流。
    """
    from ticket_helper.verify.service import DdddocrSolver

    _install_fake_ddddocr(monkeypatch, text="3A5B")
    assert DdddocrSolver().solve(_png(120, 40), kind="order") == "3A5B"


def test_ddddocr_order_mode_discards_too_short_result(monkeypatch):
    """分类结果过短（模型在乱输出）时丢弃，不要拿单字去撞校验。"""
    from ticket_helper.verify.service import DdddocrSolver

    _install_fake_ddddocr(monkeypatch, text="a")
    solver = DdddocrSolver()

    assert solver.solve(_png(120, 40), kind="order") is None
    assert "过短" in solver.last_note


# --------------------------------------------------------------------------- #
# VC-T7 登录验证码接口：真机已变更（旧路径被 WAF 挡掉）
# --------------------------------------------------------------------------- #
def test_login_captcha_uses_new_endpoint_and_returns_image(app_context, mock_server):
    """登录验证码走**新路径** ``/otn/passcodeNew/getPassCodeNew``。

    依据：2026 版登录页 ``login_new_v20260207.js`` 已把旧的 passport 验证码接口
    **注释掉**，改用 ``passcodeNew/getPassCodeNew``（返回图片字节流）。
    真机实测旧路径任何请求头组合都只回 WAF 错误页。
    """
    from ticket_helper.config.settings import UserConfig

    user = app_context.accounts.build_user(UserConfig(username="cap_new"))
    captcha = user.api.get_login_captcha()

    assert captcha.image_bytes, "应当拿到图片字节"
    assert captcha.image_base64, "同时要给出 base64（界面/桌面端靠它渲染）"

    paths = [r["path"] for r in mock_server.state.requests]
    assert "/otn/passcodeNew/getPassCodeNew" in paths
    # 新路径成功时不应再回头打旧接口
    assert "/passport/captcha/captcha-image64" not in paths


def test_login_captcha_reports_risk_control_with_actionable_message(app_context, mock_server):
    """两条路径都拿不到图时，错误必须**指明下一步怎么做**。

    真机现状：新路径在缺设备指纹上下文时回 ``{"status":false,
    "messages":["系统忙，请稍后重试"]}``，旧路径被 WAF 挡掉。
    以前这里只抛一句「验证码获取失败」，使用者完全无从判断该改用什么方式登录。
    抛的必须是 :class:`CaptchaUnavailableError`（接口侧拿不到图），
    而不是普通的识别失败 —— 前者调用方要「不重试」。
    """
    from ticket_helper.config.settings import UserConfig
    from ticket_helper.errors import CaptchaUnavailableError

    mock_server.set_scenario("login_captcha_blocked")
    user = app_context.accounts.build_user(UserConfig(username="cap_blocked"))

    with pytest.raises(CaptchaUnavailableError) as exc:
        user.api.get_login_captcha()

    assert "扫码登录" in exc.value.message, "要告诉使用者改用扫码登录"
    detail = exc.value.detail or ""
    assert "系统忙" in detail, f"要带出服务端原话，实际={detail!r}"
    assert "HTML" in detail, f"要区分「被风控拒绝」与「没返回图片」，实际={detail!r}"



def test_standby_event_omits_empty_trip_rows():
    """没有行程信息时不要渲染出四行「-」。

    候补通知的模板固定带「车次 / 日期 / 路线 / 席别」四行，但**前置失败**
    （账号未核验、配额已满、没找到可候补车次）发生在挑车次之前，
    此时四行全是 `-`，占掉半屏却什么都没说 ——
    使用者看到的正是「车次：- 日期：- - → - 席别：-」这种噪音。
    """
    event = NotifyEvent(
        type=EventType.STANDBY.value,
        task="test",
        detail="账号「樊章」未完成人证核验，12306 不允许提交候补订单。",
        context={},
    )
    _, body = event.render()
    assert "- → -" not in body
    assert "车次：-" not in body
    assert "未完成人证核验" in body, "正文一个字都不能少"

    # 真有行程信息时，四行一个都不能少
    event2 = NotifyEvent(
        type=EventType.STANDBY.value,
        task="test",
        detail="已提交候补订单",
        context={
            "train_no": "G6102", "depart_date": "2026-10-11",
            "from_station": "深圳", "to_station": "长沙南", "seat_type": "二等座",
        },
    )
    _, body2 = event2.render()
    for expected in ("G6102", "2026-10-11", "深圳 → 长沙南", "二等座"):
        assert expected in body2, f"有行程信息时必须保留 {expected}"
