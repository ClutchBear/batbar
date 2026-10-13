' agent-hidden.vbs —— 静默启动 batbar agent（无控制台窗口）。
' 被计划任务调用，也可手动双击。
Option Explicit
Dim sh, fso, cmd, root, p1, p2
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
' 取本脚本所在目录的上一级 = 项目根
root = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\") - 1)
root = Left(root, InStrRev(root, "\") - 1)
' venv 名字改过：早期是 .venv-win，现在统一 .venv。写死一个名字会在另一个
' 不存在时静默什么都不启动（2026-10-13 实测：本机只有 .venv，双击这个 vbs
' 毫无反应）——所以两个都找，最后回退 PATH 上的 pythonw。
p1 = root & "\.venv\Scripts\pythonw.exe"
p2 = root & "\.venv-win\Scripts\pythonw.exe"
If fso.FileExists(p1) Then
  cmd = """" & p1 & """ -m batbar.agent"
ElseIf fso.FileExists(p2) Then
  cmd = """" & p2 & """ -m batbar.agent"
Else
  cmd = "pythonw -m batbar.agent"
End If
sh.CurrentDirectory = root
sh.Run cmd, 0, False   ' 0 = 隐藏窗口, False = 不等待
Set fso = Nothing
Set sh = Nothing
