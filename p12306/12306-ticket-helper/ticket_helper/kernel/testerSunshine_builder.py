"""上游 ``testerSunshine/12306`` 后端（**候补的默认后端** —— 见决策 B）。

为什么要用上游
--------------
需求方 2026-09-29 明确：**上游已有的功能禁止自己实现**。
`pjialin/py12306` 里**没有候补**（`afterNate`/`confirmHB` 全仓 0 处），
所以候补这条路只能走 `testerSunshine/12306`（MIT，**有完整候补实现**）。
决策记录：``docs/pjialin内核替换能力清点与方案v2.md`` §4.1 → 选项 B。

本文件**不含任何协议逻辑**：它只是把门面的两个方法转发给
:mod:`vendor.testerSunshine.standby`（那是从上游原件生成出来的、逐字保留的代码）。
「载荷定义来自上游」这件事由
``scripts/build_vendor_testerSunshine.py --check`` 保证，不靠声明。

与另一后端（``self``）的四处差异
--------------------------------
========  =====================================================
F1        ``hbTrain`` 的 ``train_no`` 取 ② ``getSuccessRate`` 的返回值
F2        ``jzParam`` 用 ④ ``passengerInitApi`` 的日期
F3        ``passengerInfo`` **5 段**（无「是否 60 岁」位）
F4        ``confirmHB`` 只有 **4 个字段**
========  =====================================================

⚠️ **F4 有真实代价**：上游不支持 ``if_receive_wseat``（接受无座）与
``realize_limit_time_diff``（兑现时限），所以切到本后端后，
**这两个设置会失效**。这是「用上游」的代价，必须显式告诉使用者，
不能悄悄吞掉（见 :meth:`TesterSunshineStandbyPayloadBuilder.confirm_hb_params`）。

回退
----
环境变量 ``TICKET_HELPER_KERNEL_STANDBY_PAYLOAD=self`` 即一键回到改造前的写法。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from vendor.testerSunshine import standby as upstream_standby

from .base import DEFAULT_REALIZE_LIMIT_MINUTES

log = logging.getLogger(__name__)

#: 与注册表键一致的唯一名字
NAME = "testerSunshine"

#: 上游不支持的参数 —— 切到本后端时它们会被忽略，只记一条日志。
UNSUPPORTED_PARAMS = ("if_receive_wseat", "realize_limit_time_diff")


class TesterSunshineStandbyPayloadBuilder:
    """转发给上游 ``testerSunshine/12306`` 的候补请求体构造。"""

    name = NAME

    def build_passenger_info(self, passengers: Sequence[Mapping[str, Any]]) -> str:
        return upstream_standby.build_passenger_info(passengers)

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
        if if_receive_wseat != "N" or realize_limit_time_diff != DEFAULT_REALIZE_LIMIT_MINUTES:
            log.warning(
                "当前候补后端是上游 %s，它不支持 %s；这两个设置将被忽略",
                upstream_standby.UPSTREAM_REPO,
                " / ".join(UNSUPPORTED_PARAMS),
            )
        return upstream_standby.confirm_hb_params(
            passenger_info=passenger_info,
            jz_param=jz_param,
            # F1：上游用 ② getSuccessRate 返回的官方车次号，没有才回退本地推算值
            train_no=server_train_no or train_no,
            # 上游按**席别名**查表（`PASSENGER_TICKER_STR.get(SET_TYPE[0])`），
            # 传席别码会得到 None 并拼出 `...,None#`，所以这里传名字。
            seat_name=seat_name or seat_code,
        )
