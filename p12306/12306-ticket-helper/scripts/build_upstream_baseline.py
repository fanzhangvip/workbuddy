"""从上游参照文件里抽出「接口地址清单」，生成协议基线快照。

为什么需要它
------------
2026-09-29 线上事故：候补提交一直失败，日志只有一句
「响应不是合法 JSON (HTTP 405)」——真因是候补接口的**路径整族写错了**
（写成了 `/otn/confirmPassenger/confirmHB`，真机 405；正确家族是 `/otn/afterNate/`）。

这类错误之所以能混到线上，是因为**当时没有任何外部基线可以对**：
路径是照着直觉写的，本地 mock 也照着同一个直觉实现，于是离线永远测不出来。

这个脚本把两个上游开源项目的接口表抽成一份快照，`tests/test_protocol_baseline.py`
再拿本项目 `ticket_helper/inter/urls.py` 去对 —— **对不上就必须显式解释**，
不允许静默存在。

用法::

    python scripts/build_upstream_baseline.py            # 重建快照
    python scripts/build_upstream_baseline.py --check     # 只校验快照是否过期（CI/测试用）

上游与许可证
------------
- ``reference/upstream/py12306_helpers_api.py``      ← pjialin/py12306 (Apache-2.0)
- ``reference/upstream/testerSunshine_urlConf.py``   ← testerSunshine/12306 (MIT)

两个文件都是**只读参照**：不进运行时、不进打包产物，仅用于生成基线与人工对照。
许可证原文与来源 commit 见 ``reference/VENDOR.md``。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
UPSTREAM_DIR = BASE / "reference" / "upstream"
BASELINE_FILE = BASE / "reference" / "baseline" / "upstream_endpoints.json"

#: 只关心这两类路径。12306 的接口全部落在这两个前缀下
PATH_PREFIXES = ("/otn/", "/passport/")

#: 上游文件 → (来源标识, 许可证)
SOURCES = {
    "py12306_helpers_api.py": ("pjialin/py12306 · py12306/helpers/api.py", "Apache-2.0"),
    "testerSunshine_urlConf.py": ("testerSunshine/12306 · config/urlConf.py", "MIT"),
}


def _normalize(raw: str) -> str | None:
    """把上游写的一整条 URL 归成「纯路径」。

    上游把域名、查询串、随机数占位都写在一行里
    （如 ``https://kyfw.12306.cn/passport/captcha/captcha-image?login_site=E&_={random}``），
    这里去掉域名与查询串，只留路径；路径里的 ``{xxx}`` 占位保留，用于模板匹配。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    # 去掉协议+域名
    text = re.sub(r"^https?://[^/]+", "", text)
    # 去掉查询串 / 片段
    text = text.split("?", 1)[0].split("#", 1)[0]
    if not text.startswith("/"):
        return None
    if not text.startswith(PATH_PREFIXES):
        return None
    return text


def _iter_strings(node: ast.AST):
    """递归产出一棵 AST 里所有的字符串字面量。"""
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value


def _extract_py12306(path: Path) -> list[dict[str, str]]:
    """py12306 的写法：模块级常量，值是字符串，或 ``{"url": "..."}`` 字典。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[dict[str, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        key = ""
        for target in targets:
            if isinstance(target, ast.Name):
                key = target.id
        value = node.value
        candidates: list[str] = []
        if isinstance(value, ast.Dict):
            # {"url": "..."} —— 取 url 那个键
            for k, v in zip(value.keys, value.values):
                if isinstance(k, ast.Constant) and k.value == "url":
                    candidates.extend(_iter_strings(v))
                    break
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            candidates.append(value.value)
        elif isinstance(value, ast.BinOp):
            # 'https://' + HOST_URL_OF_12306 + '/otn/...' 这种拼接
            candidates.extend(_iter_strings(value))
        for raw in candidates:
            normalized = _normalize(raw)
            if normalized:
                found.append({"key": key, "path": normalized})
    return found


def _extract_testersunshine(path: Path) -> list[dict[str, str]]:
    """testerSunshine 的写法：``urls = {"键名": {"req_url": "/otn/...", ...}}``。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[dict[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key_node, value_node in zip(node.keys, node.values):
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            name = key_node.value
            for raw in _iter_strings(value_node):
                normalized = _normalize(raw)
                if normalized:
                    found.append({"key": name, "path": normalized})
    return found


def build() -> dict:
    endpoints: list[dict[str, str]] = []
    for filename, (origin, license_name) in SOURCES.items():
        path = UPSTREAM_DIR / filename
        if not path.exists():
            raise SystemExit(f"× 缺少上游参照文件：{path}（见 reference/VENDOR.md）")
        extract = _extract_py12306 if filename.startswith("py12306") else _extract_testersunshine
        for item in extract(path):
            endpoints.append({**item, "source": origin, "license": license_name})

    # 去重：同一路径可能被多个键引用
    unique: dict[str, dict[str, str]] = {}
    for item in endpoints:
        existing = unique.get(item["path"])
        if existing is None:
            unique[item["path"]] = item
        else:
            keys = {existing["key"], item["key"]}
            existing["key"] = ",".join(sorted(k for k in keys if k))
    ordered = [unique[p] for p in sorted(unique)]

    templates = [e for e in ordered if "{" in e["path"]]
    concrete = [e for e in ordered if "{" not in e["path"]]
    return {
        "_comment": (
            "上游接口清单快照。**不要手改**：改上游文件后运行 "
            "`python scripts/build_upstream_baseline.py` 重建。"
        ),
        "sources": {name: {"origin": o, "license": li} for name, (o, li) in SOURCES.items()},
        "count": {"paths": len(concrete), "templates": len(templates)},
        "paths": concrete,
        "templates": templates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从上游参照文件重建协议基线快照")
    parser.add_argument("--check", action="store_true", help="只校验快照是否为最新，不写盘")
    args = parser.parse_args(argv)

    data = build()
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    if args.check:
        if not BASELINE_FILE.exists():
            print(f"× 基线不存在：{BASELINE_FILE}")
            return 1
        if BASELINE_FILE.read_text(encoding="utf-8") != text:
            print("× 基线已过期（上游参照文件变了，或基线被手改过）。")
            print("  请运行：python scripts/build_upstream_baseline.py")
            return 1
        print(f"√ 基线是最新的（{data['count']['paths']} 个路径 + "
              f"{data['count']['templates']} 个模板）")
        return 0

    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(text, encoding="utf-8")
    print(f"√ 已写出 {BASELINE_FILE.relative_to(BASE)}")
    print(f"  路径 {data['count']['paths']} 个，模板 {data['count']['templates']} 个")
    for item in data["paths"][:5]:
        print(f"    {item['path']}   ← {item['key']}  ({item['source'].split(' · ')[0]})")
    print("    …")
    return 0


if __name__ == "__main__":
    sys.exit(main())
