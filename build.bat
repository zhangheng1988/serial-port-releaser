@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem  打包「串口释放助手」单文件 exe
rem  需要先安装： pip install pyinstaller
rem ------------------------------------------------------------------

set PY=
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not defined PY (
    where python >nul 2>nul && set PY=python
)
if not defined PY (
    where py >nul 2>nul && set PY=py
)
if not defined PY (
    echo 找不到 python，请先安装 Python 3.8+ 并加入 PATH。
    pause
    exit /b 1
)

echo 使用解释器：%PY%
"%PY%" -c "import PyInstaller" 2>nul || (
    echo 缺少 PyInstaller，正在安装...
    "%PY%" -m pip install pyinstaller || ( echo 安装失败 & pause & exit /b 1 )
)

echo.
echo 正在打包 ...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name SerialRelease --icon app.ico --version-file version.txt ^
  --add-data "app.ico;." ^
  --exclude-module numpy --exclude-module pandas --exclude-module matplotlib ^
  --exclude-module scipy --exclude-module PIL --exclude-module PyQt5 ^
  --exclude-module pytest --exclude-module setuptools ^
  --distpath dist --workpath build serial_release.py

if errorlevel 1 ( echo 打包失败 & pause & exit /b 1 )

if exist "dist\串口释放助手.exe" del "dist\串口释放助手.exe"
if exist "dist\SerialRelease.exe" ren "dist\SerialRelease.exe" "串口释放助手.exe"
rd /s /q build 2>nul
del SerialRelease.spec 2>nul

echo.
echo 打包完成：dist\串口释放助手.exe
pause
