@echo off
rem Double-click to open Clawd in its own window (no console). Runs setup the first time.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" call setup.bat
start "" ".venv\Scripts\pythonw.exe" server.py
