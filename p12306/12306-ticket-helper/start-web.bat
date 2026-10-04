@echo off
rem ============================================================
rem  12306 ticket helper - start the WEB UI (packaged build)
rem
rem  NOTE: this file is intentionally ASCII-only.
rem  cmd.exe reads .bat files using the OEM code page (GBK on a
rem  Chinese Windows). Chinese characters written as UTF-8 would
rem  be garbled, and a garbled path in a script is much worse than
rem  an English message. Keep it ASCII.
rem ============================================================
cd /d "%~dp0"

set "EXE=dist\TicketHelper\TicketHelper-CLI.exe"

if not exist "%EXE%" (
  echo [ERROR] Not found: %EXE%
  echo         Please run "python scripts\build_exe.py" first,
  echo         or start it from source: python main.py web
  pause
  exit /b 1
)

echo ============================================================
echo  12306 ticket helper - WEB UI
echo  URL   : http://127.0.0.1:8008
echo  Login : user/password are in  %%USERPROFILE%%\.ticket-helper\env.py
echo          (web.auth_user / web.auth_pass)
echo  Stop  : close this window
echo ============================================================
echo.

"%EXE%" web

echo.
echo [web server stopped]
pause
