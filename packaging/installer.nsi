; Установщик SEBE 3D (NSIS 3). Внутренние имена (PrintHub.exe, папки) прежние — для обновления старых копий. Собирается скриптом build_windows.sh.
; Ставится в профиль пользователя без прав администратора — поэтому обновления
; устанавливаются тихо, без запросов UAC.
Unicode true
Target amd64-unicode
ManifestDPIAware true
SetCompressor /SOLID lzma

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef SRCDIR
  !define SRCDIR "build\PrintHub"
!endif
!ifndef OUTFILE
  !define OUTFILE "PrintHub-Setup-${VERSION}.exe"
!endif

!define APPNAME "SEBE 3D"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\PrintHub"
!define RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"

Name "${APPNAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\PrintHub"
RequestExecutionLevel user
BrandingText "SEBE 3D ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=1049 "ProductName" "SEBE 3D"
VIAddVersionKey /LANG=1049 "FileDescription" "Установщик SEBE 3D"
VIAddVersionKey /LANG=1049 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1049 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1049 "LegalCopyright" "SEBE"

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

!define MUI_ICON "printhub.ico"
!define MUI_UNICON "printhub.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Установка SEBE 3D ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "SEBE 3D — управление 3D-принтерами Bambu Lab, Elegoo, Anycubic и Klipper в локальной сети.$\r$\n$\r$\nПрограмма установится в ваш профиль и будет обновляться автоматически.$\r$\n$\r$\nНажмите «Далее»."
!define MUI_FINISHPAGE_RUN "$INSTDIR\PrintHub.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить SEBE 3D"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

Var IsUpdate

Function StopRunning
  ; Аккуратно останавливаем запущенный PrintHub (файлы иначе заняты)
  ${If} ${FileExists} "$INSTDIR\python\python.exe"
    DetailPrint "Останавливаю запущенную программу…"
    nsExec::Exec '"$INSTDIR\python\python.exe" -m printhub.desktop --shutdown'
    Pop $0
    ; на всякий случай закрываем все процессы Python именно из папки PrintHub
    nsExec::Exec 'powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $$_.Path -like $\'$INSTDIR\python\*$\' } | Stop-Process -Force"'
    Pop $0
    Sleep 1000
  ${EndIf}
FunctionEnd

Section "SEBE 3D" SecMain
  SetShellVarContext current
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/UPDATE" $R1
  ${IfNot} ${Errors}
    StrCpy $IsUpdate "1"
  ${EndIf}

  Call StopRunning

  ; удаляем только свои подпапки — пользовательские данные лежат отдельно (%LOCALAPPDATA%\PrintHub\data)
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\python"

  SetOutPath "$INSTDIR"
  File /r "${SRCDIR}\*"

  FileOpen $0 "$INSTDIR\installed.json" w
  FileWrite $0 '{"version": "${VERSION}"}'
  FileClose $0

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; ярлыки под новым названием; старые «PrintHub» убираем (ярлык на рабочем столе — переименовываем)
  RMDir /r "$SMPROGRAMS\PrintHub"
  CreateDirectory "$SMPROGRAMS\SEBE 3D"
  CreateShortcut "$SMPROGRAMS\SEBE 3D\SEBE 3D.lnk" "$INSTDIR\PrintHub.exe" "" "$INSTDIR\printhub.ico"
  CreateShortcut "$SMPROGRAMS\SEBE 3D\Удалить SEBE 3D.lnk" "$INSTDIR\Uninstall.exe"
  ${If} ${FileExists} "$DESKTOP\PrintHub.lnk"
    Delete "$DESKTOP\PrintHub.lnk"
    CreateShortcut "$DESKTOP\SEBE 3D.lnk" "$INSTDIR\PrintHub.exe" "" "$INSTDIR\printhub.ico"
  ${EndIf}
  ${If} $IsUpdate != "1"
    CreateShortcut "$DESKTOP\SEBE 3D.lnk" "$INSTDIR\PrintHub.exe" "" "$INSTDIR\printhub.ico"
    ; автозапуск вместе с Windows (можно выключить в меню значка в трее)
    WriteRegStr HKCU "${RUN_KEY}" "PrintHub" '"$INSTDIR\PrintHub.exe" --no-browser'
  ${EndIf}

  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "SEBE 3D"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "SEBE"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\printhub.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"

  ; после тихого обновления — сразу запускаем новую версию
  ${If} $IsUpdate == "1"
    Exec '"$INSTDIR\PrintHub.exe" --no-browser'
  ${EndIf}
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  ${If} ${FileExists} "$INSTDIR\python\python.exe"
    nsExec::Exec '"$INSTDIR\python\python.exe" -m printhub.desktop --shutdown'
    Pop $0
    nsExec::Exec 'powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $$_.Path -like $\'$INSTDIR\python\*$\' } | Stop-Process -Force"'
    Pop $0
    Sleep 1000
  ${EndIf}
  DeleteRegValue HKCU "${RUN_KEY}" "PrintHub"
  DeleteRegKey HKCU "${UNINST_KEY}"
  Delete "$DESKTOP\PrintHub.lnk"
  Delete "$DESKTOP\SEBE 3D.lnk"
  RMDir /r "$SMPROGRAMS\PrintHub"
  RMDir /r "$SMPROGRAMS\SEBE 3D"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\python"
  Delete "$INSTDIR\PrintHub.exe"
  Delete "$INSTDIR\printhub.ico"
  Delete "$INSTDIR\installed.json"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  ; данные (принтеры, библиотека, история) остаются в %LOCALAPPDATA%\PrintHub\data
SectionEnd
