- **Console: docviewer + fleet font-size onto the DS type scale (#3688).**
  Every px font-size in `docviewer/docviewer.css` (2 sites) and `fleet/fleet.css`
  (8 sites) now reads the matching `--pl-font-size-*` scale token with no px fallback;
  size is theme-invariant so nothing renders differently. The `.fleet-name-link`
  focus-ring outline is left untouched.
