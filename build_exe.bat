@echo off
REM Rode este arquivo no WINDOWS, na mesma pasta do launcher.py, baixador_youtube.py e icon.ico
pip install -U pyinstaller yt-dlp mutagen pillow customtkinter
pyinstaller --noconsole --onefile --name BaixadorYT --icon icon.ico --collect-all yt_dlp --collect-all customtkinter --add-data "baixador_youtube.py;." launcher.py
echo.
echo Pronto! O programa esta em dist\BaixadorYT.exe
pause
