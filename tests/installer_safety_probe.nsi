; Disposable fixture only. No registry writes, shortcuts or real application.
Unicode true
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!ifndef ROOT
!error "ROOT is required"
!endif
!ifndef FIXTURE
!error "FIXTURE is required"
!endif
!include "${ROOT}\packaging\installer-directory.nsh"
!include "${FIXTURE}\manifest.nsh"
Name "SparkAssistant installation safety fixture"
OutFile "${FIXTURE}\probe.exe"
RequestExecutionLevel user
SilentInstall silent
SilentUnInstall silent
AutoCloseWindow true
!insertmacro SparkVerifyInstallDirectory
!insertmacro SparkDirectoryGuards
!insertmacro SparkManifestFunctions

; Both install and uninstall are confined to this newly created fixture.
!macro CheckFixtureRoot PREFIX
Function ${PREFIX}.onInit
  System::Call 'kernel32::GetFullPathNameW(w "$INSTDIR", i ${NSIS_MAX_STRLEN}, w .r0, p 0) i .r1'
  StrCpy $INSTDIR $0
  ${GetParent} "$INSTDIR" $0
  ${GetParent} "$0" $1
  StrCmp $1 "${FIXTURE}\cases" fixture_ok
  SetErrorLevel 90
  Abort
fixture_ok:
FunctionEnd
!macroend
!insertmacro CheckFixtureRoot ""
!insertmacro CheckFixtureRoot "un"

Section
  Call SparkValidateDirectory
  StrCmp $SparkDirectoryError "" 0 rejected
  Call SparkValidatePackagedPaths
  StrCmp $SparkDirectoryError "" 0 rejected
  !insertmacro SparkInstallPackagedFiles
  FileOpen $0 "$INSTDIR\.spark-install" w
  FileWrite $0 "SparkAssistant"
  FileClose $0
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  SetErrorLevel 0
  Goto done
rejected:
  SetErrorLevel 2
  Abort
done:
SectionEnd

Section "Uninstall"
  Call un.SparkValidateDirectory
  StrCmp $SparkDirectoryError "" 0 un_rejected
  Call un.SparkRemovePackagedFiles
  Delete "$INSTDIR\.spark-install"
  Delete "$INSTDIR\Uninstall.exe"
  ; Only remove the now-empty dedicated target, never recursively.
  SetOutPath "$TEMP"
  RMDir "$INSTDIR"
  SetErrorLevel 0
  Goto un_done
un_rejected:
  SetErrorLevel 2
  Abort
un_done:
SectionEnd
