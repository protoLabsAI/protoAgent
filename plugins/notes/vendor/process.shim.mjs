/* Stand-in for esm.sh's /node/process.mjs polyfill.
 *
 * esm.sh injects `import __Process$ from "/node/process.mjs"` into the @lezer/lr
 * bundle, which reads `__Process$.env.LOG` to gate the parser's debug logging. That
 * path only exists on esm.sh's own CDN, so a vendored copy 404s and — because a failed
 * module import aborts the whole graph — CodeMirror never mounts and the editor renders
 * as an empty box. (Nothing in the page throws; you just get no editor. Static checks
 * on the HTML cannot see this.)
 *
 * The import map points /node/process.mjs here. No env, so lezer's logging stays off,
 * which is what we want in a browser anyway.
 */
export default { env: {} };
