@echo off
powershell -NoProfile -Command "Start-Process pythonw -ArgumentList '\"%~dp0disk_scanner.py\"' -Verb RunAs"
