@echo off
REM Weekly dana list backup - run by Windows Task Scheduler.
REM Output is appended to backups\backup.log so a silent failure is still traceable.
cd /d "%~dp0"
echo. >> "backups\backup.log"
echo ===== %DATE% %TIME% ===== >> "backups\backup.log"
python backup_sheet.py >> "backups\backup.log" 2>&1
