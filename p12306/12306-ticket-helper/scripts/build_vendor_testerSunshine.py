"""从上游原件**生成** ``vendor/testerSunshine/standby.py``（不要手抄上游代码）。

为什么用生成器而不是手抄
------------------------
需求方要求「上游已有的功能禁止自己实现，必须用上游的」。这句话要能被**验证**，
否则它只是一句声明。手抄代码无法验证——抄错一个字符，没人会发现。

生成器把这条约束变成机器事实：

- 上游原件（逐字）放在 ``reference/upstream/testerSunshine_standby/``；
- 本脚本把它们里的**载荷定义块**逐字切出来，拼进 ``vendor/``；
- ``--check`` 重新生成一遍并与磁盘上的比对，**不一致即失败**（退出码 1）。

于是「vendor 里的代码来自上游」这件事有了可复跑的证据，而不是靠信任。

用法
----
::

    python scripts/build_vendor_testerSunshine.py            # 生成/更新
    python scripts/build_vendor_testerSunshine.py --check    # 校验是否已过期

关联
----
- 设计：``docs/内核门面架构设计.md``
- 能力清点与决策：``docs/pjialin内核替换能力清点与方案v2.md`` §4.1（决策 B）
- 上游出处：``reference/VENDOR.md``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import textwrap
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REF = BASE / "reference" / "upstream" / "testerSunshine_standby"
OUT = BASE / "vendor" / "testerSunshine" / "standby.py"
PROVENANCE = BASE / "vendor" / "testerSunshine" / "PROVENANCE.json"

#: 上游出处（与 reference/VENDOR.md 一致）
UPSTREAM_REPO = "testerSunshine/12306"
UPSTREAM_COMMIT = "a495af88346a0d794493c6030f6a6207debb5824"
UPSTREAM_COMMIT_DATE = "2021-01-04"
UPSTREAM_LICENSE = "MIT"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(name: str) -> str:
    return (REF / name).read_text(encoding="utf-8", newline="")


def cut_ticker_config() -> str:
    """切 ``TickerConfig.py`` 里的席别常量（verbatim）。"""
    text = _read("TickerConfig.py")
    start = text.index("PASSENGER_TICKER_STR = {")
    end = text.index("}", start) + 1
    ticker = text[start:end]

    line_start = text.index("SET_TYPE = [")
    line_end = text.index("\n", line_start)
    set_type = text[line_start:line_end].rstrip("\r")
    return f"{ticker}\r\n\r\n{set_type}"


def cut_confirm_hb() -> str:
    """切 ``inter/ConfirmHB.py`` 的类定义（verbatim）。

    **切到 ``sendChechFace`` 之前为止** —— 那个方法会级联 import
    ``inter.GetQueueCount`` → ``verify/`` → **tensorflow / keras / cv2**，
    在 Python 3.13 上装不上；而且它是**传输层**（发请求），
    本项目的传输层有自己的风控识别，本来就不换（见架构设计 §2）。
    """
    text = _read("ConfirmHB.py")
    start = text.index("class confirmHB:")
    end = text.index("    def sendChechFace", start)
    return text[start:end].replace("\r\n", "\n").rstrip("\n")


def cut_passenger_loop() -> str:
    """切 ``inter/GetPassengerDTOs.py`` 候补分支的拼串循环。

    **只去掉行首多余的缩进**（上游该循环嵌得比函数体深一层，
    原样搬进函数体会 ``IndentationError``）；**字符本身一个都没动**。
    """
    text = _read("GetPassengerDTOs.py").replace("\r\n", "\n")
    start = text.index("            for user in user_info:")
    end = text.index("\n", text.index("allEncStr", start))
    return textwrap.dedent(text[start:end])


def build() -> str:
    ticker = cut_ticker_config()
    confirm = cut_confirm_hb()
    loop = cut_passenger_loop()

    header = f'''# -*- coding: utf-8 -*-
"""候补请求体 —— **上游 {UPSTREAM_REPO} 的代码**。

⚠️ 本文件由 ``scripts/build_vendor_testerSunshine.py`` 生成，**不要手改**；
要改先改生成器（或上游原件），再重新生成。

出处
----
======== ==========================================================
仓库     {UPSTREAM_REPO}
commit   {UPSTREAM_COMMIT}（{UPSTREAM_COMMIT_DATE}）
许可证   {UPSTREAM_LICENSE}（Copyright (c) 2017），全文见同目录 ``LICENSE``
原件     ``reference/upstream/testerSunshine_standby/``（逐字，只读）
校验     ``python scripts/build_vendor_testerSunshine.py --check``
======== ==========================================================

相对上游的改动（**只有这些**，逐条对应 `PROVENANCE.json`）
------------------------------------------------------------
1. 删除 ``ConfirmHB.sendChechFace``：它级联 import ``inter.GetQueueCount`` →
   ``verify/`` → **tensorflow/keras/cv2**（Python 3.13 装不上），
   且它是**传输层**，本项目传输层不换（见 ``docs/内核门面架构设计.md`` §2）。
2. 新增 ``_TickerConfig`` 垫片：上游用模块级全局 ``TickerConfig``；
   这里用同名容器承载**同一组常量**（常量本身逐字取自 ``TickerConfig.py``）。
3. 新增 :func:`build_passenger_info`：把上游那个 ``for`` 循环原样嵌进去执行；
   **循环体逐字未改**，只在循环前加了参数校验（安全底线，与用哪套协议无关）。

**载荷定义本身（字段名、字段顺序、拼接格式）一个字都没有改。**
这正是「必须用上游代码」这条要求可被验证的部分。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Mapping, Sequence

from ticket_helper.errors import InterError

#: 上游仓库（出处；完整溯源见同目录 ``PROVENANCE.json``）
UPSTREAM_REPO = "{UPSTREAM_REPO}"

# ==================== 上游原文（verbatim，禁止手改） ====================
# 出处: TickerConfig.py
{ticker}


# 出处: inter/ConfirmHB.py（已删除 sendChechFace，见模块 docstring 改动 1）
{confirm}
# ==================== 上游原文结束 ====================
'''

    tail = '''

class _TickerConfig:
    """垫片：上游的模块级全局配置容器（改动 2）。

    只承载 :meth:`confirmHB.data_apr` 真正用到的那两个常量，
    值逐字取自上游 ``TickerConfig.py``（见上面的 verbatim 块）。

    ⚠️ 注意上游的语义：``SET_TYPE`` 存的是**席别名**（``'二等座'``），
    ``PASSENGER_TICKER_STR`` 是「席别名 → 席别码」的表。
    所以传席别码（``'O'``）进来会得到 ``None``，拼出 ``...,None#`` —— 这是静默错误，
    :func:`confirm_hb_params` 会在调用前拦住它。
    """

    PASSENGER_TICKER_STR = PASSENGER_TICKER_STR
    SET_TYPE = SET_TYPE


TickerConfig = _TickerConfig

#: 上游把「当前席别」放在**进程级全局**里（它是单任务脚本）。
#: 本项目一个进程跑多个任务，改写全局必须串行，否则 A 任务的席别会串到 B 任务的请求里。
_SEAT_LOCK = threading.Lock()


class _SessionLike:
    """垫片：只为满足 ``confirmHB.__init__`` 的 ``session.passengerTicketStrByAfterLate``。

    上游靠 session 对象在链路上传递已拼好的乘客串；
    本项目门面**只接原始值**，所以这里用一个最小对象承接。
    """

    def __init__(self, passenger_info: str) -> None:
        self.passengerTicketStrByAfterLate = passenger_info


def build_passenger_info(passengers: Sequence[Mapping[str, Any]]) -> str:
    """候补的 ``passengerInfo``（上游 ``GetPassengerDTOs.py`` 候补分支，改动 3）。

    循环体逐字来自上游：
    '''
    tail += f'''
{chr(10).join("    " + ln for ln in loop.split(chr(10)))}
    '''
    tail += '''
    上游不校验参数就拼串；这里**在循环之前**补校验（安全底线）：
    缺姓名或缺 ``allEncStr`` 时官方必然拒，提前报出比让官方回一句
    「系统忙，请稍后重试」要好。
    """
    for p in passengers:
        if not str(p.get("passenger_name", "")).strip():
            raise InterError("候补乘车人缺少姓名，无法提交")
        enc = str(p.get("allEncStr") or p.get("all_enc_str") or "").strip()
        if not enc:
            raise InterError(
                f"乘车人「{str(p.get('passenger_name', '')).strip()}」缺少官方加密串"
                "（allEncStr），无法用于候补；请到 12306 的个人中心确认该乘车人"
                "状态正常后才好重试"
            )

    tickers: list[str] = []
    user_info = list(passengers)
'''
    tail += f'''
{chr(10).join("    " + ln for ln in loop.split(chr(10)))}
'''
    tail += '''
    return "".join(tickers)


def confirm_hb_params(
    *,
    passenger_info: str,
    jz_param: str,
    train_no: str,
    seat_name: str,
) -> dict[str, Any]:
    """调**上游** :meth:`confirmHB.data_apr` 产出 ``confirmHB`` 的表单。

    ⚠️ 这里没有任何「等价改写」：直接实例化上游的类并调用它的方法。

    传 ``seat_name``（**席别名**，如 ``'二等座'``）而不是 ``seat_code``：
    上游 ``data_apr`` 走的是 ``PASSENGER_TICKER_STR.get(SET_TYPE[0])``，
    它要的就是席别名。传码进去会得到 ``None`` 并拼出 ``...,None#`` ——
    那种错**不会报错**，只会让官方回一句「系统忙，请稍后重试」，
    所以这里显式拦下（见下方 ``InterError``）。
    """
    name = str(seat_name or "").strip()
    if not name:
        raise InterError("候补缺少席别，无法向上游内核取席别码")
    if name not in PASSENGER_TICKER_STR:
        raise InterError(
            f"席别「{name}」不在上游 {UPSTREAM_REPO} 的席别名表里"
            f"（{', '.join(PASSENGER_TICKER_STR)}）；"
            "不能猜一个码发出去 —— 官方只会回「系统忙，请稍后重试」"
        )

    with _SEAT_LOCK:
        TickerConfig.SET_TYPE = [name]
        order = confirmHB(
            secretList="",
            session=_SessionLike(passenger_info),
            tickerNo=train_no,
            jzdhDate=jz_param,
        )
        result = order.data_apr()
    # 上游返回 OrderedDict；转成普通 dict 交给传输层，顺带钉住键顺序
    return dict(result)
'''
    return header + tail


def write_files() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(), encoding="utf-8", newline="")

    sources = {}
    for name in ("ConfirmHB.py", "GetPassengerDTOs.py", "TickerConfig.py"):
        sources[name] = {"sha256": sha256(REF / name)}
    PROVENANCE.write_text(
        json.dumps(
            {
                "_comment": "vendor 溯源清单。由 scripts/build_vendor_testerSunshine.py 生成，不要手改。",
                "upstream": {
                    "repo": UPSTREAM_REPO,
                    "commit": UPSTREAM_COMMIT,
                    "commit_date": UPSTREAM_COMMIT_DATE,
                    "license": UPSTREAM_LICENSE,
                },
                "sources": sources,
                "output": {
                    "file": "vendor/testerSunshine/standby.py",
                    "sha256": sha256(OUT),
                },
                "modifications": [
                    {
                        "where": "ConfirmHB.py",
                        "what": "删除了 sendChechFace 方法",
                        "why": (
                            "它级联 import inter.GetQueueCount → verify/ → tensorflow/keras/cv2，"
                            "Python 3.13 装不上；且它属于传输层，本项目传输层不换"
                        ),
                    },
                    {
                        "where": "ConfirmHB.py",
                        "what": "新增 _TickerConfig 垫片承载 PASSENGER_TICKER_STR / SET_TYPE",
                        "why": "上游用 157 行的全局 config 模块；这里只搬 data_apr 真正用到的两个常量，值逐字取自上游",
                    },
                    {
                        "where": "GetPassengerDTOs.py",
                        "what": "把候补分支的 for 循环原样移入 build_passenger_info，去掉了行首多余缩进（上游嵌套更深，原样搬进函数体会 IndentationError），并在循环前补参数校验",
                        "why": "循环体内的字符逐字未改；补校验是安全底线（缺 allEncStr 官方必拒）",
                    },
                ],
                "unchanged": [
                    "ConfirmHB.data_apr 的方法体（字段名/顺序/拼接格式）逐字未改",
                    "GetPassengerDTOs 候补分支的 f-string 逐字未改",
                    "TickerConfig.PASSENGER_TICKER_STR / SET_TYPE 的值逐字未改",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成/校验 vendor/testerSunshine/standby.py")
    parser.add_argument("--check", action="store_true", help="只校验，不写入；过期则退出码 1")
    args = parser.parse_args()

    expected = build()
    if args.check:
        if not OUT.exists():
            print(f"✘ {OUT.relative_to(BASE)} 不存在，请先运行生成器")
            return 1
        actual = OUT.read_text(encoding="utf-8", newline="")
        if actual != expected:
            print("✘ vendor 文件与上游原件不一致 —— 说明有人手改了它，或上游原件被换过")
            print("  修法：python scripts/build_vendor_testerSunshine.py（然后 review diff）")
            return 1
        print(f"✔ {OUT.relative_to(BASE)} 与上游原件一致")
        return 0

    write_files()
    print(f"✔ 已生成 {OUT.relative_to(BASE)}")
    print(f"✔ 已生成 {PROVENANCE.relative_to(BASE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
