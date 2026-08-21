' Fridge Manager - start the server WITHOUT a console window.
' Put a shortcut to this file into the Startup folder (shell:startup)
' to launch the app automatically at Windows sign-in.
' The browser will not open automatically in this mode; open the
' bookmark http://localhost:8341/ instead. Stop with stop.bat.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = appDir
Set env = sh.Environment("PROCESS")
env("FRIDGE_NO_BROWSER") = "1"
sh.Run """" & appDir & "\.venv\Scripts\python.exe"" """ & appDir & "\app.py""", 0, False
