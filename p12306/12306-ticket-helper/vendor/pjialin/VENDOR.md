# vendor/pjialin —— 上游源码（只读参照 + 被 import 的实现）

> 本目录内容来自 pjialin/py12306 @ `52ccae8b206b828624f185bf8178e1057c6b5fb9`（2026-06-16，Apache-2.0）。
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
| `.github/` | GitHub 的 issue / PR 模板，与源码无关 |

## 三、改动说明（Apache-2.0 §4(b)）

### `py12306/helpers/api.py`

- **改了什么**：第 53–58 行区域：把 `API_GET_BROWSER_DEVICE_ID` 从 `https://12306-rail-id-v2.pjialin.com/` 改回上游自己注释掉的官方地址 `BASE_URL_OF_12306 + '/otn/HttpZF/logdevice'`；并删除了指向 `https://12306-ocr.pjialin.com/check/` 的 `API_FREE_CODE_QCR_API` 常量。
- **为什么**：需求方明确禁止请求 pjialin 托管域（'如果要就把源码拿过来自己弄'）。改回官方地址后，query.py:130 / user/job.py:331 的 `if 'pjialin' not in API_GET_BROWSER_DEVICE_ID: return self.request_device_id2()` 会判为真，自动走上游自己实现的直连官方路径 —— 不需要自建服务。打码则由 'free' 改为 'user' 模式（本机服务），见 OCR.py 的改动。

### `py12306/helpers/OCR.py`

- **改了什么**：三处：① 删掉 `from py12306.helpers.api import API_FREE_CODE_QCR_API`；② `get_img_position` 的判定去掉 `'free'`，只留 `'user'`；③ `get_image_by_free_site` 里删掉 `if ...=='free'` 分支，只保留 POST 到 `Config().API_USER_CODE_QCR_API`。
- **为什么**：`free` 分支的唯一出口就是被禁的 `*.pjialin.com`。**坐标换算 `get_image_position_by_offset` 一个字未改** —— 它是本次要复用的上游能力（格子编号 → x,y）。


## 四、合规

| 要求 | 落点 |
|---|---|
| 许可全文随附 | `LICENSE`（上游原件） |
| 声明使用了上游 | `NOTICE` |
| 写明修改 | 本文件 §3 |
| 可复现来源 | `PROVENANCE.json` 的 `upstream.fetch`（锁定 commit 的 tarball 地址） |
