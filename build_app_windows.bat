@echo off
REM Build LIF2TIFF standalone executable for Windows
REM Requirements: Python 3.11, conda/miniconda installed
REM
REM Usage:
REM   1. Copy lif2tiff.py and gui_app.py to Windows machine
REM   2. Open Command Prompt or PowerShell
REM   3. Run: build_app_windows.bat

echo Creating conda environment...
call conda create -n lif2tiff python=3.11 -y
call conda activate lif2tiff

echo Installing dependencies...
pip install readlif tifffile numpy imagecodecs customtkinter pyinstaller

echo Building executable...
pyinstaller --onefile --windowed ^
  --name "LIF2TIFF" ^
  --hidden-import customtkinter ^
  --hidden-import readlif ^
  --hidden-import tifffile ^
  --hidden-import imagecodecs ^
  --hidden-import imagecodecs._imcd ^
  --collect-all imagecodecs ^
  --copy-metadata imagecodecs ^
  gui_app.py

echo.
echo Done! Executable: dist\LIF2TIFF.exe
echo.
pause
