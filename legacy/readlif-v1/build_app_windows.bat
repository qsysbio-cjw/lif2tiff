@echo off
REM Build LIF2TIFF standalone executable for Windows
REM Requirements: Miniconda or Anaconda installed
REM Run from the directory containing lif2tiff.py and gui_app.py

echo Creating conda environment...
call conda create -n lif2tiff python=3.11 -y

echo Installing dependencies...
call conda run -n lif2tiff pip install readlif tifffile numpy imagecodecs customtkinter pyinstaller

echo Building executable...
call conda run -n lif2tiff pyinstaller --onefile --windowed ^
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
