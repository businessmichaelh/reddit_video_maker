' Silently launches the Reddit Story Video Maker GUI with no console window.
Set objShell = CreateObject("WScript.Shell")
scriptDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
pythonw = "C:\Users\Dark3\AppData\Local\Programs\Python\Python312\pythonw.exe"
objShell.CurrentDirectory = scriptDir
objShell.Run """" & pythonw & """ """ & scriptDir & "\gui.py""", 0, False
