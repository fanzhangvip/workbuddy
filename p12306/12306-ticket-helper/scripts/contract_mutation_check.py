"""契约变异检验：证明 tests/test_contracts.py 里的测试是「真测试」。

纪律来源：一条测试写完后，**把实现撤掉再跑一遍，它必须失败**。
如果撤掉实现它仍然通过，这条测试就是假的 —— 它只能证明「代码自己和自己一致」。

本脚本对每条契约注入一次「真实的违规」（不是语法错误，而是**曾经真实发生过
或极易发生**的那种改动），然后要求对应的测试变红，最后还原。

用法::

    python scripts/contract_mutation_check.py          # 全部
    python scripts/contract_mutation_check.py -k 订单   # 只跑名字含该子串的

退出码 0 表示所有变异都被测试抓住了。


维护提醒：**本文件行尾必须是 CRLF**（项目约定，`tests/test_discipline.py` 会查）。
改它时用 `read_bytes/read_text(newline="")` 并写回 `\r\n` —— 
用普通 `read_text()` + `write_text()` 会把它悄悄折成纯 LF，
2026-09-30 已经因此被门抓到过两次。
"""

from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


@dataclass
class Mutation:
    """一次变异：把 ``old`` 改成 ``new``（只改一处），期望 ``test`` 变红。"""

    name: str
    path: str
    old: str
    new: str
    test: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        name="账号状态：后端新增一个状态，两端文案表没跟上",
        path="ticket_helper/account/manager.py",
        old='STATUS_ERROR = "error"',
        new='STATUS_ERROR = "error"\nSTATUS_EXPIRED = "expired"',
        test="tests/test_contracts.py::test_account_status_vocabulary_is_identical_across_three_places",
    ),
    Mutation(
        name="订单文案：混进一个数据里不存在的键（历史真 bug 的复现）",
        path="ticket_helper/order/service.py",
        old="ORDER_STATUS_TEXT: dict[str, str] = {",
        new='ORDER_STATUS_TEXT: dict[str, str] = {\n    "pending": "进行中",',
        test="tests/test_contracts.py::test_order_status_text_covers_exactly_the_declared_statuses",
    ),
    Mutation(
        name="前端：状态判断用了服务层不存在的值",
        path="ticket_helper/web/static/app.js",
        old="const kind = o.status === 'success' ? 'ok'",
        new="const kind = o.status === 'paid' ? 'ok'",
        test="tests/test_contracts.py::test_web_ui_order_status_branches_use_declared_values",
    ),
    Mutation(
        name="任务状态：写入处回退成裸字符串",
        path="ticket_helper/scheduler/scheduler.py",
        old='self.db.update_task_status(task_id, TASK_STATUS_STOPPED)',
        new='self.db.update_task_status(task_id, "stopped")',
        test="tests/test_contracts.py::test_task_status_values_are_used_via_constants_only",
    ),
    Mutation(
        name="任务状态：翻译表漏掉一个终态",
        path="ticket_helper/runtime/context.py",
        old="    STATE_ERROR: TASK_STATUS_ERROR,\n",
        new="",
        test="tests/test_contracts.py::test_task_status_translation_covers_terminal_states",
    ),
    Mutation(
        name="候补终态：grab.engine 又抄了一份字面量",
        path="ticket_helper/grab/engine.py",
        old="from ticket_helper.standby.engine import STANDBY_TERMINAL_STATES",
        new='STANDBY_TERMINAL_STATES = ("success", "failed", "cancelled", "stopped")',
        test="tests/test_contracts.py::test_standby_terminal_states_have_exactly_one_definition",
    ),
    Mutation(
        name="候补词汇：某个状态值失去所有产生点",
        path="ticket_helper/standby/engine.py",
        # 必须掐掉**全部**产生点（自赋 + 落库）才算真的失去产生点，
        # 否则变异本身不成立、会误判成「测试漏过」。
        old="self.status = STANDBY_CANCELLED if should_cancel else STANDBY_STOPPED",
        new="self.status = STANDBY_CANCELLED if should_cancel else STANDBY_CANCELLED",
        test="tests/test_contracts.py::test_every_standby_state_has_a_producer",
    ),
    Mutation(
        name="存储层 SQL：活跃态取错常量（配额判断会静默算错）",
        path="ticket_helper/storage/database.py",
        old="                \"SELECT id FROM standby_orders WHERE task_id = ? AND status = ?\"\n"
        "                \" ORDER BY id DESC LIMIT 1\",\n"
        "                (payload[\"task_id\"], _status_constant(\"standby_orders\", \"STANDBY_QUEUING\")),\n",
        new="                \"SELECT id FROM standby_orders WHERE task_id = ? AND status = ?\"\n"
        "                \" ORDER BY id DESC LIMIT 1\",\n"
        "                (payload[\"task_id\"], _status_constant(\"standby_orders\", \"STANDBY_SUCCESS\")),\n",
        test="tests/test_contracts.py::test_storage_sql_holds_no_standby_status_literals",
    ),
    Mutation(
        name="CLI 降级：回退成复用顶层 args（缺失 host → AttributeError）",
        path="main.py",
        old="    return build_parser().parse_args(argv)",
        new="    return args",
        test="tests/test_contracts.py::test_web_fallback_args_satisfy_cmd_web_requirements",
    ),
    Mutation(
        name="候补初始状态：回退成裸字符串（引擎自创状态值）",
        path="ticket_helper/standby/engine.py",
        old="        self.status = STANDBY_PENDING",
        new='        self.status = "idle"',
        test="tests/acceptance/test_standby_state_machine.py::TestStandbyTransitions::test_engine_does_not_use_literal_queuing_string",
    ),
    # ------------------------------------------------------------------ #
    # 乘车日期可查性（真机实测：风控与「日期不可查」回同一张 HTML 错误页）
    #
    # 这一组防的是**真实发生过**的故障：任务日期填成上一年 → 官方回 HTML →
    # 被一律当成风控 → 界面报「🚨 风控告警」，使用者降频/换账号/干等全都无效，
    # 每次重试还都在给 12306 添一次注定被拒的请求。
    # ------------------------------------------------------------------ #
    Mutation(
        name="日期判定：把「已经过去」和「还没开售」合并成同一种结论",
        path="ticket_helper/train_date.py",
        old="    if parsed < base:\n        return DATE_PAST",
        new="    if parsed < base:\n        return DATE_NOT_ON_SALE",
        test="tests/test_train_date.py::test_past_and_not_on_sale_are_distinct",
    ),
    Mutation(
        name="日期判定：「今天」冒出第二个来源（时钟冻不住，测试与线上分叉）",
        path="ticket_helper/train_date.py",
        old="    return now if now is not None else today()",
        new="    return now if now is not None else date.today()",
        test="tests/test_train_date.py::test_today_is_the_single_source_of_truth",
    ),
    Mutation(
        name="接口层：HTML 一律当风控（正是这次故障的根因）",
        path="ticket_helper/inter/api.py",
        old="            if rejected:\n                if date_problem:",
        new="            if rejected:\n                if False:",
        test="tests/test_query.py::test_out_of_presale_date_raises_date_error_not_risk",
    ),
    Mutation(
        name="查询引擎：不再跳过已经过去的日期（拿注定被拒的日期去打 12306）",
        path="ticket_helper/query/engine.py",
        old="            if train_date_util.classify(train_date) == train_date_util.DATE_PAST:",
        new="            if False:",
        test="tests/test_query.py::test_past_date_never_reaches_12306",
    ),
    Mutation(
        name="引擎准备：不再拒绝「所有乘车日期都已过去」的任务",
        path="ticket_helper/grab/engine.py",
        old="        if past_dates and len(past_dates) == len(self.task.left_dates):",
        new="        if False:",
        test="tests/test_grab.py::test_all_dates_past_refuses_to_start_with_actionable_message",
    ),
    Mutation(
        name="余票看板：不再说明「为什么一个候选都没有」",
        path="ticket_helper/runtime/context.py",
        old='            "empty_reason": self._empty_reason(engine, last, candidates),',
        new='            "empty_reason": "",',
        test="tests/test_web.py::test_remain_explains_empty_candidates",
    ),
    Mutation(
        name="任务列表：不再标出 12306 查不到的乘车日期",
        path="ticket_helper/web/api.py",
        old='            "date_hints": [',
        new='            "date_hints_off": [',
        test="tests/test_web.py::test_task_list_warns_about_dates_12306_cannot_query",
    ),
    Mutation(
        name="mock：不再复现真机的日期窗口（离线就再也测不出这条）",
        path="mock_server/server.py",
        old="        if not train_date_util.queryable(train_date):",
        new="        if False:",
        test="tests/test_query.py::test_out_of_presale_date_raises_date_error_not_risk",
    ),
    # ------------------------------------------------------------------ #
    # 云打码渠道（2026-09-29 移除）
    # ------------------------------------------------------------------ #
    Mutation(
        name="云打码：去掉 mode=cloud 的专属报错（只剩一句没有出路的「取值非法」）",
        path="ticket_helper/config/settings.py",
        old='        if self.mode == "cloud":',
        new="        if False:",
        test="tests/test_notify_verify.py::test_cloud_codec_mode_is_rejected_with_a_way_out",
    ),
    Mutation(
        name="云打码：把 CloudSolver 偷偷加回来",
        path="ticket_helper/verify/service.py",
        old="class FreeSolver(CaptchaSolver):",
        new='class CloudSolver(CaptchaSolver):\n    name = "cloud"\n\n    def available(self) -> bool://n        return True\n\n    def solve(self, image, *, hint="", kind="login"):\n        return None\n\n\nclass FreeSolver(CaptchaSolver):',
        test="tests/test_notify_verify.py::test_cloud_solver_is_gone_for_good",
    ),
    # ---------------- 内核门面（kernel/） ----------------
    # 2026-09-29：候补链路的请求体改成可切换门面，默认切到上游 testerSunshine/12306（决策 B）。
    # 这几条防的是「切换悄悄失效」—— 它不会报错，只会让线上跑的不是你以为的那一套。
    Mutation(
        name="内核：回退后端不再固定发空串 jzParam（F2 被『顺手修好』）",
        path="ticket_helper/kernel/self_builder.py",
        old='            "jzParam": "",',
        new='            "jzParam": jz_param,',
        test="tests/test_kernel_facade.py::test_c4_self_backend_output_is_the_legacy_literal",
    ),
    Mutation(
        name="内核：默认后端被无声改回 self（决策 B 被推翻）",
        path="ticket_helper/kernel/__init__.py",
        old="DEFAULT_BACKEND = TesterSunshineStandbyPayloadBuilder.name",
        new="DEFAULT_BACKEND = SelfStandbyPayloadBuilder.name",
        test="tests/test_kernel_facade.py::test_c3_default_backend_is_upstream_per_decision_b",
    ),
    Mutation(
        name="内核：上游后端改用本地车次号（F1 不再走上游语义）",
        path="ticket_helper/kernel/testerSunshine_builder.py",
        old="            train_no=server_train_no or train_no,",
        new="            train_no=train_no,",
        test="tests/test_kernel_facade.py::test_c8_facade_payload_reaches_transport",
    ),
    Mutation(
        name="内核：门面写了但没接上（api.py 回退到自己拼请求体）",
        path="ticket_helper/inter/api.py",
        old="            builder.confirm_hb_params(",
        new='            dict(passengerInfo="x", jzParam="", hbTrain="x,O#", lkParam="") or builder.confirm_hb_params(',
        test="tests/test_kernel_facade.py::test_c8_facade_payload_reaches_transport",
    ),
    # ---------------- 打码域（captcha） ----------------
    # 2026-09-30：打码改成「上游 user 模式 + 上游坐标换算」。这几条防的正是
    # 用户反复强调的那件事 —— **不要再来一次假实现**。
    Mutation(
        name="打码：借口上游协议但不再编码图片（服务端拿到的不是图）",
        path="ticket_helper/kernel/pjialin/captcha.py",
        old='        return OCR.get_img_position(base64.b64encode(image).decode("ascii"))',
        new="        return OCR.get_img_position(image.decode('latin-1'))",
        test="tests/test_kernel_captcha.py::test_upstream_backend_sends_the_upstream_field_name",
    ),
    Mutation(
        name="打码：格子换算与上游公式脱钩（CELL 从 75 改成别的值）",
        path="ticket_helper/verify/captcha_server.py",
        old="CELL = 75",
        new="CELL = 80",
        # ⚠️ 只验「理想中心落回原格」是抓不住它的：75→80 后中心仍各归各格。
        # 必须验「格边长 == 上游相邻格中心间距」才区分得开。
        test="tests/test_kernel_captcha.py::test_cell_size_equals_the_upstream_spacing",
    ),
    Mutation(
        name="打码：格网之外的点被硬塞进 1..8（把误检变成一个「合法」坐标）",
        path="ticket_helper/verify/captcha_server.py",
        old="    if not (1 <= col <= COLUMNS) or not (1 <= row <= ROWS):\n        return None",
        new="    col = min(max(col, 1), COLUMNS)\n    row = min(max(row, 1), ROWS)",
        test="tests/test_kernel_captcha.py::test_grid_math_rejects_points_outside_the_grid",
    ),
    Mutation(
        name="打码：装配处不再注入（业务层悄悄跑回「自己那套」）",
        path="ticket_helper/runtime/context.py",
        old="            self.config.codec, external_solver=self._build_external_solver(self.config.codec)",
        new="            self.config.codec",
        test="tests/test_kernel_captcha.py::test_composition_root_injects_the_upstream_solver",
    ),
    Mutation(
        name="打码：热更新时不换实例（改地址无效）",
        path="ticket_helper/runtime/context.py",
        old="        self.verify_service.apply_config(\n            config.codec, external_solver=self._build_external_solver(config.codec)\n        )",
        new="        self.verify_service.apply_config(config.codec)",
        test="tests/test_kernel_captcha.py::test_hot_reload_replaces_the_solver_instance",
    ),
    Mutation(
        name="打码：默认后端被改回 self（决策「必须用上游」被推翻）",
        path="ticket_helper/kernel/registry.py",
        old='    "captcha": "pjialin",',
        new='    "captcha": "self",',
        test="tests/test_kernel_captcha.py::test_default_backend_is_upstream_per_decision",
    ),
    # ---------------- V5 参数级基线（余票字段下标） ----------------
    # 下标错了**不会报错**，只会让字段串位（「历时」显示成「到达时间」这类
    # 看着像数据的错）。这条变异证明基线的确在拿外部锚点校验。
    Mutation(
        name="余票下标：加一个字段时把既有下标挤动了",
        path="ticket_helper/inter/parser.py",
        old="IDX_CAN_WEB_BUY = 11        # Y=可预订 / N=不可 / IS_TIME_NOT_BUY=未到起售时间",
        new="IDX_CAN_WEB_BUY = 12        # Y=可预订 / N=不可 / IS_TIME_NOT_BUY=未到起售时间",
        test="tests/test_query_baseline.py::test_our_field_indexes_match_upstream",
    ),
    # ---------------- 查询路径发现域（query_route） ----------------
    # 这几条防的是「优化上线了但请求数没变」—— 它不会报错。
    Mutation(
        name="查路径：不再把上游片段归一成本项目那种完整路径",
        path="ticket_helper/kernel/pjialin/query_route.py",
        old='            return url.split("?", 1)[0]',
        new="            return segment",
        test="tests/test_kernel_query.py::test_pjialin_normalises_the_upstream_form_to_ours",
    ),
    Mutation(
        name="查路径：取了路径却没接上（查询仍用固定候选列表）",
        path="ticket_helper/inter/api.py",
        old="            for path in self.query_route_candidates():",
        new="            for path in urls.LEFT_TICKET_QUERY_CANDIDATES:",
        test="tests/test_kernel_query.py::test_the_strategy_decides_which_path_is_requested",
    ),
    Mutation(
        name="查路径：旁路漏传（attach_api 重建 TicketApi 时丢掉策略）",
        path="ticket_helper/account/manager.py",
        # ⚠️ 锚点必须跟着实现走：这处原来是单行 `TicketApi(...)`，
        # 后来加了几个策略参数、改成多行拼接，锚点就**静默失效**了 ——
        # 清单里它一直挂着，实际永远不会被执行。
        # 直到 2026-09-30 给本脚本加了「锚点预检」才被抓出来。
        old="            self.route_resolver,\n            self.login_spec,\n",
        new="            self.login_spec,\n",
        test="tests/test_kernel_query.py::test_attaching_api_keeps_the_resolver",
    ),
    Mutation(
        name="查路径：默认后端被悄悄改成上游（换默认值 = 换行为）",
        path="ticket_helper/kernel/registry.py",
        old='    "query_route": "self",',
        new='    "query_route": "pjialin",',
        test="tests/test_kernel_query.py::test_default_backend_is_self_because_changing_it_changes_behavior",
    ),
    # ---------------- 用户域（login_check） ----------------
    # 这几条防的都是「切换看着生效了，其实没有」以及「差异被抹平」——
    # 两者都不会报错，只会让线上跑的不是你以为的那一套。
    Mutation(
        name="登录态：上游规格不再显式给取值集合（'N' 会被判成活着）",
        path="ticket_helper/kernel/pjialin/login_check.py",
        old="            alive_values=ticket_facts.LOGIN_ALIVE_VALUES,",
        new="            alive_values=None,",
        test="tests/test_kernel_user.py::test_upstream_string_field_is_not_judged_by_python_truthiness",
    ),
    Mutation(
        name="登录态：给上游那条顺手补上 Referer（把差异抹平）",
        path="ticket_helper/kernel/pjialin/login_check.py",
        old='            referer="",  # 上游没带 —— 见模块 docstring',
        new='            referer="/otn/leftTicket/init",  # 被补平了',
        test="tests/test_kernel_user.py::test_upstream_spec_keeps_the_missing_referer",
    ),
    Mutation(
        name="登录态：把「判不出来」并进「已失效」（三态被压成两态）",
        path="ticket_helper/kernel/domains/login_check.py",
        old="    return UNKNOWN",
        new="    return DEAD",
        test="tests/test_kernel_user.py::test_unknown_is_not_folded_into_dead",
    ),
    Mutation(
        name="登录态：接口层不再用注入的规格（切换形同虚设）",
        path="ticket_helper/inter/api.py",
        old="        spec = self._login_spec",
        new="        spec = self._builtin_login_spec()",
        test="tests/test_kernel_user.py::test_the_strategy_decides_which_endpoint_is_requested",
    ),
    Mutation(
        name="用户信息：不再剥 userDTO.loginUserDTO 那层（账号信息整片空白）",
        path="ticket_helper/inter/parser.py",
        old='    inner = data.get("loginUserDTO")',
        new="    inner = None",
        test="tests/test_user_baseline.py::test_user_info_field_is_unwrapped_by_our_parser",
    ),
    Mutation(
        name="登录态：协议层判活字段被改（两处会一起改，所以只有钉值的那条能抓）",
        path="ticket_helper/inter/parser.py",
        old='CHECK_USER_ALIVE_PATHS = ("data.flag", "status")',
        new='CHECK_USER_ALIVE_PATHS = ("data.flagX", "status")',
        test="tests/test_user_baseline.py::test_login_alive_field_differs_and_uses_string_comparison",
    ),
    Mutation(
        name="登录态：默认后端被悄悄改成上游（换默认值 = 换行为）",
        path="ticket_helper/kernel/registry.py",
        old='    "login_check": "self",',
        new='    "login_check": "pjialin",',
        test="tests/test_kernel_user.py::test_default_backend_is_self_because_the_response_shape_differs",
    ),
    # ---------------- 构造完整性 / 失败可见性（2026-09-30 事故）----------------
    Mutation(
        name="构造完整性：__init__ 里声明的属性被挤进别的方法体（永不执行）",
        path="ticket_helper/inter/api.py",
        old="        self._order_combo_hint: tuple[int, int] | None = None",
        new="        pass  # 该赋值被挤走了",
        test="tests/test_kernel_user.py::test_every_attribute_declared_in_init_actually_exists",
    ),
    Mutation(
        name="失败可见性：查订单失败又被降回 debug（监控会无声地永不兑现）",
        path="ticket_helper/standby/engine.py",
        old='log.warning("候补兑现检查（订单查询）失败，本轮无法判定是否已出票: %s", exc)',
        new='log.debug("候补兑现检查（订单查询）失败: %s", exc)',
        test="tests/test_standby.py::test_order_query_failure_is_visible_instead_of_swallowed",
    ),
    # ---------------- 登录验证码端点（决策 §10-4：只换端点）----------------
    # 上游那份用的 `/passport/captcha/captcha-check` 真机已被 WAF 挡，
    # 照抄它等于写一条走不通的路径，而症状只是「验证码不对」。
    Mutation(
        name="验证码：校验端点退回被 WAF 挡的旧路径",
        path="ticket_helper/inter/api.py",
        old="            urls.CHECK_RAND_CODE,  # LOGIN_CAPTCHA_CHECK",
        new="            urls.CAPTCHA_CHECK,  # 退回旧端点",
        test="tests/test_captcha_endpoint.py::test_login_captcha_verification_uses_the_working_endpoint",
    ),
    Mutation(
        name="验证码：只带 answer 不带 randCode（另一半场景会静默失败）",
        path="ticket_helper/inter/api.py",
        old='            data={"answer": answer, "randCode": answer, "rand": rand, "_json_att": ""},',
        new='            data={"answer": answer, "rand": rand, "_json_att": ""},',
        test="tests/test_captcha_endpoint.py::test_login_captcha_sends_both_answer_and_rand_code",
    ),
    # ---------------- 扫码轮询请求体域（qr_check） ----------------
    # 这几条防的是「切了后端但请求体没变」以及「差异被抹平」——
    # 两套的端点相同、返回值也相同，所以只能靠**真实表单**来判。
    Mutation(
        name="扫码：上游那套不再带设备指纹（差异被抹平）",
        path="ticket_helper/kernel/pjialin/qr_check.py",
        old="            form_keys=ticket_facts.QR_CHECK_FIELDS,",
        new='            form_keys=("uuid", "appid"),',
        test="tests/test_kernel_qr.py::test_the_strategy_decides_what_is_posted",
    ),
    Mutation(
        name="扫码：form() 不再按规格过滤键（悄悄多发字段）",
        path="ticket_helper/kernel/domains/qr_check.py",
        old='        return {key: str(values.get(key, "")) for key in self.form_keys}',
        new="        return {key: str(value) for key, value in values.items()}",
        test="tests/test_kernel_qr.py::test_form_only_contains_the_declared_keys",
    ),
    Mutation(
        name="扫码：接口层不再用注入的规格（切换形同虚设）",
        path="ticket_helper/inter/api.py",
        old="        spec = self._qr_spec",
        new="        spec = self._builtin_qr_spec()",
        test="tests/test_kernel_qr.py::test_the_strategy_decides_what_is_posted",
    ),
    Mutation(
        name="扫码：默认那套也开始读设备指纹 Cookie（平白多一个依赖）",
        path="ticket_helper/inter/api.py",
        old='                if "RAIL_DEVICEID" in spec.form_keys',
        new="                if True",
        test="tests/test_kernel_qr.py::test_default_path_does_not_read_device_cookies",
    ),
    Mutation(
        name="扫码状态：把「已扫」与「成功」的映射对调（集合相同，语义反了）",
        path="ticket_helper/inter/parser.py",
        old="    QR_CODE_SCANNED: QR_STATE_SCANNED,",
        new="    QR_CODE_SCANNED: QR_STATE_CONFIRMED,",
        test="tests/test_qr_baseline.py::test_each_code_maps_to_the_intended_state",
    ),
    Mutation(
        name="扫码：默认后端被悄悄改成上游",
        path="ticket_helper/kernel/registry.py",
        old='    "qr_check": "self",',
        new='    "qr_check": "pjialin",',
        test="tests/test_kernel_qr.py::test_default_backend_is_self",
    ),
    # ---------------- 下单六步请求规格域（order_submit） ----------------
    # 这一期是**涉资金**的链路，而它的差异几乎都看不到：六步路径完全相同、
    # 返回值也相同 —— 差别只在「表单里放了什么」「带不带 Referer」「取值从哪来」。
    # 所以下面每一条都是在**线缆层**（mock 记下的真实表单）被抓住的。
    Mutation(
        name="下单：默认后端被悄悄改成上游（六步都不带 Referer，真机会被 WAF 拒）",
        path="ticket_helper/kernel/registry.py",
        old='    "order_submit": "self",',
        new='    "order_submit": "pjialin",',
        test="tests/test_kernel_order.py::test_default_backend_is_self",
    ),
    Mutation(
        name="下单：接口层不再用注入的规格（切换形同虚设）",
        path="ticket_helper/inter/api.py",
        old="        return self._order_spec.form(step)",
        new="        return self._builtin_order_spec().form(step)",
        test="tests/test_kernel_order.py::test_switch_actually_changes_the_wire_form",
    ),
    Mutation(
        name="下单：本项目多传的 undefined 被「顺手」改了值（重构改变行为）",
        path="ticket_helper/inter/urls.py",
        old='        "const": {"purpose_codes": "ADULT", "undefined": ""},',
        new='        "const": {"purpose_codes": "ADULT", "undefined": "1"},',
        test="tests/test_kernel_order.py::test_self_forms_reproduce_the_legacy_literals",
    ),
    Mutation(
        name="下单：给上游那套顺手补上 Referer（把差异抹平）",
        path="ticket_helper/kernel/pjialin/order_submit.py",
        old='                referer="",  # 上游没带 —— 见模块 docstring',
        new='                referer="/otn/leftTicket/init",  # 被补平了',
        test="tests/test_kernel_order.py::test_upstream_backend_sends_no_referer_and_self_does",
    ),
    Mutation(
        name="下单：上游 back_train_date 的取值来源被改成同名（差异被抹平）",
        path="ticket_helper/kernel/pjialin/order_submit.py",
        old='    STEP_SUBMIT: {"back_train_date": CTX_TRAIN_DATE},',
        new="    STEP_SUBMIT: {},",
        test="tests/test_order_baseline.py::test_back_train_date_value_source_is_a_recorded_difference",
    ),
    Mutation(
        name="下单：滑块三件套的固定值 scene 丢了（凑不齐就会发半套参数）",
        path="ticket_helper/kernel/pjialin/order_submit.py",
        old="                const.update(ticket_facts.ORDER_SLIDE_CONST)",
        new="                const.update({})",
        test="tests/test_order_baseline.py::test_slide_scene_value_matches_upstream",
    ),
    Mutation(
        name="下单：半套滑块参数不再被拦（发出去官方只会回「系统忙」）",
        path="ticket_helper/kernel/domains/order_submit.py",
        old="        if provided and len(provided) != len(needed):",
        new="        if False:",
        test="tests/test_kernel_order.py::test_half_slide_group_is_rejected",
    ),
    Mutation(
        name="下单：缺取值不再报错（切了后端只看到「系统忙」）",
        path="ticket_helper/kernel/domains/order_submit.py",
        old="        if source not in ctx:",
        new="        if source not in ctx and False:",
        test="tests/test_kernel_order.py::test_missing_value_names_the_step_and_field",
    ),
    Mutation(
        name="下单：cancel_flag 从字符串「对齐」成整数（类型差异被抹平）",
        path="ticket_helper/inter/urls.py",
        old='        "const": {"cancel_flag": "2", "bed_level_order_num": "000000000000000000000000000000",',
        new='        "const": {"cancel_flag": 2, "bed_level_order_num": "000000000000000000000000000000",',
        test="tests/test_order_baseline.py::test_cancel_flag_type_difference_is_recorded",
    ),
    Mutation(
        name="下单：bed_level_order_num 被「清理」成 0（少 29 个 0，官方只回「系统忙」）",
        path="ticket_helper/inter/urls.py",
        old='"000000000000000000000000000000"',
        new='"0"',
        test="tests/test_order_baseline.py::test_bed_level_order_num_is_thirty_zeros_on_both_sides",
    ),
    Mutation(
        name="mock：排队轮询只记 token（random 的来源差异离线又不可观测）",
        path="mock_server/server.py",
        old='        state.record("/otn/confirmPassenger/queryOrderWaitTime", request.args.to_dict())',
        new='        state.record("/otn/confirmPassenger/queryOrderWaitTime", {"token": token})',
        test="tests/test_kernel_order.py::test_wait_time_random_comes_from_the_recorded_source",
    ),
    Mutation(
        name="下单：attach_api 重建接口时漏传 order_spec（这条路径悄悄降级）",
        path="ticket_helper/account/manager.py",
        old="            self.order_spec,",
        new="",
        test="tests/test_kernel_order.py::test_attaching_api_keeps_the_order_spec",
    ),
    # ---------------- 滑块风控域（slide） ----------------
    # 这一域有个特点：真跑起来要真浏览器，本机跑不了。所以下面每条防的都是
    # **「离线能验证的那部分」**：判读同源、回退语义、接线、Cookie 全量。
    Mutation(
        name="滑块：默认后端被改回 self（等于悄悄关掉这项能力）",
        path="ticket_helper/kernel/registry.py",
        old='    "slide": "pjialin",',
        new='    "slide": "self",',
        test="tests/test_kernel_slide.py::test_default_backend_is_upstream_because_self_has_no_implementation",
    ),
    Mutation(
        name="滑块：回退后端改成抛异常（回退引入新的失败点）",
        path="ticket_helper/kernel/self/slide.py",
        old="        return None",
        new='        raise RuntimeError("回退后端不做事")',
        test="tests/test_kernel_slide.py::test_rollback_backend_is_unavailable_but_does_not_raise",
    ),
    Mutation(
        name="滑块：判断正则与上游脱钩（两边各写一份）",
        path="ticket_helper/inter/urls.py",
        old='ORDER_SLIDE_STATE_PATTERN = "var if_check_slide_passcode',
        new='ORDER_SLIDE_STATE_PATTERN = "var if_check_slide_passcodeX',
        test="tests/test_kernel_slide.py::test_the_two_backends_compile_the_same_pattern",
    ),
    Mutation(
        name="滑块：结果不再填进 checkOrderInfo（接线断了）",
        path="ticket_helper/inter/api.py",
        old="                **self._slide_values,",
        new="",
        test="tests/test_kernel_slide.py::test_slide_values_reach_check_order_info",
    ),
    Mutation(
        name="滑块：每单不重置结果（上一单的 sessionId 串到下一单）",
        path="ticket_helper/inter/api.py",
        old="        self._slide_values = {}" + "\n" + "        if needs_slide(html, self._slide_spec):",
        new="        if needs_slide(html, self._slide_spec):",
        test="tests/test_kernel_slide.py::test_slide_values_are_reset_for_every_order",
    ),
    Mutation(
        name="滑块：交给浏览器的 Cookie 被筛掉一部分（少一个就被 WAF 拒）",
        path="ticket_helper/inter/base.py",
        old="            cookies = list(self.session.cookies)",
        new='            cookies = [c for c in self.session.cookies if c.name in ("tk", "RAIL_DEVICEID")]',
        test="tests/test_kernel_slide.py::test_cookie_dump_is_complete_and_browser_ready",
    ),
    Mutation(
        name="mock：不再复现下单页的滑块标志（这条判据离线再也测不到）",
        path="mock_server/server.py",
        old='        if state.scenario == "need_slide":',
        new="        if False:",
        test="tests/test_kernel_slide.py::test_rollback_keeps_the_order_flow_alive",
    ),
    # ---------------- 节奏 / 参数域（pacing · heartbeat，P4） ----------------
    # 这一期的差异**全是数字**，而且都不报错：切过去只是「更密/更保守/更容易超时」。
    # 所以每条变异都要被**装配层**的测试抓住 —— 只比规格不接线的话，
    # 「我切了」会是一句无法查证的话。
    Mutation(
        name="节奏：pacing 默认后端被悄悄改成上游（请求密度直接提上去）",
        path="ticket_helper/kernel/registry.py",
        old='    "pacing": "self",',
        new='    "pacing": "pjialin",',
        test="tests/test_kernel_pacing.py::test_default_backend_is_self[pacing]",
    ),
    Mutation(
        name="心跳：heartbeat 默认后端被悄悄改成上游（去掉地板）",
        path="ticket_helper/kernel/registry.py",
        old='    "heartbeat": "self",',
        new='    "heartbeat": "pjialin",',
        test="tests/test_kernel_pacing.py::test_default_backend_is_self[heartbeat]",
    ),
    Mutation(
        name="节奏：限速器不再采纳注入的规格（切换形同虚设）",
        path="ticket_helper/ratelimit/limiter.py",
        old="            float(getattr(pacing, \"interval_min\")) if pacing is not None else float(cfg.min_interval)",
        new="            float(cfg.min_interval)",
        test="tests/test_kernel_pacing.py::test_the_switch_actually_changes_the_limiter",
    ),
    Mutation(
        name="节奏：装配中心造了规格却没传给账号链（限速切了、超时没切）",
        path="ticket_helper/runtime/context.py",
        old="            pacing_spec=pacing_spec,",
        new="",
        test="tests/test_kernel_pacing.py::test_composition_root_hands_the_specs_to_the_account_manager",
    ),
    Mutation(
        name="心跳：装配中心不再注入「按账号解析」的可调用对象",
        path="ticket_helper/runtime/context.py",
        old="            heartbeat_spec_for=self._build_heartbeat_spec,",
        new="",
        test="tests/test_kernel_pacing.py::test_composition_root_hands_the_specs_to_the_account_manager",
    ),
    Mutation(
        name="心跳：本项目的地板被抹掉（配置写 10 就 10 秒一次）",
        path="ticket_helper/kernel/self/heartbeat.py",
        old="MIN_INTERVAL = 30.0",
        new="MIN_INTERVAL = 0.0",
        test="tests/test_kernel_pacing.py::test_self_heartbeat_has_a_floor_that_the_config_agrees_with",
    ),
    Mutation(
        name="节奏：区间不再由上游函数算出（自己写公式，抄错除数没人发现）",
        path="ticket_helper/kernel/pjialin/pacing.py",
        old='            interval_min=float(interval["min"]),',
        new="            interval_min=float(ticket_facts.QUERY_INTERVAL),",
        test="tests/test_pacing_baseline.py::test_interval_range_is_computed_by_the_upstream_function",
    ),
    Mutation(
        name="心跳：上游后端开始采纳本项目的账号配置（那就不是上游那套了）",
        path="ticket_helper/kernel/pjialin/heartbeat.py",
        old="        return HeartbeatSpec(name=NAME, interval=float(interval), min_interval=0.0)",
        new="        return HeartbeatSpec(name=NAME, interval=float(self._ignored or interval), min_interval=0.0)",
        test="tests/test_kernel_pacing.py::test_heartbeat_backend_ignores_the_account_setting",
    ),
    Mutation(
        name="节奏：传输层超时不再跟随规格（切了 pacing 只影响间隔）",
        path="ticket_helper/account/manager.py",
        old="            self.client.timeout = float(pacing_spec.timeout)",
        new="            pass  # 超时不再跟随",
        test="tests/test_kernel_pacing.py::test_the_switch_actually_changes_the_heartbeat_and_the_timeout",
    ),
    Mutation(
        name="心跳：账号心跳不再走规格（切了 heartbeat 没有任何效果）",
        path="ticket_helper/account/manager.py",
        old="        self.heartbeat_interval = self._resolve_heartbeat(config.heartbeat)",
        new="        self.heartbeat_interval = max(30, int(config.heartbeat))",
        test="tests/test_kernel_pacing.py::test_the_switch_actually_changes_the_heartbeat_and_the_timeout",
    ),
    # ---------------- 垫片 sys.path 注入位置（真机踩过） ----------------
    Mutation(
        name="垫片：把 vendor 插到 sys.path 最前面（抢走本项目的 main）",
        path="ticket_helper/kernel/pjialin/shim.py",
        old="        sys.path.append(root)",
        new="        sys.path.insert(0, root)",
        test="tests/test_vendor_pjialin.py::test_shim_does_not_shadow_our_own_modules",
    ),
    Mutation(
        name="纪律门：弱断言豁免退回只看首行（末行写 noqa 会失效）",
        path="tests/test_discipline.py",
        old="            span = lines[node.lineno - 1 : node.end_lineno or node.lineno]",
        new="            span = lines[node.lineno - 1 : node.lineno]",
        test="tests/test_verify_tool.py::test_weak_assert_exemption_covers_the_whole_statement",
    ),
    Mutation(
        name="下单：secretStr 不再 URL 解码（真机表现：官方只回「提交失败，请重试...」）",
        path="ticket_helper/inter/parser.py",
        old="        secret_str=urllib.parse.unquote(parts[IDX_SECRET_STR]),",
        new="        secret_str=parts[IDX_SECRET_STR],",
        test="tests/test_kernel_order.py::test_submitted_secret_str_is_the_decoded_one",
    ),
    Mutation(
        name="mock：initDc 不再下发 ticketInfoForPassengerForm（偏离真机形状，#64 复现）",
        path="mock_server/server.py",
        old='        if state.scenario != "no_initdc_form":',
        new="        if False:",
        test="tests/test_order_params_on_the_wire.py::test_left_ticket_comes_from_init_dc_not_the_query_row",
    ),
    Mutation(
        name="sys.path：上游目录被插到本项目根前面（import main 抢到上游入口）",
        path="tests/test_query_baseline.py",
        old="if str(VENDOR) not in sys.path:\n    sys.path.append(str(VENDOR))",
        new="if str(VENDOR) not in sys.path:\n    sys.path.insert(0, str(VENDOR))",
        test="tests/test_vendor_pjialin.py::test_our_project_root_precedes_vendor_on_sys_path",
    ),
    # ---------------- 乘车日期规则（回流 #53） ----------------
    Mutation(
        name="日期：服务端不再拦「已经过去」的乘车日期（去年的任务又能存进去了）",
        path="ticket_helper/config/settings.py",
        old="            if train_date_util.classify(date) == train_date_util.DATE_PAST:",
        new="            if False:",
        test="tests/test_config.py::test_task_validate_rejects_past_dates_but_not_future_ones",
    ),
    Mutation(
        name="日期：桌面控件不再拦过去的日期（能一路存进任务配置）",
        path="desktop_ui/token_field.py",
        old="        if parsed < _today_qdate():",
        new="        if False:",
        test="tests/test_desktop.py::test_date_field_refuses_past_and_invalid_dates",
    ),
    Mutation(
        name="桌面：日历点选日期不再自动加入（退回「还得点添加日期」的旧交互，回流 #65）",
        path="desktop_ui/token_field.py",
        old="        self.picker.dateChanged.connect(self._on_calendar_picked)",
        new="        pass  # mutated",
        test="tests/test_desktop.py::test_date_field_takes_dates_from_calendar",
    ),
    Mutation(
        name="日期：网页端不再校验（`min` 与 dateProblem 都失效，只剩服务端兜底）",
        path="ticket_helper/web/static/app.js",
        old="  const problem = dateProblem(value);",
        new="  const problem = '';",
        test="tests/test_contracts.py::test_web_date_guard_is_wired_on_both_buttons",
    ),
    # ---------------- 状态值治理（回流 #54 / #55）----------------
    Mutation(
        name="状态校验：未知状态不再被拒（非法值静默入库）",
        path="ticket_helper/storage/database.py",
        old="    if text not in allowed:\n",
        new="    if text not in allowed and False:\n",
        test="tests/test_storage.py::test_unknown_status_is_rejected_at_the_persistence_boundary",
    ),
    Mutation(
        name="DDL：又给 status 写回默认值（storage 重新持有状态语义）",
        path="ticket_helper/storage/database.py",
        old="            seat_types TEXT DEFAULT '[]',\n            status TEXT NOT NULL,\n",
        new="            seat_types TEXT DEFAULT '[]',\n"
        "            status TEXT NOT NULL DEFAULT 'submitted',\n",
        test="tests/test_contracts.py::test_ddl_holds_no_status_semantics",
    ),
    Mutation(
        name="SQL：候补活跃态又写回字面量（词表改名就静默失效）",
        path="ticket_helper/storage/database.py",
        old="            \" FROM standby_orders WHERE status = ?\",\n"
        "            (_status_constant(\"standby_orders\", \"STANDBY_QUEUING\"),),\n",
        new="            \" FROM standby_orders WHERE status IN ('submitted','queuing')\",\n"
        "            (),\n",
        test="tests/test_contracts.py::test_storage_sql_holds_no_standby_status_literals",
    ),
    Mutation(
        name="僵尸状态复活：把它加回词表却没有产生点",
        path="ticket_helper/standby/engine.py",
        old="STANDBY_STATES: tuple[str, ...] = (\n    STANDBY_PENDING,\n",
        new="STANDBY_SUBMITTED = \"submitted\"\n"
        "STANDBY_STATES: tuple[str, ...] = (\n    STANDBY_PENDING,\n    STANDBY_SUBMITTED,\n",
        test="tests/test_contracts.py::test_every_standby_state_has_a_producer",
    ),
    # ---------------- 两端导航一致 / 设置页归属（回流 #56）----------------
    Mutation(
        name="两端导航：桌面端少了「系统设置」（换客户端就找不到菜单）",
        path="desktop_ui/main_window.py",
        old='    ("settings", "系统设置"),\n',
        new='    ("settings_x", "系统设置"),\n',
        test="tests/test_contracts.py::test_desktop_and_web_navigation_are_identical",
    ),
    Mutation(
        name="系统设置：限速控件跑回通知页（页名与内容不符）",
        path="desktop_ui/pages/settings.py",
        old="        self.check_adaptive = QCheckBox(",
        new="        self.check_adaptive_elsewhere = QCheckBox(",
        test="tests/test_desktop.py::test_settings_page_owns_the_ratelimit_and_codec_controls",
    ),
    Mutation(
        name="验收工具：把「全过但退出码非 0」直接当成通过（掩盖真问题）",
        path="scripts/verify.py",
        old='    return "exit_anomaly" if has_xml else "crashed"\n',
        new='    return "ok"\n',
        test="tests/test_verify_tool.py::test_batch_outcome_classification",
    ),
    # ---------------- P5 打包完整性（上游内核到底进没进包） ----------------
    # 这一批防的全是**静默**故障：源码模式下一切正常，只有装上 exe 才暴露。
    Mutation(
        name="打包：spec 不再把 vendor 放进包里（冻结后 VENDOR_ROOT 指向空目录）",
        path="TicketHelper.spec",
        old='    (str(PROJECT_ROOT / "vendor" / "pjialin"), "vendor/pjialin"),',
        new='    # (str(PROJECT_ROOT / "vendor" / "pjialin"), "vendor/pjialin"),',
        test="tests/test_packaging.py::test_spec_ships_the_vendor_tree_where_the_shim_looks_for_it",
    ),
    Mutation(
        name="打包：上游第三方依赖掉了一条（打包后一用就打码/过滑块就 ModuleNotFoundError）",
        path="TicketHelper.spec",
        old='    "requests_html",',
        new='    # "requests_html",',
        test="tests/test_packaging.py::test_no_reachable_upstream_dependency_is_left_without_a_home",
    ),
    Mutation(
        name="打包：豁免清单混进一个**不可达**的名字（清单开始腐烂）",
        path="scripts/check_packaging.py",
        old="ALLOWED_MISSING: dict[str, str] = {",
        new='ALLOWED_MISSING: dict[str, str] = {\n    "tqdm": "随手写的理由（它根本不可达）",',
        test="tests/test_packaging.py::test_upstream_web_backend_is_kept_out_of_the_bundle",
    ),
    Mutation(
        name="打包：单文件那条路又开始自己拼 PyInstaller 参数（上游内核悄悄不进包）",
        path="scripts/build_exe.py",
        old="    command += [str(SPEC)]",
        new='    command += ["--add-data", "x;y", str(SPEC)]',
        test="tests/test_packaging.py::test_onefile_mode_goes_through_the_same_spec",
    ),
    Mutation(
        name="滑块：可选后端取规格失败时不再兜底，故障被打回账号恢复（真机表现为「0 个账号」）",
        path="ticket_helper/inter/api.py",
        old="        except Exception as exc:  # noqa: BLE001 - 任何失败都不该打断账号恢复",
        new="        except ZeroDivisionError as exc:  # 只兜一种无关的异常",
        test="tests/test_kernel_slide.py::test_a_broken_solver_cannot_break_account_restore",
    ),
    # ---------------- V0 静态纪律门 ----------------
    # 这三条防的是「纪律门本身失效」：门如果漏了，后面几层全绿也没意义。
    Mutation(
        name="纪律门：新增一条弱断言（assert x）",
        path="tests/test_contracts.py",
        old="def test_task_status_values_are_used_via_constants_only()",
        new="def test_discipline_probe_m1():\n    probe = 1\n    assert probe\n\n\ndef test_task_status_values_are_used_via_constants_only()",
        test="tests/test_discipline.py::test_no_new_weak_assertions",
    ),
    Mutation(
        name="纪律门：新增一个未豁免的 skip",
        path="tests/test_contracts.py",
        old="def test_task_status_values_are_used_via_constants_only()",
        new="def test_discipline_probe_m2():\n    import pytest as _p\n    _p.skip('偷懒')\n\n\ndef test_task_status_values_are_used_via_constants_only()",
        test="tests/test_discipline.py::test_no_unauthorized_skip",
    ),
    # ---------------- 下单参数形状（回流 #51） ----------------
    # 防的是「参数形状悄悄退回旧样子」：mock 的 checkOrderInfo 已按**上游形状**校验，
    # 撤掉任一处 -> mock 回真机那句「余票不足！」-> 线缆级用例当场变红。
    Mutation(
        name="下单参数：passengerTicketStr 漏发第 9 段官方加密串（#51 复现）",
        path="ticket_helper/inter/parser.py",
        old='                    "N",\n                    enc_str,',
        new='                    "N",',
        test="tests/test_order_params_on_the_wire.py::test_check_order_info_sends_the_enc_str_on_the_wire",
    ),
    Mutation(
        name="下单参数：oldPassengerStr 少了上游的 '__ _ _' 后缀（#51 复现）",
        path="ticket_helper/inter/parser.py",
        old='return joined.rstrip("_") + "__ _ _"',
        new='return joined',
        test="tests/test_order_params_on_the_wire.py::test_confirm_single_for_queue_sends_the_same_shape",
    ),
    # ---------------- 下单链路硬缺陷（回流 #60 / #61） ----------------
    # 这四条防的都是「离线怎么测都是绿的」那类改动：错路径 / 错字段 / 错终态语义。
    # 判据全部落在**线缆上**（mock 记下的请求），mock 侧自己也有一条反向判据。
    Mutation(
        name="下单验证码：路径退回真机不存在的 /otn/confirmPassenger/getPassCodeNew（#60 复现）",
        path="ticket_helper/inter/urls.py",
        old='GET_PASS_CODE_NEW = "/otn/passcodeNew/getPassCodeNew"                     # 下单图形验证码',
        new='GET_PASS_CODE_NEW = "/otn/confirmPassenger/getPassCodeNew"                # 下单图形验证码',
        test="tests/test_order_chain_fixes.py::test_order_captcha_uses_the_upstream_path_and_rand",
    ),
    Mutation(
        name="下单验证码：rand 退回登录用的 sjrand（#60 复现）",
        path="ticket_helper/inter/urls.py",
        old='ORDER_PASS_CODE_RAND = "randp"',
        new='ORDER_PASS_CODE_RAND = "sjrand"',
        test="tests/test_order_chain_fixes.py::test_order_captcha_uses_the_upstream_path_and_rand",
    ),
    Mutation(
        name="下单参数：leftTicket 退回席别列表 seat_types_raw（#61 复现）",
        path="ticket_helper/inter/api.py",
        old="                leftTicket=(\n                    left_ticket\n                    or (self.ticket_info or {}).get(\"leftTicketStr\")",
        new="                leftTicket=(\n                    left_ticket\n                    or \"\"",
        test="tests/test_order_params_on_the_wire.py::test_left_ticket_comes_from_init_dc_not_the_query_row",
    ),
    Mutation(
        name="下单参数：getQueueCount 的 train_date 退回 YYYY-MM-DD（真机回「系统忙」，#64 复现）",
        path="ticket_helper/inter/api.py",
        old="                train_date=_format_train_date_js(ticket.train_date),",
        new="                train_date=_format_train_date(ticket.train_date),",
        test="tests/test_order_params_on_the_wire.py::test_left_ticket_comes_from_init_dc_not_the_query_row",
    ),
    Mutation(
        name="排队终态：不认 waitTime=-2/-3，把「订单已撤销」说成「排队超时」（#61 复现）",
        path="ticket_helper/inter/api.py",
        old="                if result.wait_time in (-2, -3):",
        new="                if result.wait_time in ():",
        test="tests/test_order_chain_fixes.py::test_cancelled_queue_is_reported_as_failure",
    ),
    Mutation(
        name="initDc 解析：去掉「整页抢救」那一步（页面写法一变就静默拿空值）",
        path="ticket_helper/inter/parser.py",
        old='    for name in ("leftTicketStr", "key_check_isChange", "train_location", "purpose_codes")',
        new="    for name in ()",
        test="tests/test_order_chain_fixes.py::test_init_dc_salvages_loose_values_when_the_wrapper_is_unparsable",
    ),
    Mutation(
        name="mock 校验：把 getQueueCount 的 leftTicket 判据删掉（判据自己也要有测试）",
        path="mock_server/server.py",
        old=(
            '        if (\n'
            '            form.get("leftTicket") not in allowed_left\n'
            '            or not form.get("train_location")\n'
            '            or form.get("train_date") != expected_date\n'
            '        ):'
        ),
        new='        if not form.get("train_location"):',
        test="tests/test_order_params_on_the_wire.py::test_left_ticket_comes_from_init_dc_not_the_query_row",
    ),
    # ---------------- 「今天」只有一个来源 / 回环地址不走代理 ----------------
    # 这两条都是 2026-10-03 跑全量时**真的红了**才发现的，同属「第二个来源」：
    # 判据散成了两份，平时看不出，一遇到真实环境（机器时钟 / 系统代理）就分叉。
    Mutation(
        name="日期：桌面控件的「今天」改回 Qt 的真实今天（第二来源）",
        path="desktop_ui/token_field.py",
        old="        self.picker.setMinimumDate(_today_qdate())",
        new="        self.picker.setMinimumDate(QDate.currentDate())",
        # ⚠️ 只有「真实今天 != 冻结的今天」时才抓得住 —— 这正是它被发现的方式。
        test="tests/test_desktop.py::test_date_field_takes_today_from_the_single_source",
    ),
    Mutation(
        name="网络：回环判定失效（系统代理会把 127.0.0.1 也代理出去）",
        path="ticket_helper/inter/base.py",
        old='    return host in LOOPBACK_HOSTS or host.startswith("127.")',
        new="    return False  # 变异：回环判定失效",
        test="tests/test_ratelimit.py::test_ambient_env_proxy_never_covers_loopback",
    ),
)


def _apply(mutation: Mutation, backup: Path) -> None:
    path = ROOT / mutation.path
    shutil.copy2(path, backup)
    raw = io.open(path, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    old, new = mutation.old.replace("\n", nl), mutation.new.replace("\n", nl)
    assert raw.count(old) >= 1, f"{mutation.path}: 找不到待变异片段 {mutation.old[:60]!r}"
    io.open(path, "w", encoding="utf-8", newline="").write(raw.replace(old, new, 1))


def _restore(mutation: Mutation, backup: Path) -> None:
    shutil.copy2(backup, ROOT / mutation.path)


def main() -> int:
    parser = argparse.ArgumentParser(description="契约变异检验")
    parser.add_argument("-k", default="", help="只跑名字包含该子串的变异")
    args = parser.parse_args()

    # 自检：变异名必须唯一。
    # 2026-09-30 真的发生过：往这个文件里追加变异时被执行环境重复执行了一次，
    # 于是同一组变异被写进两份 —— 它们会被**各跑一遍**（耗时翻倍），
    # 而「共 N 个变异」这个数字也跟着虚高，看的人以为覆盖得很全。
    names = [m.name for m in MUTATIONS]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        print("✘ 变异名重复（同一变异会被跑两遍，覆盖数也会虚高）：")
        for item in duplicated:
            print(f"    - {item}")
        return 1

    # 自检：每条变异的锚点必须**真的存在**。
    #
    # 锚点会因为「别人改了代码」而失效，而失效是**静默**的：只有那条变异被跑到
    # 才会炸，而 `-k` 过滤、分批跑都可能让它长期不被执行 —— 清单却始终显示
    # 「共 N 个变异」，给人「覆盖到了」的错觉。2026-09-30 真实撞上：把 DDL 里的
    # `status TEXT DEFAULT 'stopped'` 全删掉之后，两条老变异成了死锚点，
    # 直到跑 `-k 状态` 才暴露。**清单会腐烂，所以要让它当场腐烂给你看。**
    stale: list[str] = []
    for mutation in MUTATIONS:
        source = (ROOT / mutation.path).read_text(encoding="utf-8")
        if mutation.old not in source:
            stale.append(f"{mutation.name} → {mutation.path} 找不到 {mutation.old[:50]!r}")
        elif mutation.old == mutation.new:
            stale.append(f"{mutation.name} → old 与 new 相同，等于什么都没改")
    if stale:
        print(f"✘ 变异清单已腐烂（{len(stale)} 条锚点不存在）：")
        for item in stale:
            print(f"    - {item}")
        print("  这些变异永远不会执行到，清单却仍在给它们记「覆盖」。")
        return 1

    selected = [m for m in MUTATIONS if args.k in m.name] if args.k else list(MUTATIONS)
    if not selected:
        print(f"没有匹配 {args.k!r} 的变异")
        return 2

    print(f"共 {len(selected)} 个变异。期望：每个变异都让对应测试**失败**。\n")
    print(f"{'变异':<58}{'对应测试':<20}{'结果'}")
    print("-" * 96)

    survived: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "backup.py"
        for mutation in selected:
            try:
                _apply(mutation, backup)
                proc = subprocess.run(
                    [PYTHON, "-m", "pytest", mutation.test, "-q", "--no-header", "-p", "no:cacheprovider"],
                    cwd=str(ROOT), capture_output=True, text=True,
                )
            finally:
                _restore(mutation, backup)

            caught = proc.returncode != 0
            mark = "抓到 ✓" if caught else "**漏过 ✗**"
            if not caught:
                survived.append(mutation.name)
            short_test = mutation.test.rsplit("::", 1)[-1]
            label = short_test if len(short_test) <= 18 else short_test[:17] + "…"
            print(f"{mutation.name:<58}{label:<20}{mark}")

    print("-" * 96)
    if survived:
        print(f"× {len(survived)} 个变异没有被测试抓住（这些测试是假的）：")
        for name in survived:
            print(f"    {name}")
        return 1
    print(f"√ 全部 {len(selected)} 个变异都被抓住了：契约测试是有效的（撤掉实现就会红）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
