' zh2en silent launcher - starts with no console window
' Quit: press Ctrl+Alt+Q
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

pyw = "C:\Windows\pyw.exe"
If fso.FileExists(pyw) Then
  sh.Run """" & pyw & """ """ & dir & "\zh2en.py""", 0, False
ElseIf fso.FileExists("C:\Program Files\Python312\pythonw.exe") Then
  sh.Run """C:\Program Files\Python312\pythonw.exe"" """ & dir & "\zh2en.py""", 0, False
ElseIf fso.FileExists("C:\Program Files\Python311\pythonw.exe") Then
  sh.Run """C:\Program Files\Python311\pythonw.exe"" """ & dir & "\zh2en.py""", 0, False
Else
  sh.Run "pythonw """ & dir & "\zh2en.py""", 0, False
End If
