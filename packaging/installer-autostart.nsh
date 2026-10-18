!ifndef SPARK_INSTALLER_AUTOSTART_NSH
!define SPARK_INSTALLER_AUTOSTART_NSH
!include "LogicLib.nsh"

; Only remove the exact command generated for this installation. Source runs,
; other installation paths and unknown command formats belong to the user.
; Parameters let the regression probe use its own isolated temporary key.
!macro SparkRemoveOwnedAutostart ROOT_KEY SUB_KEY VALUE_NAME
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  ClearErrors
  ReadRegStr $0 ${ROOT_KEY} "${SUB_KEY}" "${VALUE_NAME}"
  ${IfNot} ${Errors}
    StrCpy $1 '$\"$INSTDIR\SparkAssistant.exe$\" --tray'
    ${If} $0 == $1
      DeleteRegValue ${ROOT_KEY} "${SUB_KEY}" "${VALUE_NAME}"
    ${Else}
      ; subprocess.list2cmdline does not quote an executable without whitespace.
      ; Never accept the ambiguous unquoted form when the path has whitespace.
      StrCpy $2 "$INSTDIR\SparkAssistant.exe"
      StrCpy $3 0
      StrCpy $4 0
      ${Do}
        StrCpy $1 "$2" 1 $3
        ${If} $1 == ""
          ${ExitDo}
        ${EndIf}
        ${If} $1 == " "
        ${OrIf} $1 == "$\t"
          StrCpy $4 1
          ${ExitDo}
        ${EndIf}
        IntOp $3 $3 + 1
      ${Loop}
      ${If} $4 == 0
        StrCpy $1 "$2 --tray"
        ${If} $0 == $1
          DeleteRegValue ${ROOT_KEY} "${SUB_KEY}" "${VALUE_NAME}"
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
!macroend

!endif
