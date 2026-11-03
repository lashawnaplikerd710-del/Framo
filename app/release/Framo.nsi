Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
Name "Framo"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\Framo"
InstallDirRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo" "InstallLocation"
RequestExecutionLevel user
SetCompressor lzma
SetCompressorDictSize 64
VIProductVersion "${APP_VERSION_QUAD}"
VIAddVersionKey /LANG=2052 "ProductName" "Framo"
VIAddVersionKey /LANG=2052 "FileDescription" "Framo 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "Framo"
!define MUI_ICON "${PAYLOAD}\desktop-assets\framo.ico"
!define MUI_UNICON "${PAYLOAD}\desktop-assets\framo.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "安装 Framo 创作工作台，包含长短视频剪辑、预览及中文、英语和日语本地音色所需依赖。$\r$\n$\r$\n支持 Windows 10 22H2 / Windows 11（64 位）。智能剪辑需要联网，并在设置中填写单独发放的工作台访问码。$\r$\n$\r$\n任务、缓存和默认成品保存在所选安装目录的 UserData 文件夹。可选择 D 盘等空间充足且可写的位置。"
!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE ValidateDirectory
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TEXT "Framo 已安装。$\r$\n$\r$\n从桌面或开始菜单启动，在设置中填写工作台访问码。$\r$\n$\r$\n卸载保留 UserData 中的任务、缓存和成品。安装包不包含访问码。"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
Var Isolated

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "需要 Windows 10/11 64 位系统。"
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "需要 Windows 10 或更新系统。"
    Abort
  ${EndIf}
  SetShellVarContext current
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/ISOLATED" $R1
  ${IfNot} ${Errors}
    StrCpy $Isolated "1"
  ${EndIf}
FunctionEnd

Function ValidateDirectory
  # Never merge the payload into a pre-existing unrelated directory.
  IfFileExists "$INSTDIR\installed-profile.json" existing
  FindFirst $0 $1 "$INSTDIR\*.*"
  loop:
    StrCmp $1 "" clean
    StrCmp $1 "." next
    StrCmp $1 ".." next
    FindClose $0
    MessageBox MB_ICONSTOP "请选择一个空文件夹，或现有 Framo 安装目录。不会覆盖其他程序。" /SD IDOK
    Abort
  next:
    FindNext $0 $1
    Goto loop
  clean:
    FindClose $0
    Goto writable
  existing:
    IfFileExists "$INSTDIR\python\python.exe" 0 writable
    ExecWait '"$INSTDIR\python\python.exe" -B "$INSTDIR\installer_guard.py"' $0
    ${If} $0 != 0
      MessageBox MB_ICONSTOP "请先退出此安装目录中的 Framo 和剪辑任务，再进行安装。" /SD IDOK
      Abort
    ${EndIf}
  writable:
    CreateDirectory "$INSTDIR"
    ClearErrors
    FileOpen $0 "$INSTDIR\.framo-write-test" w
    ${If} ${Errors}
      MessageBox MB_ICONSTOP "目录不可写，请选择当前用户可写的文件夹。" /SD IDOK
      Abort
    ${EndIf}
    FileClose $0
    Delete "$INSTDIR\.framo-write-test"
FunctionEnd

Section "Framo" SEC_MAIN
  Call ValidateDirectory
  SetOutPath "$INSTDIR"
  File /r "${PAYLOAD}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  ${If} $Isolated != "1"
    WriteINIStr "$INSTDIR\install-integration.ini" "Install" "Integrated" "1"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo" "DisplayName" "Framo"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo" "DisplayIcon" "$INSTDIR\Framo.exe"
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo" "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo" "NoRepair" 1
    CreateDirectory "$SMPROGRAMS\Framo"
    # Preserve an existing owner's shortcut to a different portable runtime.
    IfFileExists "$DESKTOP\Framo.lnk" +3 0
    CreateShortCut "$DESKTOP\Framo.lnk" "$INSTDIR\Framo.exe" "" "$INSTDIR\desktop-assets\framo.ico"
    WriteINIStr "$INSTDIR\install-integration.ini" "Install" "DesktopCreated" "1"
    CreateShortCut "$SMPROGRAMS\Framo\Framo.lnk" "$INSTDIR\Framo.exe" "" "$INSTDIR\desktop-assets\framo.ico"
    CreateShortCut "$SMPROGRAMS\Framo\卸载.lnk" "$INSTDIR\Uninstall.exe"
  ${EndIf}
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  IfFileExists "$INSTDIR\installed-profile.json" +3 0
  MessageBox MB_ICONSTOP "安装标识缺失，已停止卸载以保护文件。" /SD IDOK
  Abort
  ExecWait '"$INSTDIR\python\python.exe" -B "$INSTDIR\installer_guard.py"' $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "请先退出 Framo 和剪辑任务，再卸载。" /SD IDOK
    Abort
  ${EndIf}
  ReadINIStr $1 "$INSTDIR\install-integration.ini" "Install" "Integrated"
  ${If} $1 == "1"
    ReadRegStr $2 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo" "InstallLocation"
    ${If} $2 == $INSTDIR
      DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo"
      ReadINIStr $3 "$INSTDIR\install-integration.ini" "Install" "DesktopCreated"
      ${If} $3 == "1"
        Delete "$DESKTOP\Framo.lnk"
      ${EndIf}
      Delete "$SMPROGRAMS\Framo\Framo.lnk"
      Delete "$SMPROGRAMS\Framo\卸载.lnk"
      RMDir "$SMPROGRAMS\Framo"
    ${EndIf}
  ${EndIf}
  # Exact payload file list only; never recursively remove the selected folder.
  !include "${DELETE_LIST}"
  Delete "$INSTDIR\install-integration.ini"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
