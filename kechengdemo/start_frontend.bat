@echo off
cd /d "%~dp0frontend"
echo Starting frontend dev server on http://127.0.0.1:5173 ...
call npm run dev
pause
