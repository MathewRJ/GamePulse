@echo off
rem Windows launcher (cmd.exe and PowerShell).
rem All logic lives in gpx.py — this file exists so `gpx <cmd>` works on PATH.
rem Uses the Python launcher `py` if present (recommended on Windows),
rem falling back to `python` on PATH.
where /q py
if %ERRORLEVEL%==0 (
  py -3 "%~dp0gpx.py" %*
) else (
  python "%~dp0gpx.py" %*
)
