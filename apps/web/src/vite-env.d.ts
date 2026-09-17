/// <reference types="vite/client" />

// @protolabsai/ui ships source (`./src/*.tsx`), so we typecheck its deps too: culori's
// types come from the @types/culori devDependency. (An untyped `declare module "culori"`
// shim can't express the type-only imports the DS's themes/ modules use.)
