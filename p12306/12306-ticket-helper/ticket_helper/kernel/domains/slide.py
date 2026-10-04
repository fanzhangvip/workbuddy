"""下单滑块风控域（A13）：**这一单要不要过滑块，以及怎么过**。

它到底是什么
------------
``initDc`` 页里带一行 ``var if_check_slide_passcode = '0'|'1'``：
``'1'`` 表示这一单必须过滑块风控，否则紧接着的 ``checkOrderInfo`` 会被拒 ——
而官方的拒绝话术仍是那句万能的「系统忙，请稍后重试」，看不出是滑块没过。

两套实现的差别是**根本性**的：

============ ==================================================================
后端          怎么过
============ ==================================================================
``self``     没有程序化解法（登录链路走扫码；下单遇到滑块只能在 App/浏览器里人工过）
``pjialin``  用 pyppeteer 起真浏览器，按上游脚本拖 ``#slide_passcode``，取回
             ``csessionid`` 与 ``sig``
============ ==================================================================

⚠️ 所以这里的 ``self`` **不是「另一套等价实现」**，而是「**回退到改造前的行为**」
（一键回退开关的意义所在）。因此当它 ``available()`` 为假时，
调用方**不报错**、只留一条 WARNING 继续往下走 ——
回退不允许引入新的失败点。

判据（本域最要紧的一条）
------------------------
「要不要过滑块」只由 **initDc 页面正文**决定，判据是上游正则在页面里抓到的那个数字。
本项目踩过一次「判据要素没被记下来 → 离线只能证明自洽」（回流 #26），
所以正则**以源码文本形式**存在生成物 ``ticket_facts.ORDER_SLIDE_STATE_REGEX_SOURCE`` 里，
由 :func:`needs_slide` 唯一解释。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: 滑块求解结果的属性名（与 ``checkOrderInfo`` 表单里的键**同名**）——
#: 上游取的 ``session_id``/``sig`` 最终就是要填进那两个字段，
#: 名字不一致会在「填表单」那一步变成一次静默的字段名错配。
FIELD_SESSION_ID = "sessionId"
FIELD_SIG = "sig"


@dataclass(frozen=True)
class SlideSpec:
    """滑块的**判断**规格（全是数据，没有逻辑）。

    ⚠️ 这里**没有**鼠标轨迹、viewport、launch 参数这类东西：
    那些属于「怎么拖」，而上游那套是**直接调用上游方法**完成的，
    常量留在上游代码里就好 —— 抄一份出来只会多一处将来会腐烂的地方。
    本域只固定「要不要过」这一件事。
    """

    #: 后端名（必须与 `kernel/registry.py` 的注册键一致，由契约测试核对）
    name: str
    #: 判「这一单要不要过滑块」的**正则源码**（可直接 ``re.compile``）。
    #: 上游原件里它是 ``r"var if_check_slide_passcode.*='(\d?)'"`` 这样的**字面量写法**，
    #: 由适配层求值后交到这里（本项目那份写在 `inter/urls.py`，
    #: 两者是否一致由 `tests/test_kernel_slide.py` 钉住）。
    state_pattern_source: str
    #: 滑块 DOM 选择器（上游用 ``page.waitForSelector`` 等它出现）。
    selector: str
    #: 过滑块后随 ``checkOrderInfo`` 一起提交的场景名（上游写死 ``nc_login``）。
    scene: str

    def state_pattern(self) -> re.Pattern[str]:
        """把源码文本编译成正则。"""
        return re.compile(self.state_pattern_source)


@dataclass(frozen=True)
class SlideResult:
    """过一次滑块的结果：两个要填进 ``checkOrderInfo`` 的值。"""

    session_id: str
    sig: str

    def as_form_values(self) -> dict[str, str]:
        """转成 ``checkOrderInfo`` 表单里的两个键（**键名与上游一致**）。"""
        return {FIELD_SESSION_ID: self.session_id, FIELD_SIG: self.sig}


@runtime_checkable
class SlideSolver(Protocol):
    """滑块求解后端。"""

    #: 后端名（必须与注册表键一致）
    name: str

    def spec(self) -> SlideSpec:
        """返回判断规格。"""
        ...

    def available(self) -> tuple[bool, str]:
        """``(能不能用, 说明)``。**不能只返回 bool** —— 使用者需要知道为什么、
        以及要装什么才能用（「不可用」而说不出原因，等于没说）。"""
        ...

    def solve(
        self, *, html: str, cookies: Sequence[Mapping[str, Any]]
    ) -> SlideResult | None:
        """过滑块。

        - 返回 :class:`SlideResult`：拿到 ``session_id``/``sig``；
        - 返回 ``None``：**这套实现做不到**（例如回退后端），调用方照旧往下走；
        - 抛异常：这套实现本该能做到却失败了 —— 必须往上报，不能吞。
        """
        ...


def needs_slide(html: str, spec: SlideSpec) -> bool:
    """这一单要不要过滑块。**全项目唯一一处判读**。

    ⚠️ **判不出来一律当「不需要」**，这是刻意的：
    本项目在「人证核验」上吃过反过来的亏（把「判不出来」当「未核验」，
    结果正常账号被永久挡住）。这里反过来更安全 ——
    真的需要滑块时官方会拒，而那条拒绝会走到本域的另一条出路（错误提示），
    比「凭空拦住一单」要好。
    """
    match = spec.state_pattern().search(html or "")
    if not match:
        return False
    try:
        return int(match.group(1)) == 1
    except (IndexError, TypeError, ValueError):
        return False
