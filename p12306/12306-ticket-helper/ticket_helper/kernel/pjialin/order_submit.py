"""下单请求规格 —— **上游 pjialin/py12306 写法**。

六步的路径 / 字段名 / 固定取值全部取自生成物
``vendor/pjialin/ticket_facts.py``（由 ``scripts/build_vendor_pjialin.py``
从上游 ``order/order.py`` 与 ``user/job.py`` **逐字切出**，含源文件 sha256），
**不是抄进来的** —— 抄错一个字段名没人会发现，而官方的反应只有一句
「系统忙，请稍后重试」。

⚠️ **上游这几步都不带 Referer**，本项目带。这里**如实保留上游的形态**（``referer=""``）：
本域的价值是「把两套实现的差异摆出来」，不是「让上游跑得更顺」。
替它补上 Referer，差异就被抹平了 —— 切默认过去时会以为已经验证过，
实际上那条路径从没被真机检验。**差异要留在能被看见的地方。**

⚠️ 上游把 ``back_train_date`` 传成**出发日**、把 ``random`` 传成**随机小数串**；
本项目在两处用的是别的取值。同一个字段、两套取值来源 —— 靠
:data:`_VALUE_SOURCES` 显式写出来，而不是靠「字段同名所以一定一样」的假设。
"""

from __future__ import annotations

from ticket_helper.errors import ConfigError

from ..domains.order_submit import (
    CTX_RANDOM_FRACTION,
    CTX_TRAIN_DATE,
    STEP_CHECK_ORDER_INFO,
    STEP_QUERY_WAIT_TIME,
    STEP_SUBMIT,
    STEPS,
    OrderFormsSpec,
    OrderStepForm,
)
from .shim import install

#: 与注册表键一致的唯一名字
NAME = "pjialin"

#: 键 → 取值袋里的名字。**只列与「同名同义」不同的那些**。
_VALUE_SOURCES: dict[str, dict[str, str]] = {
    # 上游：`'back_train_date': self.query_ins.left_date`（出发日）
    STEP_SUBMIT: {"back_train_date": CTX_TRAIN_DATE},
    # 上游：`'random': str(random.random())[2:]`（随机小数串）
    STEP_QUERY_WAIT_TIME: {"random": CTX_RANDOM_FRACTION},
}


class PjialinOrderSubmitter:
    """上游写法：``POST /otn/leftTicket/submitOrderRequest`` 起，六步都不带 Referer。"""

    name = NAME

    def forms(self) -> OrderFormsSpec:
        # 装垫片必须早于 import py12306（见 shim 的说明）
        install()
        import ticket_facts  # noqa: PLC0415  （生成物：位于 sys.path 上的 vendor/pjialin）

        forms: dict[str, OrderStepForm] = {}
        for step in STEPS:
            if step not in ticket_facts.ORDER_PATHS:
                raise ConfigError(
                    f"生成物 ticket_facts 里没有步骤 {step!r} 的路径；"
                    "请跑 python scripts/build_vendor_pjialin.py --init 重建"
                )
            const = dict(ticket_facts.ORDER_FORM_CONST.get(step, {}))
            # 滑块三件套只在 checkOrderInfo 那一步、且只在 `initDc` 页告诉我们
            # 「这一单要过滑块」时才追加。`scene` 是里面唯一写死的取值（`nc_login`），
            # 另外两个（`sessionId`/`sig`）要等浏览器拖完滑块才有 ——
            # 所以它必须**并进这一条的 const**，否则可选字段组凑不齐「必要键」。
            optional_keys: tuple[str, ...] = ()
            if step == STEP_CHECK_ORDER_INFO:
                const.update(ticket_facts.ORDER_SLIDE_CONST)
                optional_keys = ticket_facts.ORDER_SLIDE_FIELDS
            forms[step] = OrderStepForm(
                step=step,
                path=ticket_facts.ORDER_PATHS[step],
                method="get" if step == STEP_QUERY_WAIT_TIME else "post",
                referer="",  # 上游没带 —— 见模块 docstring
                keys=ticket_facts.ORDER_FORM_FIELDS[step],
                const=const,
                alias=_VALUE_SOURCES.get(step, {}),
                optional_keys=optional_keys,
            )
        return OrderFormsSpec(name=NAME, forms=forms)
