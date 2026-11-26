@echo off
chcp 936 >nul
title 旅行青蛙存档抓取器 · 卸载
set "DEST=%LOCALAPPDATA%\TravelFrogGrabber"

rem 若已在 TEMP 中运行的副本，直接进入清理流程
if /i "%~dp0"=="%TEMP%\" goto :run
if /i "%~dp0"=="%TEMP%" goto :run

echo ============================================================
echo   卸载 旅行青蛙存档抓取器
echo ============================================================
echo.
echo   程序位置：%DEST%
echo   注意：卸载只删除程序本体，不会动你另外保存的存档文件。
echo.
choice /c YN /n /m "确认卸载？(Y/N) "
if errorlevel 2 exit /b 0

rem 复制自身到 TEMP 再运行，避免「删除自己所在目录」失败
copy /y "%~f0" "%TEMP%\_tfg_uninstall.bat" >nul
start "" /min cmd /c ""%TEMP%\_tfg_uninstall.bat""
exit /b 0

:run
echo.
echo [1/3] 停止正在运行的程序...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*TravelFrogGrabber*app.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/3] 删除快捷方式与注册表项...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); Remove-Item (Join-Path $d '旅行青蛙抓取器.lnk') -Force -ErrorAction SilentlyContinue; $s=Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'; Remove-Item (Join-Path $s '旅行青蛙抓取器.lnk') -Force -ErrorAction SilentlyContinue; Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TravelFrogGrabber' -Recurse -Force -ErrorAction SilentlyContinue" >nul 2>&1

echo [3/3] 删除程序文件...
timeout /t 1 /nobreak >nul
rmdir /s /q "%DEST%" >nul 2>&1
timeout /t 1 /nobreak >nul
if exist "%DEST%" rmdir /s /q "%DEST%" >nul 2>&1

del /q "%TEMP%\_tfg_uninstall.bat" >nul 2>&1

echo.
echo ============================================================
if exist "%DEST%" (
  echo   卸载完成，但有少量文件残留（可能正在被占用）。
  echo   请重启电脑后手动删除：%DEST%
) else (
  echo   卸载完成，已全部清理干净。
)
echo ============================================================
timeout /t 5 >nul
exit /b 0
