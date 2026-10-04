"""登录态探测 —— **本项目写法（默认后端）**。

请求规格（路径 / 方法 / 参数 / Referer）与判活字段**全部引用协议层的唯一真源**：

- ``inter/urls.py::CHECK_USER_PROBE`` —— 请求侧四个值；
- ``inter/parser.py::CHECK_USER_ALIVE_PATHS`` —— 响应侧判活字段候选。

这里**一个协议字面量都不许写**：本文件的全部内容就是「把两份常量组装成
:class:`LoginProbeSpec`」。若哪天有人把路径直接写在这里，
`tests/test_kernel_user.py::test_self_spec_equals_the_builtin_default` 会变红
—— 它断言「本后端产出的规格」与「接口层没被注入时自己组装的规格」**完全相等**，
所以任何一边改动而另一边没跟，都会立刻暴露。
"""

from __future__ import annotations

from ticket_helper.inter import parser as inter_parser
from ticket_helper.inter import urls as inter_urls

from ..domains.login_check import LoginProbeSpec

#: 与注册表键一致的唯一名字
NAME = "self"


class SelfLoginProbe:
    """本项目写法：``POST /otn/login/checkUser``，看 ``data.flag``。"""

    name = NAME

    def spec(self) -> LoginProbeSpec:
        return LoginProbeSpec(
            name=NAME,
            alive_paths=inter_parser.CHECK_USER_ALIVE_PATHS,
            **inter_urls.CHECK_USER_PROBE,
        )
