; protoAgent NSIS installer hooks (#1685).
;
; The stock Tauri template closes the MAIN app before (un)installing, but the
; bundled sidecar (protoagent-server.exe) is a separate process it has never
; heard of — and it also runs STANDALONE (the documented pre-#1678 workaround).
; A live server holds its exe / the install dir, so a reinstall-over-kept-data
; died with a locked-file error that read as "the directory already exists".
; Stopping it first makes keep-data uninstall → reinstall the blessed,
; reconfigure-free upgrade path. taskkill on a non-running process is a no-op
; (nsExec swallows the exit code); /T reaps any child tree; the kept %APPDATA%
; data dir is never touched.

!macro NSIS_HOOK_PREINSTALL
  nsExec::Exec 'taskkill /F /IM protoagent-server.exe /T'
  Pop $0
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  nsExec::Exec 'taskkill /F /IM protoagent-server.exe /T'
  Pop $0
!macroend
