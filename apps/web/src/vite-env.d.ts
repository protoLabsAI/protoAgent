/// <reference types="vite/client" />

// @protolabsai/ui ships source (`./src/*.tsx`), so we typecheck its deps too. culori
// (used by the DS ThemePanel) ships no types — declare it so tsc doesn't choke.
declare module "culori";
