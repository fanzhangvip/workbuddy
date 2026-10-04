# -*- coding: utf-8 -*-
"""候补请求体 —— **上游 testerSunshine/12306 的代码**。

⚠️ 本文件由 ``scripts/build_vendor_testerSunshine.py`` 生成，**不要手改**；
要改先改生成器（或上游原件），再重新生成。

出处
----
======== ==========================================================
仓库     testerSunshine/12306
commit   a495af88346a0d794493c6030f6a6207debb5824（2021-01-04）
许可证   MIT（Copyright (c) 2017），全文见同目录 ``LICENSE``
原件     ``reference/upstream/testerSunshine_standby/``（逐字，只读）
校验     ``python scripts/build_vendor_testerSunshine.py --check``
======== ==========================================================

相对上游的改动（**只有这些**，逐条对应 `PROVENANCE.json`）
------------------------------------------------------------
1. 删除 ``ConfirmHB.sendChechFace``：它级联 import ``inter.GetQueueCount`` →
   ``verify/`` → **tensorflow/keras/cv2**（Python 3.13 装不上），
   且它是**传输层**，本项目传输层不换（见 ``docs/内核门面架构设计.md`` §2）。
2. 新增 ``_TickerConfig`` 垫片：上游用模块级全局 ``TickerConfig``；
   这里用同名容器承载**同一组常量**（常量本身逐字取自 ``TickerConfig.py``）。
3. 新增 :func:`build_passenger_info`：把上游那个 ``for`` 循环原样嵌进去执行；
   **循环体逐字未改**，只在循环前加了参数校验（安全底线，与用哪套协议无关）。

**载荷定义本身（字段名、字段顺序、拼接格式）一个字都没有改。**
这正是「必须用上游代码」这条要求可被验证的部分。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Mapping, Sequence

from ticket_helper.errors import InterError

#: 上游仓库（出处；完整溯源见同目录 ``PROVENANCE.json``）
UPSTREAM_REPO = "testerSunshine/12306"

# ==================== 上游原文（verbatim，禁止手改） ====================
# 出处: TickerConfig.py
PASSENGER_TICKER_STR = {
    '一等座': 'M',
    '特等座': 'P',
    '二等座': 'O',
    '商务座': 9,
    '硬座': 1,
    '无座': 1,
    '软座': 2,
    '软卧': 4,
    '硬卧': 3,
}

SET_TYPE = ["二等座"]


# 出处: inter/ConfirmHB.py（已删除 sendChechFace，见模块 docstring 改动 1）
class confirmHB:
    def __init__(self, secretList, session, tickerNo, jzdhDate):
        """
        人脸识别
        """
        self.secretList = secretList
        self.session = session
        self.passengerTicketStrByAfterLate = session.passengerTicketStrByAfterLate
        self.tickerNo = tickerNo
        self.jzdhDate = jzdhDate

    def data_apr(self):
        """
        passengerInfo	1#XXXX#1#***************77X#bf6ae40d3655ae7eff005ee21d95876b38ab97a8031b464bc2f74a067e3ec957;
        jzParam	2019-08-31#19#00
        hbTrain	5l000G177230,O#
        lkParam
        :return:
        """
        ticker = TickerConfig.PASSENGER_TICKER_STR.get(TickerConfig.SET_TYPE[0])
        data = OrderedDict()
        data["passengerInfo"] = self.passengerTicketStrByAfterLate
        data["jzParam"] = self.jzdhDate
        data["hbTrain"] = f"{self.tickerNo},{ticker}#"
        data["lkParam"] = ""
        return data
# ==================== 上游原文结束 ====================


class _TickerConfig:
    """垫片：上游的模块级全局配置容器（改动 2）。

    只承载 :meth:`confirmHB.data_apr` 真正用到的那两个常量，
    值逐字取自上游 ``TickerConfig.py``（见上面的 verbatim 块）。

    ⚠️ 注意上游的语义：``SET_TYPE`` 存的是**席别名**（``'二等座'``），
    ``PASSENGER_TICKER_STR`` 是「席别名 → 席别码」的表。
    所以传席别码（``'O'``）进来会得到 ``None``，拼出 ``...,None#`` —— 这是静默错误，
    :func:`confirm_hb_params` 会在调用前拦住它。
    """

    PASSENGER_TICKER_STR = PASSENGER_TICKER_STR
    SET_TYPE = SET_TYPE


TickerConfig = _TickerConfig

#: 上游把「当前席别」放在**进程级全局**里（它是单任务脚本）。
#: 本项目一个进程跑多个任务，改写全局必须串行，否则 A 任务的席别会串到 B 任务的请求里。
_SEAT_LOCK = threading.Lock()


class _SessionLike:
    """垫片：只为满足 ``confirmHB.__init__`` 的 ``session.passengerTicketStrByAfterLate``。

    上游靠 session 对象在链路上传递已拼好的乘客串；
    本项目门面**只接原始值**，所以这里用一个最小对象承接。
    """

    def __init__(self, passenger_info: str) -> None:
        self.passengerTicketStrByAfterLate = passenger_info


def build_passenger_info(passengers: Sequence[Mapping[str, Any]]) -> str:
    """候补的 ``passengerInfo``（上游 ``GetPassengerDTOs.py`` 候补分支，改动 3）。

    循环体逐字来自上游：
    
    for user in user_info:
        tickers.append(f"1#{user['passenger_name']}#1#{user['passenger_id_no']}#{user['allEncStr']};")
    
    上游不校验参数就拼串；这里**在循环之前**补校验（安全底线）：
    缺姓名或缺 ``allEncStr`` 时官方必然拒，提前报出比让官方回一句
    「系统忙，请稍后重试」要好。
    """
    for p in passengers:
        if not str(p.get("passenger_name", "")).strip():
            raise InterError("候补乘车人缺少姓名，无法提交")
        enc = str(p.get("allEncStr") or p.get("all_enc_str") or "").strip()
        if not enc:
            raise InterError(
                f"乘车人「{str(p.get('passenger_name', '')).strip()}」缺少官方加密串"
                "（allEncStr），无法用于候补；请到 12306 的个人中心确认该乘车人"
                "状态正常后才好重试"
            )

    tickers: list[str] = []
    user_info = list(passengers)

    for user in user_info:
        tickers.append(f"1#{user['passenger_name']}#1#{user['passenger_id_no']}#{user['allEncStr']};")

    return "".join(tickers)


def confirm_hb_params(
    *,
    passenger_info: str,
    jz_param: str,
    train_no: str,
    seat_name: str,
) -> dict[str, Any]:
    """调**上游** :meth:`confirmHB.data_apr` 产出 ``confirmHB`` 的表单。

    ⚠️ 这里没有任何「等价改写」：直接实例化上游的类并调用它的方法。

    传 ``seat_name``（**席别名**，如 ``'二等座'``）而不是 ``seat_code``：
    上游 ``data_apr`` 走的是 ``PASSENGER_TICKER_STR.get(SET_TYPE[0])``，
    它要的就是席别名。传码进去会得到 ``None`` 并拼出 ``...,None#`` ——
    那种错**不会报错**，只会让官方回一句「系统忙，请稍后重试」，
    所以这里显式拦下（见下方 ``InterError``）。
    """
    name = str(seat_name or "").strip()
    if not name:
        raise InterError("候补缺少席别，无法向上游内核取席别码")
    if name not in PASSENGER_TICKER_STR:
        raise InterError(
            f"席别「{name}」不在上游 {UPSTREAM_REPO} 的席别名表里"
            f"（{', '.join(PASSENGER_TICKER_STR)}）；"
            "不能猜一个码发出去 —— 官方只会回「系统忙，请稍后重试」"
        )

    with _SEAT_LOCK:
        TickerConfig.SET_TYPE = [name]
        order = confirmHB(
            secretList="",
            session=_SessionLike(passenger_info),
            tickerNo=train_no,
            jzdhDate=jz_param,
        )
        result = order.data_apr()
    # 上游返回 OrderedDict；转成普通 dict 交给传输层，顺带钉住键顺序
    return dict(result)
