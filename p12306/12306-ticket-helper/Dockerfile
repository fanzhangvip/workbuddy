# 12306 抢票助手 · 容器镜像（CF-05：一套镜像多环境运行）
#
# 构建：docker build -t ticket-helper:1.0 .
# 运行：docker run -d --name th -p 8008:8008 \
#         -v ticket-helper-data:/data \
#         -e TICKET_HELPER_HOME=/data \
#         -e TH_WEB_HOST=0.0.0.0 \
#         -e TH_WEB_AUTH_PASS=请改成强密码 \
#         ticket-helper:1.0
#
# 说明：
# - 不把任何密钥写进镜像，敏感配置一律用环境变量注入（TH_* 前缀）；
# - 数据卷挂到 /data，容器重建不丢配置/订单/日志。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TICKET_HELPER_HOME=/data \
    TH_LOG_LEVEL=INFO

WORKDIR /app

# 先装依赖，利用 Docker 层缓存加速后续构建
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 运行期数据目录（会被数据卷覆盖）
RUN mkdir -p /data/data /data/logs /data/runtime \
    && useradd --create-home --shell /bin/bash runner \
    && chown -R runner:runner /data /app
USER runner

EXPOSE 8008

# 健康检查直接打 /health（无需鉴权，供容器探针使用）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8008/health', timeout=3).status==200 else 1)"

CMD ["python", "main.py", "web", "--host", "0.0.0.0", "--port", "8008"]
