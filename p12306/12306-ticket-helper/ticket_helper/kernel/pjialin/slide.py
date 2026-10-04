"""滑块风控 —— **上游 pjialin/py12306 写法（真浏览器拖拽）**。

上游的实现是 ``py12306/order/order.py::Browser.request_init_slide``：
用 pyppeteer 起一个无头 Chromium，把 **initDc 的 HTML** 塞进请求拦截里当页面，
再按脚本拖 ``#slide_passcode .nc-lang-cnt``，最后从页面里读出
``csessionid`` 与 ``sig``。

本适配层**只做搬运**（决策 §10-16）：

1. 本项目的会话 Cookie → 上游要的 ``session.dump_cookies()`` 形状（:class:`_SessionShim`）；
2. 本项目的 initDc 正文 → 上游方法的 ``html`` 参数；
3. 上游返回的 ``{'session_id','sig'}` → :class:`SlideResult`。

**没有重写任何拖拽逻辑** —— 鼠标轨迹、viewport、launch 参数、等待条件
全部留在上游代码里。那些常量抄一份出来只会多一处将来会腐烂的地方。

⚠️ **首次运行会联网下载 Chromium（约 150MB）** —— 这是需求方明确接受的方式
（决策 ③）。下载失败时 :meth:`solve` 会抛出带出路的错误，
**不吞异常**：吞掉的话使用者只会看到「这一单被滑块拦住了」，无从知道是没网。
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from typing import Any

from ticket_helper.errors import InterError

from ..domains.slide import SlideResult, SlideSpec
from .shim import install

#: 与注册表键一致的唯一名字
NAME = "pjialin"


class _SessionShim:
    """垫片：上游 ``request_init_slide`` 只用到 ``session.dump_cookies()``。

    上游靠 session 对象把 Cookie 交给浏览器；本项目门面**只接原始值**，
    所以这里用一个最小对象承接（与候补那边的 ``_SessionLike`` 同一手法）。
    """

    def __init__(self, cookies: Sequence[Mapping[str, Any]]) -> None:
        self._cookies = [dict(item) for item in cookies]

    def dump_cookies(self) -> list[dict[str, Any]]:
        return list(self._cookies)


class PjialinSlideSolver:
    """上游写法：pyppeteer 真拖拽 ``#slide_passcode``。"""

    name = NAME

    def spec(self) -> SlideSpec:
        install()
        import ticket_facts  # noqa: PLC0415  （生成物：位于 sys.path 上的 vendor/pjialin）

        # 上游原件里这一项是**字面量写法**（`r"var if_check_slide_passcode.*='(\d?)'"`），
        # 所以求值一次才是能编译的模式。
        return SlideSpec(
            name=NAME,
            state_pattern_source=ast.literal_eval(ticket_facts.ORDER_SLIDE_STATE_REGEX_SOURCE),
            selector=ticket_facts.ORDER_SLIDE_SELECTOR,
            scene=ticket_facts.ORDER_SLIDE_CONST["scene"],
        )

    def available(self) -> tuple[bool, str]:
        """能不能用。

        **不能只返回 bool**：「不可用」而说不出原因，等于没说。
        这里把「缺 pyppeteer」和「缺浏览器（首次会自动下载）」分开报，
        因为两者的出路完全不同。
        """
        try:
            import pyppeteer  # noqa: PLC0415,F401
        except Exception as exc:  # noqa: BLE001 - 缺依赖就是缺依赖，如实报
            return False, f"pyppeteer 不可用（{exc}）"

        try:
            from pyppeteer.chromium_downloader import (
                chromium_executable,
                check_chromium,
            )

            if check_chromium():
                return True, f"将使用已下载的 Chromium：{chromium_executable()}"
            return (
                True,
                "本机还没有 Chromium —— 首次过滑块时会自动联网下载（约 150MB）；"
                "若下载失败，可手动执行 python -m pyppeteer.commands install",
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"pyppeteer 的浏览器管理模块不可用（{exc}）"

    def solve(
        self, *, html: str, cookies: Sequence[Mapping[str, Any]]
    ) -> SlideResult | None:
        """调**上游**的实现过滑块。返回 ``None`` 表示上游没给出可用结果。"""
        if not html:
            raise InterError(
                "过滑块需要 initDc 的页面正文，调用方没有传（这是代码里的接线问题）"
            )

        install()
        try:
            from py12306.order.order import Browser  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise InterError(
                f"加载上游滑块实现失败：{exc}。"
                "它依赖 pyppeteer；缺失时请 pip install pyppeteer==2.0.0"
            ) from exc

        try:
            info = Browser().request_init_slide(_SessionShim(cookies), html)
        except Exception as exc:  # noqa: BLE001
            # **不吞**：过滑块是我们声称能做到的事，做不到必须说清。
            raise InterError(
                f"上游滑块实现执行失败：{exc}。"
                "首次使用需要联网下载 Chromium（约 150MB）；"
                "也可以本机执行 python -m pyppeteer.commands install 后重试，"
                "或在 12306 App 里手动完成这一单"
            ) from exc

        if not isinstance(info, Mapping):
            return None
        session_id = str(info.get("session_id") or "")
        sig = str(info.get("sig") or "")
        if not session_id or not sig:
            # 上游那边也是这么判的（`if not slide_info.get('session_id') or not ...: raise`）
            return None
        return SlideResult(session_id=session_id, sig=sig)
