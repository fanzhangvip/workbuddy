"""登录态探测 —— **上游 pjialin/py12306 写法**。

上游用的是 ``GET /otn/login/conf``，判 ``data.is_login == 'Y'``
（``user/job.py::check_user_is_login``）。两处都取自生成物
``vendor/pjialin/ticket_facts.py``（从上游原件逐字切出，含 sha256 出处），
**不是抄进来的**。

⚠️ **上游那条请求不带 Referer**。本项目在真机上验证过：没逛过页面的会话
调这类接口会被 WAF 拒（回 HTML 错误页而不是 4xx）。这里**如实保留上游的形态** ——
不替它补 Referer。

理由：本域的价值是「把两套实现的差异摆出来」而不是「让上游跑得更顺」。
替它补上 Referer，差异就被我们抹平了，差分表会显示一切都好，
而真机上把默认切过去时才会发现别的坑。差异要留在**能被看见**的地方。

（按决策 C3「请求用上游、判定保留」，这里只管请求；判定逻辑在
``domains/login_check.py::alive_of``，两套后端共用一份。）
"""

from __future__ import annotations

from ..domains.login_check import LoginProbeSpec
from .shim import install

#: 与注册表键一致的唯一名字
NAME = "pjialin"


class PjialinLoginProbe:
    """上游写法：``GET /otn/login/conf``，看 ``data.is_login``。"""

    name = NAME

    def spec(self) -> LoginProbeSpec:
        # 装垫片必须早于 import py12306（见 shim 的说明）
        install()
        import ticket_facts  # noqa: PLC0415  （生成物：位于 sys.path 上的 vendor/pjialin）

        return LoginProbeSpec(
            name=NAME,
            path=ticket_facts.LOGIN_CHECK_PATH,
            method="get",
            data={},
            referer="",  # 上游没带 —— 见模块 docstring
            alive_paths=(ticket_facts.LOGIN_ALIVE_FIELD,),
            # 上游是 `== 'Y'`：必须显式给取值集合，
            # 否则 `is_login="N"` 会被 Python 真值判成「活着」（非空字符串）。
            alive_values=ticket_facts.LOGIN_ALIVE_VALUES,
        )
