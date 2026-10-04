# 上游参照文件（只读）

本目录存放**上游开源项目的原始文件**，用途只有一个：给本项目提供
**外部权威基线** —— 协议路径、参数、请求头不再靠直觉写，而是有出处可以对。

> ⚠️ 这里的文件**不进运行时、不进打包产物**（`TicketHelper.spec` 的
> `datas`/`hiddenimports` 都不包含 `reference/`）。
> 本项目**不会 import** 它们中的任何一行；它们只被
> `scripts/build_upstream_baseline.py` 读取并生成
> `reference/baseline/upstream_endpoints.json`。

---

## 一、文件清单与出处

| 本地文件 | 来源仓库 | 上游路径 | 许可证 |
|---|---|---|---|
| `py12306_helpers_api.py` | `pjialin/py12306` | `py12306/helpers/api.py` | **Apache-2.0** |
| `testerSunshine_urlConf.py` | `testerSunshine/12306` | `config/urlConf.py` | **MIT** |

抓取时的来源 commit（可复现）：

| 仓库 | commit | 该 commit 的日期 |
|---|---|---|
| `pjialin/py12306` | `52ccae8b206b828624f185bf8178e1057c6b5fb9` | 2026-06-16 |
| `testerSunshine/12306` | `a495af88346a0d794493c6030f6a6207debb5824` | 2021-01-04 |

更新方式（会改这两个文件，然后需要重建基线）：

```bash
curl -sS -o reference/upstream/py12306_helpers_api.py \
  https://raw.githubusercontent.com/pjialin/py12306/master/py12306/helpers/api.py
curl -sS -o reference/upstream/testerSunshine_urlConf.py \
  https://raw.githubusercontent.com/testerSunshine/12306/master/config/urlConf.py
python scripts/build_upstream_baseline.py     # 重建快照
python -m pytest tests/test_protocol_baseline.py -q
```

---

## 二、许可证与合规

本项目对这两个文件的用法是**「读取 → 生成数据清单」**，没有修改、没有链接、
没有再分发到产物里。但既然文件原件落在仓库里，仍按许可证要求保留声明：

- **pjialin/py12306 — Apache License 2.0**
  Copyright the py12306 contributors.
  许可证全文：https://github.com/pjialin/py12306/blob/master/LICENSE

- **testerSunshine/12306 — MIT License**
  Copyright (c) 2017
  许可证全文：https://github.com/testerSunshine/12306/blob/master/LICENSE

若日后要把上游代码**真的引入运行时**（路线 B：vendor 成内核），
必须在本文件里补上：修改说明、保留的版权头、以及 `NOTICE`（Apache-2.0 要求）。

---

## 三、第三个项目：`testerSunshine/12306model` —— **不引入**

| 项 | 值 |
|---|---|
| 仓库 | `testerSunshine/12306model`（「本地识别模型下载」） |
| 内容 | 只有三个文件：`12306.image.model.h5`（57.4 MB）、`model.v2.0.h5`（0.6 MB）、`README.md` |
| 最后提交 | 2019-09-03 |
| **许可证** | **没有 LICENSE 文件** → 默认「保留所有权利」 |
| 运行要求 | TensorFlow 1.14 / Keras 2.2.4 → **只支持 Python ≤ 3.7** |

**结论（2026-09-29 决策）**：**不引入**。两个独立原因：

1. **法律**：无许可证 → 把它打进 `TicketHelper.exe` 再分发就是再分发无许可作品。
2. **技术**：本项目运行在 **Python 3.13**，TF 1.14 装不上、`.h5` 也加载不了。
   要强上就得把整个项目降到 Python 3.7 + TF1.14，等于放弃 Flask 3 / PySide6 6.11 /
   cryptography 42 等全部现代依赖 —— 不可逆的降级。

**采用的替代**：`ddddocr`（`pip install ddddocr`，走 onnxruntime，Python 3.13 原生可用），
已在 `ticket_helper/verify/service.py` 里接入；识别不了时走人工兜底。

本项目从未把该模型放进仓库（`find . -name "*.h5"` 为 0 个结果）。
本文件仅记录「我们评估过它、为什么不采用」，以免以后重复讨论。
