@echo off
REM Сборка DdSTT.exe без консоли, с встроенной гифкой.
REM Запускать в папке проекта (там, где ddstt_tray.py и assets\DdSTT.gif)

pip install pyinstaller pywin32 pillow pystray

pyinstaller --noconfirm --onefile --windowed ^
    --name DdSTT ^
    --add-data "assets\DdSTT.gif;assets" ^
    ddstt_tray.py

echo.
echo Готово. Файл: dist\DdSTT.exe
pause
