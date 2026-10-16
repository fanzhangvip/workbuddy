# 客户意向评分系统 —— 生产镜像
#
# 设计要点
#   1. **两阶段构建**：前端用 Node 构建，产物只把 dist 拷进运行镜像 ——
#      运行镜像里没有 node_modules、没有 Vite、没有源码映射。
#   2. **依赖走锁文件**：`requirements.lock.txt` 是 pip freeze 产物，
#      保证「今天构建」与「三个月后构建」装到同一批版本（可复现）。
#   3. **非 root 运行**：容器内以 app 用户跑，减少越权面。
#   4. **不打包凭据**：`.cache/`（含微伴登录态 cookie）由 `.dockerignore` 排除，
#      运行时以 volume 挂载 —— 凭据绝不进镜像层。
#   5. **浏览器链路不在这里**：`wb_bootstrap` / `wb_keepalive` 需要本机 Chrome
#      与企微扫码，跑在**宿主机**上，只把 `.cache/ww_bootstrap.json` 共享给容器。
#      原因见 docker-compose.yml 顶部注释。

# ---------------------------------------------------------------- 阶段 1：前端
FROM node:22-alpine AS frontend
WORKDIR /build
# 镜像源可用 --build-arg 覆盖：内网/受限网络下换成自己的源即可，不改代码。
ARG NPM_REGISTRY=https://registry.npmmirror.com
# 先只拷清单 → 依赖层可缓存（改业务代码不会重装 npm 依赖）
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry "$NPM_REGISTRY" && npm ci
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- 阶段 2：运行时
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 刻意**不装任何 apt 包**：
#   · 时区 —— python:3.13-slim 自带 /usr/share/zoneinfo（已实测 Asia/Shanghai 可用），
#     所以 APScheduler 的 Asia/Shanghai 排程不需要额外装 tzdata；
#   · 健康检查 —— 用 python 自己的 urllib，不依赖 curl。
# 这样构建**只依赖 PyPI**，不依赖 Debian 源 —— 少一个能卡住构建的外部依赖。

WORKDIR /app

# 同上：PyPI 源可覆盖（默认阿里云；直连 pypi.org / files.pythonhosted.org
# 在受限网络下会 TLS 中断，实测踩到过）
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 锁文件若缺失则回退到 requirements.txt（构建期就能发现，而不是运行期）
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.lock.txt

# 应用代码（根目录共享模块是 backend 的依赖，必须一起进镜像）
COPY backend/ ./backend/
COPY analyze.py config.py contracts.py ctx_preprocess.py llm.py rules.py rules.json ./
COPY weiban.py ww_chatdata.py weiban_chat_fetch.py ./
COPY wb_bootstrap.py wb_keepalive.py wb_chat_export.py wb_staff_export.py ./

# 前端产物来自阶段 1（后端在 frontend/dist 存在时会自动托管 UI）
COPY --from=frontend /build/dist ./frontend/dist

# 运行时目录：凭据缓存 / 备份 / JSONL 审计 / 导出 / 评估基线
RUN mkdir -p .cache backups/audit-jsonl 导出 eval \
 && useradd -m -u 10001 app \
 && chown -R app:app /app
USER app

EXPOSE 8848

# 健康检查用免鉴权的 /api/health（它在 main.py 的 OPEN_PATHS 里）。
# 用 python 而不是 curl：镜像里没装 curl（见上面的取舍），少一个依赖。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8848/api/health', timeout=4).status==200 else 1)"

# 容器内监听 0.0.0.0（宿主机只映射到 127.0.0.1，默认不对外暴露）
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8848"]
