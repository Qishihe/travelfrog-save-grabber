@echo off
chcp 936 >nul
setlocal EnableExtensions
set "SILENT="
if /i "%~1"=="/S" set "SILENT=1"
title 旅行青蛙存档抓取器 · 安装

set "SRC=%~dp0"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
set "DEST=%LOCALAPPDATA%\TravelFrogGrabber"

echo ============================================================
echo   旅行青蛙 · 存档抓取器   安装程序
echo ============================================================
echo.
echo   安装到：%DEST%
echo.

if not exist "%SRC%\runtime\python\python.exe" (
  echo [X] 当前目录不是完整的安装包（缺 runtime\python）。
  echo     请先把压缩包完整解压，再运行本安装程序。
  echo.
  if not defined SILENT pause
  exit /b 1
)

echo [1/4] 检查是否已有程序在运行...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*TravelFrogGrabber*app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

echo [2/4] 复制程序文件（首次安装约需十几秒）...
if not exist "%DEST%" mkdir "%DEST%"
robocopy "%SRC%" "%DEST%" /E /XD output logs __pycache__ .git /XF *.pyc /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo.
  echo [X] 复制文件失败。请确认抓取器已关闭，或手动删除 %DEST% 后重试。
  if not defined SILENT pause
  exit /b 1
)

rem 抓取需要底稿：单独补进 output（历史抓取结果不打包）
if not exist "%DEST%\output" mkdir "%DEST%\output"
set "BASEFILE=%SRC%\output\模板文件.json"
if exist "%BASEFILE%" (
  copy /y "%BASEFILE%" "%DEST%\output\" >nul
) else (
  echo [!] 警告：缺少底稿 模板文件.json，抓取可能失败。
)

echo [3/4] 创建桌面与开始菜单快捷方式...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SRC%\_make_shortcut.ps1" -Dest "%DEST%" >nul
if errorlevel 1 (
  echo [!] 快捷方式创建失败，可从 %DEST%\launch.vbs 手动启动。
)

echo [4/4] 登记卸载信息（可在「设置 - 应用」中卸载）...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SRC%\_write_uninstall.ps1" -Dest "%DEST%" >nul
if errorlevel 1 echo [!] 卸载信息登记失败，不影响正常使用（可在程序目录里运行 卸载.bat）。

echo.
echo ============================================================
echo   安装完成！
echo.
echo   桌面和开始菜单已生成「旅行青蛙抓取器」快捷方式。
echo   双击即可打开界面（会自动打开浏览器）。
echo ============================================================
echo.
if defined SILENT goto :end
choice /c YN /n /m "现在打开抓取器吗？(Y/N) "
if errorlevel 2 goto :end
start "" "%DEST%\launch.vbs"

:end
echo.
if not defined SILENT pause
exit /b 0
