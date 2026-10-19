@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  局域网访问版：只把监听地址换成 0.0.0.0，其余逻辑全部复用 start_backend.bat
rem
rem  ⚠️ 为什么不是复制一份 uvicorn 命令：那样"换 Python 路径 / 加启动参数"要改两处，
rem     漏一处就会出现"本机能起、局域网版起不来"。批处理不能 import，
rem     所以**参数化**是唯一不复制实现的办法（AGENTS.md 的 Reuse Before Rewrite）。
rem
rem  ⚠️ 对局域网开放前必须做两件事：
rem     1) .env 配 API_PASSWORD 或 API_USERS（Basic/会话鉴权），不要 API_AUTH=off；
rem     2) 前面挂 HTTPS 反代（Caddy/nginx）—— 明文 HTTP 上口令会被抓包。
rem ============================================================

echo.
echo [LAN] 将以 0.0.0.0:8848 启动 —— 同一局域网内的设备都能访问。
echo [LAN] 启动前请确认已配置 API_PASSWORD/API_USERS 且前置 HTTPS 反代。
echo.

call "%~dp0start_backend.bat" 0.0.0.0
