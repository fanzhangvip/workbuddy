"""版本形态：正式版 / 教学演示版 —— **全项目唯一来源**。

为什么需要单独一个模块
----------------------
录播课程要求界面能**长期复用**：课程今年录、明年还要放。一旦界面上写着
``乘车日期 2026-10-13`` 这种具体日期，回放时就成了「过期的截图」。
所以教学演示版把界面上**所有「乘车日期」**统一显示成「明天」——
相对表述，任何时间回放都成立。

因此这里只做一件事，且只在这一处做：

- **只改显示，不动数据**：任务里存的仍是真实日期（``left_dates`` 原样进出
  数据库与 12306 请求），抢票链路一行没改。改错这里的后果是「显示不对」，
  而不是「抢不到票」；
- **「订票时间 / 抢票时间」不替换**：那是真实发生过的时间，本来就该是历史
  事实（替换它只会让日志与订单对不上）。要换的只有「乘车日期」这一族。

``DEMO_MODE`` 是**编译期常量**
------------------------------
打包演示版时由 ``scripts/build_exe.py --demo`` 临时把下面那一行改成 ``True``，
打包结束再改回来。**刻意不读环境变量**：打包产物运行时不会带着构建机的环境
变量，写进代码才是可靠的（做成运行期开关只会多出「明明设了却没生效」的排查
成本）。

⚠️ 改 ``DEMO_MODE`` 那一行时请同步 ``scripts/build_exe.py`` 里的锚点。
"""

from __future__ import annotations

from collections.abc import Iterable

#: 是否教学演示版。**唯一开关** —— 不要在别处再判断一次。
DEMO_MODE = False

#: 演示版把「乘车日期」显示成的文字。网页端（app.js）有同名常量，
#: 两端一致性由 tests/test_contracts.py 的契约 8 守住。
DEMO_DATE_TEXT = "明天"

#: 版本后缀（正式版为空串）。窗口标题 / CLI banner / 网页标题用它。
EDITION = "教学演示版" if DEMO_MODE else ""


def display_train_date(value: object, *, fallback: str = "") -> str:
    """把**一个**乘车日期渲染成界面文字。

    正式版原样返回；演示版一律「明天」。空值返回 ``fallback`` ——
    列表里空着，比显示一个并不存在的「明天」诚实。
    """
    text = str(value or "").strip()
    if not text:
        return fallback
    return DEMO_DATE_TEXT if DEMO_MODE else text


def display_train_dates(
    values: Iterable[object] | None,
    *,
    separator: str = " / ",
    fallback: str = "-",
) -> str:
    """把**一组**乘车日期渲染成一行文字（任务卡片那种）。

    演示版把多个日期合并成**一个**「明天」—— 否则「连加 3 天」会显示成
    「明天 / 明天 / 明天」，一眼假。
    """
    items = [str(item).strip() for item in (values or []) if str(item).strip()]
    if not items:
        return fallback
    return DEMO_DATE_TEXT if DEMO_MODE else separator.join(items)
