@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=..\venv\Scripts\python.exe
if not exist db.sqlite3 (
  %PY% manage.py migrate
  %PY% manage.py seed_data
)
echo Airline system: http://127.0.0.1:8095/admin/
echo Keep this window open. Close it to stop the server.
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8095/admin/"
%PY% manage.py runserver 8095
pause
