; Does not install files, create target directories or modify the registry.
; Each invocation calls the real installer callback, records its outcome next
; to this disposable probe executable, and exits without showing a window.
Unicode true
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!ifndef ROOT
!define ROOT ".."
!endif
!include "${ROOT}\packaging\installer-directory.nsh"
Name "SparkAssistant directory validation probe"
OutFile "${ROOT}\.local\installer-directory-probe\installer-directory-probe.exe"
RequestExecutionLevel user
SilentInstall silent
AutoCloseWindow true

Var TestCase
Var Expected
Var OriginalDir
Var OriginalRegister
Var CallbackReturned
Var ProbeResult
Var Actual

!insertmacro SparkVerifyInstallDirectory

Function .onInit
  ${GetParameters} $1
  ${GetOptions} "$1" "/CASE=" $TestCase
  StrCpy $Expected "valid"
  StrCpy $CallbackReturned "0"
  ${If} $TestCase == "valid-new"
    StrCpy $INSTDIR "$EXEDIR\never-created\SparkAssistant"
  ${ElseIf} $TestCase == "valid-default"
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\SparkAssistant"
  ${ElseIf} $TestCase == "valid-trailing"
    StrCpy $INSTDIR "$EXEDIR\never-created\SparkAssistant\"
  ${ElseIf} $TestCase == "valid-multiple-trailing"
    StrCpy $INSTDIR "$EXEDIR\never-created\SparkAssistant\\"
  ${ElseIf} $TestCase == "valid-case"
    StrCpy $INSTDIR "$EXEDIR\never-created\sparkassistant"
  ${ElseIf} $TestCase == "valid-spaces-unicode"
    StrCpy $INSTDIR "$EXEDIR\never-created\含 空格的目录\SparkAssistant"
  ${ElseIf} $TestCase == "invalid-other"
    StrCpy $INSTDIR "$EXEDIR\never-created\Other"
    StrCpy $Expected "invalid"
  ${ElseIf} $TestCase == "invalid-root"
    StrCpy $INSTDIR "C:\"
    StrCpy $Expected "invalid"
  ${ElseIf} $TestCase == "invalid-parent"
    StrCpy $INSTDIR "$EXEDIR\never-created\SparkAssistant\.."
    StrCpy $Expected "invalid"
  ${ElseIf} $TestCase == "invalid-empty"
    StrCpy $INSTDIR ""
    StrCpy $Expected "invalid"
  ${Else}
    SetErrorLevel 90
    Quit
  ${EndIf}
  StrCpy $OriginalDir "$INSTDIR"
  StrCpy $0 "register-zero-sentinel"
  StrCpy $OriginalRegister "$0"
FunctionEnd

Section
  ; Abort propagates to .onInstFailed, so both callback exits are tested.
  Call .onVerifyInstDir
  StrCpy $CallbackReturned "1"
SectionEnd

Function .onInstSuccess
  StrCpy $Actual "valid"
  Call CheckResult
FunctionEnd

Function .onInstFailed
  StrCpy $Actual "invalid"
  Call CheckResult
FunctionEnd

Function CheckResult
  StrCpy $ProbeResult "PASS"
  ${If} $Actual != $Expected
    StrCpy $ProbeResult "FAIL: callback accepted/rejected the wrong path"
  ${ElseIf} $INSTDIR != $OriginalDir
    StrCpy $ProbeResult "FAIL: callback modified INSTDIR"
  ${ElseIf} $0 != $OriginalRegister
    StrCpy $ProbeResult "FAIL: callback did not restore register zero"
  ${EndIf}
  FileOpen $9 "$EXEDIR\result-$TestCase.txt" w
  IfErrors write_failed
  FileWrite $9 "case=$TestCase$\r$\nexpected=$Expected$\r$\nactual=$Actual$\r$\ncallback-returned=$CallbackReturned$\r$\nresult=$ProbeResult$\r$\n"
  FileClose $9
  ${If} $ProbeResult == "PASS"
    SetErrorLevel 0
  ${Else}
    SetErrorLevel 91
  ${EndIf}
  Return
write_failed:
  SetErrorLevel 92
FunctionEnd
