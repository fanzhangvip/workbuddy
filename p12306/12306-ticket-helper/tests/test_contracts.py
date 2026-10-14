"""跨模块契约测试。

这个文件盯的不是「某个函数算得对不对」，而是**契约本身**：

- 同一个语义的状态值是否**只有一个定义处**；
- 引用了该契约的模块，在契约变更时会不会**立刻报错**（而不是静默失效）。

为什么单独一个文件：这类约束以前只写在注释里。注释是文档，不是约束 ——
下一个人（或下一次的自己）照样会犯。凡是注释里写了「不能这样做」的地方，
这里都应该有一条测试拦住它。

每条测试的 docstring 都写明：它防的是**哪一种真实会发生且静默的故障**。
删掉对应实现后这些测试必须变红；如果某条测试在实现被撤掉后仍然通过，
说明它是假测试，要立刻改掉。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8", errors="replace")


def _js_object_keys(source: str, const_name: str) -> set[str]:
    """从 JS 里取出 ``const NAME = { a: ..., b: ... };`` 的键集合。"""
    match = re.search(rf"const {const_name} = \{{(.*?)\}};", source, re.S)
    assert match, f"web/static/app.js 里找不到 const {const_name}"
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", match.group(1)))


# =========================================================================== #
# 契约 1：账号状态词表
# =========================================================================== #
def test_account_status_vocabulary_is_identical_across_three_places():
    """账号状态：后端常量 / 桌面端文案表 / 网页端文案表，三者必须是同一套词。

    防的故障（真实存在过同类问题）：后端新增一个状态（例如 ``expired``）时，
    只有一处跟着改。桌面端会把 ``STATUS_TEXT.get(status, status)`` 退化成
    显示**英文原文**，网页端会显示裸状态字符串 —— 全程没有任何报错，
    只在某台机器上「看起来怪怪的」。
    """
    from ticket_helper.account import manager

    # **必须靠内省发现**，不能手写一份常量清单：
    # 手写清单的话，「后端新增了一个状态」这个最该被抓到的情况反而漏过
    # ——（这一条是被 scripts/contract_mutation_check.py 抓出来的）。
    backend = {
        value
        for name, value in vars(manager).items()
        if name.startswith("STATUS_") and isinstance(value, str)
    }
    assert len(backend) >= 6, f"没扫描到账号状态常量，内省方式需要跟着改：{backend}"

    from desktop_ui.pages.accounts import STATUS_TEXT as DESKTOP_TEXT

    web_keys = _js_object_keys(_read("ticket_helper/web/static/app.js"), "STATUS_TEXT")

    assert set(DESKTOP_TEXT) == backend, (
        f"桌面端账号状态文案表的键与后端常量不一致：\n"
        f"  后端独有 {backend - set(DESKTOP_TEXT)}；桌面端独有 {set(DESKTOP_TEXT) - backend}"
    )
    assert web_keys == backend, (
        f"网页端账号状态文案表的键与后端常量不一致：\n"
        f"  后端独有 {backend - web_keys}；网页端独有 {web_keys - backend}"
    )


# =========================================================================== #
# 契约 2：订单状态文案（唯一一份定义）
# =========================================================================== #
def test_order_status_text_covers_exactly_the_declared_statuses():
    """订单状态文案表的键必须**恰好**等于服务层声明的状态集合。

    防的故障（真实发生过）：桌面端曾自己写
    ``{"success": ..., "failed": ..., "pending": "进行中"}``，
    而服务层实际写入的是 ``submitted`` / ``queued`` —— 「pending」这个键
    在真实数据里**永远不会出现**，于是「已提交 / 排队中」的订单
    在界面上显示英文原文，且无任何报错。
    """
    from ticket_helper.order.service import (
        ORDER_CANCELLED,
        ORDER_FAILED,
        ORDER_QUEUED,
        ORDER_STATUS_TEXT,
        ORDER_STATUSES,
        ORDER_SUBMITTED,
        ORDER_SUCCESS,
    )

    declared = {
        ORDER_SUBMITTED,
        ORDER_QUEUED,
        ORDER_SUCCESS,
        ORDER_FAILED,
        ORDER_CANCELLED,
    }
    assert set(ORDER_STATUS_TEXT) == declared, (
        f"订单文案表与状态常量不一一对应：\n"
        f"  常量独有 {declared - set(ORDER_STATUS_TEXT)}；"
        f"文案表独有 {set(ORDER_STATUS_TEXT) - declared}"
    )
    assert set(ORDER_STATUSES) == declared
    # 文案不能为空：空串会让界面显示成空白格，比显示英文更难排查
    assert all(ORDER_STATUS_TEXT.values()), "订单状态文案存在空值"
    # 每个状态都必须有中文（不能有键却没值/留英文占位）
    for key, text in ORDER_STATUS_TEXT.items():
        assert re.search(r"[\u4e00-\u9fff]", text), f"{key} 的文案没有中文：{text!r}"


def test_web_ui_order_status_branches_use_declared_values():
    """网页端做状态判断时用的字面量，必须落在服务层声明的状态集合内。

    前端没法 import Python 常量，所以这是**跨语言契约**里最容易腐烂的一处：
    后端改了状态值，前端仍在比旧字符串，徽标会静默变成「warn」色，
    既不报错也不显眼。这条测试把两边钉在一起。
    """
    from ticket_helper.order.service import ORDER_STATUSES

    js = _read("ticket_helper/web/static/app.js")
    used = set(re.findall(r"o\.status\s*===\s*'([^']+)'", js))
    assert used, "没能从 app.js 里解析出任何订单状态判断（正则需要跟着改）"
    assert used <= set(ORDER_STATUSES), (
        f"网页端用了服务层不存在的订单状态字面量：{used - set(ORDER_STATUSES)}"
    )


# =========================================================================== #
# 契约 3：任务状态（持久化值）
# =========================================================================== #
def test_task_status_values_are_used_via_constants_only():
    """写入 ``tasks.status`` 的地方**不许出现裸字符串**。

    这是纯静态检查：扫描 ``ticket_helper/`` 下所有 ``update_task_status(...)``
    调用，第二个参数必须是名字（常量引用），不能是字符串字面量。
    防的故障：状态值改了常量，某处漏改 —— 数据库里于是同时存在两套值，
    「启动时恢复上次运行的任务」这类按 status 过滤的逻辑会静默漏掉一批任务。
    """
    offenders: list[str] = []
    for path in (ROOT / "ticket_helper").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "update_task_status"):
                continue
            if len(node.args) < 2:
                continue
            second = node.args[1]
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {second.value!r}")
    assert not offenders, (
        "update_task_status 的第二参数出现了字符串字面量，应改用 settings.TASK_STATUS_*：\n  "
        + "\n  ".join(offenders)
    )


def test_task_status_translation_covers_terminal_states():
    """引擎状态 → 任务状态的翻译表：键必须是真状态，且**终态全覆盖**。

    防的故障：翻译表漏了某个终态（例如只映射了 ``success`` 却漏了 ``error``），
    任务失败后数据库里的状态仍停在 ``running`` ——
    「按 status='running' 恢复任务」会在下次启动时把一个已经失败的任务再拉起来。
    这正是「终态不可转出」在持久化层的落点。
    """
    from ticket_helper.config.settings import TASK_STATUSES
    from ticket_helper.grab import engine as g
    from ticket_helper.runtime.context import ENGINE_STATE_TO_TASK_STATUS as mapping

    declared_states = {
        g.STATE_IDLE,
        g.STATE_READY,
        g.STATE_GRABBING,
        g.STATE_SUCCESS,
        g.STATE_STOPPED,
        g.STATE_ERROR,
    }
    assert set(mapping) <= declared_states, (
        f"翻译表里有不是引擎状态的键：{set(mapping) - declared_states}"
    )

    terminal = (g.STATE_SUCCESS, g.STATE_STOPPED, g.STATE_ERROR)
    missing = [s for s in terminal if s not in mapping]
    assert not missing, f"终态没有映射到持久化状态，任务状态会永远停在运行中：{missing}"

    assert set(mapping.values()) <= set(TASK_STATUSES), (
        f"翻译结果不在 TASK_STATUSES 里：{set(mapping.values()) - set(TASK_STATUSES)}"
    )
    assert len(set(mapping.values())) == len(mapping), "多个引擎状态翻译成了同一个持久化状态"


def test_task_config_default_status_is_a_declared_value():
    """``TaskConfig.status`` 的默认值必须在合法取值集合里（否则新建任务即非法）。"""
    from ticket_helper.config.settings import TASK_STATUSES, TaskConfig

    assert TaskConfig(name="t").status in TASK_STATUSES


# =========================================================================== #
# 契约 4：候补终态只有一处定义
# =========================================================================== #
def test_standby_terminal_states_have_exactly_one_definition():
    """``STANDBY_TERMINAL_STATES`` 只能有一处定义，``grab.engine`` 必须引用它。

    防的故障（这条是审计时**真的发现过**的）：``grab.engine`` 曾经自己抄了一份
    ``("success", "failed", "cancelled", "stopped")``，理由是「互相 import 会成环」
    —— 那个理由是错的。抄一份的后果很具体：``STANDBY_SUCCESS`` 一旦改名，
    这边会留着旧字面量，「仅候补」的任务永远等不到终态，**挂死且无报错**。

    用 ``is`` 而不是 ``==``：相等只说明当前内容相同，
    我们要的是「同一个对象」，即真的引用而不是各写一遍恰好一样。
    """
    from ticket_helper.grab import engine as g
    from ticket_helper.standby import engine as s

    assert g.STANDBY_TERMINAL_STATES is s.STANDBY_TERMINAL_STATES, (
        "grab.engine 的候补终态不是 standby.engine 那个对象 —— 又抄了一份"
    )
    assert set(s.STANDBY_TERMINAL_STATES) == {
        s.STANDBY_SUCCESS,
        s.STANDBY_FAILED,
        s.STANDBY_CANCELLED,
        s.STANDBY_STOPPED,
    }, "候补终态集合与 standby.engine 的常量对不上"


def _table_status_spec(table: str) -> str:
    """取出某张表建表语句里 ``status`` 列的**列定义片段**（``TEXT`` 之后到逗号）。

    必须**精确定位到具体表**：早先这里写成「值在整个文件里出现过就算有产生点」，
    于是 ``STANDBY_STOPPED``（"stopped"）被 ``tasks`` 表的默认值误判成有产生点
    —— 两个完全不同的域。这条是被 scripts/contract_mutation_check.py 抓出来的。
    """
    src = _read("ticket_helper/storage/database.py")
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\n\s*\);", src, re.S)
    assert match, f"database.py 里找不到表 {table} 的建表语句"
    column = re.search(r"status\s+TEXT([^,]*)", match.group(1))
    if column is None:
        raise AssertionError(f"表 {table} 没有 status 列（这条正需要跟着 DDL 一起改）")
    return column.group(1).strip().upper()


def test_every_standby_state_has_a_producer():
    """候补词表（``STANDBY_STATES``）里的每个值都必须有**产生点**。

    产生点只有两种，且必须**同域**：
    ① 引擎把它赋给 ``self.status``；② 引擎把它写进 ``standby_orders``。

    ⚠️ 原先还有第三种判据「``standby_orders`` 建表语句里的默认值」——
    那个判据是**错的**：建表默认值恰恰是「没有任何地方写过这个值」的证据。
    它把全仓从不赋值的 ``submitted`` 判成「有来源」，于是僵尸状态守卫
    长期放行（真实事故）。删掉 DDL 默认值之后，本测试立刻抓到了它。

    ⚠️ 第二个坑：原来这里写的是**硬编码的名字元组**。那样新增一个状态常量
    根本不会被检查 ——「枚举常量漏检」是假测试的经典成因。
    现在遍历 ``STANDBY_STATES`` 这个**声明**，声明的值一个都跑不掉。
    """
    from ticket_helper.standby import engine as s

    engine_src = _read("ticket_helper/standby/engine.py")

    # 只取「赋值给 self.status 的那一行」，再看该行有没有引用这个常量。
    # 不能用 ``self\.status\s*=\s*NAME`` 这种紧贴写法：
    # ``self.status = STANDBY_CANCELLED if should_cancel else STANDBY_STOPPED``
    # 里常量在 ``else`` 后面，紧贴式正则会漏判成「没有产生点」——
    # 这是被 scripts/contract_mutation_check.py 抓出来的（测试写错，不是代码错）。
    status_assignments = [
        line for line in engine_src.splitlines() if re.search(r"self\.status\s*=", line)
    ]
    assert status_assignments, "没能从 standby/engine.py 里解析出 self.status 赋值（正则需要跟着改）"

    # 从源码里收集「被产生过的常量名」，再按名字解析成值（值可能在别处被改）
    produced_names: set[str] = set()
    for line in status_assignments:
        produced_names |= set(re.findall(r"\bSTANDBY_[A-Z_]+\b", line))
    produced_names |= set(re.findall(r'"status":\s*(STANDBY_[A-Z_]+)', engine_src))

    produced_values = {getattr(s, name) for name in produced_names if hasattr(s, name)}
    zombies = [value for value in s.STANDBY_STATES if value not in produced_values]
    assert zombies == [], (
        f"候补词表里有僵尸状态值：{zombies} —— 声明了却没有任何地方产生它，"
        f"读者会以为存在这么一个状态并据此写判断（要么删掉，要么给它真实的进入点）。"
        f"全部声明：{list(s.STANDBY_STATES)}；有产生点的：{sorted(produced_values)}"
    )


def test_ddl_holds_no_status_semantics():
    """★ 建表语句里**不允许**再出现任何 ``status`` 默认值，且列必须 NOT NULL。

    来源（真实事故）：``status TEXT DEFAULT 'submitted'`` 这类默认值不只是冗余。
    它让持久化层**自己持有一份状态语义**（同一个值散在 DDL / 应用层缺省 /
    词表三处），更糟的是它被 ``test_every_standby_state_has_a_producer``
    当成「产生点」—— 于是那个**全仓从不赋值**的 ``submitted``
    一直以「有来源」的面目通过守卫。删掉默认值之后，守卫立刻抓到了它。
    「默认值算产生点」这个判据本身就是错的：默认值恰恰是**没有**产生点的证明。

    现在状态一律由应用层显式写入，并在 ``storage._require_status`` 处按词表
    校验；DDL 只剩 ``status TEXT NOT NULL``（不填就报 IntegrityError，
    而不是静默落一个谁也没写过的默认值）。
    """
    for table in ("users", "tasks", "orders", "standby_orders"):
        # 只允许这一个形状：同一句同时证明「没有默认值」与「非空」
        spec = _table_status_spec(table)
        assert spec == "NOT NULL", (
            f"表 {table} 的 status 列定义是 {spec!r}（期望 'NOT NULL'）—— "
            "带 DEFAULT 就等于 storage 自己持有一份状态语义；"
            "允许 NULL 则会让「漏传 status」静默入库"
        )


def test_storage_sql_holds_no_standby_status_literals():
    """★ 存储层 SQL 里**不允许**出现候补状态字面量，必须按常量名去取词表值。

    来源：原先 ``status IN ('submitted','queuing')`` 把两个状态写死在 SQL 里。
    写死的后果是**静默的**：词表改名后 SQL 不会报错，只会永远匹配不到，
    于是「未兑现候补单」计数恒为 0 → 候补配额判断永远算成「还有额度」→
    官方拒单，而本地日志一切正常。

    现在改成 ``status = ?`` + ``_status_constant("standby_orders", "STANDBY_QUEUING")``
    —— 值是运行时从 engine 词表取的，storage 不持有副本。这条测试钉住
    「SQL 里不许再有字面量」与「活跃态只认 STANDBY_QUEUING」。
    """
    from ticket_helper.standby import engine as s

    db_src = _read("ticket_helper/storage/database.py")
    # 只看 SQL 字符串区域（含 standby_orders 的那些串）
    sql_text = "\n".join(re.findall(r'"([^"]*standby_orders[^"]*)"', db_src))
    # 具体形状：两条查询都必须是参数化的 `status = ?`
    assert "standby_orders WHERE task_id = ? AND status = ?" in sql_text, (
        f"standby_orders 的「活跃单」查询不再是参数化写法：{sql_text!r}"
    )
    assert "standby_orders WHERE status = ?" in sql_text, (
        f"standby_orders 的计数查询不再是参数化写法：{sql_text!r}"
    )

    # 一个状态字面量都不许留在 SQL 里
    leaked = [value for value in s.STANDBY_STATES if f"'{value}'" in sql_text]
    assert leaked == [], (
        f"存储层 SQL 里又出现了候补状态字面量：{leaked} —— "
        "请改用 _status_constant(\"standby_orders\", <常量名>)"
    )
    # 活跃态必须是「排队中」这一个；改语义时这里会红
    active = re.findall(r'_status_constant\(\s*"standby_orders"\s*,\s*"([A-Z_]+)"\s*\)', db_src)
    assert set(active) == {"STANDBY_QUEUING"}, (
        f"「未兑现候补单」的活跃态变了：{sorted(set(active))} —— 这直接影响候补配额判断"
    )


# =========================================================================== #
# 契约 6：两端导航一致
# =========================================================================== #
def test_desktop_and_web_navigation_are_identical():
    """★ 防的是：两端导航项 / 顺序漂移，换客户端就得重新找菜单。

    来源（真实体验问题）：桌面端把「限速 / 打码」塞在「通知设置」页里当第三个
    tab，网页端却是独立的「系统设置」页 —— 两端**页面清单不一致**，
    而界面上不会有任何报错，使用者只会觉得「怎么找不到」。

    比的是三样：**键**、**标题**、**顺序**。只比集合会让「菜单顺序悄悄变了」
    和「标题各写各的」一起漏过去。
    """
    from desktop_ui.main_window import PAGES

    html = _read("ticket_helper/web/static/index.html")
    pairs = re.findall(
        r'<button class="nav-item[^"]*"\s+data-page="([a-z_]+)">([^<]+)</button>', html
    )
    assert len(pairs) >= 8, f"没能从网页端解析出导航项（正则需要跟着改）：{pairs}"
    web_pages = [(key, title.strip()) for key, title in pairs]
    assert web_pages == [tuple(item) for item in PAGES], (
        f"两端导航不一致：\n  网页 {web_pages}\n  桌面 {[tuple(i) for i in PAGES]}"
    )
# =========================================================================== #
# 契约 5：CLI 降级路径的参数完整性
# =========================================================================== #
def test_web_fallback_args_satisfy_cmd_web_requirements():
    """「无参数启动 + 无 PySide6」降级到 Web 时，参数必须**完整**。

    来源是人的验收层不变量（``tests/acceptance/test_cli_contract.py``）：
    降级路径下 ``cmd_web`` 需要的属性必须都存在，且**不允许给 Namespace
    动态补属性来绕过**。原先直接复用顶层 args，缺 ``host`` →
    ``AttributeError: 'Namespace' object has no attribute 'host'``（已复现）。

    这里不走 subprocess（真实运行会常驻 Web 服务），只校验参数构造本身。
    """
    import argparse

    import main as main_mod

    top_level = argparse.Namespace(command=None, port=None)     # 无参数启动的形态
    web_args = main_mod._web_fallback_args(top_level)

    for attr in ("host", "port"):
        assert hasattr(web_args, attr), f"降级参数缺少 cmd_web 需要的属性：{attr}"

    # 顶层显式给的端口必须被带过去（否则用户会发现端口「不生效」）
    with_port = main_mod._web_fallback_args(argparse.Namespace(command=None, port=18123))
    assert with_port.port == 18123, "顶层 --port 在降级时丢失"
    assert with_port.host is None, "未指定 host 时应留给 env.py 决定，不该硬编码"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_web_date_guard_is_wired_on_both_buttons() -> None:
    """★ 网页端「乘车日期」的两道防线必须都在且都接着线。

    为什么用**静态**检查而不是跑 JS：pytest 里没有 JS 运行时。
    所以这里查的是「有没有接」——`min` 设了没有、两个按钮是不是都调了
    `dateProblem`；而**真正拦得住**的那一道在服务端
    （`TaskConfig.validate` → 保存直接 400，见
    `tests/test_web.py::test_saving_a_task_with_a_past_date_is_rejected`）。

    ⚠️ 静态检查证明不了行为，只能证明**接线没断**；所以它的价值全在
    「有人删了/改了其中一个调用点时会红」。
    """
    source = (ROOT / "ticket_helper" / "web" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function dateProblem(" in source, "日期校验函数没了"
    assert "/^\\d{4}-\\d{2}-\\d{2}$/" in source, "日期格式正则没了"
    assert "picker.min = isoDate(new Date());" in source, "日期控件没设 min（还能选到过去的日期）"

    add_calls = source.count("const problem = dateProblem(")
    assert add_calls == 2, f"「添加日期」与「连加 3 天」都要校验，实际只有 {add_calls} 处调用"


# =========================================================================== #
# 契约 7：品牌名与免责声明两端一致
# =========================================================================== #
def test_web_brand_matches_the_python_constant():
    """★ 防的是：改名只改了一半。

    桌面端与 CLI 读的是 ``ticket_helper.APP_NAME``；网页端是**静态 HTML**，
    import 不到 Python 常量 —— 于是「改名」变成两处手工同步。
    真实故障形态：桌面端已是新名字、网页端还是旧名字，两边都不报错，
    使用者看到两个不同的产品名，只会怀疑自己是不是开错了程序。

    这条把网页端文案钉在常量上：改常量而不改 HTML，立刻变红。
    """
    from ticket_helper import APP_AUDIENCE, APP_COPYRIGHT, APP_NAME, APP_WEBSITE

    html = _read("ticket_helper/web/static/index.html")
    assert f"<title>{APP_NAME}（{APP_AUDIENCE}）· 管理后台</title>" in html, "网页标题没跟着 APP_NAME 走"
    assert f"<h1>{APP_NAME}</h1>" in html, "登录页主标题没跟着 APP_NAME 走"
    assert APP_COPYRIGHT in html, f"网页端缺少版权声明「{APP_COPYRIGHT}」"
    assert APP_WEBSITE in html, f"网页端缺少官网链接「{APP_WEBSITE}」"


def test_old_brand_name_is_gone_from_every_surface():
    """★ 防的是：旧品牌名残留在「不是标题的地方」。

    最容易漏的恰恰是不在页面上、平时看不见的三处：托盘气泡、邮件 From 显示名、
    exe 文件属性（右键 → 属性 → 详细信息）。改完界面就以为改完了，
    直到收到一封署着旧名字的邮件、或在任务管理器里看到旧名字。
    """
    surfaces = [
        "main.py",
        "desktop_ui/app.py",
        "desktop_ui/main_window.py",
        "desktop_ui/tray.py",
        "ticket_helper/notify/gateway.py",
        "ticket_helper/notify/channels.py",
        "ticket_helper/web/static/index.html",
        "TicketHelper.spec",
    ]
    for name in surfaces:
        text = _read(name)
        for stale in ("12306 抢票助手", "12306抢票助手"):
            assert stale not in text, f"{name} 里还留着旧品牌名「{stale}」"


# =========================================================================== #
# 契约 8：教学演示版「乘车日期一律显示成明天」的两端一致性
# =========================================================================== #
def test_demo_switch_defaults_to_the_official_edition():
    """★ 演示开关的**默认值必须是关**。

    演示版靠打包脚本临时打开（``scripts/build_exe.py --demo``）。一旦有人把源码
    里的默认值改成 True 并提交，之后每一次正式打包都会悄悄变成演示版 ——
    而症状极轻：界面照常跑、只是日期都写着「明天」，很容易被当成「新加的功能」
    而不是「打包错了」。
    """
    from ticket_helper import edition

    assert edition.DEMO_MODE is False, "源码里的 DEMO_MODE 必须是 False（演示版由打包脚本临时打开）"
    assert edition.EDITION == "", "正式版不该带版本后缀"
    assert edition.display_train_date("2026-10-13") == "2026-10-13"
    assert edition.display_train_dates(["2026-10-13", "2026-10-14"]) == "2026-10-13 / 2026-10-14"


def test_demo_switch_rewrites_train_dates_only(monkeypatch):
    """★ 打开开关后：乘车日期一律「明天」，且**多个日期合并成一个**。

    为什么必须合并：任务支持「连加 3 天」，不合并就会渲染成「明天 / 明天 / 明天」，
    录播画面上一眼就是假的。

    空值**不许**被编造成「明天」—— 列表里空着，比显示一个并不存在的日期诚实。
    """
    from ticket_helper import edition

    monkeypatch.setattr(edition, "DEMO_MODE", True)

    assert edition.display_train_date("2026-10-13") == edition.DEMO_DATE_TEXT
    assert edition.display_train_dates(["2026-10-13", "2026-10-14", "2026-10-15"]) == edition.DEMO_DATE_TEXT
    assert edition.display_train_date("") == "", "空值不该被编造成「明天」"
    assert edition.display_train_dates([]) == "-", "空列表该落到 fallback 上"
    assert edition.display_train_dates(["2026-10-13"], separator=", ") == edition.DEMO_DATE_TEXT


def test_web_side_reads_the_switch_from_the_backend():
    """★ 网页端**不许**自己判断「是不是演示版」，必须由后端下发。

    网页是静态 HTML + 原生 JS，import 不到 Python 常量，所以「明天」这个词天然
    会有两份实现（Python 一份、JS 一份）。这里钉住两件事：

    1. 那个词两边**是同一个**（改一边不改另一边，立刻红）；
    2. 开关的来源是**唯一**的 —— 后端把 ``edition.DEMO_MODE`` 塞进登录响应，
       前端只消费它。否则「打包时是演示版、网页却按正式版渲染」这种只在一端
       出现的错位，根本没地方能发现。
    """
    from ticket_helper import edition

    js = _read("ticket_helper/web/static/app.js")
    api = _read("ticket_helper/web/api.py")

    assert f"const DEMO_DATE_TEXT = '{edition.DEMO_DATE_TEXT}';" in js, (
        "网页端的「明天」与 ticket_helper/edition.py 的 DEMO_DATE_TEXT 不是同一个词"
    )
    assert "let DEMO_MODE = false;" in js, "网页端的演示开关默认值必须是 false"
    assert '"demo_mode": edition.DEMO_MODE' in api, "后端没把演示开关下发给网页端"
    assert "setEdition(data.demo_mode)" in js, "登录成功后没把后端下发的开关接住"
    assert "setEdition(me && me.demo_mode)" in js, "已有令牌直接进入时没把开关接住"


def test_both_ends_render_train_dates_through_the_shared_helper():
    """★ 防「只改了本地、漏了网页」：两条渲染链都必须走同一个转换函数。

    这是**接线检查**（静态），证明不了行为 —— 但它能在有人新加一处日期渲染而
    忘了转换时变红，而这恰恰是这类改动最容易漏的地方。
    """
    js = _read("ticket_helper/web/static/app.js")
    assert "function demoDate(value)" in js and "function demoDates(" in js, "网页端的转换函数没了"
    # 订单记录、个人中心（待支付 / 历史订单）、余票看板（候选 + 全部车次）
    assert js.count("escapeHtml(demoDate(") >= 5, "网页端还有乘车日期没走 demoDate（订单/看板）"
    assert "escapeHtml(demoDates(t.dates))" in js, "拉车次结果里的日期没走 demoDates"
    assert "display: demoDate," in js, "任务配置里的日期标签没接演示版"

    desktop_hits = [
        ("desktop_ui/pages/tasks.py", 'display_train_dates(task.get("left_dates"))'),
        ("desktop_ui/pages/orders.py", 'display_train_date(order.get("depart_date"))'),
        ("desktop_ui/pages/board.py", 'display_train_date(item.get("train_date"))'),
        ("desktop_ui/train_picker.py", 'display_train_dates(train.get("dates"))'),
    ]
    for name, needle in desktop_hits:
        assert needle in _read(name), f"{name} 里的乘车日期没走转换函数"

    profile = _read("desktop_ui/profile_panel.py")
    assert profile.count('display_train_date(order.get("train_date"))') == 2, (
        "个人中心的「历史订单」与「待支付」两张表都要转换"
    )
    assert "edition.DEMO_DATE_TEXT" in _read("desktop_ui/token_field.py"), "任务配置里的日期标签没接演示版"


def test_packaging_script_switches_the_same_anchor():
    """★ 打包脚本改的那一行，就是 edition.py 里定义的那一行。

    ``build_exe.py`` 用**文本锚点**把 ``DEMO_MODE = False`` 改成 True —— 这个约定
    是隐式的：谁把那行换个写法（加类型注解、改名），打包就会在「找不到锚点」上
    失败。失败是好事，但得让人一眼看明白，所以把两端的字面量对齐着钉住。
    """
    source = _read("ticket_helper/edition.py")
    packer = _read("scripts/build_exe.py")

    assert 'DEMO_MODE_ANCHOR = "DEMO_MODE = False"' in packer, "打包脚本的锚点常量被改了"
    assert "DEMO_MODE = False\n" in source, "edition.py 里找不到打包脚本要改的那一行"
    assert '"TICKET_HELPER_BUILD_NAME"' in packer, "打包脚本没告诉 spec 产物名"
    assert 'DEMO_BUILD_NAME = "TicketHelper-Demo"' in packer, "演示版产物名没了"

