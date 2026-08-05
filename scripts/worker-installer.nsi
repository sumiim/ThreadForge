Unicode True
RequestExecutionLevel user

!ifndef WORKER_EXE
  !error "WORKER_EXE is required"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif
!ifndef WORKER_SERVICE_EXE
  !error "WORKER_SERVICE_EXE is required"
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
  Sleep 300
  nsExec::ExecToStack 'taskkill /F /IM threadforge-worker-service.exe'
  Pop $0
  Pop $1

  SetOutPath "$INSTDIR"
  File "/oname=threadforge-worker.exe" "${WORKER_EXE}"
  File "/oname=threadforge-worker-service.exe" "${WORKER_SERVICE_EXE}"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ThreadForgeWorker" "DisplayName" "ThreadForge Worker Companion"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ThreadForgeWorker" "DisplayVersion" "${WORKER_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ThreadForgeWorker" "UninstallString" '"$INSTDIR\uninstall.exe"'

  WriteRegStr HKCU "Software\Classes\threadforge" "" "URL:ThreadForge Worker"
  WriteRegStr HKCU "Software\Classes\threadforge" "URL Protocol" ""
  WriteRegStr HKCU "Software\Classes\threadforge\shell\open\command" "" '"$INSTDIR\threadforge-worker-service.exe" protocol "%1"'

  CreateShortCut "$SMSTARTUP\ThreadForge Worker.lnk" "$INSTDIR\threadforge-worker-service.exe" "service"
  CreateDirectory "$SMPROGRAMS\ThreadForge"
  CreateShortCut "$SMPROGRAMS\ThreadForge\Worker status.lnk" "$INSTDIR\threadforge-worker.exe" "status"
  CreateShortCut "$SMPROGRAMS\ThreadForge\Uninstall Worker.lnk" "$INSTDIR\uninstall.exe"

  ExecShell "open" "$INSTDIR\threadforge-worker-service.exe" "service"
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
  RMDir /r "$SMPROGRAMS\ThreadForge"
  DeleteRegKey HKCU "Software\Classes\threadforge"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ThreadForgeWorker"
  Delete "$INSTDIR\threadforge-worker.exe"
  Delete "$INSTDIR\threadforge-worker-service.exe"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
