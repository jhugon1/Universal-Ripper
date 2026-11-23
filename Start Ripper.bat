@echo off
title Ripped Ripper - close this window to quit
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First start: setting everything up, this takes a minute or two...
  call "%~dp0Setup.bat"
  if not exist ".venv\Scripts\python.exe" exit /b 1
)
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 (
  echo.
  echo The Ripper stopped with an error. Run Setup.bat again, or see the README.
  pause
)
