!macro STOP_ODOO_MANAGER_PROCESSES
  DetailPrint "Closing the running Odoo Manager instance..."
  nsExec::ExecToLog 'curl.exe --silent --max-time 3 --request POST http://127.0.0.1:18765/api/system/shutdown'
  Sleep 1500

  ; Windows locks installed executables. These fallbacks also clean up a
  ; backend orphaned by an older Odoo Manager build.
  nsExec::ExecToLog 'taskkill.exe /T /F /IM "Odoo Manager.exe"'
  nsExec::ExecToLog 'taskkill.exe /T /F /IM "odoo-manager.exe"'
  nsExec::ExecToLog 'taskkill.exe /T /F /IM "odoo-manager-backend.exe"'
  Sleep 750
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro STOP_ODOO_MANAGER_PROCESSES
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro STOP_ODOO_MANAGER_PROCESSES
!macroend
