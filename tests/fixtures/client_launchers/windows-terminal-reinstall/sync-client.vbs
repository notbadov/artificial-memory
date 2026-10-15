Option Explicit
Dim shell, result
Set shell = CreateObject("WScript.Shell")
result = shell.Run("powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{ROOT}/home of O'Neil & Сын/.agent-platform/client/sync-client.ps1""", 0, True)
WScript.Quit result
