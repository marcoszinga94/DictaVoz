; Instalador NSIS de DictaVoz para Windows.
; Se ejecuta desde packaging/windows (makensis installer.nsi).
; Espera dist/DictaVoz.exe (salida de PyInstaller) en la raíz del proyecto.
; Incluye el certificado de firma y lo marca como CONFIABLE al instalarse,
; así no hace falta descargar el .cer ni ejecutar scripts aparte.

!include "MUI2.nsh"

!define APPNAME "DictaVoz"
!define APPEXE "DictaVoz.exe"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\DictaVoz"
!define MUI_ICON "..\..\icons\dictavoz.ico"
!define MUI_UNICON "..\..\icons\dictavoz.ico"

Name "${APPNAME}"
OutFile "..\..\dist\DictaVoz-Setup.exe"
InstallDir "$PROGRAMFILES64\DictaVoz"
InstallDirRegKey HKLM "Software\DictaVoz" "InstallDir"
RequestExecutionLevel admin

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Spanish"

Section "Instalar ${APPNAME}"
  SetOutPath "$INSTDIR"
  File "..\..\dist\${APPEXE}"

  ; Copia el certificado de firma y márcalo como CONFIABLE (raíz + editor)
  ; en el almacén del usuario que instala. Así Windows reconoce la app y
  ; el propio instalador en futuros arranques, sin descargar nada aparte.
  File "DictaVoz.cer"
  ExecWait '"$SYSDIR\certutil.exe" -addstore -user Root "$INSTDIR\DictaVoz.cer"'
  ExecWait '"$SYSDIR\certutil.exe" -addstore -user TrustedPublisher "$INSTDIR\DictaVoz.cer"'

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKLM "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKLM "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\${APPEXE}"
  WriteRegStr HKLM "${UNINSTKEY}" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINSTKEY}" "Publisher" "DictaVoz"
  WriteRegDWORD HKLM "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINSTKEY}" "NoRepair" 1
  WriteRegStr HKLM "Software\DictaVoz" "InstallDir" "$INSTDIR"

  CreateDirectory "$SMPROGRAMS\DictaVoz"
  CreateShortcut "$SMPROGRAMS\DictaVoz\DictaVoz.lnk" "$INSTDIR\${APPEXE}"
  CreateShortcut "$DESKTOP\DictaVoz.lnk" "$INSTDIR\${APPEXE}"
SectionEnd

Section "Uninstall"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$INSTDIR\${APPEXE}"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\DictaVoz\DictaVoz.lnk"
  RMDir "$SMPROGRAMS\DictaVoz"
  Delete "$DESKTOP\DictaVoz.lnk"
  DeleteRegKey HKLM "${UNINSTKEY}"
  DeleteRegKey HKLM "Software\DictaVoz"
SectionEnd