@echo off
cd /d "%~dp0frontend"
echo Building frontend into frontend\dist ...
call npm install
call npm run build
echo.
echo Done. Backend will serve the built UI at http://127.0.0.1:8848
pause
