"""生成并校验 ``vendor/pjialin/`` 的溯源清单与合规文件（Apache-2.0）。

为什么要有这个脚本
------------------
需求方要求「**必须使用 `pjialin/py12306` 的源码，它有的功能禁止自己写**」。
这句话要能被**验证**，否则只是一句声明。手抄无法验证 —— 抄错一个字符没人会发现。

本脚本把这条约束变成机器事实：

- vendor 目录与上游**锁定 commit**（`52ccae8b`）逐文件 sha256 对应；
- **没有未经登记的文件**（多一个就报错）；
- **没有未登记的改动**（改一个字节就报错）；
- 确实要改的文件（如去掉被禁的 pjialin 托管域）必须登记在
  :data:`KNOWN_MODIFICATIONS`，并写清「改了哪几行、为什么」。

用法::

    python scripts/build_vendor_pjialin.py --init     # 首次建清单（上游刚拉下来时）
    python scripts/build_vendor_pjialin.py --check    # 校验（CI / 测试用），不一致退出码 1

合规（Apache-2.0 §4）::

    LICENSE   随附上游许可全文（vendor 目录里已有，本脚本校验它在）
    NOTICE    生成，声明本项目使用了 py12306
    修改说明  写入 vendor/pjialin/VENDOR.md（§4(b) 要求「carry prominent notices
              stating that You changed the files」）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
VENDOR = BASE / "vendor" / "pjialin"
PROVENANCE = VENDOR / "PROVENANCE.json"
NOTICE = VENDOR / "NOTICE"
VENDOR_DOC = VENDOR / "VENDOR.md"

UPSTREAM_REPO = "pjialin/py12306"
UPSTREAM_COMMIT = "52ccae8b206b828624f185bf8178e1057c6b5fb9"
UPSTREAM_COMMIT_DATE = "2026-06-16"
UPSTREAM_LICENSE = "Apache-2.0"
UPSTREAM_URL = f"https://github.com/{UPSTREAM_REPO}"
FETCH_URL = f"https://codeload.github.com/{UPSTREAM_REPO}/tar.gz/{UPSTREAM_COMMIT}"

#: 刻意不 vendor 的东西（都不是源码）：(路径, 原因)
EXCLUDED: list[tuple[str, str]] = [
    (".github/", "GitHub 的 issue / PR 模板，与源码无关"),
]

#: 本脚本自己不作为「上游文件」登记（它是我们写的）
SELF_FILES = {"PROVENANCE.json", "NOTICE", "VENDOR.md"}

#: 由本脚本**生成**的上游投影文件（内容全部来自上游原件，逐字切出）
#:
#: 为什么要生成而不是 import 上游：`py12306.query.job` 一 import 就拉起
#: `order` / `user` / `helpers.qrcode`（→ `png` / `pyppeteer`）一大片，
#: 而我们要的只是 10 个整数常量与一条正则。生成的这份**有逐行出处 + 源文件 sha256**，
#: 比手抄可验证得多（手抄错一个下标没人会发现）。
GENERATED_FILES = {"ticket_facts.py"}

#: 生成物里每个片段的上游出处（供 V5 参数级基线引用）
FACT_SOURCES = {
    "index": ("py12306/query/job.py", "INDEX_* 字段下标常量"),
    "route_regex": ("py12306/query/query.py", "从余票页 HTML 读查询路径的正则"),
    "route_url": ("py12306/helpers/api.py", "余票查询的 URL 模板（含 {type} 占位）"),
    "user_endpoints": ("py12306/helpers/api.py", "用户域端点路径（登录态 / 用户信息 / 乘车人 / 扫码）"),
    "user_fields": ("py12306/user/job.py", "上游读响应用的点号字段路径"),
    "qr_check": ("py12306/user/job.py", "扫码轮询的请求字段名与 result_code 分支值"),
    "order_endpoints": ("py12306/helpers/api.py", "下单六步的端点路径"),
    "order_forms": ("py12306/order/order.py", "下单五步的表单字段名与固定取值"),
    "init_dc_form": ("py12306/user/job.py", "initDc 表单与 token/滑块标记的取法"),
    "order_slide": ("py12306/order/order.py", "滑块风控的表单附加字段与 scene 取值"),
    "pacing_params": ("py12306/config.py", "查询节奏与请求超时的默认值"),
    "pacing_funcs": ("py12306/helpers/func.py", "数字→间隔区间、区间→随机等待的两个函数"),
}

#: **已经改过的上游文件**：文件 → (改了哪几行, 为什么)
#: 结构：{"py12306/helpers/api.py": {"what": "...", "why": "..."}}
#:
#: 登记规则：先改文件，再登记，最后 `--init`。三者顺序错了 `--check` 会失败（这是故意的）。
KNOWN_MODIFICATIONS: dict[str, dict[str, str]] = {
    "py12306/helpers/api.py": {
        "what": (
            "第 53–58 行区域：把 `API_GET_BROWSER_DEVICE_ID` 从 "
            "`https://12306-rail-id-v2.pjialin.com/` 改回上游自己注释掉的官方地址 "
            "`BASE_URL_OF_12306 + '/otn/HttpZF/logdevice'`；并删除了指向 "
            "`https://12306-ocr.pjialin.com/check/` 的 `API_FREE_CODE_QCR_API` 常量。"
        ),
        "why": (
            "需求方明确禁止请求 pjialin 托管域（'如果要就把源码拿过来自己弄'）。"
            "改回官方地址后，query.py:130 / user/job.py:331 的 "
            "`if 'pjialin' not in API_GET_BROWSER_DEVICE_ID: return self.request_device_id2()` "
            "会判为真，自动走上游自己实现的直连官方路径 —— 不需要自建服务。"
            "打码则由 'free' 改为 'user' 模式（本机服务），见 OCR.py 的改动。"
        ),
    },
    "py12306/helpers/OCR.py": {
        "what": (
            "三处：① 删掉 `from py12306.helpers.api import API_FREE_CODE_QCR_API`；"
            "② `get_img_position` 的判定去掉 `'free'`，只留 `'user'`；"
            "③ `get_image_by_free_site` 里删掉 `if ...=='free'` 分支，"
            "只保留 POST 到 `Config().API_USER_CODE_QCR_API`。"
        ),
        "why": (
            "`free` 分支的唯一出口就是被禁的 `*.pjialin.com`。"
            "**坐标换算 `get_image_position_by_offset` 一个字未改** —— "
            "它是本次要复用的上游能力（格子编号 → x,y）。"
        ),
    },
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def upstream_files() -> list[Path]:
    """vendor 目录里属于「上游原件」的文件（排除我们自己生成的那几个）。"""
    out = []
    for p in sorted(VENDOR.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(VENDOR).as_posix()
        if rel in SELF_FILES or rel in GENERATED_FILES:
            continue
        # __pycache__ 之类的运行期垃圾不进清单
        if "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        out.append(p)
    return out


# --------------------------------------------------------------------------- #
# 生成「协议事实」：从上游原件逐字切出，不手抄
# --------------------------------------------------------------------------- #
#: 上游原件里的字段下标行，形如 ``    INDEX_TICKET_NUM = 11``
_INDEX_LINE = re.compile(r"^\s*(INDEX_[A-Z_]+)\s*=\s*(\d+)\s*(?:#.*)?$")
#: 上游读取查询路径的那一行（整行取出来，不重写）
_ROUTE_LINE_MARK = "CLeftTicketUrl"

#: 用户域要切的上游端点常量名。**只切这几个**，不是整张表 ——
#: 整表会带上大量下单/候补端点，那些不属于本批（写域）。
#: `API_USER_LOGIN_CHECK` 是本次的**差异点**：它指向 `/otn/login/conf`，
#: 而本项目登录态检测用的是 `/otn/login/checkUser`。
USER_ENDPOINTS = (
    "API_USER_LOGIN_CHECK",
    "API_USER_INFO",
    "API_USER_PASSENGERS",
    "API_AUTH_QRCODE_BASE64_DOWNLOAD",
    "API_AUTH_QRCODE_CHECK",
)

#: 上游端点定义有三类写法，统一取其中的 ``BASE_URL_OF_12306 + '<path>'`` 片段：
#:   API_X = BASE_URL_OF_12306 + '/path'
#:   API_X = { 'url': BASE_URL_OF_12306 + '/path' }        （跨行）
#:   API_X = { 'url': BASE_URL_OF_12306 + '/a?x={y}' }     （含占位）
_ENDPOINT_PATH = re.compile(r"BASE_URL_OF_12306\s*\+\s*'([^']+)'")
#: 上游常量的定义行（用于按名字定位，再在其后几行里找路径）
_ENDPOINT_DEF = re.compile(r"^(API_[A-Z0-9_]+)\s*=")

#: 上游读响应字段用的是**点号路径字符串**，形如
#:   result.get('data.is_login', False)
#:   result.get('data.normal_passengers')
#:   result.get('data.userDTO.loginUserDTO')
#: 「数据嵌在哪一层」本身就是协议事实。
_FIELD_PATH = re.compile(r"\.get\(\s*'((?:data|userDTO)[.\w]*)'")
#: 上游的函数定义行（用来把字段路径按「读它的函数」归类）
_FUNC_DEF = re.compile(r"^\s*def\s+([a-z_]+)\s*\(")


def _read_upstream(rel: str) -> str:
    return (VENDOR / rel).read_text(encoding="utf-8", errors="replace")


def cut_index_constants() -> dict[str, int]:
    """切出 ``py12306/query/job.py`` 里的 ``INDEX_*`` 常量（逐字，不改名）。"""
    out: dict[str, int] = {}
    for line in _read_upstream(FACT_SOURCES["index"][0]).splitlines():
        m = _INDEX_LINE.match(line)
        if m:
            out[m.group(1)] = int(m.group(2))
    if not out:
        raise SystemExit(f"✘ 没能从 {FACT_SOURCES['index'][0]} 切出 INDEX_* 常量（上游结构变了？）")
    return out


def cut_route_regex() -> str:
    """切出上游从余票页 HTML 取查询路径的**正则源码文本**（逐字）。"""
    for line in _read_upstream(FACT_SOURCES["route_regex"][0]).splitlines():
        if _ROUTE_LINE_MARK in line and "re.search" in line:
            start = line.index("re.search(") + len("re.search(")
            end = line.index(", response.text)")
            return line[start:end].strip()
    raise SystemExit(
        f"✘ 没能从 {FACT_SOURCES['route_regex'][0]} 切出路径正则（上游结构变了？）"
    )


def cut_route_url_template() -> str:
    """切出上游余票查询的 URL 模板（含 ``{type}`` 占位），**去掉 BASE_URL 前缀**。

    用途：把「页面给的路径片段」还原成**上游自己会请求的那种路径**，
    而不是我们另发明一种拼接方式。这解决了两个后端返回**形态不同**的问题
    （上游给 ``leftTicket/queryO``，本项目候选列表给 ``/otn/leftTicket/queryZ``）。
    """
    text = _read_upstream(FACT_SOURCES["route_url"][0])
    marker = '"url": BASE_URL_OF_12306 + '
    for line in text.splitlines():
        if marker in line:
            value = line.split(marker, 1)[1].strip().rstrip(",")
            if not (value.startswith('"') and value.endswith('"')):
                raise SystemExit(f"✘ 余票 URL 模板不是简单字符串字面量: {value}")
            return value[1:-1]
    raise SystemExit(f"✘ 没能从 {FACT_SOURCES['route_url'][0]} 切出余票 URL 模板")


def _cut_named_endpoints(names: tuple[str, ...], where: str) -> dict[str, str]:
    """从 ``where`` 里切出指定常量名对应的**路径**（去掉 ``BASE_URL_OF_12306`` 前缀）。

    只取路径：基址是部署细节，路径才是协议事实。
    找不到任何一个都报错退出 —— 上游结构变了宁可失败，也不要静默少一项
    （少一项的表现是「某个步骤没被对照」，离线完全看不出来）。
    """
    lines = _read_upstream(where).splitlines()
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        m = _ENDPOINT_DEF.match(line)
        if not m or m.group(1) not in names:
            continue
        # 字典写法会把 'url' 放在下面几行，所以往后多看几行
        for probe in lines[i : i + 5]:
            pm = _ENDPOINT_PATH.search(probe)
            if pm:
                out[m.group(1)] = pm.group(1)
                break
    missing = [name for name in names if name not in out]
    if missing:
        raise SystemExit(
            f"✘ 没能从 {where} 切出这些端点：{missing}（上游改名或改结构了？）"
        )
    return out


def cut_user_endpoints() -> dict[str, str]:
    """切出用户域端点**路径**（逐字，去掉 ``BASE_URL_OF_12306`` 前缀）。

    只取路径：基址是部署细节，路径才是协议事实。
    找不到任何一个都要报错退出 —— 说明上游结构变了，宁可失败也不要静默少一项。
    """
    return _cut_named_endpoints(USER_ENDPOINTS, FACT_SOURCES["user_endpoints"][0])


def cut_user_response_fields() -> dict[str, str]:
    """切出上游读响应用的点号字段路径，按**读它的函数**归类。

    归类而不是拉平，是因为「哪个函数从哪读」才是可判定的对照关系：
    我们的 `check_user` 对应上游 `check_user_is_login`、`get_user_info` 对应
    `get_user_info`…… 拉平后就分不清谁该对上谁了。
    """
    lines = _read_upstream(FACT_SOURCES["user_fields"][0]).splitlines()
    out: dict[str, str] = {}
    func = ""
    for line in lines:
        fm = _FUNC_DEF.match(line)
        if fm:
            func = fm.group(1)
            continue
        for path in _FIELD_PATH.findall(line):
            if not path.startswith("data"):
                continue
            # 每个函数取**第一条** data.* 路径：那就是它判定的主字段
            out.setdefault(func, path)
    if not out:
        raise SystemExit(
            f"✘ 没能从 {FACT_SOURCES['user_fields'][0]} 切出响应字段路径（上游结构变了？）"
        )
    return out


#: 下单六步：上游 ``helpers/api.py`` 的常量名 → 本项目步骤键。
#: 步骤键同时是 `kernel/domains/order_submit.py` 里 ``STEP_*`` 的取值。
#: **顺序就是下单顺序** —— 顺序错了官方会在中途回「系统忙」，离线看不出来。
ORDER_ENDPOINT_STEPS: dict[str, str] = {
    "API_SUBMIT_ORDER_REQUEST": "submit",
    "API_INITDC_URL": "init_dc",
    "API_CHECK_ORDER_INFO": "check_order_info",
    "API_GET_QUEUE_COUNT": "get_queue_count",
    "API_CONFIRM_SINGLE_FOR_QUEUE": "confirm_single_for_queue",
    "API_QUERY_ORDER_WAIT_TIME": "query_wait_time",
}

#: 上游 ``order/order.py`` 里承载表单的方法名 → 步骤键。
#: ⚠️ initDc 不在这里 —— 它的表单在 ``user/job.py::request_init_dc_page``。
ORDER_FORM_METHODS: dict[str, str] = {
    "submit_order_request": "submit",
    "check_order_info": "check_order_info",
    "get_queue_count": "get_queue_count",
    "confirm_single_for_queue": "confirm_single_for_queue",
    "query_order_wait_time": "query_wait_time",
}

#: 表单字典开始的那一行（``        data = {``）
_DICT_LINE = re.compile(r"^\s*data\s*=\s*\{")
#: 表单里的一行键值（键一律是单引号字符串字面量）
_FORM_ITEM = re.compile(r"^\s*'([A-Za-z_][A-Za-z0-9_]*)'\s*:\s*(.*?)\s*,?\s*(?:#.*)?$")
#: 单行字符串字面量取值
_STR_LITERAL = re.compile(r"^'(.*)'$")
#: 整数字面量取值
_INT_LITERAL = re.compile(r"^(\d+)$")
#: 方法定义行（用来限定扫描范围）
_METHOD_DEF = re.compile(r"^\s*def\s+([a-z_]+)\s*\(self")


def _parse_form_block(lines: list[str], begin: int, end: int) -> tuple[list[str], dict]:
    """解析 ``begin`` 处 ``data = {`` 开始、到同级 ``}`` 为止的表单。

    返回 ``(字段名列表, {字段名: 字面量取值})``。

    **只收字面量**：像 ``self.query_ins.left_date`` 这种运行期取值，
    在基线里没有可对照的常量 —— 记成「没有字面量」比记一个猜出来的值诚实。
    """
    keys: list[str] = []
    const: dict = {}
    head = lines[begin]
    inline = head.split("{", 1)[1]
    if "}" in inline:
        body = [inline.split("}", 1)[0]]
    else:
        body = []
        i = begin + 1
        while i < end:
            if lines[i].strip().startswith("}"):
                break
            body.append(lines[i])
            i += 1
    for line in body:
        m = _FORM_ITEM.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if key in keys:
            continue
        keys.append(key)
        sm = _STR_LITERAL.match(raw)
        if sm:
            const[key] = sm.group(1)
            continue
        im = _INT_LITERAL.match(raw)
        if im:
            const[key] = int(im.group(1))
    return keys, const


def cut_order_endpoints() -> dict[str, str]:
    """切出下单六步的**路径**，按本项目步骤键归类。"""
    raw = _cut_named_endpoints(
        tuple(ORDER_ENDPOINT_STEPS), FACT_SOURCES["order_endpoints"][0]
    )
    out: dict[str, str] = {}
    for const_name, step in ORDER_ENDPOINT_STEPS.items():
        path = raw[const_name]
        # 上游把查询串直接写进了常量（``API_QUERY_ORDER_WAIT_TIME = ...?{}``）。
        # 拆开：**路径**是协议事实；「用哪些参数」是另一件事（见表单字段）。
        if "?" in path:
            path, _, query = path.partition("?")
            if query != "{}":
                raise SystemExit(
                    f"✘ {const_name} 的查询串不是预期的 '{{}}' 而是 {query!r}（上游结构变了？）"
                )
        out[step] = path
    return out


def cut_order_forms() -> dict[str, dict]:
    """切出下单各步的**表单字段名与固定取值**（逐字，只收字面量）。"""
    text = _read_upstream(FACT_SOURCES["order_forms"][0]).replace("\r\n", "\n")
    lines = text.splitlines()
    out: dict[str, dict] = {}
    for i, line in enumerate(lines):
        m = _METHOD_DEF.match(line)
        if not m or m.group(1) not in ORDER_FORM_METHODS:
            continue
        step = ORDER_FORM_METHODS[m.group(1)]
        for j in range(i + 1, len(lines)):
            if _METHOD_DEF.match(lines[j]):
                raise SystemExit(f"✘ {m.group(1)} 里没找到 `data = {{`（上游结构变了？）")
            if _DICT_LINE.match(lines[j]):
                keys, const = _parse_form_block(lines, j, len(lines))
                out[step] = {"keys": tuple(keys), "const": const}
                break
    missing = [s for s in ORDER_FORM_METHODS.values() if s not in out]
    if missing:
        raise SystemExit(f"✘ 没能切出这些步骤的表单：{missing}（上游结构变了？）")

    # initDc 的表单在 user/job.py 的 request_init_dc_page 里（单行写法）
    jlines = _read_upstream(FACT_SOURCES["init_dc_form"][0]).replace("\r\n", "\n").splitlines()
    for i, line in enumerate(jlines):
        if not line.strip().startswith("def request_init_dc_page("):
            continue
        for j in range(i + 1, len(jlines)):
            if _METHOD_DEF.match(jlines[j]):
                break
            if _DICT_LINE.match(jlines[j]):
                keys, const = _parse_form_block(jlines, j, len(jlines))
                out["init_dc"] = {"keys": tuple(keys), "const": const}
                break
        break
    if "init_dc" not in out:
        raise SystemExit(f"✘ 没能从 {FACT_SOURCES['init_dc_form'][0]} 切出 initDc 表单")
    return out


def cut_order_slide() -> dict:
    """切出滑块风控的**事实**：附加字段 / scene 取值 / 选择器 / 状态正则。"""
    text = _read_upstream(FACT_SOURCES["order_slide"][0]).replace("\r\n", "\n")
    lines = text.splitlines()

    selector = ""
    for line in lines:
        if "'#slide_passcode" in line and not selector:
            sm = re.search(r"'([^']*#slide_passcode[^']*)'", line)
            if sm:
                selector = sm.group(1)

    keys: tuple[str, ...] = ()
    const: dict = {}
    for i, line in enumerate(lines):
        if "data.update({" not in line:
            continue
        patched = [li.replace("data.update({", "data = {", 1) for li in lines]
        got_keys, const = _parse_form_block(patched, i, len(lines))
        keys = tuple(got_keys)
        break

    state_regex = ""
    jtext = _read_upstream(FACT_SOURCES["init_dc_form"][0]).replace("\r\n", "\n")
    for line in jtext.splitlines():
        if "if_check_slide_passcode" in line and "re.search" in line:
            start = line.index("re.search(") + len("re.search(")
            end = line.index(", html)")
            state_regex = line[start:end].strip()
            break

    if not selector or not keys or not state_regex:
        raise SystemExit(
            "✘ 没能切全滑块事实（选择器/附加字段/状态正则）："
            f"selector={selector!r} keys={keys} state_regex={state_regex!r}"
        )
    return {
        "selector": selector,
        "keys": keys,
        "const": const,
        "state_regex": state_regex,
    }


#: 要切的上游配置常量。⚠️ 只切**这几个数字**，不是整张 config 表。
PACING_PARAM_NAMES: tuple[str, ...] = (
    "QUERY_INTERVAL",
    "TIME_OUT_OF_REQUEST",
    "USER_HEARTBEAT_INTERVAL",
)

#: 要切的上游函数。**切源码文本**，由适配层 exec 使用 ——
#: 「一个数字怎么换算成区间」的权威实现是上游那一份，不是我们抄的公式。
PACING_FUNC_NAMES: tuple[str, ...] = (
    "init_interval_by_number",
    "get_interval_num",
)

#: 顶层 ``def`` 行（用来界定函数体范围）
_TOP_DEF = re.compile(r"^def\s+([a-z_][a-z0-9_]*)\s*\(")


def cut_pacing_params() -> dict[str, int]:
    """切出上游 ``config.py`` 里的节奏/超时常量（逐字，只取整数）。"""
    out: dict[str, int] = {}
    for line in _read_upstream(FACT_SOURCES["pacing_params"][0]).splitlines():
        stripped = line.strip()
        for name in PACING_PARAM_NAMES:
            prefix = f"{name} ="
            if stripped.startswith(prefix):
                raw = stripped[len(prefix):].split("#")[0].strip()
                if raw.isdigit():
                    out[name] = int(raw)
    missing = [n for n in PACING_PARAM_NAMES if n not in out]
    if missing:
        raise SystemExit(
            f"✘ 没能从 {FACT_SOURCES['pacing_params'][0]} 切出这些常量：{missing}（上游改名了？）"
        )
    return out


def cut_pacing_funcs() -> dict[str, str]:
    """切出上游那两个函数的**源码文本**（def 行到下一个顶层 def 之前）。"""
    lines = _read_upstream(FACT_SOURCES["pacing_funcs"][0]).splitlines()
    starts: dict[str, int] = {}
    tops: list[int] = []
    for i, line in enumerate(lines):
        m = _TOP_DEF.match(line)
        if m:
            starts[m.group(1)] = i
            tops.append(i)
    out: dict[str, str] = {}
    for name in PACING_FUNC_NAMES:
        begin = starts.get(name)
        if begin is None:
            continue
        after = [i for i in tops if i > begin]
        end = after[0] if after else len(lines)
        out[name] = "\n".join(lines[begin:end]).rstrip()
    missing = [n for n in PACING_FUNC_NAMES if n not in out]
    if missing:
        raise SystemExit(
            f"✘ 没能从 {FACT_SOURCES['pacing_funcs'][0]} 切出这些函数：{missing}（上游改名了？）"
        )
    return out


def cut_qr_check_facts() -> tuple[tuple[str, ...], tuple[int, ...], str]:
    """切出扫码轮询的**请求字段名**与 ``result_code`` 分支值。

    两样都是协议事实：

    - 字段名说明「上游往表单里放什么」（``RAIL_DEVICEID`` / ``RAIL_EXPIRATION``
      是本项目**没放**的两个 —— 差异要留档，所以必须能被机器读出来）；
    - ``result_code`` 的四个分支值说明「官方用哪几个数字表达扫码状态」
      （0 待扫 / 1 已扫 / 2 成功 / 3 过期）。本项目有一张自己的映射表，
      两边必须指向同一组数字 —— 这是本域最重要的对照。
    """
    text = _read_upstream(FACT_SOURCES["qr_check"][0])
    lines = text.splitlines()

    # ⚠️ 必须**限定在 qr_login 这个函数体内**：同一个文件里
    # `login2` 也有一个 `data = {...}`（username/password/appid），
    # 不限定范围就会把两处字段混在一起 —— 第一版正是这么错的。
    begin = None
    for i, line in enumerate(lines):
        if line.strip().startswith("def qr_login("):
            begin = i
            break
    if begin is None:
        raise SystemExit(f"✘ {FACT_SOURCES['qr_check'][0]} 里找不到 qr_login")
    end = len(lines)
    for j in range(begin + 1, len(lines)):
        if lines[j].startswith("    def "):
            end = j
            break

    fields: list[str] = []
    collecting = False
    codes: list[int] = []
    appid = ""
    for line in lines[begin:end]:
        stripped = line.strip()
        if stripped == "data = {":
            collecting = True
            continue
        if collecting:
            if stripped.startswith("}"):
                collecting = False
                continue
            fm = re.match(r"'([A-Za-z_][A-Za-z0-9_]*)':", stripped)
            if fm:
                fields.append(fm.group(1))
            vm = re.match(r"'appid':\s*'([^']+)'", stripped)
            if vm:
                appid = vm.group(1)
        cm = re.match(r"(?:if|elif) result_code == (\d+):", stripped)
        if cm:
            codes.append(int(cm.group(1)))

    if not fields:
        raise SystemExit(f"✘ 没能从 {FACT_SOURCES['qr_check'][0]} 切出扫码表单字段名")
    if len(codes) < 4:
        raise SystemExit(
            f"✘ 只切到 {len(codes)} 个 result_code 分支（上游结构变了？）：{codes}"
        )
    if not appid:
        raise SystemExit(f"✘ 没能从 {FACT_SOURCES['qr_check'][0]} 切出 appid 取值")
    return tuple(fields), tuple(codes), appid


def render_ticket_facts() -> str:
    """生成 ``vendor/pjialin/ticket_facts.py``。"""
    idx = cut_index_constants()
    regex = cut_route_regex()
    url_template = cut_route_url_template()
    endpoints = cut_user_endpoints()
    fields = cut_user_response_fields()
    qr_fields, qr_codes, qr_appid = cut_qr_check_facts()
    order_paths = cut_order_endpoints()
    order_forms = cut_order_forms()
    order_slide = cut_order_slide()
    pacing_params = cut_pacing_params()
    pacing_funcs = cut_pacing_funcs()
    qr_field_body = "\n".join(f"    {name!r}," for name in qr_fields)
    qr_code_body = ", ".join(str(code) for code in qr_codes)
    endpoint_body = "\n".join(f"    {k!r}: {v!r}," for k, v in endpoints.items())
    field_body = "\n".join(f"    {k!r}: {v!r}," for k, v in fields.items())
    order_path_body = "\n".join(f"    {k!r}: {v!r}," for k, v in order_paths.items())
    order_field_body = "\n".join(
        "    {!r}: (\n{}    ),".format(
            step, "".join(f"        {name!r},\n" for name in data["keys"])
        )
        for step, data in order_forms.items()
    )
    order_const_body = "\n".join(
        "    {!r}: {!r},".format(step, data["const"]) for step, data in order_forms.items()
    )
    slide_field_body = "\n".join(f"    {name!r}," for name in order_slide["keys"])
    order_slide_const = order_slide["const"]
    pacing_param_body = "\n".join(f"{k} = {v}" for k, v in pacing_params.items())
    pacing_func_body = "".join(
        f"{chr(10)}{chr(10)}{name.upper()}_SOURCE = r{chr(34)*3}{chr(10)}{text}{chr(10)}{chr(34)*3}"
        for name, text in pacing_funcs.items()
    )
    order_slide_selector = order_slide["selector"]
    order_slide_regex = order_slide["state_regex"]
    sources = "\n".join(
        f"- ``{where}`` —— {what}（sha256 ``{sha256(VENDOR / where)[:12]}``）"
        for where, what in FACT_SOURCES.values()
    )
    body = "\n".join(f"{name} = {value}" for name, value in idx.items())
    return f'''"""从上游原件**逐字切出**的协议事实（生成物，不要手改）。

上游出处：

{sources}

为什么单独生成这一份
--------------------
`py12306.query.job` 一 import 就拉起 `order` / `user` / `helpers.qrcode`
（→ `png` / `pyppeteer`）一大片，而这里要的只是 {len(idx)} 个整数常量与一条正则。
生成（而不是手抄）才能被机器验证：本文件由
`scripts/build_vendor_pjialin.py` 产出，`--check` 会**重新生成一遍再比对**。

用途：`tests/test_query_baseline.py` 拿它当**外部锚点**校验本项目的字段下标，
`kernel/pjialin/query_route.py` 用它读余票页给出的查询路径。
"""

from __future__ import annotations

import ast

#: 余票结果（``|`` 分隔）的字段下标 —— 变量名与取值都保持上游原样。
#: ⚠️ 名字只反映上游当年的理解，**不要**按名字推断语义：
#: 例如 ``INDEX_TICKET_NUM``（下标 {idx.get("INDEX_TICKET_NUM")}）实际被上游拿去和 ``'Y'`` 比较
#: （见 ``is_has_ticket``），那是「可否网订」而不是「余票数」。
{body}

#: 上游从余票页 HTML 里取查询路径的正则**源码文本**（逐字，含原始转义）。
ROUTE_REGEX_SOURCE = r"""{regex}"""

#: 真正可用的正则：对上面的源码文本求值（它本来就是 Python 字符串字面量）。
#: 这样「逐字」与「可用」两件事不用二选一 —— 也避免了手抄转义。
ROUTE_PATTERN = ast.literal_eval(ROUTE_REGEX_SOURCE)

#: 上游从中取出的分组就是查询路径，例如 ``leftTicket/queryZ``。
ROUTE_GROUP = 1

#: 余票查询的 URL 模板（上游 ``helpers/api.py`` 的 ``LEFT_TICKETS['url']``，
#: **已去掉 BASE_URL_OF_12306 前缀**，逐字切出）。
#: 用它把页面给的 ``type`` 还原成上游会请求的完整路径 ——
#: 这样「上游片段 → 本项目路径」的转换有出处，不是我们自己发明的拼接。
ROUTE_URL_TEMPLATE = r"""{url_template}"""

#: 用户域端点**路径**（上游 ``helpers/api.py``，逐字切出，已去掉 BASE_URL 前缀）。
#: ⚠️ ``API_USER_LOGIN_CHECK`` 指 ``/otn/login/conf`` —— 与本项目登录态检测用的
#: ``/otn/login/checkUser`` **不是同一条**。这正是 ``login_check`` 域的差异点。
USER_ENDPOINTS = {{
{endpoint_body}
}}

#: 上游读响应用的**点号字段路径**（上游 ``user/job.py``，按函数归类）。
#: 上游是「点号路径直接取嵌套」（``result.get('data.is_login')``），
#: 本项目是逐层取（``first_of``）—— 两种写法必须能对上，
#: 由 ``tests/test_user_baseline.py`` 逐项校验。
USER_RESPONSE_FIELDS = {{
{field_body}
}}

#: 语义别名 —— 把上游常量名翻译成本项目调用点用的名字。
#: 有了它们，`kernel/pjialin/**` 里就不必出现上游常量名（少一处将来会腐烂的耦合）。
#: 值仍然来自上面两张表，**不是重新抄一遍**。
LOGIN_CHECK_PATH = USER_ENDPOINTS["API_USER_LOGIN_CHECK"]
USER_INFO_PATH = USER_ENDPOINTS["API_USER_INFO"]
PASSENGERS_PATH = USER_ENDPOINTS["API_USER_PASSENGERS"]
QR_CREATE_PATH = USER_ENDPOINTS["API_AUTH_QRCODE_BASE64_DOWNLOAD"]
QR_CHECK_PATH = USER_ENDPOINTS["API_AUTH_QRCODE_CHECK"]

#: 上游判读「是否登录」用的字段：``data.is_login``，且与 ``'Y'`` 比较。
LOGIN_ALIVE_FIELD = USER_RESPONSE_FIELDS["check_user_is_login"]
LOGIN_ALIVE_VALUES = ("Y",)
#: 上游读用户信息用的是**点号路径直接取嵌套**（``data.userDTO.loginUserDTO``）。
USER_INFO_FIELD = USER_RESPONSE_FIELDS["get_user_info"]
#: 乘车人列表所在字段。
PASSENGERS_FIELD = USER_RESPONSE_FIELDS["get_user_passengers"]

#: 扫码轮询（``checkqr``）上游往表单里放的字段名（逐字切出）。
#: ⚠️ 前两个是本项目**没有**放的 —— 差异留档在此，供基线对照。
QR_CHECK_FIELDS = (
{qr_field_body}
)

#: 上游 ``qr_login`` 按这几个 ``result_code`` 值分支：
#: 0 继续等 / 1 提示「请确认登录」/ 2 跳出（成功）/ 3 重新下载（过期）。
#: 本项目的 ``QR_STATE_MAP`` 必须指向**同一组数字**。
QR_RESULT_CODES = ({qr_code_body})

#: 上游扫码用的 ``appid`` 取值（官方固定值）。
QR_APPID = {qr_appid!r}

#: 下单六步的端点**路径**（上游 ``helpers/api.py``，逐字切出，已去掉 BASE_URL 前缀）。
#: ⚠️ 上游把查询串写进了 ``API_QUERY_ORDER_WAIT_TIME``（``...?{{}}``）；这里只留路径。
ORDER_PATHS = {{
{order_path_body}
}}

#: 下单各步的表单**字段名**（顺序 = 上游拼装顺序）。
ORDER_FORM_FIELDS = {{
{order_field_body}
}}

#: 表单里上游**写死的取值**（只收字面量）。
#: 动态取值不在其中 —— 那正是要与本项目逐项对照的部分。
#: ⚠️ ``check_order_info.cancel_flag`` 上游写的是**整数** ``2``，本项目写的是字符串 ``"2"``：
#: 表单编码后都是 ``2``，但这个差异要留在能被看见的地方。
ORDER_FORM_CONST = {{
{order_const_body}
}}

#: 滑块风控：``check_order_info`` 里 ``data.update({{...}})`` 追加的字段
#: （只在 ``is_slide`` 为真时追加 —— 上游至此才知道这一单要不要过滑块）。
ORDER_SLIDE_FIELDS = (
{slide_field_body}
)
ORDER_SLIDE_CONST = {order_slide_const!r}
#: 上游等滑块时用的 DOM 选择器。
ORDER_SLIDE_SELECTOR = {order_slide_selector!r}
#: 上游判「这一单要不要过滑块」的正则**源码文本**（逐字，含原始转义）。
ORDER_SLIDE_STATE_REGEX_SOURCE = {order_slide_regex!r}

#: ---- P4 节奏 / 参数（A15 / A17）----
#: 上游 ``config.py`` 的默认值（逐字切出）。
{pacing_param_body}

#: 上游那两个函数的**源码文本**（逐字）。适配层 exec 它们来算间隔区间 ——
#: 「一个数字怎么换算成区间」的权威实现是上游那一份，不是我们抄的公式。
{pacing_func_body}
'''


def write_ticket_facts() -> None:
    (VENDOR / "ticket_facts.py").write_text(
        render_ticket_facts(), encoding="utf-8", newline=""
    )


def check_ticket_facts() -> list[str]:
    """生成物必须与「从当前上游原件重新生成」的结果一致。"""
    target = VENDOR / "ticket_facts.py"
    if not target.exists():
        return ["生成物缺失: ticket_facts.py（跑 --init 重建）"]
    actual = target.read_text(encoding="utf-8")
    expected = render_ticket_facts()
    if actual != expected:
        return ["生成物与上游原件不一致: ticket_facts.py（上游文件变了？跑 --init 重建）"]
    return []


def build_manifest() -> dict:
    files: dict[str, dict] = {}
    for p in upstream_files():
        rel = p.relative_to(VENDOR).as_posix()
        entry: dict = {"sha256": sha256(p)}
        mod = KNOWN_MODIFICATIONS.get(rel)
        if mod:
            entry["modified"] = True
            entry["modifications"] = [mod]
        else:
            entry["modified"] = False
        files[rel] = entry
    return {
        "_comment": (
            "vendor/pjialin 溯源清单。**不要手改**："
            "改上游文件请在 scripts/build_vendor_pjialin.py 的 KNOWN_MODIFICATIONS 里登记，"
            "然后重跑 --init。"
        ),
        "upstream": {
            "repo": UPSTREAM_REPO,
            "commit": UPSTREAM_COMMIT,
            "commit_date": UPSTREAM_COMMIT_DATE,
            "license": UPSTREAM_LICENSE,
            "homepage": UPSTREAM_URL,
            "fetch": FETCH_URL,
        },
        "excluded": [rel for rel, _ in EXCLUDED],
        "count": {"files": len(files), "modified": sum(1 for v in files.values() if v["modified"])},
        #: 由本脚本生成的上游投影文件（内容来自上游原件，逐字切出）
        "generated": {
            rel: {
                "from": [
                    {"path": where, "what": what, "sha256": sha256(VENDOR / where)}
                    for where, what in FACT_SOURCES.values()
                ]
            }
            for rel in sorted(GENERATED_FILES)
        },
        "files": files,
    }


def render_notice() -> str:
    return f"""py12306 内核替换 · 第三方组件声明
=====================================

本项目（12306 抢票助手，ticket_helper）在 {VENDOR.relative_to(BASE).as_posix()}/ 下
包含了以下开源项目的源代码：

  py12306
    Copyright the py12306 contributors
    来源：{UPSTREAM_URL}
    版本：{UPSTREAM_COMMIT}（{UPSTREAM_COMMIT_DATE}）
    许可：Apache License 2.0（全文见同目录 LICENSE）

用途：按需求方要求，把 12306 的协议交互能力**改为复用上游成熟实现**，
而不是自行重写。本项目不修改其许可声明，也不在其名称上做任何暗示性使用。

修改声明（Apache-2.0 §4(b) 要求）：
  见同目录 VENDOR.md。当前被修改的上游文件数：{len(KNOWN_MODIFICATIONS)}。
"""


def render_vendor_doc() -> str:
    if not KNOWN_MODIFICATIONS:
        changes = "**当前没有修改任何上游文件** —— vendor 目录与上游原件逐字节一致。\n"
    else:
        lines = []
        for rel, mod in KNOWN_MODIFICATIONS.items():
            lines.append(f"### `{rel}`\n\n- **改了什么**：{mod['what']}\n- **为什么**：{mod['why']}\n")
        changes = "\n".join(lines)
    return f"""# vendor/pjialin —— 上游源码（只读参照 + 被 import 的实现）

> 本目录内容来自 {UPSTREAM_REPO} @ `{UPSTREAM_COMMIT}`（{UPSTREAM_COMMIT_DATE}，{UPSTREAM_LICENSE}）。
> **不要手改**：改动必须登记在 `scripts/build_vendor_pjialin.py` 的 `KNOWN_MODIFICATIONS`，
> 然后重跑 `python scripts/build_vendor_pjialin.py --init`。
> 校验：`python scripts/build_vendor_pjialin.py --check`（CI 与测试都跑它）。

## 一、为什么是「整包」而不是「抽几个文件」

上游是**配置驱动的独立应用**（无 `setup.py`），模块之间耦合紧：
`py12306/query/job.py` 会 import `order/order.py`、`user/job.py`、`helpers/*`，
`helpers/*` 又会 import `config.py` 与 `app.py`。
抽几个文件出来会立刻遇到 import 缺失，反而让人以为「抄一份就能跑」。

所以**整包 vendor**，再由 `ticket_helper/kernel/pjialin/` 做适配
（隔离方案 I-1：进程内 + `Config` 垫片，见
`docs/pjialin内核化总体架构设计方案v3.md` §4.3）。

## 二、排除项

| 排除 | 原因 |
|---|---|
{chr(10).join(f"| `{rel}` | {why} |" for rel, why in EXCLUDED)}

## 三、改动说明（Apache-2.0 §4(b)）

{changes}

## 四、合规

| 要求 | 落点 |
|---|---|
| 许可全文随附 | `LICENSE`（上游原件） |
| 声明使用了上游 | `NOTICE` |
| 写明修改 | 本文件 §3 |
| 可复现来源 | `PROVENANCE.json` 的 `upstream.fetch`（锁定 commit 的 tarball 地址） |
"""


def write_all() -> None:
    manifest = build_manifest()
    PROVENANCE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline=""
    )
    NOTICE.write_text(render_notice(), encoding="utf-8", newline="")
    VENDOR_DOC.write_text(render_vendor_doc(), encoding="utf-8", newline="")
    write_ticket_facts()
    print(f"✔ 已生成 {PROVENANCE.relative_to(BASE)}（{manifest['count']['files']} 个文件）")
    print(f"✔ 已生成 {NOTICE.relative_to(BASE)}")
    print(f"✔ 已生成 {VENDOR_DOC.relative_to(BASE)}")
    print(f"✔ 已生成 {(VENDOR / 'ticket_facts.py').relative_to(BASE)}（从上游逐字切出）")


def check() -> int:
    if not PROVENANCE.exists():
        print(f"✘ {PROVENANCE.relative_to(BASE)} 不存在，请先跑 --init")
        return 1
    manifest = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    recorded: dict[str, dict] = manifest["files"]

    problems: list[str] = []

    # ① 文件必须存在、且 sha 与登记一致
    for rel, entry in recorded.items():
        p = VENDOR / rel
        if not p.exists():
            problems.append(f"缺失: {rel}")
            continue
        actual = sha256(p)
        if actual != entry["sha256"]:
            why = "（这是登记为『已修改』的文件，说明有人又手改了它）" if entry["modified"] else "（未登记为已修改，说明有人手改了上游原件）"
            problems.append(f"内容不一致: {rel} {why}")

    # ② 不允许有未登记的文件（多出来的文件也必须说清来源）
    for p in upstream_files():
        rel = p.relative_to(VENDOR).as_posix()
        if rel not in recorded:
            problems.append(f"未登记的文件: {rel}")

    # ③ 「已修改」的文件必须有改动说明
    for rel, entry in recorded.items():
        if entry["modified"] and not entry.get("modifications"):
            problems.append(f"标记为已修改但没有改动说明: {rel}")

    # ④ 合规文件必须在
    for name in ("LICENSE", "NOTICE", "VENDOR.md"):
        if not (VENDOR / name).exists():
            problems.append(f"合规文件缺失: {name}")

    # ⑤ 生成物必须能由当前上游原件重新生成出一模一样的内容
    problems.extend(check_ticket_facts())

    if problems:
        print(f"✘ vendor/pjialin 校验失败，{len(problems)} 处问题：")
        for item in problems[:20]:
            print(f"    - {item}")
        if len(problems) > 20:
            print(f"    … 还有 {len(problems) - 20} 处")
        print("\n  修法：确认改动是有意的 → 在 KNOWN_MODIFICATIONS 登记 → 重跑 --init")
        return 1

    print(
        f"✔ vendor/pjialin 校验通过：{manifest['count']['files']} 个文件，"
        f"{manifest['count']['modified']} 个已登记修改，上游 {UPSTREAM_REPO}@{UPSTREAM_COMMIT[:8]}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成/校验 vendor/pjialin 溯源与合规文件")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true", help="按当前 vendor 内容重建清单")
    group.add_argument("--check", action="store_true", help="校验清单与内容是否一致")
    args = parser.parse_args()

    if args.init:
        write_all()
        return 0
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
