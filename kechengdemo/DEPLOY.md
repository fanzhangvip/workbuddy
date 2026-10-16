# 部署手册（Docker 全栈）

> 一键起：`docker compose up -d --build`
> 带 HTTPS：`docker compose --profile https up -d`
> 带定时备份：`docker compose --profile ops up -d`
> 完整（推荐）：`docker compose --profile https --profile ops up -d --build`

全部组件都在容器里：**PostgreSQL + 应用（API / 调度器 / 前端静态托管） + HTTPS 反代 + 定时备份**。

---

## 1. 架构与容器

| 容器 | 作用 | 端口 | 必需 |
|---|---|---|---|
| `weiban-chat-pg` | PostgreSQL 16（pgvector 镜像） | 仅宿主 `127.0.0.1:5433`（排障用） | ✅ |
| `weiban-scoring-app` | FastAPI + APScheduler + 托管前端 dist | 仅宿主 `127.0.0.1:8848` | ✅ |
| `weiban-scoring-proxy` | Caddy，TLS 终结（`tls internal` 自签内部 CA） | 仅宿主 `127.0.0.1:8443` | 多人访问时必须 |
| `weiban-scoring-backup` | 定时 `pg_dump` + **校验** + 按保留期清理 | — | 建议 |

**默认只绑 `127.0.0.1`**：本机单人用时口令不跨网络，明文 HTTP 也可接受；
一旦要给别人用，请起 `https` profile（Basic Auth 在明文 HTTP 下口令会裸奔）。

---

## 2. ⚠️ 一个必须知道的边界：浏览器凭据链路在宿主机

`wb_bootstrap` / `wb_keepalive` 需要**本机 Chrome + 企业微信扫码**
（微伴后台没有账号密码登录，只有企微授权一种方式）。容器里既没有 Chrome 也没有显示器，
所以这一步**必然跑在宿主机**：

```powershell
# 首次：扫码建立登录态（产出 .cache/ww_bootstrap.json）
python wb_bootstrap.py
# 之后：定期保活（登录态是滑动过期，7 天窗口；项目内已排了定时保活）
python wb_keepalive.py
```

容器通过 volume 读**同一个** `.cache/`，之后的同步 / 解密 / 评分 / 回写全是纯 HTTP，全在容器内完成。
这是外部约束（对方只有扫码登录），不是本项目缺陷。

---

## 3. 从零部署一台新机器

**前置**：Windows / Docker Desktop / Chrome（宿主机）/ 项目里的 `.venv`（或 Python 3.13）

| 步骤 | 命令 | 说明 |
|---|---|---|
| 1 | 复制仓库到新机器 | `.env` 不在仓库里（正确），需单独取 |
| 2 | 建 `.env` | 照 `.env.example` 填 `corp_id` `secret` `LLM_API_KEY` `POSTGRES_PASSWORD` |
| 3 | 拉基础镜像（受限网络） | `docker pull docker.m.daocloud.io/library/python:3.13-slim` 等，见 §6 |
| 4 | `docker compose up -d --build` | 首次会建库（空库，`init_db()` 自动建 27 张表） |
| 5 | `python wb_bootstrap.py` | 宿主机扫码，产出 `.cache/ww_bootstrap.json` |
| 6 | 打开 `http://127.0.0.1:8848` | 浏览器会弹 Basic 登录框；用户名 `admin`，口令见 `.env` 的 `API_PASSWORD` |
| 7 | 同步数据 | 前端「同步中心」点一次全量，或 `POST /api/sync/start` |

**换机不需要改代码**：`CHROME_PATH` / `BROWSER_PROFILE_DIR` / `CDP_PORT` / `DATABASE_URL` /
`POSTGRES_PASSWORD` / `API_USERS` 全部走环境变量（见 `.env.example`）。

---

## 4. 日常运维

```powershell
docker compose ps                      # 看状态（app 应为 healthy）
docker compose logs -f app             # 看应用日志
docker compose logs -f backup          # 看备份日志
docker compose restart app             # 重启应用（数据卷不受影响）
docker compose down                    # 停（**不要加 -v**，加了会删数据卷）
```

**⚠️ 改了代码只 `restart` 不会生效** —— 代码是打进镜像的，必须**重新构建**：

```powershell
docker compose --profile https --profile ops up -d --build
# 确认容器跑的确实是新代码（后端逐文件比 SHA256 + 前端产物比文件名）
.\scripts\verify_image_matches_worktree.ps1
```

**重建会重启 app**，正在跑的同步/评分会被打断。这对数据是安全的（同步是增量的，
从水位继续；被打断的行会由心跳僵尸规则在 15 分钟内回收、状态写明原因），
但如果想更干净，可以先优雅停：

```powershell
# 后台配置的「日常任务」（同步中心 → 日常任务，默认 20:00 主同步 / 03:30 补偿同步）
# 到点时才会自动跑；要重建就避开这两个时刻，或先手动停：
curl -u admin:<API_PASSWORD> -X POST http://127.0.0.1:8848/api/sync/stop
```

**备份与恢复**

```powershell
# 自动：backup 容器按 BACKUP_INTERVAL_HOURS 定时产出到 backups/，并自动校验
ls backups\*.dump
# 手动恢复（脚本会先做"恢复前快照"，再覆盖）
.\scripts\restore.sh backups\weiban_chat-YYYYmmdd-HHMMSS.dump   # WSL/Git Bash
```

---

## 5. 权限（角色）

**怎么登录**：浏览器打开首页 → 应用自己的**登录页**（用户名 + 口令 + 「记住我」）。
不再是浏览器那个原生弹框了 —— 勾了「记住我」，这台机器 30 天内免登录；
不勾则关掉浏览器就要重登（服务端也会在 12 小时后过期）。口令**不会**存在浏览器里，
浏览器只留一个 HttpOnly 的会话 id；顶栏右侧能看到当前账号与「退出」。

**脚本 / curl 不必登录**：HTTP Basic 照样可用（两种方式并存）：

```powershell
curl -u admin:<API_PASSWORD> http://127.0.0.1:8848/api/sync/schedule
```

口令在部署机 `.env` 的 `API_PASSWORD`（首次为空时自动生成并打印在启动日志）。
想看「现在谁在用」，可查表：`SELECT username, last_seen_at, client_ip FROM login_session;`

单账号即 `admin`。要分权，在 `.env` 里配 `API_USERS=user:pwd:role,...`：

| 角色 | 能做什么 |
|---|---|
| `viewer` | 只读 |
| `reviewer` | 提交人工复核、标注评估集 |
| `operator` | 发起任务、改运行时设置、执行需确认的回写 |
| `admin` | 全部 + 审计导出 / 数据清理 / 规则发布 / 建标签 |

共 **26 个**危险接口挂了角色守卫。格式写错的条目会被**跳过**（不会静默提权成 admin），
启动日志会打印解析出的账号与角色。

---

## 6. 受限网络下的构建（实测踩过）

本机 Docker Hub / Debian 源 / `files.pythonhosted.org` 都不可直连。三个应对：

```powershell
# ① 基础镜像走可达镜像源，然后重命名成本地名（Dockerfile 不用改）
docker pull docker.m.daocloud.io/library/python:3.13-slim
docker tag  docker.m.daocloud.io/library/python:3.13-slim python:3.13-slim
# node:22-alpine / caddy:2-alpine / postgres:16-alpine 同理

# ② pip / npm 源已是可覆盖的 build-arg（默认阿里云 PyPI + npmmirror）
docker compose build app
# 或指定自己的源：
docker compose build --build-arg PIP_INDEX_URL=https://自己的镜像/simple/ app
```

**为什么镜像里不装 apt 包**：`python:3.13-slim` 自带 `/usr/share/zoneinfo`
（已实测 `Asia/Shanghai` 可用），健康检查用 python 的 urllib 而不是 curl ——
于是构建**只依赖 PyPI**，不依赖 Debian 源。少一个能卡住构建的外部依赖。

---

## 7. 已知限制（如实列出）

| 限制 | 说明 |
|---|---|
| Windows-only | `.bat` 启动脚本、`taskkill`、`%USERPROFILE%` 是 Windows 语义；容器本身是 Linux，但宿主机侧的凭据链路仍是 Windows |
| 单机单实例 | 无高可用；`pg_advisory_lock`/任务抢占未实现，多实例会抢同一批任务 |
| 凭据链路脆弱 | 微伴登录态靠浏览器保活；失效需人工重新扫码 |
| 回写不可逆 | 微伴「客户评分」只能累加、无覆盖接口，写错只能靠差值水位补偿 |
| 评估集尚未人工标注 | 30 条样本中 27 条是 AI 预标注、**人工确认为 0** → 回归门禁目前算不出指标（见 ARCHITECTURE §11.1） |
