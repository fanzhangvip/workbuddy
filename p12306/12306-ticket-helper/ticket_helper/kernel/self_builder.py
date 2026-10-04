"""本项目自研写法（**回退后端**，2026-09-29 起不再是默认）。

用途：当上游写法在真机上出问题时，用环境变量一键回到这里：
::

    TICKET_HELPER_KERNEL_STANDBY_PAYLOAD=self

⚠️ **本文件的输出必须与 2026-09-29 改造前逐字节一致。**
这是「回退真的能回到从前」的唯一凭证，由契约测试 C4 守住：
把这里的任一字面量改动，`tests/test_kernel_facade.py` 必须变红。

四处已知分歧（F1–F4，详见 ``docs/内核替换对照报告.md``）在**本后端里一律维持原样**：

========  =====================================================
F1        ``train_no`` 用调用方给的（本地推算），不查 ② 的返回值
F2        ``jzParam`` 固定空串
F3        ``passengerInfo`` 6 段（末尾带「是否 60 岁以上」）
F4        ``confirmHB`` 共 10 个字段
========  =====================================================
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Sequence

from ticket_helper.errors import InterError

from .base import (
    DEFAULT_REALIZE_LIMIT_MINUTES,
    HB_TRAIN_TEMPLATE,
)

#: 与注册表键一致的唯一名字
NAME = "self"


def _is_over_60(born: Any) -> bool:
    """按出生日期判断是否 60 岁以上（官方要 ``0/1``）。

    ⚠️ 基准年是 ``date.today().year``（**与原实现一致，刻意不改**）：
    改成固定年份会让「默认行为零变化」这条契约当场失效。
    代价是同一份输入在不同年份产出不同请求体 ——
    测它请用 ``conftest.py::frozen_clock`` 冻结「今天」，别改这里。
    """
    text = str(born or "").strip()
    if not text:
        return False
    match = re.search(r"(\d{4})", text)
    if not match:
        return False
    try:
        return (date.today().year - int(match.group(1))) >= 60
    except ValueError:
        return False


class SelfStandbyPayloadBuilder:
    """现行写法。**不读环境、不读配置、纯函数**，便于逐字节比对。"""

    name = NAME

    def build_passenger_info(self, passengers: Sequence[Mapping[str, Any]]) -> str:
        chunks: list[str] = []
        for p in passengers:
            name = str(p.get("passenger_name", "")).strip()
            if not name:
                continue
            enc = str(p.get("allEncStr") or p.get("all_enc_str") or "").strip()
            if not enc:
                raise InterError(
                    f"乘车人「{name}」缺少官方加密串（allEncStr），无法用于候补；"
                    "请到 12306 的个人中心确认该乘车人状态正常后才好重试"
                )
            older = "1" if _is_over_60(p.get("born_date") or p.get("birthday")) else "0"
            chunks.append(
                "#".join(
                    [
                        str(p.get("passenger_type", "1")),
                        name,
                        str(p.get("passenger_id_type_code", "1")),
                        str(p.get("passenger_id_no", "")),
                        enc,
                        older,
                    ]
                )
                + ";"
            )
        return "".join(chunks)

    def confirm_hb_params(
        self,
        *,
        passenger_info: str,
        jz_param: str,  # noqa: ARG002 - F2：本后端刻意忽略它，固定发空串
        train_no: str,
        seat_code: str,
        seat_name: str = "",  # noqa: ARG002 - 本项目按编码查表，不用席别名
        server_train_no: str = "",  # noqa: ARG002 - F1：本后端刻意不用官方向的值
        if_receive_wseat: str = "N",
        realize_limit_time_diff: int = DEFAULT_REALIZE_LIMIT_MINUTES,
    ) -> dict[str, Any]:
        return {
            "passengerInfo": passenger_info,
            # ---- F2：历史上一直发空串。上游填的是 ④ 返回的日期 ----
            "jzParam": "",
            # 官方原样格式：<内部车次号>,<席别码>#
            "hbTrain": HB_TRAIN_TEMPLATE.format(train_no=train_no, seat_code=seat_code),
            "lkParam": "",
            # ---- F4：以下 6 个字段上游全仓库 0 处 ----
            "sessionId": "",
            "sig": "",
            "scene": "nc_login",
            "encryptedData": "",
            "if_receive_wseat": if_receive_wseat,
            "realize_limit_time_diff": realize_limit_time_diff,
        }
