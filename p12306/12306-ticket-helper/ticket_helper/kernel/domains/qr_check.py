"""扫码轮询请求体域（``checkqr`` 的表单**怎么构造**）。

两套实现的**端点完全一致**（``/passport/web/checkqr``），差异只在表单里放什么：

===============  ==============================================
后端             表单
===============  ==============================================
``self``         ``uuid, appid, _json_att``
``pjialin``      ``RAIL_DEVICEID, RAIL_EXPIRATION, uuid, appid``
===============  ==============================================

上游多带两个**设备指纹 Cookie**（`RAIL_DEVICEID` / `RAIL_EXPIRATION`）。
本项目不带也真机跑通了（扫码登录是目前唯一可靠的登录路径），
但上游显式放进表单属于「双保险」—— 差异**留档并做成可切**，默认不动。

⚠️ 这个域**只有请求体**，没有状态判定。原因：
``result_code`` 的四个取值（0 待扫 / 1 已扫 / 2 成功 / 3 过期）两边语义一致，
映射表在协议层的 `inter/parser.py` 里是唯一实现，
由 ``tests/test_qr_baseline.py`` 对着上游出处断言 —— 不需要再来一份。
这也是「**不抽用不上的东西**」的一次实践：端点相同、语义相同，
硬造一个域只会多一处将来要同步的地方。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class QrCheckSpec:
    """一次 ``checkqr`` 请求的规格（**全是数据**）。"""

    #: 后端名（必须与 `kernel/registry.py` 的注册键一致，由契约测试核对）
    name: str
    #: 站内路径
    path: str
    #: 表单里要放的键，**顺序即上报顺序**
    form_keys: tuple[str, ...]
    #: 固定值（不随调用变化的键），例如 ``appid``
    constants: Mapping[str, str] = field(default_factory=dict)

    def form(self, *, uuid: str, device_id: str, expiration: str) -> dict[str, str]:
        """把「这次调用拿到的值」组装成表单。

        **两套后端共用这一份**（`self` 用不到的键会被 ``form_keys`` 过滤掉），
        所以切换后端不可能顺手改掉组装逻辑。取值缺失一律当空串 ——
        官方对空值的反应是「状态查不出来」，比抛异常好定位。
        """
        values: dict[str, str] = {
            "uuid": uuid,
            "RAIL_DEVICEID": device_id,
            "RAIL_EXPIRATION": expiration,
            **self.constants,
        }
        return {key: str(values.get(key, "")) for key in self.form_keys}


@runtime_checkable
class QrCheckBuilder(Protocol):
    """扫码轮询请求体后端。两个后端都必须满足它。"""

    #: 后端名（必须与注册表键一致）
    name: str

    def spec(self) -> QrCheckSpec:
        """返回本次请求的规格。"""
        ...
