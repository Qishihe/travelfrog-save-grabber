Option Explicit
' 旅行青蛙存档抓取器 · 启动器
' 作用：隐藏控制台窗口启动本地服务，并自动打开浏览器界面。
Dim fso, sh, baseDir, py, appFile, logDir, cmdLine
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
py      = baseDir & "\runtime\python\python.exe"
appFile = baseDir & "\app.py"
logDir  = baseDir & "\logs"

If Not fso.FileExists(py) Then
  MsgBox "找不到内置运行环境：" & vbCrLf & py & vbCrLf & vbCrLf & "程序目录不完整，请重新安装。", 16, "启动失败"
  WScript.Quit 1
End If

If Not fso.FileExists(appFile) Then
  MsgBox "找不到主程序 app.py，程序目录不完整，请重新安装。", 16, "启动失败"
  WScript.Quit 1
End If

If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)

sh.CurrentDirectory = baseDir

' 0 = 隐藏窗口；输出重定向到 logs\app.log，便于排查启动异常
cmdLine = "cmd /c cd /d """ & baseDir & """ && """ & py & """ """ & appFile & """ >> """ & logDir & "\app.log"" 2>&1"
sh.Run cmdLine, 0, False
