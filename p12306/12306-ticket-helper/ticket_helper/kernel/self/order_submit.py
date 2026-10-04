"""下单请求规格 —— **本项目写法（默认后端）**。

六步的路径 / 方法 / Referer / 字段名 / 固定取值**全部引用协议层的唯一真源**
``inter/urls.py::ORDER_STEP_FORMS``：本文件不写一个协议字面量
（连字段名都不写），只做「把那份数据包装成 :class:`OrderFormsSpec`」。

这样「接口层没被注入时自己组装的规格」与「本后端产出的规格」必然是同一份，
由 ``tests/test_kernel_order.py::test_self_spec_equals_the_builtin_default`` 钉住 ——
任何一边改了而另一边没跟，测试立刻变红，而不是等到真机上「参数少传一个」。
"""

from __future__ import annotations

from ticket_helper.errors import ConfigError
from ticket_helper.inter import urls as inter_urls

from ..domains.order_submit import STEPS, OrderFormsSpec, OrderStepForm

#: 与注册表键一致的唯一名字
NAME = "self"


class SelfOrderSubmitter:
    """本项目写法：``POST /otn/leftTicket/submitOrderRequest`` 起，六步都带同源 Referer。"""

    name = NAME

    def forms(self) -> OrderFormsSpec:
        missing = [step for step in STEPS if step not in inter_urls.ORDER_STEP_FORMS]
        if missing:
            # 少一步是**静默灾难**：六步少一步，官方在那一步回「系统忙」，
            # 事后无法判断是参数问题还是顺序问题。宁可启动就炸。
            raise ConfigError(
                f"协议层 ORDER_STEP_FORMS 缺少步骤定义：{', '.join(missing)}"
            )
        forms = {
            step: OrderStepForm(step=step, **inter_urls.ORDER_STEP_FORMS[step])
            for step in STEPS
        }
        return OrderFormsSpec(name=NAME, forms=forms)
