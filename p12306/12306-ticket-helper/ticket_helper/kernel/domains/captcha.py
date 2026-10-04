"""打码域：图片 → 答案。

答案有两种形态，由 ``kind`` 决定（**不是**由后端决定）：

==============  ============  ==========================================
``kind``        验证码形态     答案样例
==============  ============  ==========================================
``login``       点击型         ``"38,38,257,36"`` —— x,y 交替，偶数个数字
``order``       字符型         ``"3A5B"``
==============  ============  ==========================================

契约（两个后端都必须满足）
--------------------------
1. ``available()`` 为假时，调用方**不会**调用 ``solve``；
2. ``solve`` 失败返回 ``None``，**不抛异常** —— 识别链要靠 ``None`` 继续往下走；
3. **失败原因必须留在对象上**（``last_error``），不许只打日志。
   本项目吃过这个亏：老云打码渠道失败被 ``log.warning`` 吞掉，
   使用者只看到「验证码识别失败」，永远不知道**请求根本没发出去**。

为什么 ``solve`` 的返回值是字符串而不是结构化对象
--------------------------------------------------
因为它要能直接填进 12306 的表单（``randCode`` / 点击坐标串）。
在这一层做结构化，只会给每个调用点都加一次序列化。
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

#: 支持的验证码形态（唯一处定义；别处不许硬写 "login"/"order" 字面量）
KIND_LOGIN = "login"
KIND_ORDER = "order"
SUPPORTED_KINDS: tuple[str, ...] = (KIND_LOGIN, KIND_ORDER)


@runtime_checkable
class CaptchaSolver(Protocol):
    """一个打码后端。业务层与识别链只认它。"""

    #: 后端名（必须与 `kernel/registry.py` 的注册键一致，由契约测试核对）
    name: str

    #: 最近一次失败的原因。**必须可读**（见模块 docstring 第 3 条）
    last_error: str

    def available(self) -> bool:
        """当前是否**用得上**（没配地址、没装模型都算用不上）。"""
        ...

    def solve(self, image: bytes, *, hint: str = "", kind: str = KIND_LOGIN) -> str | None:
        """识别；失败返回 ``None`` 并把原因写进 :attr:`last_error`。"""
        ...


def format_positions(positions: Sequence[int]) -> str:
    """把坐标序列拼成 12306 要的 ``"x1,y1,x2,y2,…"``。

    只此一处实现 —— 上游返回 ``list[int]``，我们的接口要字符串，
    两边若各拼一次，「逗号 vs 空格」「先 x 还是先 y」这类差异不会有任何报错。
    """
    return ",".join(str(int(value)) for value in positions)
