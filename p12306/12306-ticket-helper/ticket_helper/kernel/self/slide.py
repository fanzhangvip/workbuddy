"""滑块风控 —— **本项目写法（= 一键回退后端）**。

本项目**没有**程序化滑块解法：

- 登录链路的滑块（``nc.js``）从来没做过，扫码登录是唯一可靠路径；
- 下单链路的滑块此前也没有实现，只能人工完成那一单。

所以这个后端的存在意义只有一个：**回退**。
``available()`` 恒为 ``False``，``solve()`` 恒返回 ``None``，
调用方据此**照旧往下走**（与改造前完全一致的行为）——
回退不允许引入新的失败点：以前不会因为「没有滑块解法」而报错，现在也不许。

⚠️ 判断规格（正则 / 选择器 / scene）仍然从这里出 ——
它与上游那份**同源**（本项目那份写在 `inter/urls.py`，取值等于上游生成物里的正则），
由 `tests/test_kernel_slide.py` 钉住「两边编译出来的正则一模一样」，
所以「要不要过滑块」的判读不会因为选哪个后端而变。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ticket_helper.inter import urls as inter_urls

from ..domains.slide import SlideSpec, SlideResult

#: 与注册表键一致的唯一名字
NAME = "self"



class SelfSlideSolver:
    """本项目写法：**没有程序化解法**（回退语义）。"""

    name = NAME

    def spec(self) -> SlideSpec:
        return SlideSpec(
            name=NAME,
            state_pattern_source=inter_urls.ORDER_SLIDE_STATE_PATTERN,
            selector=inter_urls.ORDER_SLIDE_SELECTOR,
            scene=inter_urls.ORDER_SLIDE_SCENE,
        )

    def available(self) -> tuple[bool, str]:
        return (
            False,
            "本项目没有程序化滑块解法（回退后端）：这一单如果被滑块拦住，"
            "请在 12306 App 或浏览器里手动完成，或把 "
            "TICKET_HELPER_KERNEL_BACKENDS 的 slide 域切到 pjialin 用真浏览器过滑块",
        )

    def solve(
        self, *, html: str, cookies: Sequence[Mapping[str, Any]]
    ) -> SlideResult | None:
        # 回退后端**不做事、也不报错** —— 调用方照旧往下走，与改造前一致。
        return None
