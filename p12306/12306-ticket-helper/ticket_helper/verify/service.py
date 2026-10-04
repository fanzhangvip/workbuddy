"""验证码识别（VC-01 ~ VC-04）。

多源可插拔设计，按顺序尝试、失败自动切换（VC-04），并受 ``retry_limit`` 约束：

1. ``LocalModelSolver`` —— 本地模型（onnxruntime / 轻量自定义模型），零网络开销；
2. ``DdddocrSolver``    —— 本地 ONNX 识别（pip 可装，见该类说明）；
3. ``FreeSolver``       —— 社区免费打码接口（可配置 endpoint，默认不启用）；
4. ``ManualSolver``     —— **人工兜底**：把验证码图片推给 Web/桌面端，由使用者点选后回填。

**付费云打码（CloudSolver）已于 2026-09-29 整体移除**，原因是它**任何配置下都不可能成功**，
却会"假装可用"：抓真实请求体证实，超级鹰分支发的是 ``user=&pass2=&softid=0&codetype=9004``
（账号空、**图片根本没上传**，而 ``HttpClient`` 也没有 multipart 文件上传能力），
云码分支 ``username`` 为空；偏偏 ``available()`` 只看"有没有填 Key"，于是识别链会去调它、
必然失败，失败又被吞成一句「验证码识别失败」—— 使用者永远不知道它压根没发出去。
留一个必然会骗人的渠道，比没有这个渠道更糟。

关于「人工兜底」为什么要放在链里：个人的验证码识别模型往往因为官方改版而失效，
而验证码是**下单链路上唯一无法绕过的环节**。有一个人工通道，可以保证在模型失效时
工具仍然可用（降级为「半自动」而非「完全不可用」）。
"""

from __future__ import annotations

import base64
import io
import logging
import queue
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from ticket_helper.config.settings import CodecConfig
from ticket_helper.errors import VerifyCodeError

log = logging.getLogger(__name__)


@dataclass
class CaptchaTask:
    """一次待识别的验证码（推送到前端供人工处理）。"""

    id: str
    image_base64: str
    hint: str = ""
    kind: str = "login"
    created_at: float = field(default_factory=time.time)
    answer: str = ""
    answered_at: float = 0.0

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > 120

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "image": self.image_base64,
            "hint": self.hint,
            "kind": self.kind,
            "created_at": self.created_at,
            "answer": self.answer,
        }


class CaptchaSolver(ABC):
    """验证码识别器抽象。"""

    name = "base"
    #: 该识别器是否需要网络
    needs_network = False

    @abstractmethod
    def solve(self, image: bytes, *, hint: str = "", kind: str = "login") -> str | None:
        """返回识别结果（如 ``"112,45"``），无法识别返回 None。"""

    def available(self) -> bool:
        return True


class CallbackSolver(CaptchaSolver):
    """把识别委托给外部回调（测试注入 / 自定义模型）。"""

    name = "callback"

    def __init__(self, callback: Callable[[bytes, str, str], str | None], *, name: str = "callback") -> None:
        self._callback = callback
        self.name = name

    def solve(self, image: bytes, *, hint: str = "", kind: str = "login") -> str | None:
        return self._callback(image, hint, kind)


class ManualSolver(CaptchaSolver):
    """人工兜底：把验证码推给前端，等待使用者录入。

    两种协作模式：
    - **推送式**：通过 :meth:`attach` 注册推送函数（桌面端弹窗），验证码直接弹给用户；
    - **拉取式**：前端定时请求 ``/api/captcha/pending`` 主动取待处理验证码
      （Web 后台采用这种模式，因为 WebSocket 断了也不该漏掉验证码）。

    拉取式不需要注册任何回调，因此必须通过 :meth:`enable_pull` 显式「武装」，
    否则在纯命令行（无界面）场景下会白等一次超时。
    """

    name = "manual"

    def __init__(self, *, timeout: float = 120.0) -> None:
        self.timeout = timeout
        self._inbox: "queue.Queue[CaptchaTask]" = queue.Queue()
        self._pending: dict[str, CaptchaTask] = {}
        self._lock = threading.RLock()
        self._publishers: list[Callable[[CaptchaTask], None]] = []
        self.auto_submit: str | None = None
        #: 是否存在可以「拉取」验证码的前端界面（Web 后台 / 桌面端）
        self.pull_enabled = False

    def enable_pull(self, enabled: bool = True) -> None:
        """声明已有前端界面可人工处理验证码。"""
        with self._lock:
            self.pull_enabled = bool(enabled)

    def attach(self, publisher: Callable[[CaptchaTask], None]) -> None:
        with self._lock:
            self._publishers.append(publisher)

    def submit(self, task_id: str, answer: str) -> bool:
        """由前端调用，回填人工识别的结果。"""
        with self._lock:
            task = self._pending.get(task_id)
            if task is None:
                return False
            task.answer = answer
            task.answered_at = time.time()
        return True

    def pending(self) -> list[CaptchaTask]:
        with self._lock:
            return [t for t in self._pending.values() if not t.answer and not t.expired]

    def solve(self, image: bytes, *, hint: str = "", kind: str = "login") -> str | None:
        if self.auto_submit is not None:
            return self.auto_submit
        task = CaptchaTask(
            id=uuid.uuid4().hex[:10],
            image_base64=base64.b64encode(image).decode("ascii") if image else "",
            hint=hint,
            kind=kind,
        )
        with self._lock:
            self._pending[task.id] = task
            publishers = list(self._publishers)
        for publisher in publishers:
            try:
                publisher(task)
            except Exception as exc:  # noqa: BLE001
                log.error("验证码推送失败: %s", exc)

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            with self._lock:
                if task.answer:
                    answer = task.answer
                    self._pending.pop(task.id, None)
                    return answer
                if task.expired:
                    break
            time.sleep(0.2)
        with self._lock:
            self._pending.pop(task.id, None)
        log.warning("人工验证码超时未处理 (kind=%s)", kind)
        return None

    def available(self) -> bool:
        with self._lock:
            return bool(self._publishers) or self.auto_submit is not None or self.pull_enabled


class LocalModelSolver(CaptchaSolver):
    """本地模型识别。

    支持两种模型文件：
    - ``*.onnx``（需安装 onnxruntime）；
    - ``*.tflite`` / ``*.h5``（需安装 tensorflow，体积大，打包时可选）。

    模型缺失或依赖缺失时 ``available()`` 返回 False，自动跳过本环节，
    不会让整条识别链失败。
    """

    name = "local"

    def __init__(self, model_path: str = "") -> None:
        self.model_path = model_path
        self._session = None
        self._backend = ""
        self._load_attempted = False

    def _load(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True
        if not self.model_path:
            return
        from pathlib import Path

        path = Path(self.model_path)
        if not path.exists():
            log.warning("本地验证码模型不存在: %s", self.model_path)
            return
        if path.suffix.lower() == ".onnx":
            try:
                import onnxruntime  # type: ignore

                self._session = onnxruntime.InferenceSession(str(path), providers=["CPUExecutionProvider"])
                self._backend = "onnx"
                log.info("本地验证码模型加载成功(onnx): %s", path.name)
            except ImportError:
                log.warning("未安装 onnxruntime，本地模型不可用（pip install onnxruntime）")
            except Exception as exc:  # noqa: BLE001
                log.error("本地模型加载失败: %s", exc)
        else:
            log.warning("暂不支持的模型格式: %s（支持 .onnx）", path.suffix)

    def available(self) -> bool:
        self._load()
        return self._session is not None

    def solve(self, image: bytes, *, hint: str = "", kind: str = "login") -> str | None:
        self._load()
        if self._session is None or not image:
            return None
        try:  # pragma: no cover - 依赖真实模型
            import numpy as np
            from PIL import Image

            array = np.asarray(Image.open(io.BytesIO(image)).convert("L").resize((94, 40))).astype("float32") / 255.0
            array = array.reshape(1, 1, 40, 94)
            inputs = {self._session.get_inputs()[0].name: array}
            outputs = self._session.run(None, inputs)[0][0]
            digits = [int(i) for i in outputs]
            return _digits_to_answer(digits)
        except Exception as exc:  # noqa: BLE001
            log.debug("本地模型识别失败: %s", exc)
            return None


class FreeSolver(CaptchaSolver):
    """**本项目自己**的外部打码服务客户端（可配置 endpoint）。

    ⚠️ 它与上游（``pjialin`` 内核后端）**不是同一个协议**：

    ==============  ===========================  ==================================
    项              本类（``self`` 内核后端）      上游（``pjialin`` 内核后端）
    ==============  ===========================  ==================================
    请求字段        ``image``（base64）+ ``kind``  ``img``（base64）
    响应            ``{"answer": "x,y,…"}``       ``{"msg":"success","result":[编号…]}``
    坐标来源        服务端直接给                   上游 ``get_image_position_by_offset``
    ==============  ===========================  ==================================

    哪一套是「有实现的上游」：**上游那套**（需求方要求用它）。
    本类保留为回退后端，并且**不做坐标换算** —— 换算在
    ``ticket_helper/kernel/pjialin/captcha.py`` 里由上游代码完成。
    """

    name = "free"
    needs_network = True

    def __init__(self, endpoint: str = "", *, timeout: float = 10.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        #: 最近一次失败的原因。**必须可读** ——
        #: 老实现把异常吞在 `log.debug` 里，默认日志级别下完全看不见，
        #: 使用者只被告知「识别失败」，不知道是没发出去还是被拒绝。
        self.last_error = ""

    def available(self) -> bool:
        if not self.endpoint:
            self.last_error = "未配置 endpoint（免费打码需自行提供符合协议的地址）"
            return False
        return True

    def solve(self, image: bytes, *, hint: str = "", kind: str = "login") -> str | None:
        if not self.available():
            return None
        if not image:
            self.last_error = "图片为空，未发起请求"
            return None
        self.last_error = ""
        try:
            from ticket_helper.inter.base import HttpClient

            client = HttpClient(base_url="http://127.0.0.1", timeout=self.timeout, retries=0)
            response = client.post(
                self.endpoint,
                data={"image": base64.b64encode(image).decode("ascii"), "kind": kind},
                allow_retry=False,
                check_risk=False,
            )
            client.close()
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 这里原先是 `log.debug` + return None —— **失败被吞**。
            # 改成 WARNING + last_error：失败可以返回 None（好让识别链继续走下一个），
            # 但**必须留下可观测的痕迹**。
            self.last_error = f"请求失败: {exc}"
            log.warning("免费打码请求失败（endpoint=%s）: %s", self.endpoint, exc)
            return None

        payload = response.json or {}
        # **本项目自己的约定**：服务端直接给坐标 —— ``{"answer": "38,38,257,36"}``。
        # 注意它与上游 ``user`` 模式**不是同一个协议**（上游要格子编号），
        # 这一差异就是「老实现为什么配了也用不了」的答案，见
        # `ticket_helper/kernel/self/captcha.py` 的对照表。
        answer = str(payload.get("answer") or "").strip()
        if answer:
            return answer

        # 收到的是字典型别的 {msg, result} —— 那说明对端是按**上游**约定实现的。
        # 本后端不做换算（换算是上游后端的职责），但**必须说清原因**：
        # 否则使用者只看到「识别失败」，永远不知道是「协议对不上」。
        offsets = payload.get("result")
        if payload.get("msg") == "success" and offsets:
            self.last_error = (
                f"对方返回的是上游协议的格子编号 {offsets}，而本后端约定直接返回坐标"
                '（{"answer": "x,y"}）。请把内核切到上游实现：'
                "TICKET_HELPER_KERNEL_BACKENDS=captcha=pjialin（这也是默认值），"
                "或把 codec.user_endpoint 指向按 answer 应答的服务"
            )
            log.warning("打码协议不匹配：收到格子编号 %s，本后端要的是坐标", offsets)
            return None

        self.last_error = f"响应无法解析: {str(payload)[:120]}"
        log.warning("免费打码响应无法解析: %s", str(payload)[:200])
        return None


class VerifyCodeService:
    """识别链编排：按模式构建链 → 逐个尝试 → 失败重试直至上限。"""

    def __init__(
        self,
        config: CodecConfig | None = None,
        *,
        external_solver: CaptchaSolver | None = None,
    ) -> None:
        self.config = config or CodecConfig()
        self.manual = ManualSolver()
        self.local = LocalModelSolver(self.config.local_model_path)
        # 现代本地识别（ONNX，可选安装）——见 DdddocrSolver 的说明
        self.ddddocr = DdddocrSolver()
        # ③「外部打码服务」这**一个位置**由装配方注入（composition root），
        # 因为它是全流程里唯一需要按「当前用哪套实现」切换的环节：
        #   self     → 本项目自己的请求/响应约定（字段 image + {"answer": …}）
        #   pjialin  → 上游 user 模式（字段 img + {msg,result} + **上游坐标换算**）
        # 不注入时退回本项目实现，保证单元测试与改造前的行为一致。
        self.external = external_solver if external_solver is not None else FreeSolver()
        self._chain: list[CaptchaSolver] = []
        self._custom: CaptchaSolver | None = None
        self._lock = threading.RLock()
        self.rebuild_chain()

    # ------------------------------------------------------------------ #
    def set_solver(self, solver: CaptchaSolver) -> None:
        """注入自定义识别器（测试或自定义模型）。"""
        with self._lock:
            self._custom = solver

    def rebuild_chain(self) -> list[CaptchaSolver]:
        """按 ``codec.mode`` 与 fallback 开关构建识别链（VC-04）。"""
        mode = self.config.mode
        primary: list[CaptchaSolver]
        if mode == "local":
            primary = [self.local, self.ddddocr]
        elif mode == "manual":
            primary = [self.manual]
        else:  # free
            primary = [self.external, self.local]

        chain = list(primary)
        if self.config.fallback_enabled:
            for solver in (self.local, self.ddddocr, self.external, self.manual):
                if solver not in chain:
                    chain.append(solver)
        else:
            # 去重：``mode=manual`` 时 ``primary`` 里已经有人工兜底，
            # 再 append 一次会让同一张验证码**弹两次人工录入框**（一次请求收到两个弹窗），
            # 而且第一次录入的答案会被第二次覆盖。
            if self.manual not in chain:
                chain.append(self.manual)
        with self._lock:
            self._chain = chain
        return chain

    def set_external_solver(self, solver: CaptchaSolver | None) -> None:
        """替换「外部打码服务」这一个环节，并重建识别链。

        装配方（``runtime/context.py``）用它按当前配置注入对应实现；
        配置热更新后也要再调一次 —— 否则会**继续用旧地址**，
        而现象是「我明明改了地址，还是打不通」，极难联想。
        """
        with self._lock:
            self.external = solver if solver is not None else FreeSolver()
        self.rebuild_chain()

    def apply_config(
        self, config: CodecConfig, *, external_solver: CaptchaSolver | None = None
    ) -> None:
        """配置热更新。``external_solver`` 给了就一并换掉（见 :meth:`set_external_solver`）。"""
        self.config = config
        self.local.model_path = config.local_model_path
        self.local._load_attempted = False  # noqa: SLF001 - 允许重新加载模型
        if external_solver is not None:
            with self._lock:
                self.external = external_solver
        self.rebuild_chain()

    # ------------------------------------------------------------------ #
    def recognize(self, image: bytes, *, hint: str = "", kind: str = "login") -> str:
        """识别验证码；全部失败抛 :class:`VerifyCodeError`。"""
        with self._lock:
            chain = ([self._custom] if self._custom else []) + list(self._chain)

        attempts: list[str] = []
        manual_exhausted = False
        for round_index in range(max(1, self.config.retry_limit)):
            for solver in chain:
                if solver is None:
                    continue
                try:
                    if not solver.available():
                        continue
                except Exception:  # noqa: BLE001
                    continue
                try:
                    answer = solver.solve(image, hint=hint, kind=kind)
                except Exception as exc:  # noqa: BLE001
                    attempts.append(f"{solver.name}:异常({exc})")
                    continue
                if answer:
                    log.info("验证码识别成功 (来源=%s, 第%d轮)", solver.name, round_index + 1)
                    return answer
                attempts.append(f"{solver.name}:未识别")
                if solver is self.manual or getattr(solver, "name", "") == "manual":
                    # 人工兜底返回空 = 使用者没在时限内录入（或主动跳过）。
                    # 再重试一轮只是让他再等一个完整超时（默认 2 分钟），
                    # 三倍下来就是 6 分钟白等，而且他不会得到任何新信息。
                    #
                    # 这里按名字而不是对象身份判断：注入的替身/自定义识别器同样适用，
                    # 否则只能识别 service.manual 本体，测试与扩展都会踩坑。
                    manual_exhausted = True
            log.debug("第 %d 轮验证码识别全部失败", round_index + 1)
            if manual_exhausted:
                break

        raise VerifyCodeError(
            "验证码识别失败，已用尽全部识别源"
            + ("（人工录入已超时，可在界面中重新登录）" if manual_exhausted else ""),
            detail="; ".join(attempts[-6:]) if attempts else "no-solver",
        )

    def can_recognize(self) -> bool:
        with self._lock:
            chain = ([self._custom] if self._custom else []) + list(self._chain)
        return any(s is not None and s.available() for s in chain)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            chain = ([self._custom] if self._custom else []) + list(self._chain)
        return {
            "mode": self.config.mode,
            "retry_limit": self.config.retry_limit,
            "fallback_enabled": self.config.fallback_enabled,
            "manual_pull_enabled": self.manual.pull_enabled,
            "pending_captcha": len(self.manual.pending()),
            "chain": [{"name": s.name, "available": s.available()} for s in chain if s is not None],
        }


def _digits_to_answer(digits: list[int]) -> str:
    """4 位数字模型输出 → 点击坐标串。

    12306 的 8 张验证码图片按固定 2 行 4 列排布，每张图宽 72px 高 40px，
    图片中心坐标可由此推算（识别模型只输出 4 个数字下标时使用）。
    """
    coords = []
    for index in digits[:4]:
        index = max(0, min(7, int(index)))
        row, col = divmod(index, 4)
        coords.append(f"{col * 72 + 36},{row * 40 + 20}")
    return ",".join(coords)


class DdddocrSolver(CaptchaSolver):
    """用 ddddocr（ONNX）做本地识别（VC-03 的现代替代方案）。

    为什么不是 testerSunshine 那套模型
    ----------------------------------
    那个方案是 ``12306.image.model.h5``（60MB）+ ``model.v2.0.h5``，
    依赖 **TensorFlow 1.14 / Keras 2.2.4** —— 只支持 Python ≤ 3.7，
    在本项目（Python 3.13）上**无法加载**，而且 TF+Keras+OpenCV 约 600MB，
    对「免环境双击即用」的交付形态是灾难。

    ddddocr 是同一思路的现代实现：纯 ONNX、pip 可装、体积小得多。

    安装（可选，不装就走人工兜底）::

        pip install ddddocr

    两类验证码要分开处理（``kind`` 决定走哪条）
    ------------------------------------------
    12306 有两种形态完全不同的验证码，**不能用同一套逻辑**：

    ==============  ============  ====================  ==========
    kind            形态          答案格式              本适配器
    ==============  ============  ====================  ==========
    ``login``       点击型        ``"x1,y1,x2,y2"``    检测框 → 取中心点
    ``order``       字符型        ``"3A5B"``           整图分类 → 取字符
    ==============  ============  ====================  ==========

    实测记录（重要，别高估它）
    -------------------------
    拿真实登录验证码样本实测（293×190 的点击型图）：

    - ddddocr 的通用检测模型返回 **10 个框，其中 9 个是顶部那句提示文字**
      （"点击下图中所有的…"被当成了待点击目标）；
    - 真正的目标字符只检出 1 个。

    也就是说**通用模型对 12306 的点击型验证码并不适用** —— 字符集与题型都不同，
    专用模型（testerSunshine 那种）是专门训练过的。所以这里做了两件事：

    1. **剔除提示文字行**：点击型验证码顶部那行提示是固定布局，把它从候选里去掉，
       至少不会出现「答案里全是提示文字坐标」这种必然错误；
    2. **如实降级**：命中率仍然不建议指望。真正可靠的是**人工兜底**；
       而登录这条链路上，**扫码才是唯一可靠路径**（见 README）。
       不要因为「加了本地模型」就删掉人工兜底 —— 那是刻意保留的可靠路径。
    """

    name = "ddddocr"
    needs_network = False

    #: 提示文字行可能占据的画面高度比例（顶部）
    PROMPT_BAND_RATIO = 0.15

    #: 判定为「提示文字行」所需的最少框数（少于它不认为是成行文字）
    PROMPT_ROW_MIN_BOXES = 4

    def __init__(self) -> None:
        self._det = None
        self._cls = None
        self._load_attempted = False
        self._error = ""
        #: 最近一次识别的说明（排障用）
        self.last_note = ""

    def _load(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True
        try:
            import ddddocr  # type: ignore[import-not-found]

            # show_ad=False：避免库在控制台打印推广信息（会污染日志与 GBK 控制台）
            self._det = ddddocr.DdddOcr(det=True, show_ad=False)
            self._cls = ddddocr.DdddOcr(show_ad=False)
        except Exception as exc:  # noqa: BLE001 - 未安装是常态，不该报错
            self._error = str(exc)
            log.debug("ddddocr 不可用: %s", exc)

    def available(self) -> bool:
        self._load()
        return self._det is not None or self._cls is not None

    # ------------------------------------------------------------------ #
    def solve(self, image: bytes, *, hint: str = "", kind: str = "login") -> str | None:
        if not image:
            return None
        self._load()
        self.last_note = ""
        if kind in ("login", "login_click", "click"):
            return self._solve_click(image)
        return self._solve_text(image)

    # ------------------------------------------------------------------ #
    def detect_rects(self, image: bytes) -> list[tuple[int, int, int, int]]:
        """点击型：检出候选框，并剔除顶部提示文字行。**唯一实现处**。

        单独抽成公开方法，是因为它还服务于 `verify/captcha_server.py`
        （本机打码服务要拿**框**去换算格子编号，而不是拿点击点）。
        若各写一遍，两处的「剔提示行」迟早会分叉 ——
        而分叉后表现为「识别结果时好时坏」，极难定位。
        """
        self._load()
        if self._det is None:
            return []
        try:
            boxes = self._det.detection(image) or []
        except Exception as exc:  # noqa: BLE001
            log.debug("ddddocr 检测失败: %s", exc)
            return []

        rects: list[tuple[int, int, int, int]] = []
        for box in boxes:
            try:
                x1, y1, x2, y2 = (int(v) for v in box[:4])
            except Exception:  # noqa: BLE001
                continue
            if x2 > x1 and y2 > y1:
                rects.append((x1, y1, x2, y2))
        if not rects:
            self.last_note = "未检出任何候选框"
            return []

        kept, dropped = self._drop_prompt_row(rects, image)
        if dropped:
            self.last_note = f"剔除顶部提示文字行 {dropped} 个框，保留 {len(kept)} 个候选"
            log.debug("ddddocr: %s", self.last_note)
        if not kept:
            self.last_note = "候选框全是顶部提示文字，跳过（交给下一个识别源）"
            log.debug("ddddocr: %s", self.last_note)
        return kept

    def _solve_click(self, image: bytes) -> str | None:
        """点击型（登录验证码）：返回 ``"x1,y1,x2,y2"`` 形式的点击点。"""
        kept = self.detect_rects(image)
        if not kept:
            return None

        # 官方要求按从左到右的顺序给出点击点
        points = sorted(((x1 + x2) // 2, (y1 + y2) // 2) for x1, y1, x2, y2 in kept)
        return ",".join(f"{x},{y}" for x, y in points)

    def _drop_prompt_row(
        self, rects: list[tuple[int, int, int, int]], image: bytes
    ) -> tuple[list[tuple[int, int, int, int]], int]:
        """剔除顶部的提示文字行。返回 ``(保留的框, 剔除数量)``。

        点击型验证码顶部会画一句提示（"点击下图中所有的…"）。通用检测模型会把
        这句里的每个字都当成待点击目标 —— 实测 10 个框里有 9 个是提示文字。
        这些字排成**紧凑的一行且贴着顶边**，据此识别并剔除。
        """
        height = self._image_height(image)
        if height is None or len(rects) < self.PROMPT_ROW_MIN_BOXES:
            return rects, 0

        band = height * self.PROMPT_BAND_RATIO
        row = [
            r for r in rects
            if ((r[1] + r[3]) / 2) <= band and r[1] < band
        ]
        if len(row) < self.PROMPT_ROW_MIN_BOXES:
            return rects, 0
        # 同一行：纵向区间要明显重叠，否则可能只是碰巧都在顶部
        tops = [r[1] for r in row]
        bottoms = [r[3] for r in row]
        overlap = min(bottoms) - max(tops)
        if overlap <= 0:
            return rects, 0

        keep = [r for r in rects if r not in row]
        return keep, len(row)

    @staticmethod
    def _image_height(image: bytes) -> int | None:
        """取图片高度；拿不到就返回 None（此时不做剔除，宁可保守）。"""
        try:
            from PIL import Image  # type: ignore[import-not-found]

            with Image.open(io.BytesIO(image)) as im:
                return int(im.height)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ #
    def _solve_text(self, image: bytes) -> str | None:
        """字符型（下单验证码）：整图分类，返回字符（如 ``"3A5B"``）。"""
        if self._cls is None:
            return None
        try:
            text = self._cls.classification(image) or ""
        except Exception as exc:  # noqa: BLE001
            log.debug("ddddocr 分类失败: %s", exc)
            return None
        cleaned = "".join(ch for ch in str(text) if ch.isalnum())
        if len(cleaned) < 2:
            self.last_note = f"分类结果过短，丢弃: {text!r}"
            return None
        return cleaned
