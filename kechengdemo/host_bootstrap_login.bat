@echo off
chcp 65001 >nul
cd /d "%~dp0"
set NO_PROXY=127.0.0.1,localhost
set no_proxy=127.0.0.1,localhost
rem 输出编码固定 utf-8：脚本会打印 ✅/❌ 等字符，GBK 控制台下会 UnicodeEncodeError
set PYTHONIOENCODING=utf-8

rem ============================================================
rem  宿主机专用：扫码登录 / 刷新微伴凭据
rem
rem  为什么必须在宿主机跑：容器里没有 Chrome、也没有显示器（BROWSER_ENABLED=0），
rem  所以 Web UI 上那个"弹出浏览器扫码登录"按钮在容器部署下**必然失败**。
rem  本脚本弹出 Chrome → 你扫码 → 凭据写入 .cache\ww_bootstrap.json →
rem  容器通过挂载的 .cache 目录**自动**读到（无需重启容器）。
rem ============================================================

set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" set PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe
if not exist "%PY%" set PY=python

echo 即将打开 Chrome（本项目专用 profile，不影响你其他窗口）。
echo 若未登录微伴，请在窗口里用「企业微信」扫码完成登录。
echo 凭据将写入 .cache\ww_bootstrap.json，容器会自动读到。
echo.
"%PY%" wb_bootstrap.py %*
echo.
pause
