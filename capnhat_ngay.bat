@echo off
cd /d D:\files
if not exist output mkdir output
echo ===== %date% %time% ===== >> output\log_daily.txt
python main.py daily >> output\log_daily.txt 2>&1
