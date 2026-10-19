@echo off
chcp 65001 >nul
cd /d "%~dp0"
set NO_PROXY=127.0.0.1,localhost
set no_proxy=127.0.0.1,localhost

rem 可选第 1 个参数 = 监听地址。默认只绑本机（最安全）；
rem 要让局域网其他设备访问，用 start_backend_lan.bat（它只是传 0.0.0.0 调本脚本）。
set HOST=%~1
if "%HOST%"=="" set HOST=127.0.0.1

rem 优先用项目级 .venv（阶段B 起，含 langgraph）；缺失时回退到共享环境，再回退到 PATH
set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" set PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" set PY=python

echo Starting backend on http://%HOST%:8848 ...
echo Python: %PY%
if not "%HOST%"=="127.0.0.1" (
  echo.
  echo [警告] 已对局域网开放（%HOST%:8848）。请确认：
  echo        1^) .env 里已配置 API_PASSWORD 或 API_USERS（不要留空 / 不要 API_AUTH=off）
  echo        2^) 前面挂了 HTTPS 反代 —— Basic 口令走明文 HTTP 等于裸奔
  echo        3^) Windows 防火墙已按需放行 8848
  echo.
)
"%PY%" -m uvicorn backend.main:app --host %HOST% --port 8848
pause
