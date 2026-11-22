- **The peer-started private-steer test no longer fails at random on CI.**
  It read the task's state once, straight after the scripted model signalled that it had
  started, and asserted "working". The executor stores that state a moment later, so on a
  loaded runner the read came back unset and the test failed (`assert 0 == 2`), on Linux and
  Windows, in every run on main since it was added. It now waits for the stored state. Test
  only; no product code changed.
