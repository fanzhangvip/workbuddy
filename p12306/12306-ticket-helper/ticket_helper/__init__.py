"""智泊AI抢票助手 · 核心包。

智泊AI版权所有，仅限智泊AI学员使用 · https://www.zhipoai.com/
仅供学习使用，严禁商用；请勿用于代抢、黄牛倒票等任何营利性用途。
"""

# 版本形态（正式版 / 教学演示版）与「乘车日期」的显示规则都在 edition.py ——
# 那里是唯一来源；这里只把它拼成对外显示的完整名字。
from ticket_helper.edition import DEMO_DATE_TEXT, DEMO_MODE, EDITION

__version__ = "1.0.0"

#: 产品显示名 —— 桌面窗口标题 / 托盘提示 / Web 页面 / CLI banner 的**唯一来源**。
#: 改名只改这一处。网页端是静态 HTML，import 不到这个常量，
#: 靠 tests/test_contracts.py 的品牌一致性契约测试守住两端不漂移。
APP_NAME = "智泊AI抢票助手"

#: 使用范围 —— 出现在窗口标题 / 网页 title 这类「一眼就能看到」的位置。
APP_AUDIENCE = "仅限智泊AI学员使用"

#: 版权与使用范围声明 —— 桌面状态栏 / Web 页脚 / CLI banner / exe 文件属性同源。
#: 必须常驻可见，不允许只写在 README 里。
APP_COPYRIGHT = f"智泊AI版权所有，{APP_AUDIENCE}"

#: 官方网址。界面上「去哪学、去哪反馈」都指这里，也是版权声明的一部分。
APP_WEBSITE = "https://www.zhipoai.com/"

#: 界面上常驻显示的完整声明（版权 + 网址），一行放得下。
APP_TAGLINE = f"{APP_COPYRIGHT} · {APP_WEBSITE}"

#: 合规底线。不抢界面位置，写在 CLI banner 与 README 里。
APP_USAGE_NOTICE = "仅供学习使用，严禁商用"

#: 对外显示的**完整**名字：正式版就是 ``APP_NAME``；教学演示版带「（教学演示版）」后缀。
#: ``APP_NAME`` 本身刻意不变 —— 契约测试拿它钉网页端的静态 HTML，
#: 「这是哪个版本」属于版本形态，不该污染产品名。
APP_DISPLAY_NAME = f"{APP_NAME}（{EDITION}）" if EDITION else APP_NAME

__all__ = [
    "__version__",
    "APP_NAME",
    "APP_AUDIENCE",
    "APP_COPYRIGHT",
    "APP_WEBSITE",
    "APP_TAGLINE",
    "APP_USAGE_NOTICE",
    "APP_DISPLAY_NAME",
    "DEMO_MODE",
    "DEMO_DATE_TEXT",
]
