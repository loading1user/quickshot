@echo off
cd /d "%~dp0"
rem shot.raw — временный снимок, в сборку не нужен
del /q ui\shot.raw 2>nul
python -m PyInstaller --noconfirm --windowed --name QuickShot --icon icon.ico --add-data "ui;ui" --add-data "icon.ico;." quickshot.py
