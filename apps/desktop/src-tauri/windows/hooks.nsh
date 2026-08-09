; protoAgent NSIS installer hooks (#1685; _MEI cleanup since #2410).
;
; The stock Tauri template closes the MAIN app before (un)installing, but the
; bundled sidecar (protoagent-server.exe) is a separate process it has never
; heard of — and it also runs STANDALONE (the documented pre-#1678 workaround).
; A live server holds its exe / the install dir, so a reinstall-over-kept-data
; died with a locked-file error that read as "the directory already exists".
; Stopping it first makes keep-data uninstall → reinstall the blessed,
; reconfigure-free upgrade path.
;
; _MEI CLEANUP (#2410): the sidecar is a PyInstaller ONEFILE binary — a
; bootloader that extracts ~140 MB to %TEMP%\_MEI* and only sweeps it on a
; clean exit. taskkill /F skips that sweep, and a graceful close is NOT
; reachable from here: the desktop image (protoagent_desktop.exe — the CARGO
; crate name, not the productName) absorbs a non-force taskkill into
; close-to-tray (verified live in #2410 QA: 26s after a successful WM_CLOSE
; the app, sidecar, and listener were all still up). So: force-kill both
; images, then delete ONLY the extraction dirs that carry our own bundled
; `protolabs_a2a` package as the marker — no other application ships it, so
; a foreign app's live _MEI can never match. Runs in BOTH hooks, so an
; install also sweeps residue stranded by older versions.
;
; taskkill on a non-running process is a no-op (nsExec swallows the exit
; code); /T reaps child trees; the kept %APPDATA% data dir is never touched.

!macro PROTOAGENT_STOP_AND_SWEEP suffix
  nsExec::Exec 'taskkill /F /IM protoagent_desktop.exe /T'
  Pop $0
  nsExec::Exec 'taskkill /F /IM protoagent-server.exe /T'
  Pop $0
  Sleep 500
  FindFirst $0 $1 "$TEMP\_MEI*"
  mei_loop_${suffix}:
    StrCmp $1 "" mei_done_${suffix}
    IfFileExists "$TEMP\$1\protolabs_a2a\*.*" 0 mei_next_${suffix}
      RMDir /r "$TEMP\$1"
    mei_next_${suffix}:
    FindNext $0 $1
    Goto mei_loop_${suffix}
  mei_done_${suffix}:
  FindClose $0
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro PROTOAGENT_STOP_AND_SWEEP pre
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro PROTOAGENT_STOP_AND_SWEEP preun
!macroend
