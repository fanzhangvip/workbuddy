"""品牌与版权信息 —— 界面各处引用的唯一来源。

改产品名 / 官网 / 版权声明只动这一个文件。以前是每处各写一份字面量，
改一次名要满仓库找（还漏过），现在集中在这里。
"""

from __future__ import annotations

#: 产品名
APP_TITLE = "智泊AI视频播放器"

#: 使用声明
USAGE_NOTICE = "仅供学习，严禁商用"

#: 版权声明
COPYRIGHT = "智泊AI版权所有，仅限智泊AI学员使用"

#: 官网
WEBSITE = "https://www.zhipoai.com/"
WEBSITE_LABEL = "www.zhipoai.com"

#: 状态栏常驻短版（比 COPYRIGHT 短一点，窄窗口也放得下）
STATUS_NOTICE = "智泊AI版权所有 · 仅限智泊AI学员使用"

#: 窗口标题：版权 + 许可 + 官网，完整一行
WINDOW_TITLE = f"{COPYRIGHT}，{WEBSITE}"


def notice_line() -> str:
    """一行式声明（写日志用）。"""
    return f"{COPYRIGHT} · {WEBSITE}"
