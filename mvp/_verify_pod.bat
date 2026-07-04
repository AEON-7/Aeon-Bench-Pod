@echo off
set AEON_ROLE=pod
set AEON_DB=%TEMP%\aeon_localpod.db
set AEON_PORT=8097
set AEON_HOST=127.0.0.1
set AEON_MOTHERSHIP=http://127.0.0.1:8090
set AEON_DB_URL=
cd /d "C:\Users\Albert\AEON Bench\mvp"
"C:\Users\Albert\AppData\Local\Programs\Python\Python314\python.exe" serve.py
