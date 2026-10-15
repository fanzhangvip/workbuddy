@echo off
cd /d "%~dp0"
set NO_PROXY=127.0.0.1,localhost
set no_proxy=127.0.0.1,localhost

rem 优先用项目级 .venv（阶段B 起，含 langgraph）；缺失时回退到共享环境，再回退到 PATH
set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" set PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" set PY=python

echo Starting backend on http://127.0.0.1:8848 ...
echo Python: %PY%
"%PY%" -m uvicorn backend.main:app --host 127.0.0.1 --port 8848
pause
