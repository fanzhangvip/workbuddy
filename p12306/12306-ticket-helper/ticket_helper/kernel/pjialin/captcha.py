"""打码域 · 上游写法（``pjialin/py12306``，**默认后端**）。

决策依据
--------
``docs/pjialin内核化总体架构设计方案v3.md`` §10-1/2/3：打码走
**P-A（用上游渠道 + 上游换算）**，渠道用上游的 **``user`` 模式**，
服务地址由我们自己提供（需求方禁止第三方付费平台，也禁止请求 pjialin 托管域）。

★ 关键：坐标换算**一行都不是我们写的**
--------------------------------------
上游 ``helpers/OCR.py`` 的三段串起来就是完整流程：

1. ``OCR.get_img_position(img)``          —— 按 ``AUTO_CODE_PLATFORM`` 分流；
2. ``get_image_by_free_site(img)``        —— POST 出去（``user`` 模式）、解析 ``{msg, result}``；
3. ``get_image_position_by_offset(pos)``  —— **格子编号 → x,y 坐标**（本项目缺的正是这一段）。

本文件只做三件「搬运」性质的事（决策 §10-16 允许，且不得更多）：

1. 把 PNG 字节转成上游要的 base64 字符串（上游从 12306 拿到的本来就是 base64）；
2. 把上游 ``Config`` 垫片的三个值设好（模式名 / 服务地址 / 超时）；
3. 把上游返回的 ``list[int]`` 拼成我们接口要的 ``"x,y,…"`` 字符串。

**没有第 4 件。** 这里不出现任何 URL 字面量 —— 地址由调用方给，
由 `tests/test_discipline.py` 的适配层检查机械保证。
"""

from __future__ import annotations

import base64

from ..domains.captcha import KIND_LOGIN, format_positions
from . import shim
from .shim import is_available

#: 与注册表键一致的唯一名字
NAME = "pjialin"

#: 上游用它表示「把图 POST 给自己配置的服务」这一模式。
#: 这是**上游自己的模式名**，不是我们发明的协议字面量。
USER_MODE = "user"


class PjialinCaptchaSolver:
    """用上游代码完成「图 → 点击坐标」。"""

    name = NAME

    def __init__(self, endpoint: str = "", *, timeout: float = 10.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.last_error = ""

    # ------------------------------------------------------------------ #
    def available(self) -> bool:
        if not is_available():
            self.last_error = "上游 vendor 未就位（vendor/pjialin 不存在）"
            return False
        if not self.endpoint:
            self.last_error = (
                "未配置打码服务地址（codec.user_endpoint）；"
                "可先启动本机服务：python -m ticket_helper.verify.captcha_server"
            )
            return False
        return True

    def solve(self, image: bytes, *, hint: str = "", kind: str = KIND_LOGIN) -> str | None:
        if not self.available():
            return None
        if kind != KIND_LOGIN:
            # 上游的 user 模式只做**点击型**；字符型（下单）它没有实现。
            # 如实说清，让识别链继续往下走（本地模型 / 人工兜底）。
            self.last_error = (
                f"上游 user 模式只支持点击型（kind={KIND_LOGIN}），收到 {kind!r}；"
                "字符型请走本地模型或人工兜底"
            )
            return None
        if not image:
            self.last_error = "图片为空，未发起请求"
            return None

        self.last_error = ""
        try:
            positions = self._call_upstream(image)
        except Exception as exc:  # noqa: BLE001 - 识别链靠 None 继续，不靠异常
            # ⚠️ 上游的失败**不是确定性的**：连接失败有时被它的 `Request.request()`
            # 吞成空响应（→ 走下面 `not positions` 那条），有时又把 JSONDecodeError
            # 抛出来传到这里。两条路径的报错必须**一样有用**，
            # 否则同一件事会得到两种质量不同的提示，看起来像两个不同的故障。
            self.last_error = (
                f"调用上游打码失败（地址 {self.endpoint}）: "
                f"{type(exc).__name__}: {exc}；"
                "先确认服务在跑：python -m ticket_helper.verify.captcha_server"
            )
            return None

        if not positions:
            # ⚠️ 上游 ``Request.request()`` 会把**连接失败也吞成空响应**
            # （它捕获 RequestException 后伪造一个空 HTMLResponse），
            # 所以「拿不到坐标」既可能是服务不可达、也可能是协议不对。
            # 我们**看不出是哪一种** —— 那就如实说两种可能，并给出自检指令，
            # 而不是猜一个原因（猜错会让人去查错误的方向）。
            self.last_error = (
                f"打码服务未返回可用坐标（地址 {self.endpoint}）："
                "可能不可达，也可能未按上游协议应答 "
                "{msg:'success', result:[格子编号]}。"
                "先确认服务在跑：python -m ticket_helper.verify.captcha_server"
            )
            return None
        return format_positions(positions)

    # ------------------------------------------------------------------ #
    def _call_upstream(self, image: bytes) -> list[int] | None:
        """调上游三连：``get_img_position`` → POST → 编号换算成坐标。"""
        # 装垫片必须在 import py12306 之前（见 shim.py 的时序说明）
        shim.install()

        import py12306.config as cfg_mod  # noqa: PLC0415

        config = cfg_mod.Config()
        config.AUTO_CODE_PLATFORM = USER_MODE
        config.API_USER_CODE_QCR_API = self.endpoint
        config.TIME_OUT_OF_REQUEST = self.timeout

        from py12306.helpers.OCR import OCR  # noqa: PLC0415

        # 上游从 12306 接口拿到的图片**本来就是 base64 字符串**，
        # 所以这里只需把我们的 PNG 字节转成同样的形态（类型搬运，不是协议逻辑）。
        return OCR.get_img_position(base64.b64encode(image).decode("ascii"))
