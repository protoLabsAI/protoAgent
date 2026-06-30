// ESM shim for `react-dom/client` → the UMD ReactDOM global (createRoot lives there in R18 UMD).
const RD = window.ReactDOM;
export const createRoot = RD.createRoot;
export const hydrateRoot = RD.hydrateRoot;
export default RD;
