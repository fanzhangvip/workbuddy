"""扫码轮询请求体 —— **上游 pjialin/py12306 写法**。

上游把两个**设备指纹 Cookie 也放进表单**（``user/job.py::qr_login``）::

    data = {
        'RAIL_DEVICEID': self.session.cookies.get('RAIL_DEVICEID'),
        'RAIL_EXPIRATION': self.session.cookies.get('RAIL_EXPIRATION'),
        'uuid': image_uuid,
        'appid': 'otn'
    }

字段名与 ``appid`` 取值都取自生成物 ``vendor/pjialin/ticket_facts.py``
（从上游原件逐字切出，含 sha256 出处），**不是抄进来的**。

⚠️ 上游读 Cookie 用的是 ``self.session.cookies.get(...)``；本项目通过
``HttpClient.cookie_of()`` 拿同样的值 —— 那是**同一份数据**（都是那次会话的
Cookie Jar），只是取法不同。取不到时是空串：上游也一样
（``cookies.get`` 缺省返回 ``None``，表单里就成了 ``"None"`` 字样 ——
本项目用空串，比让官方收到字符串 ``"None"`` 更干净）。
"""

from __future__ import annotations

from ..domains.qr_check import QrCheckSpec
from .shim import install

#: 与注册表键一致的唯一名字
NAME = "pjialin"


class PjialinQrCheck:
    """上游写法：表单里额外带 ``RAIL_DEVICEID`` / ``RAIL_EXPIRATION``。"""

    name = NAME

    def spec(self) -> QrCheckSpec:
        # 装垫片必须早于 import py12306（见 shim 的说明）
        install()
        import ticket_facts  # noqa: PLC0415  （生成物：位于 sys.path 上的 vendor/pjialin）

        return QrCheckSpec(
            name=NAME,
            path=ticket_facts.QR_CHECK_PATH,
            form_keys=ticket_facts.QR_CHECK_FIELDS,
            constants={"appid": ticket_facts.QR_APPID},
        )
