#!/bin/bash
# Build LIF2TIFF standalone executable for Linux/Mac
# Run from /home/dionysus-cao/0309/ with the conda env active:
#   conda activate ./env && bash build_app.sh

set -e
cd "$(dirname "$0")"

echo "Installing dependencies..."
pip install pyinstaller customtkinter -q

echo "Building executable..."
pyinstaller --onefile --windowed \
  --name "LIF2TIFF" \
  --hidden-import customtkinter \
  --hidden-import readlif \
  --hidden-import tifffile \
  --hidden-import imagecodecs \
  --hidden-import imagecodecs._imcd \
  --collect-all imagecodecs \
  --copy-metadata imagecodecs \
  gui_app.py

echo ""
echo "Done. Executable: dist/LIF2TIFF"
echo "Size: $(du -h dist/LIF2TIFF | cut -f1)"
