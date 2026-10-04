"""打码域 · 本项目自己的写法（**回退后端**）。

它把 ``ticket_helper/verify/service.py::FreeSolver`` 包一层，好处是切换开关
两侧都能跑，差异可以被打印出来。

⚠️ **它与 ``pjialin`` 后端的差异，就是「老实现为什么用不了」的答案**：

===============  ==========================  ==========================
项               ``self``（本项目）            ``pjialin``（上游）
===============  ==========================  ==========================
请求字段名        ``image``（base64）           ``img``（base64）
响应约定          ``{"answer": "x,y,…"}``      ``{"msg":"success","result":[格子编号,…]}``
坐标来源          服务端直接给坐标              上游本地把**格子编号**换算成坐标
===============  ==========================  ==========================

也就是说：**换的不是「谁写得更漂亮」，而是请求字段名与响应约定本身**。
服务端按上游协议实现时，``self`` 后端必然对不上 —— 这就是当年的假实现。
"""

from __future__ import annotations

from ..domains.captcha import KIND_LOGIN

#: 与注册表键一致的唯一名字（与 `kernel/self_builder.py` 同一写法）
NAME = "self"


class SelfCaptchaSolver:
    """本项目自己的「外部打码服务」客户端（转发 ``FreeSolver``）。"""

    name = NAME

    def __init__(self, endpoint: str = "", *, timeout: float = 10.0) -> None:
        # 延迟 import：`kernel/self/` 是 `verify/` 的适配层，
        # 而 `verify/` 是业务侧模块；放模块顶层会让 `import kernel` 顺带拉起一大片。
        from ticket_helper.verify.service import FreeSolver  # noqa: PLC0415

        self._impl = FreeSolver(endpoint, timeout=timeout)

    @property
    def endpoint(self) -> str:
        return self._impl.endpoint

    @property
    def last_error(self) -> str:
        return self._impl.last_error

    def available(self) -> bool:
        return self._impl.available()

    def solve(self, image: bytes, *, hint: str = "", kind: str = KIND_LOGIN) -> str | None:
        return self._impl.solve(image, hint=hint, kind=kind)
