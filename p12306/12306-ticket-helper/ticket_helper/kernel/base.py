"""门面的契约定义（Protocol）与参数规格。

**只抽两个方法**：``build_passenger_info`` 与 ``confirm_hb_params``。
理由：候补七步里其余六步的请求体经逐接口对照**与上游一致**，
没有可切换的内容。**不抽用不上的东西** —— 多抽一层就多一处没人验证的抽象。

设计约束（见 ``docs/内核门面架构设计.md``）：

- 本模块**只依赖标准库**；
- 门面**只接原始值**，不接 ``ticket`` / ``session`` 这类大对象 ——
  大对象会把 ``inter/`` 的类型泄漏进门面，切换后端时就得连类型一起换。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

#: ``confirmHB`` 的官方原样格式：``<内部车次号>,<席别码>#``（唯一处定义）
HB_TRAIN_TEMPLATE = "{train_no},{seat_code}#"

#: `if_receive_wseat` 的合法取值（是否接受无座）
RECEIVE_WSEAT_CHOICES = ("Y", "N")

#: `realize_limit_time_diff` 默认值（分钟）
DEFAULT_REALIZE_LIMIT_MINUTES = 1440


def build_jz_param(date_text: str, hour_text: str) -> str:
    """把 ④ ``passengerInitApi`` 的响应字段拼成 ``jzParam``。

    官方格式 ``<日期>#<时>#<分>``，例如 ``2026-10-01#19#00``。
    时间里的 ``:`` 要换成 ``#`` —— 这一步来自上游
    ``PassengerInitApi.sendPassengerInitApi``::

        jzdhHourE = data.get("jzdhHourE").replace(":", "#")
        jzdhDate = f"{jzdhDateE}#{jzdhHourE}"

    **返回空串表示「没拿到」**，调用方据此决定传什么给后端 ——
    不在这里猜默认值（猜出来的默认值就是当年那条错误路径的成因）。
    """
    day = str(date_text or "").strip()
    hour = str(hour_text or "").strip().replace(":", "#")
    if not day or not hour:
        return ""
    return f"{day}#{hour}"


@runtime_checkable
class StandbyPayloadBuilder(Protocol):
    """候补请求体构造器。

    ``name`` 必须与注册表里的键一致 —— 契约测试 C6 会核对，
    防止「注册名」和「自报名」两处漂移成两个不同的东西。
    """

    name: str

    def build_passenger_info(self, passengers: Sequence[Mapping[str, Any]]) -> str:
        """拼 ``confirmHB`` 的 ``passengerInfo``。

        参数
        ----
        passengers
            乘车人列表，元素来自 ``getPassengerDTOs``，需含
            ``passenger_name`` / ``passenger_id_no`` / ``allEncStr``。

        返回
        ----
        非空字符串。**乘车人缺少 ``allEncStr`` 时必须抛 ``InterError``**
        （该守卫与上游写法无关，属安全底线，两个后端都必须有）。
        """
        ...

    def confirm_hb_params(
        self,
        *,
        passenger_info: str,
        jz_param: str,
        train_no: str,
        seat_code: str,
        seat_name: str = "",
        server_train_no: str = "",
        if_receive_wseat: str = "N",
        realize_limit_time_diff: int = DEFAULT_REALIZE_LIMIT_MINUTES,
    ) -> dict[str, Any]:
        """拼 ``confirmHB`` 的整个表单（**不含 URL、不含请求头**）。

        参数来源见设计文档 §3.3：

        - ``passenger_info``：``build_passenger_info`` 的返回；
        - ``jz_param``：④ ``passengerInitApi`` 的 ``jzdhDateE`` + ``jzdhHourE``
          经 :func:`build_jz_param` 拼好；**未知时为空串**。
          是否真的用上它由具体后端决定 —— 这正是 F2 的分歧所在；
        - ``train_no``：**本地推算**的内部车次号（``ticket.train_no or train_code``）；
        - ``server_train_no``：② ``getSuccessRate.data.flag[0].train_no``（官方向，
          可能为空）。**两个都给**，因为「用哪个」本身就是 F1 的分歧 ——
          只给一个就等于在 api 层替后端做了选择，切换点就不止一处了；
        - ``seat_code``：官方席别**编码**（``O``/``M``/``9``…），**由调用方解析好**；
        - ``seat_name``：官方席别**名**（``二等座``…）。两个都给，理由同 F1：
          两套上游对「席别」的表达方式不同 ——
          ``testerSunshine`` 用名字查它自己的码表（``PASSENGER_TICKER_STR``），
          本项目用编码。让后端自己选，门面里不出现「哪个后端」的判断；
        - 其余两个见 :data:`RECEIVE_WSEAT_CHOICES` 与
          :data:`DEFAULT_REALIZE_LIMIT_MINUTES`。

        URL 与 Referer **不属于**本返回值：它们属传输层，不在这条门面上，
        也不允许被这个开关切换。
        """
        ...
