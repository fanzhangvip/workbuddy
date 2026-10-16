# Repository Guidelines

## Project Structure & Module Organization

This is a Chinese-language WeCom (企微) customer-intent scoring system: a FastAPI backend plus a Vue 3 UI.

- `backend/` — FastAPI service. `main.py` is the entry point; `api_*.py` are routers; `api_common.py` holds the shared list/pagination skeleton; `audit.py`, `http_client.py` are the shared audit/HTTP layers; `scoring_service.py`, `sync_service.py`, `eval_runner.py`, `scheduler.py` hold domain logic; `schema.sql` defines the PostgreSQL schema.
- Root modules — shared logic imported by both backend and scripts: `weiban.py`, `ww_chatdata.py`, `rules.py`, `contracts.py`, `confidence.py`, `config.py`, `analyze.py`.
- `frontend/` — Vue 3 + Vite app. Views live in `frontend/src/views/`, shared UI in `frontend/src/components/` (e.g. `ChatBubbles.vue`), reusable composables in `frontend/src/composables/` (e.g. `usePolling.js`), routing in `router.js`; HTTP calls plus shared display helpers (bubble parsing, media classification, formatters) live in `api.js`.
- `tests/` — standalone script-style tests. `导出/` and `backups/` are runtime artifacts and are git-ignored.
- Requirement/spec documents are the `客户意向评分_*.md` files; treat `客户意向评分_架构设计说明书.md` as the source of truth.

## Build, Test, and Development Commands

- `docker compose up -d` — start PostgreSQL (host port 5433).
- `.venv\Scripts\python.exe -m pip install -r requirements.txt` — install Python dependencies.
- `start_backend.bat` — run the API at `http://127.0.0.1:8848`.
- `start_frontend.bat` — run the Vite dev server at `http://127.0.0.1:5173` (proxies `/api` to 8848).
- `build_frontend.bat` — build the UI into `frontend/dist`, served by the backend.

## Coding Style & Naming Conventions

Python 3.13, 4-space indentation, `snake_case` functions/variables, `PascalCase` classes. Keep the `# -*- coding: utf-8 -*-` header and Chinese module docstrings. Files start with a docstring stating purpose and the application entry (`python -m uvicorn backend.main:app`). Endpoints live in `api_<area>.py` and expose `router`. Add `# noqa: E402` for imports that must follow `sys.path` setup. No formatter/linter is configured; match surrounding code.

## Reuse Before Rewrite (mandatory)

**能复用的地方必须复用；不允许复制粘贴一份「差不多的」实现。** 这是硬规则，不是偏好。

写新逻辑前先 `rg` 搜一遍（搜**概念**，不只搜名字）。已有相似实现就扩展它，把共用部分放到双方都能 import 的地方：

- **判断逻辑**（解析、分类、格式化、阈值、状态推导）→ 放 `frontend/src/api.js` 或后端对应领域模块并**导出函数**，不要在两个 `.vue` 里各写一遍。
- **渲染结构**（同一份 UI 出现在多个页面）→ 抽 `frontend/src/components/*.vue`，样式随组件走（`scoped`），不要再复制一份 CSS。
- **后端同类操作**（重试/退避、审计写入、JSONB 读写、分页、参数校验）→ 抽公共模块，由调用方传参。现成可用的：退避 `backend/http_client.backoff_seconds`、审计 `backend/audit.record_action`、JSONB `backend/db.as_list`、分页 `backend/api_common.paginate`。
- **配置读取 / 摘要 / 队列回收 / 批量任务骨架**：`db.setting*`（app_setting > .env > 默认）、`fingerprint.digest`、`db.reclaim_stuck_sending`、`job_service.JobService`（跑批服务的线程 + 跨进程状态 + 心跳僵尸判定）——别再各写一份。
- **测试样板**（路径引导、输出编码、`check()`、汇总）→ `tests/_harness.py`：`from _harness import ROOT, check, summary`。新增测试照着写，不要再抄一份样板。

**为什么是硬规则**：复用之后同一个 bug 只有一处要改。实测教训 —— 聊天气泡渲染被抄了三份，于是「多行消息被拆散」「左右方向判错」「引用回复被误读」「表情图片不显示」这一串**同一根因**的 bug，必须在 5 个地方各修一遍，漏改概率成倍。

需要「有点不一样的版本」时：

1. 优先用 **props / 参数**表达差异（`compact`、`showDay`、`variant`），而不是复制。
2. 两个调用方确实分叉时，**保留共用部分**，只把差异单独写出来。
3. 确实无法共用 → **在注释里写明为什么**（哪一处差异导致不能复用），不要默默复制。

依据的设计原则：DRY、单一职责、依赖倒置（依赖抽象而非具体实现）。
判断标准一句话：**「改一次需求要动几个文件？」答案应当是 1。**

## Testing Guidelines

Tests are self-contained scripts (no pytest) that print PASS/FAIL and exit non-zero on failure. Name them `test_<topic>.py`, mirroring the architecture feature (e.g. `test_confidence.py`, `test_g6_outbox_survives_watermark.py`). Run them directly, or through the unified entry:

```bat
.venv\Scripts\python.exe tests\run_all.py                              :: 默认 t0+t1，实测 ~60s
.venv\Scripts\python.exe tests\run_all.py --changed backend\db.py      :: 只跑与被改文件相关的
.venv\Scripts\python.exe tests\run_all.py --tier t0                    :: 只跑纯逻辑（~5s）
.venv\Scripts\python.exe tests\run_all.py --t2                         :: 加上真模型/真通道（十几分钟）
.venv\Scripts\python.exe tests\test_confidence.py                      :: 单个文件
```

**每个测试文件必须声明分层**（docstring 之后一行）：

```python
TIER = "t1"   # t0 = 纯逻辑（不碰 PG/网络）｜ t1 = 需要 PG 5433 ｜ t2 = 真实外部（真模型/真微伴企微/Chrome）
```

没声明 `TIER` 的文件会被 `run_all.py` **拒绝执行**（漏登记 = 悄悄不跑，比跑得慢危险）。
分层是 2026-10-16 逐个实测后定的：t2 里 `test_stage_a_acceptance` 单跑就要 31~48 分钟
（真模型调用），所以**默认不跑**；发布前再用 `--t2` 跑一次。

Most tests need PostgreSQL on 5433; they create and clean up their own rows.
改一个模块只想跑相关测试时用 `--changed <改动的文件>`（映射见 `tests/_map.py`；
文件不在表里 → 自动退回全量，宁可多跑不可漏跑）。

**⚠️ 跑测试前先确认没有正在运行的同步/评分。** 评分与同步是**互斥**的（避免评分读到
半截会话，见 `scoring_service.start` 的注释）：只要库里有一条 `status='running'`
的 `sync_job`，`scoring_service.start()` 就会拒绝启动 —— 依赖启动批量的测试会因此
失败（报「同步任务进行中」）。

触发真实同步的只有两条路径：**手动点「开始同步」**，或 **日常任务到点**
（默认 `20:00` 主同步、`03:30` 补偿同步，可在「同步中心 → 日常任务」改）。
固定间隔同步默认是关的（`SYNC_INTERVAL_MINUTES=0`）。

所以跑回归时：`docker compose stop app`（最省事），或避开那两个时刻。
另外**别在容器开着时跑依赖启动批量的用例**：日常任务正好在跑的话同样会撞上互斥闸。

## 规则变更与门禁（运维必读）

**改了 `rules.json` 就一定要走发布接口**，否则会被闸6 挡住**日常评分**：

```bat
curl -u user:pwd -X POST http://127.0.0.1:8848/api/scoring/rules/publish -H "Content-Type: application/json" -d "{}"
```

`scoring_service.start()` 在**规则版本发生变化**时会查一次门禁（`scoring_service.py` 的闸6
段），判定为"不通过"就直接拒绝启动。而日常任务链（`scheduler._daily_watch_job`）拿到
`ok:false` **只打一行 print** —— 表现是「当天评分静默不跑」。所以：

- 想让新规则生效：走 `POST /api/scoring/rules/publish`；确有理由强推时加 `force=true`（会写审计）。
- 门禁的三种结果：无基线 / 无已人工确认样本 → `decidable=false`（**放行但不代表通过**）；
  有基线且指标下降 → `blocked=true`；否则通过。看 `GET /api/scoring/rules/gate` 的 `reason`。
- **基线**：`POST /api/eval/baseline`（存在 `app_setting.eval_baseline_run_id` → `eval_run` 行，
  库内持久；`eval/baseline.json` 只是兜底与留档）。基线里缺某指标时门禁会明说它"本次不受保护"。
- **两步发布**（`eval_publish_requires_run=true` 时启用）：先 `POST /api/eval/start` 跑一轮
  （后台，十几分钟；**别用同步的 `/api/eval/run`，浏览器 120 秒就断了**），再带 `eval_run_id` 发布。
- **定期体检**：`eval_regression_cron`（每周一，默认 09:00）跑一次回归；异常看
  `GET /api/eval/alerts`（门禁回退 / 单规则误报率 / 复核积压 / 凭据与同步新鲜度）。
- **保活状态**：`wb_keepalive` 现在把上次结果写进 `app_setting.keepalive_last`（改造前只写内存，
  重启即失）。`GET /api/sync/keepalive` 可查。

## Commit & Pull Request Guidelines

Commit messages are short and Chinese, prefixed `feat:` (e.g. `feat: 初步完成`). PRs should describe the change, link the relevant issue or spec section, note DB/schema impact, and include screenshots for UI changes.

## Security & Configuration Tips

Configuration is read from `.env` (git-ignored) via `config.py`; system environment variables take precedence. Never commit `.env`, `.cache/`, or exported chat data, and never print secrets to logs.
