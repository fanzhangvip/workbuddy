"""扫码轮询请求体 —— **本项目写法（默认后端）**。

表单只放 ``uuid`` + ``appid`` + ``_json_att``：官方靠 **Cookie** 拿设备指纹，
真机上不带这两个字段也能拿到扫码状态（这条路径是实测跑通的）。

⚠️ 设备指纹那两个键**不是「忘了加」**，是刻意不加 —— 差异见
:mod:`ticket_helper.kernel.pjialin.qr_check`，由 `scripts/login_check_diff.py`
旁边的 `tests/test_kernel_qr.py` 与 `tests/test_qr_baseline.py` 钉住。

请求规格**全部引用协议层的唯一真源**（`inter/urls.py`），本文件不写协议字面量。
"""

from __future__ import annotations

from ticket_helper.inter import urls as inter_urls

from ..domains.qr_check import QrCheckSpec

#: 与注册表键一致的唯一名字
NAME = "self"


class SelfQrCheck:
    """本项目写法：``POST /passport/web/checkqr``，设备指纹只走 Cookie。"""

    name = NAME

    def spec(self) -> QrCheckSpec:
        return QrCheckSpec(
            name=NAME,
            path=inter_urls.CHECK_QR,
            form_keys=inter_urls.QR_CHECK_FORM_KEYS,
            constants=inter_urls.QR_CHECK_CONSTANTS,
        )
