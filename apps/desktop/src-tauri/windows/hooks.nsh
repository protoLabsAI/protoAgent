; protoAgent NSIS installer hooks (#1685, graceful-first since #2410).
;
; The stock Tauri template closes the MAIN app before (un)installing, but the
; bundled sidecar (protoagent-server.exe) is a separate process it has never
; heard of — and it also runs STANDALONE (the documented pre-#1678 workaround).
; A live server holds its exe / the install dir, so a reinstall-over-kept-data
; died with a locked-file error that read as "the directory already exists".
; Stopping it first makes keep-data uninstall → reinstall the blessed,
; reconfigure-free upgrade path.
;
; WHY GRACEFUL FIRST (#2410): the sidecar is a PyInstaller ONEFILE binary — a
; bootloader process that extracts to %TEMP%\_MEI* and spawns its python child.
; The bootloader deletes that extraction dir when the child exits; taskkill /F
; kills the whole tree and skips the cleanup, stranding ~128 MB per install.
; Closing the desktop shell WITHOUT /F instead lets the sidecar's parent-death
; watchdog (2s poll, server/__init__.py) exit the python child cleanly, and the
; bootloader sweeps _MEI on its way out. The force taskkill stays as the
; fallback — a hung shell, or a STANDALONE server (no watchdog parent, so it
; can only be force-killed; its residue is unavoidable without a kill) — and is
; a no-op when the graceful path already emptied the process table.
;
; taskkill on a non-running process is a no-op (nsExec swallows the exit code);
; /T reaps any child tree; the kept %APPDATA% data dir is never touched.
; The wait probe: `find` exits 1 when tasklist lists no matching process.

!macro NSIS_HOOK_PREINSTALL
  nsExec::Exec 'taskkill /IM protoAgent.exe'
  Pop $0
  StrCpy $R9 12
  pre_wait:
    nsExec::Exec 'cmd /c tasklist /FI "IMAGENAME eq protoagent-server.exe" /NH | find /I "protoagent-server.exe" >nul'
    Pop $0
    StrCmp $0 "1" pre_done
    IntOp $R9 $R9 - 1
    StrCmp $R9 "0" pre_done
    Sleep 1000
    Goto pre_wait
  pre_done:
  nsExec::Exec 'taskkill /F /IM protoAgent.exe'
  Pop $0
  nsExec::Exec 'taskkill /F /IM protoagent-server.exe /T'
  Pop $0
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  nsExec::Exec 'taskkill /IM protoAgent.exe'
  Pop $0
  StrCpy $R9 12
  preun_wait:
    nsExec::Exec 'cmd /c tasklist /FI "IMAGENAME eq protoagent-server.exe" /NH | find /I "protoagent-server.exe" >nul'
    Pop $0
    StrCmp $0 "1" preun_done
    IntOp $R9 $R9 - 1
    StrCmp $R9 "0" preun_done
    Sleep 1000
    Goto preun_wait
  preun_done:
  nsExec::Exec 'taskkill /F /IM protoAgent.exe'
  Pop $0
  nsExec::Exec 'taskkill /F /IM protoagent-server.exe /T'
  Pop $0
!macroend
