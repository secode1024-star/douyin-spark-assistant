; Exercises the production macro using only a caller-created unique test key.
Unicode true
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "${ROOT}\packaging\installer-autostart.nsh"
!ifndef FIXTURE
!error "FIXTURE is required"
!endif
!ifndef REGKEY
!error "Unique isolated REGKEY is required"
!endif
Name "SparkAssistant autostart ownership probe"
OutFile "${FIXTURE}\probe.exe"
RequestExecutionLevel user
SilentInstall silent
AutoCloseWindow true
Var TestCase
Var Original
Var ExpectedRemove
Var ActualMissing
Var Result

Function .onInit
  ${GetParameters} $0
  ${GetOptions} "$0" "/CASE=" $TestCase
  StrCpy $ExpectedRemove 0
  StrCpy $INSTDIR "C:\Spark AutostartProbe\SparkAssistant"
  ${If} $TestCase == "owned-quoted"
    StrCpy $Original '$\"$INSTDIR\SparkAssistant.exe$\" --tray'
    StrCpy $ExpectedRemove 1
  ${ElseIf} $TestCase == "owned-unquoted"
    StrCpy $INSTDIR "C:\SparkAutostartProbe\SparkAssistant"
    StrCpy $Original '$INSTDIR\SparkAssistant.exe --tray'
    StrCpy $ExpectedRemove 1
  ${ElseIf} $TestCase == "owned-case-insensitive"
    StrCpy $Original '$\"C:\SPARK AUTOSTARTPROBE\SPARKASSISTANT\SPARKASSISTANT.EXE$\" --tray'
    StrCpy $ExpectedRemove 1
  ${ElseIf} $TestCase == "other-installation"
    StrCpy $Original '$\"C:\Other Installation\SparkAssistant\SparkAssistant.exe$\" --tray'
  ${ElseIf} $TestCase == "source-version"
    StrCpy $Original '$\"C:\Python\pythonw.exe$\" $\"D:\Source\desktop_main.py$\" --tray'
  ${ElseIf} $TestCase == "unknown-arguments"
    StrCpy $Original '$\"$INSTDIR\SparkAssistant.exe$\" --tray --other'
  ${ElseIf} $TestCase == "ambiguous-unquoted-space"
    StrCpy $Original '$INSTDIR\SparkAssistant.exe --tray'
  ${ElseIf} $TestCase == "missing-value"
    StrCpy $ExpectedRemove 1
  ${Else}
    SetErrorLevel 90
    Abort
  ${EndIf}
FunctionEnd

Section
  ; Never touch the real Windows Run key. The runner validates this exact key.
  ClearErrors
  ${If} $TestCase != "missing-value"
    WriteRegStr HKCU "${REGKEY}" "SparkAssistant" "$Original"
  ${EndIf}
  WriteRegStr HKCU "${REGKEY}" "UnrelatedValue" "preserve"
  ${If} ${Errors}
    StrCpy $Result "FAIL: unable to write isolated registry fixture"
    Goto fixture_cleanup
  ${EndIf}
  !insertmacro SparkRemoveOwnedAutostart HKCU "${REGKEY}" "SparkAssistant"
  StrCpy $ActualMissing 0
  ClearErrors
  ReadRegStr $0 HKCU "${REGKEY}" "SparkAssistant"
  ${If} ${Errors}
    StrCpy $ActualMissing 1
  ${EndIf}
  StrCpy $Result "PASS"
  ${If} $ActualMissing != $ExpectedRemove
    StrCpy $Result "FAIL: ownership mismatch"
  ${ElseIf} $ExpectedRemove == 0
    ${If} $0 S!= $Original
      StrCpy $Result "FAIL: unrelated command changed"
    ${EndIf}
  ${EndIf}
  ReadRegStr $1 HKCU "${REGKEY}" "UnrelatedValue"
  ${If} $1 != "preserve"
    StrCpy $Result "FAIL: unrelated registry value changed"
  ${EndIf}
fixture_cleanup:
  ; These are the only two values created by this disposable probe.
  DeleteRegValue HKCU "${REGKEY}" "SparkAssistant"
  DeleteRegValue HKCU "${REGKEY}" "UnrelatedValue"
  DeleteRegKey /ifempty HKCU "${REGKEY}"
  FileOpen $0 "$EXEDIR\result-$TestCase.txt" w
  FileWrite $0 "$Result"
  FileClose $0
  ${If} $Result == "PASS"
    SetErrorLevel 0
  ${Else}
    SetErrorLevel 91
  ${EndIf}
SectionEnd
