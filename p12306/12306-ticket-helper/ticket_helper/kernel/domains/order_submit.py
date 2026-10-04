"""下单六步的**请求规格**域（写法沿用 C3 决策：请求用上游、判定保留）。

为什么要有这个域
----------------
下单是本项目唯一**涉资金**的链路，而它最容易出的错**不是崩，是静默错**：
官方对不合法的参数基本不用 4xx 表达，而是回一句
``{"status":false,"messages":["系统忙，请稍后重试"]}`` ——
「参数不对」和「真的系统忙」在日志里长得一模一样。
所以这里把六步的**请求规格**（路径 / 方法 / Referer / 表单字段名与顺序 / 固定取值）
全部**数据化**：两套后端各给一份，差异一眼可见，切换只是换数据。

两套实现的差异（逐字核对 ``vendor/pjialin/py12306/order/order.py`` 与 ``user/job.py``）
----------------------------------------------------------------------------------
=========== ============================================== ==========================================
步骤         上游 ``pjialin``                                本项目 ``self``
=========== ============================================== ==========================================
submit      ``back_train_date`` = **出发日**                ``back_train_date`` = **今天**
            *没有* ``undefined``                          **多传** ``undefined=""``
init_dc     ——                                             Referer 带 ``initDc``（上游不带）
check_info  ``cancel_flag`` = **整数** ``2``                ``cancel_flag`` = **字符串** ``"2"``
            支持滑块三件套（``sessionId``/``sig``/``scene``）  不支持（本项目此前没有滑块实现）
            判成功看 ``data.submitStatus``                 **同一份判定**（见下）
wait_time   ``random`` = **随机小数串**                      ``random`` = **毫秒时间戳**
=========== ============================================== ==========================================

⚠️ **差异不抹平**：不做「顺手把上游的 ``back_train_date`` 改成今天」「顺手给上游补 Referer」。
补平差异不是修 bug，是**藏 bug** —— 切到上游后端时真机上遇到的坑会失去预警。

⚠️ 判定（判成功/判失败）**不在这里**，在 ``inter/api.py`` 里保留本项目那一份：
本项目的判定是从真实响应里逐条踩出来的（``data`` 可能是 ``"N"``/``"1"``/dict、
失败原因可能藏在 ``messages``/``data.errMsg``），上游那份只有 ``result.get('data') == '0'``，
真实响应稍一变形就判错。换判定等于换风险，而它**换不来任何东西**。

「拿不到结论」怎么办
--------------------
:func:`build_form` 只做一件事：**按规格把上下文里的值组装成表单**。
缺值一律抛 :class:`InterError` 并**指名道姓**说是哪一步的哪个字段 ——
「系统忙，请稍后重试」这种伪装失败的话，事后根本查不出是缺值造成的。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ticket_helper.errors import InterError

# --------------------------------------------------------------------------- #
# 步骤名（**唯一处定义**）：别处不许再写这些字符串
# --------------------------------------------------------------------------- #
#: ① 提交订单请求（锁票）
STEP_SUBMIT = "submit"
#: ② 初始化下单页（拿 ``REPEAT_SUBMIT_TOKEN`` 与 ``ticketInfoForPassengerForm``）
STEP_INIT_DC = "init_dc"
#: ③ 校验订单信息（含滑块三件套）
STEP_CHECK_ORDER_INFO = "check_order_info"
#: ④ 查排队人数
STEP_GET_QUEUE_COUNT = "get_queue_count"
#: ⑤ 确认排队（这一步才真正占座）
STEP_CONFIRM_SINGLE_FOR_QUEUE = "confirm_single_for_queue"
#: ⑥ 轮询排队结果（拿到 ``orderId`` 才算成功）
STEP_QUERY_WAIT_TIME = "query_wait_time"

#: 六步的**先后顺序**。官方对顺序敏感（例如 ``passengerInitApi`` 提前调会回页面），
#: 顺序本身是协议事实，所以放在这里而不是散在各个后端里。
STEPS: tuple[str, ...] = (
    STEP_SUBMIT,
    STEP_INIT_DC,
    STEP_CHECK_ORDER_INFO,
    STEP_GET_QUEUE_COUNT,
    STEP_CONFIRM_SINGLE_FOR_QUEUE,
    STEP_QUERY_WAIT_TIME,
)

#: 步骤名 → ctx 的取值来源别名（两套后端各自用到不同的键）。
CTX_TODAY = "today"
CTX_TRAIN_DATE = "train_date"
CTX_RANDOM_MS = "random_ms"
CTX_RANDOM_FRACTION = "random_fraction"


@dataclass(frozen=True)
class OrderStepForm:
    """一个下单步骤的请求规格（**全是数据，没有逻辑**）。"""

    #: 步骤名（必须是 :data:`STEPS` 里的一个）
    step: str
    #: 站内路径
    path: str
    #: ``"post"`` / ``"get"``
    method: str = "post"
    #: 同源 Referer 路径（空串 = 不带）。**差异如实保留**：上游这几步都不带，
    #: 本项目带 ``/otn/confirmPassenger/initDc``（真机上「没逛过页面」会被 WAF 拒）。
    referer: str = ""
    #: 表单里要出现的键，**顺序 = 上报顺序**（官方对字段顺序不敏感，但顺序一变
    #: diff 就看不见真差异了，所以钉住）。
    keys: tuple[str, ...] = ()
    #: 与调用上下文**无关**的固定取值。
    const: Mapping[str, Any] = field(default_factory=dict)
    #: 键 → 从上下文哪个名字取值（默认同名）。
    #: 上游 ``back_train_date`` 取 ``train_date``、本项目取 ``today`` ——
    #: 「同一个字段、两套取值来源」正是本域要摆出来的差异之一。
    alias: Mapping[str, str] = field(default_factory=dict)
    #: 可选字段**组**：组内**必要键**（不在 :attr:`const` 里的）都有非空值时整体加入，
    #: 都没有时整体跳过；**部分有**视为调用方出错（半套滑块参数发出去必然被拒）。
    #: 目前只有上游 ``check_order_info`` 用到（滑块三件套）。
    optional_keys: tuple[str, ...] = ()

    def source_of(self, key: str) -> str:
        """键在上下文里的取值名字。"""
        return self.alias.get(key, key)


@dataclass(frozen=True)
class OrderFormsSpec:
    """六步的规格集合（**全是数据**）。"""

    #: 后端名（必须与 `kernel/registry.py` 的注册键一致，由契约测试核对）
    name: str
    forms: Mapping[str, OrderStepForm]

    def form(self, step: str) -> OrderStepForm:
        """取某一步的规格；步骤名写错**快速失败**（绝不静默给个默认值）。"""
        try:
            return self.forms[step]
        except KeyError:
            raise InterError(
                f"下单步骤名非法：{step!r}；已知 {', '.join(STEPS)}"
            ) from None


@runtime_checkable
class OrderSubmitter(Protocol):
    """下单请求规格后端。两个后端都必须满足它。"""

    #: 后端名（必须与注册表键一致）
    name: str

    def forms(self) -> OrderFormsSpec:
        """返回六步的规格。"""
        ...


def build_form(form: OrderStepForm, ctx: Mapping[str, Any]) -> dict[str, Any]:
    """按规格把上下文里的值组装成表单。**两套后端共用这一份**。

    规则：

    1. 键在 :attr:`OrderStepForm.const` 里 → 直接用固定取值；
    2. 否则按 :meth:`OrderStepForm.source_of` 去 ``ctx`` 里取；
    3. 取不到 → 抛 :class:`InterError`，**指名道姓**说清是哪一步的哪个字段、
       以及它想从上下文的哪个名字取 —— 否则切了后端只看到「系统忙」。

    ⚠️ 上下文里**多余的键会被忽略**（袋子是共享的，两套后端要的键不同）；
    「有没有多传字段」由基线测试（``tests/test_order_baseline.py``）钉住，
    不在运行期报错 —— 运行期报错会让「上游多一个字段」变成切不过去，
    而那条差异恰恰是要被**看见**的。
    """
    out: dict[str, Any] = {}
    for key in form.keys:
        if key in form.const:
            out[key] = form.const[key]
            continue
        source = form.source_of(key)
        if source not in ctx:
            raise InterError(
                f"下单步骤「{form.step}」缺少取值：字段 {key} 需要上下文的 {source!r}，"
                "但调用方没有提供（这是代码里的接线问题，不是官方拒绝）"
            )
        out[key] = ctx[source]

    if form.optional_keys:
        needed = [k for k in form.optional_keys if k not in form.const]
        provided = [k for k in needed if ctx.get(form.source_of(k)) not in (None, "")]
        if provided and len(provided) != len(needed):
            missing = [k for k in needed if k not in provided]
            raise InterError(
                f"下单步骤「{form.step}」的可选字段只给了一半："
                f"缺 {', '.join(missing)}（半套参数发出去官方必然拒绝）"
            )
        if len(provided) == len(needed):
            for key in form.optional_keys:
                # ⚠️ 不能写成 `form.const.get(key, ctx[...])`：`.get` 的默认值是**先求值**的，
                # 于是「键在 const 里」时也会去 ctx 里取一次 —— 而像 `scene` 这种
                # 「组内的固定值」恰恰不在取值袋里，会直接 KeyError。
                # （第一版就是这么写的，被 `scripts/order_diff.py` 当场抓到。）
                if key in form.const:
                    out[key] = form.const[key]
                else:
                    out[key] = ctx[form.source_of(key)]
    return out


#: 自检：六步必须**不多不少、且不重复**。
#: 重复会让「按顺序遍历步骤」静默少发一步（例如把 submit 发两遍、initDc 一次没发），
#: 而官方对此的反应仍然是那句「系统忙，请稍后重试」。
_DECLARED = (
    STEP_SUBMIT,
    STEP_INIT_DC,
    STEP_CHECK_ORDER_INFO,
    STEP_GET_QUEUE_COUNT,
    STEP_CONFIRM_SINGLE_FOR_QUEUE,
    STEP_QUERY_WAIT_TIME,
)
assert len(STEPS) == len(_DECLARED) == len(set(STEPS)), "STEPS 有重复或缺项"
assert set(STEPS) == set(_DECLARED), "STEPS 与步骤常量不一致"
assert STEPS[-1] == STEP_QUERY_WAIT_TIME, "最后一步必须是排队轮询（拿到 orderId 才算成功）"
