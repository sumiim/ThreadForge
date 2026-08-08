Unicode True
RequestExecutionLevel user

!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif
!ifndef WORKER_SERVICE_DIR
  !error "WORKER_SERVICE_DIR is required"
!endif
!ifndef WORKER_VERSION
  !error "WORKER_VERSION is required"
!endif

Name "ThreadForge Worker Companion ${WORKER_VERSION}"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\ThreadForge\WorkerApp"
ShowInstDetails show
AutoCloseWindow true

Page instfiles

Section "Install"
  SetShellVarContext current

  nsExec::ExecToStack 'taskkill /F /IM threadforge-worker.exe'
  Pop $0
  Pop $1
  nsExec::ExecToStack 'taskkill /F /IM threadforge-worker-service.exe'
  Pop $0
  Pop $1
  Sleep 500

  ; Upgrade through the installed uninstaller so stale binaries, shortcuts,
  ; protocol handlers and registry values cannot survive the replacement.
  ; User data lives in $LOCALAPPDATA\ThreadForge\Worker and is intentionally
  ; outside $INSTDIR, so pairing, workspaces, history and model config remain.
  IfFileExists "$INSTDIR\uninstall.exe" 0 install_files
  ExecWait '"$INSTDIR\uninstall.exe" /S _?=$INSTDIR' $0
  IntCmp $0 0 install_files
  Abort "Unable to remove the previous ThreadForge Worker installation (exit code $0)."

install_files:
  SetOutPath "$INSTDIR"
  ; Keep the service executable at the historical root path, but ship its
  ; PyInstaller dependencies as a shared directory for both launchers. This
  ; avoids onefile _MEI extraction, duplicate runtimes and antivirus churn.
  File /r "${WORKER_SERVICE_DIR}\*"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ThreadForgeWorker" "DisplayName" "ThreadForge Worker Companion"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ThreadForgeWorker" "DisplayVersion" "${WORKER_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ThreadForgeWorker" "UninstallString" '"$INSTDIR\uninstall.exe"'

  WriteRegStr HKCU "Software\Classes\threadforge" "" "URL:ThreadForge Worker"
  WriteRegStr HKCU "Software\Classes\threadforge" "URL Protocol" ""
  WriteRegStr HKCU "Software\Classes\threadforge\shell\open\command" "" '"$INSTDIR\threadforge-worker-service.exe" protocol "%1"'

  ; HKCU Run is reliable for per-user installs and does not depend on the
  ; shell Startup folder accepting an unsigned shortcut.
  Delete "$SMSTARTUP\ThreadForge Worker.lnk"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "ThreadForgeWorker" '"$INSTDIR\threadforge-worker-service.exe" service'
  CreateDirectory "$SMPROGRAMS\ThreadForge"
  CreateShortCut "$SMPROGRAMS\ThreadForge\Worker status.lnk" "$INSTDIR\threadforge-worker.exe" "status"
  CreateShortCut "$SMPROGRAMS\ThreadForge\Uninstall Worker.lnk" "$INSTDIR\uninstall.exe"

  ; An installer launched by a PyInstaller onefile Worker inherits the old
  ; _MEI runtime. Force the replacement service to start as a new top-level
  ; frozen process even when the old updater did not sanitize its environment.
  System::Call 'Kernel32::SetEnvironmentVariable(t "PYINSTALLER_RESET_ENVIRONMENT", t "1") i .r0'
  ; Start directly and asynchronously. ShellExecute can keep the installer UI
  ; waiting while Windows resolves the executable association and trust state.
  Exec '"$INSTDIR\threadforge-worker-service.exe" service'
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  nsExec::ExecToStack 'taskkill /F /IM threadforge-worker.exe'
  Pop $0
  Pop $1
  nsExec::ExecToStack 'taskkill /F /IM threadforge-worker-service.exe'
  Pop $0
  Pop $1
  Delete "$SMSTARTUP\ThreadForge Worker.lnk"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "ThreadForgeWorker"
  RMDir /r "$SMPROGRAMS\ThreadForge"
  DeleteRegKey HKCU "Software\Classes\threadforge"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ThreadForgeWorker"
  Delete "$INSTDIR\threadforge-worker.exe"
  Delete "$INSTDIR\uninstall.exe"
  RMDir /r "$INSTDIR"
SectionEnd
