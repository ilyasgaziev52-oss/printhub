; Установщик PrintHub (NSIS 3). Собирается скриптом build_windows.sh.
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

!define APPNAME "PrintHub"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\PrintHub"
!define RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"

Name "${APPNAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\PrintHub"
RequestExecutionLevel user
BrandingText "PrintHub ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=1049 "ProductName" "PrintHub"
VIAddVersionKey /LANG=1049 "FileDescription" "Установщик PrintHub"
VIAddVersionKey /LANG=1049 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1049 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1049 "LegalCopyright" "PrintHub"

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

!define MUI_ICON "printhub.ico"
!define MUI_UNICON "printhub.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Установка PrintHub ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "PrintHub — управление 3D-принтерами Bambu Lab, Elegoo, Anycubic и Klipper в локальной сети.$\r$\n$\r$\nПрограмма установится в ваш профиль и будет обновляться автоматически.$\r$\n$\r$\nНажмите «Далее»."
!define MUI_FINISHPAGE_RUN "$INSTDIR\PrintHub.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить PrintHub"

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
    DetailPrint "Останавливаю запущенный PrintHub…"
    nsExec::Exec '"$INSTDIR\python\python.exe" -m printhub.desktop --shutdown'
    Pop $0
    ; на всякий случай закрываем все процессы Python именно из папки PrintHub
    nsExec::Exec 'powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $$_.Path -like $\'$INSTDIR\python\*$\' } | Stop-Process -Force"'
    Pop $0
    Sleep 1000
  ${EndIf}
FunctionEnd

Section "PrintHub" SecMain
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

  CreateDirectory "$SMPROGRAMS\PrintHub"
  CreateShortcut "$SMPROGRAMS\PrintHub\PrintHub.lnk" "$INSTDIR\PrintHub.exe" "" "$INSTDIR\printhub.ico"
  CreateShortcut "$SMPROGRAMS\PrintHub\Удалить PrintHub.lnk" "$INSTDIR\Uninstall.exe"
  ${If} $IsUpdate != "1"
    CreateShortcut "$DESKTOP\PrintHub.lnk" "$INSTDIR\PrintHub.exe" "" "$INSTDIR\printhub.ico"
    ; автозапуск вместе с Windows (можно выключить в меню значка в трее)
    WriteRegStr HKCU "${RUN_KEY}" "PrintHub" '"$INSTDIR\PrintHub.exe" --no-browser'
  ${EndIf}

  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "PrintHub"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "PrintHub"
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
  RMDir /r "$SMPROGRAMS\PrintHub"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\python"
  Delete "$INSTDIR\PrintHub.exe"
  Delete "$INSTDIR\printhub.ico"
  Delete "$INSTDIR\installed.json"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  ; данные (принтеры, библиотека, история) остаются в %LOCALAPPDATA%\PrintHub\data
SectionEnd
