!ifndef SPARK_INSTALLER_DIRECTORY_NSH
!define SPARK_INSTALLER_DIRECTORY_NSH
!include "FileFunc.nsh"
!include "LogicLib.nsh"

; Keep this callback shared with the lightweight regression probe. NSIS's
; GetFullPathName may reject a directory that has not been created yet, so do
; not use it here and never modify the user's $INSTDIR during live validation.
; GetFileName operates on strings and ignores trailing backslashes. The native
; directory page still validates the drive, root-directory policy and free space.
!macro SparkVerifyInstallDirectory
Function .onVerifyInstDir
  Push $0
  ${GetFileName} "$INSTDIR" $0
  StrCmp $0 "SparkAssistant" spark_directory_valid
  Pop $0
  Abort
spark_directory_valid:
  Pop $0
FunctionEnd
!macroend

; The directory-page callback is deliberately only a live UI check. Silent
; installations skip it; these guards must also run before any installation IO.
!macro SparkPathGuardFunction PREFIX
Function ${PREFIX}SparkPathHasReparsePoint
  Exch $0
  Push $1
  Push $2
  StrCpy $2 0
spark_path_loop:
  System::Call 'kernel32::GetFileAttributesW(w r0) i .r1'
  IntCmp $1 -1 spark_path_parent
  IntOp $1 $1 & 0x400
  IntCmp $1 0 spark_path_parent
  StrCpy $2 1
  Goto spark_path_done
spark_path_parent:
  ${GetParent} "$0" $1
  StrCmp $1 "" spark_path_done
  StrCmp $1 $0 spark_path_done
  StrCpy $0 $1
  Goto spark_path_loop
spark_path_done:
  StrCpy $0 $2
  Pop $2
  Pop $1
  Exch $0
FunctionEnd
!macroend

!macro SparkDirectoryGuardFunction PREFIX REQUIRE_MARKER
Function ${PREFIX}SparkValidateDirectory
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  StrCpy $SparkDirectoryError ""
  ${GetFileName} "$INSTDIR" $0
  StrCmp $0 "SparkAssistant" 0 spark_directory_bad_name
  ; Use Win32's lexical canonicalization: unlike the NSIS instruction it does
  ; not require the final directory to exist. Do not call this from the page.
  System::Call 'kernel32::GetFullPathNameW(w "$INSTDIR", i ${NSIS_MAX_STRLEN}, w .r0, p 0) i .r1'
  IntCmp $1 0 spark_directory_bad_name
  IntCmp $1 ${NSIS_MAX_STRLEN} spark_directory_bad_name 0 spark_directory_bad_name
  ${GetFileName} "$0" $1
  StrCmp $1 "SparkAssistant" 0 spark_directory_bad_name
  StrCpy $INSTDIR $0
  Push "$INSTDIR"
  Call ${PREFIX}SparkPathHasReparsePoint
  Pop $0
  StrCmp $0 0 0 spark_directory_link
  System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR") i .r0'
  IntCmp $0 -1 spark_directory_missing
  IntOp $0 $0 & 0x10
  IntCmp $0 0 spark_directory_unknown
  IfFileExists "$INSTDIR\.spark-install" spark_directory_marker
!if ${REQUIRE_MARKER} == 1
  Goto spark_directory_unknown
!else
  ; Only a genuinely empty directory may be claimed by a new installation.
  ClearErrors
  FindFirst $3 $4 "$INSTDIR\*"
  IfErrors spark_directory_unknown
spark_directory_scan:
  StrCmp $4 "" spark_directory_empty
  StrCmp $4 "." spark_directory_next
  StrCmp $4 ".." spark_directory_next
  FindClose $3
  Goto spark_directory_unknown
spark_directory_next:
  ClearErrors
  FindNext $3 $4
  IfErrors spark_directory_empty
  Goto spark_directory_scan
spark_directory_empty:
  FindClose $3
  Goto spark_directory_done
!endif
spark_directory_marker:
  Push "$INSTDIR\.spark-install"
  Call ${PREFIX}SparkPathHasReparsePoint
  Pop $0
  StrCmp $0 0 0 spark_directory_link
  ClearErrors
  FileOpen $3 "$INSTDIR\.spark-install" r
  IfErrors spark_directory_unknown
  FileSeek $3 0 END $2
  FileSeek $3 0 SET
  FileRead $3 $4 15
  FileClose $3
  IntCmp $2 14 0 spark_directory_unknown spark_directory_unknown
  StrCmpS $4 "SparkAssistant" spark_directory_done spark_directory_unknown
spark_directory_missing:
!if ${REQUIRE_MARKER} == 1
  Goto spark_directory_unknown
!else
  Goto spark_directory_done
!endif
spark_directory_bad_name:
  StrCpy $SparkDirectoryError "安装目录名称必须是 SparkAssistant。请重新选择专用目录。"
  Goto spark_directory_done
spark_directory_unknown:
  StrCpy $SparkDirectoryError "目录包含其他文件或安装标记无效。为保护已有资料，请选择空的 SparkAssistant 目录。"
  Goto spark_directory_done
spark_directory_link:
  StrCpy $SparkDirectoryError "安装目录包含目录联接或符号链接，已停止以保护链接目标。"
spark_directory_done:
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd
!macroend

!macro SparkDirectoryGuards
Var SparkDirectoryError
!insertmacro SparkPathGuardFunction ""
!insertmacro SparkPathGuardFunction "un."
!insertmacro SparkDirectoryGuardFunction "" 0
!insertmacro SparkDirectoryGuardFunction "un." 1
!macroend

!endif
