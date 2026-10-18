Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!ifndef VERSION
!error "Build with scripts/build-windows.ps1 or supply /DVERSION from spark_assistant.__version__"
!endif
!ifndef ROOT
!define ROOT ".."
!endif
Name "火花助手"
OutFile "${ROOT}\release\SparkAssistant-Setup-${VERSION}-windows-x64.exe"
InstallDir "$LOCALAPPDATA\Programs\SparkAssistant"
InstallDirRegKey HKCU "Software\SparkAssistant" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID zlib
Icon "${ROOT}\assets\icon.ico"
UninstallIcon "${ROOT}\assets\icon.ico"
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${ROOT}\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

!macro RequireStopped
  System::Call 'kernel32::OpenMutexW(i 0x00100000, i 0, w "Local\SparkAssistant.Desktop") p .r0'
  ${If} $0 != 0
    System::Call 'kernel32::CloseHandle(p r0)'
    MessageBox MB_OK|MB_ICONEXCLAMATION "火花助手仍在运行。请在系统托盘菜单选择退出后，再继续安装或卸载。" /SD IDOK
    Abort
  ${EndIf}
!macroend

Function .onInit
  !insertmacro RequireStopped
FunctionEnd

!include "${ROOT}\packaging\installer-directory.nsh"
!include "${ROOT}\packaging\installer-autostart.nsh"
!insertmacro SparkVerifyInstallDirectory
!insertmacro SparkDirectoryGuards
!include "${ROOT}\build\windows-uninstall-manifest.nsh"
!insertmacro SparkManifestFunctions

Function un.onInit
  !insertmacro RequireStopped
  Call un.SparkValidateDirectory
  ${If} $SparkDirectoryError != ""
    MessageBox MB_OK|MB_ICONEXCLAMATION "$SparkDirectoryError" /SD IDOK
    SetErrorLevel 2
    Abort
  ${EndIf}
FunctionEnd

Section "火花助手" SEC_MAIN
  ; Always validate here: /S and /D must not bypass the directory contract.
  Call SparkValidateDirectory
  ${If} $SparkDirectoryError == ""
    Call SparkValidatePackagedPaths
  ${EndIf}
  ${If} $SparkDirectoryError != ""
    MessageBox MB_OK|MB_ICONEXCLAMATION "$SparkDirectoryError" /SD IDOK
    SetErrorLevel 2
    Abort
  ${EndIf}
  SetShellVarContext current
  ClearErrors
  !insertmacro SparkInstallPackagedFiles
  IfErrors spark_install_failed
  ClearErrors
  FileOpen $0 "$INSTDIR\.spark-install" w
  IfErrors spark_install_failed
  FileWrite $0 "SparkAssistant"
  FileClose $0
  IfErrors spark_install_failed
  ClearErrors
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  IfErrors spark_install_failed
  CreateDirectory "$SMPROGRAMS\火花助手"
  CreateShortcut "$SMPROGRAMS\火花助手\火花助手.lnk" "$INSTDIR\SparkAssistant.exe"
  CreateShortcut "$DESKTOP\火花助手.lnk" "$INSTDIR\SparkAssistant.exe"
  WriteRegStr HKCU "Software\SparkAssistant" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\SparkAssistant" "DisplayName" "火花助手"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\SparkAssistant" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\SparkAssistant" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\SparkAssistant" "DisplayIcon" "$INSTDIR\SparkAssistant.exe"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\SparkAssistant" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\SparkAssistant" "NoRepair" 1
  Goto spark_install_done
spark_install_failed:
  MessageBox MB_OK|MB_ICONSTOP "安装文件或安装标记写入失败，请检查磁盘空间和目录权限。" /SD IDOK
  SetErrorLevel 3
  Abort
spark_install_done:
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  !insertmacro SparkRemoveOwnedAutostart HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "SparkAssistant"
  Delete "$DESKTOP\火花助手.lnk"
  Delete "$SMPROGRAMS\火花助手\火花助手.lnk"
  RMDir "$SMPROGRAMS\火花助手"
  Call un.SparkRemovePackagedFiles
  Delete "$INSTDIR\.spark-install"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\SparkAssistant"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\SparkAssistant"
  ; Private state is preserved. Users can clear it in the app before uninstalling.
SectionEnd
