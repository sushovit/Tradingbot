' run_hidden.vbs — run a batch job with NO visible console window.
'
' Task Scheduler launching "cmd /c ..." pops a console every time (the
' watchdog alone fires every 15 minutes). Setting the tasks to S4U would
' also hide them but needs admin rights, which we do not have here.
' wscript has no console of its own, so Run(..., 0, True) starts the job
' fully hidden and waits for it, letting the task record a real exit code.
'
' Usage:  wscript.exe run_hidden.vbs "D:\TradingBot\jobs\watchdog.bat"

Option Explicit
Dim sh, target, rc

If WScript.Arguments.Count < 1 Then
    WScript.Quit 2
End If

target = WScript.Arguments(0)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\TradingBot"

' 0 = hidden window, True = wait so the exit code propagates to the task.
rc = sh.Run("""" & target & """", 0, True)
WScript.Quit rc
