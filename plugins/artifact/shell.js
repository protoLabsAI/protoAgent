  // The DS plugin-kit owns the protoagent:init handshake (bearer + theme, incl. live
  // re-themes onto the --pl-* tokens) and slug-aware authed fetches — replacing the
  // hand-rolled listener/theme map this page carried. plugin-kit.js is an ES MODULE,
  // so it loads via dynamic import (a classic <script src> throws on its exports;
  // see protoAgent docs/how-to/build-a-plugin-view.md). If the kit fails to load,
  // fail LOUDLY and name it (#2392) — a tokenless shim silently 401s every gated
  // call on an authed host, and the kit ships with the host bundle on every tier,
  // so absence means a broken/partial install, not a downlevel host to paper over.
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) {
    var kerr = document.createElement("div");
    kerr.style.cssText = "padding:14px 16px;color:var(--pl-color-danger,#f85149);font:13px/1.5 var(--pl-font-sans,system-ui)";
    kerr.textContent = "The DS plugin kit failed to load — the console bundle (/_ds) is missing or stale " +
      "(a source install without a web build, or a packaging regression). Artifacts cannot authenticate without it.";
    document.body.prepend(kerr);
    throw e;
  }
  // Store mirror: arts = [{id,kind,title,versions:[{code,ts,by}]}], curId = focused.
  // selId/selVer = the artifact + version the USER is viewing (selVer null = latest, so
  // it auto-follows new versions). followNewest jumps to the newest artifact on create
  // unless the user navigated to an older one.
  var arts = [], curId = null, selId = null, selVer = null, followNewest = true, lastRendered = "";
  var renderingId = null, renderingVer = 0;  // the (id, 1-based version) currently in the frame — for render-status (#1458)
  var EXT = { html: "html", svg: "svg", mermaid: "mmd", react: "jsx" };
  function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;"); }
  // The NESTED artifact iframe (sandboxed, no stylesheet access) gets the live theme
  // injected as literal colors — read the kit-managed tokens at render time.
  // Injected into EVERY artifact (the window.claude.complete analog): the artifact
  // calls window.protoArtifact.ask(prompt) → a Promise that round-trips via the shell
  // (postMessage) to the gated /ask endpoint → the agent → back. parent.postMessage
  // works from the sandbox; the shell validates e.source and calls the bearer-gated
  // endpoint. ask() rejects if the operator hasn't enabled it (ARTIFACT_ASK_ENABLED).
  var SHIM = '<script>(function(){var s=0,w={};'
    + 'window.addEventListener("message",function(e){var m=e.data||{};'
    // Live re-theme (#1872): base() bakes the tokens in as literals at render time,
    // so a later app-theme switch left the frame in the stale palette. The shell
    // pushes fresh tokens; update :root + html/body in place (no re-render, so
    // interactive artifact state survives).
    + 'if(m.type==="protoArtifact:theme"&&m.tokens){var st=document.documentElement.style;'
    + 'for(var k in m.tokens){if(k.indexOf("--pl-")===0)st.setProperty(k,String(m.tokens[k]));}'
    + 'var bg=m.tokens["--pl-color-bg"],fg=m.tokens["--pl-color-fg"];'
    + 'if(bg){st.background=bg;if(document.body)document.body.style.background=bg;}'
    + 'if(fg&&document.body)document.body.style.color=fg;return;}'
    + 'if(m.type!=="protoArtifact:result")return;'
    + 'var p=w[m.id];if(!p)return;delete w[m.id];m.error?p.reject(new Error(m.error)):p.resolve(m.text);});'
    + 'window.protoArtifact={ask:function(prompt){return new Promise(function(res,rej){var id=++s;w[id]={resolve:res,reject:rej};'
    + 'parent.postMessage({type:"protoArtifact:ask",id:id,prompt:String(prompt)},"*");'
    + 'setTimeout(function(){if(w[id]){delete w[id];rej(new Error("ask timed out"));}},60000);});}};'
    + '})();<\/script>';
  // Design-system surface: link the same-origin DS plugin-kit stylesheet (host-served at
  // /_ds/, min_protoagent_version 0.34.0) into html/react/markdown artifacts so they can use
  // the `.pl-*` component classes + `--pl-*` tokens and match the console. A cross-origin
  // <link> applies without CORS (only CSSOM access is gated), so the opaque sandbox can load it.
  function dsLink(){ return '<link rel="stylesheet" href="' + ORIGIN + '/_ds/plugin-kit.css">'; }
  // Error surfacing (injected into EVERY artifact via base()): register global error /
  // unhandledrejection handlers that lazily drop a fixed bottom overlay into the frame — so a
  // broken artifact shows WHY instead of a silent blank. Exposes window.__artErr(msg) for the
  // harness's own guards (e.g. the React no-mount check) to reuse.
  // Also REPORTS the render result up to the shell (#1458): rep(false,msg) on any error,
  // rep(true) once it's confirmed rendered — the shell relays it to /render-status so the
  // agent's create/edit reply (and check_artifact) can surface a render failure. Once-only
  // (__artRep) so the first verdict wins. KIND (set by base) gates the on-load OK: react
  // confirms via the no-mount guard's firstChild check instead (mount is async, post-load).
  var ERRBOOT = '<script>(function(){var W=window;'
    + 'function rep(ok,err){if(W.__artRep)return;W.__artRep=1;'
    + 'try{parent.postMessage({type:"protoArtifact:render",ok:!!ok,error:err?String(err).slice(0,2000):""},"*");}catch(_){}}'
    + 'function show(m){var d=document.getElementById("__arterr");'
    + 'if(!d){d=document.createElement("div");d.id="__arterr";'
    + 'd.style.cssText="position:fixed;left:0;right:0;bottom:0;max-height:60%;overflow:auto;margin:0;padding:10px 13px;background:#2a0f12;color:#ffb4b4;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;border-top:2px solid #f87171;z-index:2147483647";'
    + '(document.body||document.documentElement).appendChild(d);}d.textContent=String(m);rep(false,m);}'
    + 'W.__artErr=show;W.__artOk=function(){rep(true,"");};'
    + 'addEventListener("error",function(e){show("⚠ "+(e.message||(e.error&&e.error.message)||"Script error")+(e.lineno?" (line "+e.lineno+")":""));},true);'
    + 'addEventListener("unhandledrejection",function(e){show("⚠ "+((e.reason&&e.reason.message)||e.reason));});'
    + 'addEventListener("load",function(){if(W.__artKind!=="react")setTimeout(function(){if(!W.__artRep)W.__artOk();},80);});'
    + '})();<\/script>';
  function base(kind){
    var cs = getComputedStyle(document.documentElement);
    function tok(n,d){ return (cs.getPropertyValue(n) || d).trim(); }
    var bg=tok("--pl-color-bg","#0a0a0c"), fg=tok("--pl-color-fg","#ededed"),
        accent=tok("--pl-color-accent","#9b87f2"), border=tok("--pl-color-border","rgba(255,255,255,.08)");
    // Carry the live theme's key tokens into the nested frame (plugin-kit.css ships only the
    // DEFAULT palette); inline bg = no white flash; SHIM = the protoArtifact.ask bridge.
    // __artKind lets ERRBOOT decide how to confirm a clean render (on-load vs react mount).
    return '<style>:root{--pl-color-bg:'+bg+';--pl-color-fg:'+fg+';--pl-color-accent:'+accent+';--pl-color-border:'+border+'}'
      + 'html,body{margin:0;background:'+bg+';color:'+fg+'}</style>'
      + '<script>window.__artKind=' + JSON.stringify(kind||"") + ';<\/script>' + SHIM + ERRBOOT;
  }
  // Artifact libs are VENDORED + served same-origin (/plugins/artifact/vendor/…), so
  // react/mermaid renders work fully OFFLINE — no cdnjs dependency. Still pinned with
  // Subresource Integrity (sha512 of the exact vendored bytes), so a tampered served
  // file won't execute. Absolute URL (origin + base) because an srcdoc iframe has no
  // own URL to resolve a relative path against. Bump the file AND the hash together.
  var ORIGIN = location.origin + window.__base;  // "" + base, slug-aware
  var LIB = {
    mermaid: ["mermaid.min.js",
      "sha512-6a80OTZVmEJhqYJUmYd5z8yHUCDlYnj6q9XwB/gKOEyNQV/Q8u+XeSG59a2ZKFEHGTYzgfOQKYEBtrZV7vBr+Q=="],
    react: ["react.production.min.js",
      "sha512-QVs8Lo43F9lSuBykadDb0oSXDL/BbZ588urWVCRwSIoewQv/Ewg1f84mK3U790bZ0FfhFa1YSQUmIhG+pIRKeg=="],
    reactDom: ["react-dom.production.min.js",
      "sha512-6a1107rTlA4gYpgHAqbwLAtxmWipBdJFcq8y5S/aTge3Bp+VAklABm2LO+Kg51vOWR9JMZq1Ovjl5tpluNpTeQ=="],
    babel: ["babel.min.js",
      "sha512-bAHF//mCdqGSgyUBqhtDgaGLxsraipURsQRGG+3uNncZdsFA6/283u21SOwB6rzINUXSATUMoZaXm4IaV2Lw2Q=="],
  };
  // crossorigin="anonymous" is REQUIRED even though the lib is same-origin to the
  // shell: the artifact runs in a no-same-origin sandbox (opaque origin), so its
  // subresource loads are cross-origin — SRI on a cross-origin script without
  // crossorigin can't validate and the browser blocks it. The vendor route sends
  // Access-Control-Allow-Origin:* to satisfy the CORS fetch.
  function cdn(name){ var c = LIB[name];
    return '<script crossorigin="anonymous" integrity="' + c[1] + '" src="' + ORIGIN + '/plugins/artifact/vendor/' + c[0] + '"><\/script>'; }
  // Curated ESM import map for `react` artifacts (offline-vendored, served same-origin with
  // CORS). Bare specifiers resolve to the vendored modules: react/react-dom via tiny shims that
  // re-export the UMD globals (so the artifact, the @pl/ui wrappers, and any lib share ONE
  // React instance), plus d3 / chart.js / lucide and the authored @pl/ui DS wrappers.
  var V = ORIGIN + "/plugins/artifact/vendor/";
  var IMPORTMAP = JSON.stringify({ imports: {
    "react": V + "react.shim.mjs",
    "react-dom": V + "react-dom-client.shim.mjs",
    "react-dom/client": V + "react-dom-client.shim.mjs",
    "@pl/ui": V + "pl-ui.mjs",
    "d3": V + "d3.mjs",
    "chart.js": V + "chartjs.mjs",
    "chart.js/auto": V + "chartjs.mjs",
    "lucide": V + "lucide.mjs"
  }});
  // Prose styling for markdown, keyed to --pl-* tokens (the DS link supplies component classes).
  var MD_CSS = '#md{max-width:50rem;margin:0 auto;padding:20px;line-height:1.6}'
    + '#md h1,#md h2,#md h3,#md h4{line-height:1.25;margin:1.4em 0 .5em}#md h1{font-size:1.7em}#md h2{font-size:1.35em}#md h3{font-size:1.12em}'
    + '#md a{color:var(--pl-color-accent,#9b87f2)}'
    + '#md code{font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-size:.9em;background:rgba(127,127,127,.16);padding:.15em .35em;border-radius:4px}'
    + '#md pre{background:rgba(127,127,127,.12);padding:12px;border-radius:6px;overflow:auto}#md pre code{background:none;padding:0}'
    + '#md table{border-collapse:collapse}#md th,#md td{border:1px solid var(--pl-color-border,rgba(255,255,255,.14));padding:6px 10px}'
    + '#md blockquote{margin:1em 0;padding-left:1em;border-left:3px solid var(--pl-color-border,rgba(255,255,255,.2));color:var(--pl-color-fg-muted,#9aa0aa)}'
    + '#md img{max-width:100%}#md .mermaid{background:none;border:0;padding:0}';

  // Crisp fit-to-window viewport for the GRAPHIC kinds (svg + mermaid — #1517). Both render an
  // <svg> into the sandboxed frame; scale it as a VECTOR to fit the frame (max-width/height:100%,
  // !important to beat mermaid's inline max-width:Npx on its own svg). No CSS transform / raster
  // layer: transform-scaling rasterized the SVG at 1x then GPU-scaled the bitmap, so zooming in
  // pixelated/blurred on WKWebView. This keeps it sharp at any size (pan/zoom traded for crispness).
  // Self-contained (needs no DS stylesheet) — styled off the base() token carry.
  var VP_CSS = '<style>html,body{margin:0;height:100%;overflow:hidden}'
    + '#__vp{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:12px;box-sizing:border-box}'
    + '#__vp pre.mermaid{display:contents}'
    + '#__vp svg{max-width:100% !important;max-height:100% !important;width:auto !important;height:auto !important;display:block}</style>';
  // Wrap graphic content in the fit-to-window viewport (svg + mermaid share this).
  function viewport(inner){ return VP_CSS + '<body><div id="__vp">' + inner + '</div>'; }

  function srcdoc(kind, code) {
    if (kind === "html") return dsLink() + base(kind) + code;
    if (kind === "svg") return '<!doctype html>' + base(kind) + viewport(code) + '</body>';
    if (kind === "mermaid") return '<!doctype html>' + base(kind) + viewport('<pre class="mermaid">' + esc(code) + '</pre>') +
      cdn("mermaid") +
      '<script>mermaid.initialize({startOnLoad:false,theme:"dark"});'
      + 'try{mermaid.run();}catch(_){}<\/script></body>';
    if (kind === "markdown") return mdDoc(code);
    // `react`: import map + UMD react/react-dom/babel, compiled as a MODULE so `import` works
    // (no-import artifacts still run — they use the UMD React/ReactDOM globals as before).
    // The artifact module + a forgiving AUTO-MOUNT epilogue (appended INSIDE the same babel
    // module so it can see module-scoped `App`): the #1 first-try mistake is defining `App` but
    // never calling render(). If #root is still empty a tick after the module runs and an `App`
    // is in scope, mount <App/> for them. An explicit render() still wins — this fires ONLY when
    // nothing mounted, so it never double-renders a self-mounting artifact.
    if (kind === "react") return '<!doctype html>' + dsLink() + base(kind) + '<body><div id="root"></div>' +
      '<script type="importmap">' + IMPORTMAP + '<\/script>' +
      cdn("react") + cdn("reactDom") + cdn("babel") +
      '<script type="text/babel" data-type="module" data-presets="react">' + code
      + '\n;(function(){var r=document.getElementById("root");if(!r)return;setTimeout(function(){'
      + 'if(r.firstChild)return;'  // the artifact mounted itself — leave it alone
      + 'try{if(typeof App!=="undefined"&&App){var R=window.ReactDOM;'
      + 'if(R&&R.createRoot){R.createRoot(r).render(React.createElement(App));}'
      + 'else if(R&&R.render){R.render(React.createElement(App),r);}}}'
      + 'catch(e){if(window.__artErr)window.__artErr(String((e&&e.message)||e));}'
      + '},0);})();<\/script>' +
      // No-mount guard: if #root is STILL empty (no `App` to auto-mount, or it threw) after a few
      // seconds and nothing else errored, surface an actionable message (also reported up, #1458).
      '<script>(function(){var n=0,t=setInterval(function(){var r=document.getElementById("root");'
      + 'if(r&&r.firstChild){clearInterval(t);if(window.__artOk)window.__artOk();return;}'
      + 'if(++n>=30){clearInterval(t);if(window.__artErr&&!document.getElementById("__arterr"))'
      + 'window.__artErr("Nothing rendered into #root — name your top-level component `App` (it auto-mounts) or call createRoot(document.getElementById(\'root\')).render(<App/>) yourself.");'
      + '}},100);})();<\/script></body>';
    return '<!doctype html>' + base(kind) + '<body style="font-family:sans-serif;padding:16px">unsupported artifact kind</body>';
  }
  // markdown → HTML via the vendored `marked` ESM. The source is base64'd into the module
  // (unicode-safe; sidesteps quote / newline / closing-tag escaping pitfalls). A fenced
  // mermaid block also pulls mermaid in and upgrades those code blocks to live diagrams.
  // DS classes/tokens via dsLink + MD_CSS.
  function mdDoc(code){
    var b64 = btoa(unescape(encodeURIComponent(code)));
    var hasMermaid = code.indexOf("```mermaid") >= 0;
    var mmRun = hasMermaid
      ? 'document.querySelectorAll("#md pre>code.language-mermaid").forEach(function(c){var d=document.createElement("pre");d.className="mermaid";d.textContent=c.textContent;c.parentNode.replaceWith(d);});'
        + 'if(window.mermaid){mermaid.initialize({startOnLoad:false,theme:"dark"});mermaid.run();}'
      : "";
    return '<!doctype html>' + dsLink() + base("markdown") + '<style>' + MD_CSS + '</style>' +
      '<body><div id="md" class="pl-prose"></div>' +
      '<script type="importmap">{"imports":{"marked":"' + V + 'marked.mjs"}}<\/script>' +
      (hasMermaid ? cdn("mermaid") : "") +
      '<script type="module">import { marked } from "marked";' +
      'document.getElementById("md").innerHTML = marked.parse(decodeURIComponent(escape(atob("' + b64 + '"))));' +
      mmRun + '<\/script></body>';
  }
  // `file` artifacts (ADR 0092 D2) don't iframe generated code — they show a static,
  // themed DOWNLOAD CARD whose preview is TYPED by the file's extension: csv/tsv parse
  // into a real table, .json pretty-prints, .md renders through mdDoc (the same sandboxed
  // machinery as the markdown kind), everything else stays a text scroll box. The
  // table/json/text srcdocs carry no scripts, so those sandboxes stay inert; only the
  // .md path runs script, and it's the already-trusted markdown renderer.
  function fmtSize(n){ n=+n||0; return n<1024?n+" B":n<1048576?(n/1024).toFixed(1)+" KB":(n/1048576).toFixed(1)+" MB"; }
  // Mirror of the Python _PREVIEW_TRUNC note (drift-guarded by a test): detect + strip it
  // so a clipped preview doesn't feed the marker into the table/json parsers.
  var TRUNC_MARK="(preview truncated — download the file for the full content)";
  function stripTrunc(code){
    var i=code.lastIndexOf(TRUNC_MARK);
    if(i<0 || i+TRUNC_MARK.length<code.length-2) return {code:code, truncated:false};
    var j=code.lastIndexOf("\n…", i); // the note's own lead-in, appended by _clip
    return {code:code.slice(0, j>=0?j:i), truncated:true};
  }
  function previewKind(name){
    var n=String(name||"").toLowerCase(), i=n.lastIndexOf("."), ext=i<0?"":n.slice(i+1);
    if(ext==="csv"||ext==="tsv") return "table";
    if(ext==="md"||ext==="markdown") return "md";
    if(ext==="json") return "json";
    return "text";
  }
  // Minimal RFC-4180: quoted fields, doubled quotes, delimiters/newlines inside quotes.
  function parseDsv(text, delim){
    var rows=[], row=[], cur="", q=false;
    for(var i=0;i<text.length;i++){
      var c=text[i];
      if(q){ if(c==='"'){ if(text[i+1]==='"'){cur+='"';i++;} else q=false; } else cur+=c; }
      else if(c==='"') q=true;
      else if(c===delim){ row.push(cur); cur=""; }
      else if(c==="\n"){ row.push(cur); rows.push(row); row=[]; cur=""; }
      else if(c!=="\r") cur+=c;
    }
    if(cur!==""||row.length) { row.push(cur); rows.push(row); }
    return rows;
  }
  var TABLE_MAX_ROWS=500;
  function fileCard(v){
    var cs=getComputedStyle(document.documentElement);
    function tok(n,d){ return (cs.getPropertyValue(n)||d).trim(); }
    var bg=tok("--pl-color-bg","#0a0a0c"), fg=tok("--pl-color-fg","#ededed"),
        muted=tok("--pl-color-fg-muted","#9aa0aa"), border=tok("--pl-color-border","rgba(255,255,255,.12)");
    var f=v.file||{}, name=f.filename||"file", mime=f.mime||"application/octet-stream";
    var st=stripTrunc(v.code||""), code=st.code, truncated=st.truncated;
    var pk=previewKind(name);
    if(pk==="md")
      return mdDoc(truncated ? code+"\n\n> *(preview truncated — download the file for the full document)*" : code);
    var thumb = f.thumb
      ? '<img src="'+f.thumb+'" alt="" style="max-width:200px;max-height:200px;border-radius:8px;border:1px solid '+border+'">'
      : '<div style="font-size:44px;line-height:1">📄</div>';
    var body, pvl="Preview";
    if(pk==="table"){
      var rows=parseDsv(code, name.slice(-4)===".tsv" ? "\t" : ",");
      if(truncated && rows.length>1) rows=rows.slice(0,-1); // last row may be mid-cut
      var head=rows[0]||[], data=rows.slice(1), shown=data.slice(0,TABLE_MAX_ROWS);
      body='<div class="tw"><table><thead><tr>'
        + head.map(function(c){return "<th>"+esc(c)+"</th>";}).join("")
        + '</tr></thead><tbody>'
        + shown.map(function(r){return "<tr>"+r.map(function(c){return "<td>"+esc(c)+"</td>";}).join("")+"</tr>";}).join("")
        + '</tbody></table></div>';
      pvl=head.length+" columns × "+data.length+(truncated?"+":"")+" rows"
        + (data.length>shown.length||truncated ? " · first "+shown.length+" shown — download for all" : "");
    } else {
      if(pk==="json" && !truncated){
        try{ code=JSON.stringify(JSON.parse(code), null, 2); }catch(_){ /* not valid JSON → raw */ }
      }
      body='<pre class="pv">'+esc(code)+(truncated?'\n… (preview truncated — download the file for the full content)':'')+'</pre>';
    }
    return '<!doctype html><meta charset="utf-8"><style>'
      + 'html,body{margin:0;height:100%;background:'+bg+';color:'+fg+';font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif)}'
      + '.wrap{display:flex;flex-direction:column;height:100%;box-sizing:border-box;padding:18px;gap:14px}'
      + '.hd{display:flex;gap:14px;align-items:center}.meta{min-width:0}'
      + '.nm{font-size:15px;font-weight:600;word-break:break-all}.mt{color:'+muted+';font-size:12px;margin-top:2px}'
      + '.pvl{color:'+muted+';font-size:11px;text-transform:uppercase;letter-spacing:.05em}'
      + 'pre.pv{flex:1;min-height:0;overflow:auto;margin:0;padding:12px;border:1px solid '+border+';border-radius:8px;'
      + 'background:rgba(127,127,127,.08);white-space:pre-wrap;word-break:break-word;'
      + 'font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-size:12px;line-height:1.5}'
      + '.tw{flex:1;min-height:0;overflow:auto;border:1px solid '+border+';border-radius:8px;background:rgba(127,127,127,.08)}'
      + '.tw table{border-collapse:collapse;width:100%;font-size:12px;line-height:1.4}'
      + '.tw th{position:sticky;top:0;background:'+bg+';text-align:left;font-weight:600;border-bottom:1px solid '+border+'}'
      + '.tw th,.tw td{padding:6px 10px;border-right:1px solid '+border+';white-space:nowrap;max-width:28em;overflow:hidden;text-overflow:ellipsis}'
      + '.tw th:last-child,.tw td:last-child{border-right:0}'
      + '.tw tbody tr:nth-child(even){background:rgba(127,127,127,.06)}'
      + '</style><div class="wrap"><div class="hd">'+thumb
      + '<div class="meta"><div class="nm">'+esc(name)+'</div><div class="mt">'+esc(mime)+' · '+fmtSize(f.size)+'</div></div></div>'
      + '<div class="pvl">'+esc(pvl)+'</div>'+body+'</div>';
  }
  var $art=document.getElementById("art"), $vprev=document.getElementById("vprev"),
      $vnext=document.getElementById("vnext"), $vlabel=document.getElementById("vlabel"),
      $dl=document.getElementById("dl"), $del=document.getElementById("del"),
      $bar=document.getElementById("bar"), $empty=document.getElementById("empty"),
      $frame=document.getElementById("frame"), $edit=document.getElementById("edit"),
      $editor=document.getElementById("editor"), $code=document.getElementById("code"),
      $run=document.getElementById("run"), $cancel=document.getElementById("cancel"),
      $estat=document.getElementById("estat");
  var editing=false;

  // Persist the user's selection (artifact + version + whether to auto-follow the newest) so it
  // STICKS across a tab-away/back — the plugin-view iframe unmounts on tab switch, so the shell
  // reloads fresh; without this it snapped back to the latest artifact. Same-origin page → plain
  // localStorage; best-effort (a blocked/full store just no-ops).
  var SEL_KEY = "protoartifact.sel";
  function saveSel(){ try{ localStorage.setItem(SEL_KEY, JSON.stringify({selId:selId, selVer:selVer, followNewest:followNewest})); }catch(_){} }
  function loadSel(){ try{ var s=JSON.parse(localStorage.getItem(SEL_KEY)||"null");
    if(s&&typeof s==="object"){ selId=s.selId||null; selVer=(s.selVer==null?null:s.selVer); followNewest=(s.followNewest!==false); } }catch(_){} }

  function selArt(){ for(var i=0;i<arts.length;i++) if(arts[i].id===selId) return arts[i]; return arts[0]||null; }
  function verIdx(a){ // selVer clamped to a's range; null/out-of-range → latest (auto-follow)
    if(!a) return 0; var n=a.versions.length;
    return (selVer===null||selVer<0||selVer>n-1) ? n-1 : selVer;
  }
  function rebuildArtSelect(){
    $art.innerHTML="";
    arts.forEach(function(a){
      var o=document.createElement("option"); o.value=a.id;
      o.textContent=(a.id===curId?"● ":"")+(a.title||(a.kind+" artifact"))+"  ·  "+a.kind+"  · v"+a.versions.length;
      $art.appendChild(o);
    });
    var a=selArt(); if(a) $art.value=a.id;
  }
  function render(){
    $bar.style.display = arts.length ? "flex" : "none";
    var a=selArt();
    if(!a){ $empty.style.display="flex"; $frame.style.display="none"; lastRendered=""; return; }
    var vi=verIdx(a), v=a.versions[vi];
    $vlabel.textContent="v"+(vi+1)+"/"+a.versions.length;
    $vprev.disabled = vi<=0; $vnext.disabled = vi>=a.versions.length-1;
    $empty.style.display="none";
    $edit.style.display = a.kind==="file" ? "none" : "";  // a file's preview isn't user-editable
    var key=a.id+"@"+vi;  // re-srcdoc only when the shown version actually changes
    if(key!==lastRendered){ lastRendered=key; renderingId=a.id; renderingVer=vi+1;
      $frame.srcdoc = a.kind==="file" ? fileCard(v) : srcdoc(a.kind, v.code); $frame.style.display="block"; }
  }

  // Live re-theme (#1872): base() bakes the theme tokens into the srcdoc as literal
  // colors, so an app-theme switch after render left the artifact in the stale
  // palette. The DS plugin-kit re-themes THIS page by rewriting the --pl-* tokens on
  // the root element; observe that and push the fresh tokens into the frame (the
  // SHIM applies them in place — no re-srcdoc, so interactive artifact state survives).
  function pushTheme(){
    if(!$frame || !$frame.contentWindow || $frame.style.display==="none") return;
    var cs=getComputedStyle(document.documentElement);
    function tok(n,d){ return (cs.getPropertyValue(n)||d).trim(); }
    var tokens={"--pl-color-bg":tok("--pl-color-bg","#0a0a0c"),"--pl-color-fg":tok("--pl-color-fg","#ededed"),
                "--pl-color-accent":tok("--pl-color-accent","#9b87f2"),"--pl-color-border":tok("--pl-color-border","rgba(255,255,255,.08)")};
    try{ $frame.contentWindow.postMessage({type:"protoArtifact:theme",tokens:tokens},"*"); }catch(_){}
  }
  new MutationObserver(pushTheme).observe(document.documentElement,{attributes:true,attributeFilter:["style","class","data-theme"]});
  // A theme switch can race a render — re-push once the fresh srcdoc has loaded.
  $frame.addEventListener("load", pushTheme);

  $art.addEventListener("change", function(e){
    selId=e.target.value; selVer=null; followNewest=(selId===(curId||(arts[0]&&arts[0].id)));
    saveSel(); render();
  });
  $vprev.addEventListener("click", function(){ var a=selArt(); if(!a)return; var vi=verIdx(a);
    if(vi>0){ selVer=vi-1; followNewest=false; saveSel(); render(); } });
  $vnext.addEventListener("click", function(){ var a=selArt(); if(!a)return; var vi=verIdx(a);
    if(vi<a.versions.length-1){ selVer=vi+1; if(selVer===a.versions.length-1) selVer=null; saveSel(); render(); } });

  function saveBlob(b, name){ var u=URL.createObjectURL(b);
    var el=document.createElement("a"); el.href=u; el.download=name;
    document.body.appendChild(el); el.click(); el.remove(); setTimeout(function(){URL.revokeObjectURL(u);},1000); }
  $dl.addEventListener("click", async function(){
    var a=selArt(); if(!a)return; var vi=verIdx(a), v=a.versions[vi];
    if(a.kind==="file"){  // download the STORED BYTES via the gated blob route (ADR 0092 D2)
      try{ var r=await kit.apiFetch("/api/plugins/artifact/artifact/"+encodeURIComponent(a.id)+"/blob?version="+(vi+1));
        if(!r.ok) throw 0; saveBlob(await r.blob(), (v.file&&v.file.filename)||("artifact-"+a.id)); }
      catch(e){ $dl.textContent="Failed"; setTimeout(function(){ $dl.textContent="Download"; },1800); }
      return;
    }
    saveBlob(new Blob([v.code],{type:"text/plain"}), "artifact-"+a.id+"-v"+(vi+1)+"."+(EXT[a.kind]||"txt"));
  });

  // Inline two-click confirm (no confirm() — a sandboxed plugin iframe may block modals).
  var delArm=null, delT=null, delFlashT=null;
  $del.addEventListener("click", async function(){
    var a=selArt(); if(!a)return;
    clearTimeout(delFlashT);  // an arm-click mid-flash must not get reverted under it
    if(delArm!==a.id){ delArm=a.id; $del.textContent="Confirm?";
      clearTimeout(delT); delT=setTimeout(function(){ if(delArm===a.id){delArm=null;$del.textContent="Delete";} },3000); return; }
    clearTimeout(delT); delArm=null; $del.textContent="Delete";
    // A failed delete must SAY so (#2885) — flash the verdict on the button (the download
    // button's pattern) and KEEP the selection: the artifact still exists.
    try{ var r=await kit.apiFetch("/api/plugins/artifact/artifact/"+encodeURIComponent(a.id),{method:"DELETE"});
      if(!r.ok) throw 0; }
    catch(e){ $del.textContent="Delete failed"; delFlashT=setTimeout(function(){ $del.textContent="Delete"; },1800); return; }
    selId=null; selVer=null; followNewest=true; saveSel(); poll();
  });

  // In-panel code editor — edit the SELECTED version's source and save it as a NEW
  // version (by:"user"), so direct editing never clobbers the agent's versions.
  // The editor is an OPAQUE overlay (#editor is position:absolute, inset:0) that sits
  // ABOVE the artifact frame — so we never hide or re-srcdoc the frame to edit. Tearing
  // it down and re-rendering on EXIT raced the display:block reflow: mermaid then
  // measured its text at 0 size and emitted `transform: translate(undefined, NaN)`,
  // leaving a blank (black) panel that the version-keyed `lastRendered` cache never
  // repainted (→ "went black, needed a page refresh"). Keeping the frame laid out the
  // whole time means any re-render only happens while it's visible and sized.
  function enterEdit(){
    var a=selArt(); if(!a) return; var vi=verIdx(a);
    $code.value=a.versions[vi].code; $estat.textContent="";
    editing=true; $edit.textContent="Editing"; $editor.style.display="flex";
    $empty.style.display="none"; $code.focus();
  }
  function exitEdit(){ editing=false; $edit.textContent="Edit"; $editor.style.display="none"; render(); }
  $edit.addEventListener("click", function(){ editing ? exitEdit() : enterEdit(); });
  $cancel.addEventListener("click", exitEdit);
  $run.addEventListener("click", async function(){
    var a=selArt(); if(!a) return;
    $estat.textContent="Saving…"; $run.disabled=true;
    try{
      var r=await kit.apiFetch("/api/plugins/artifact/artifact/"+encodeURIComponent(a.id),
        {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:$code.value})});
      if(!r.ok) throw 0;
      followNewest=true; saveSel(); await poll(); exitEdit();   // show the just-saved new version
    }catch(e){ $estat.textContent="Save failed"; }
    $run.disabled=false;
  });

  // Agent-callback bridge: an artifact's window.protoArtifact.ask(prompt) posts here;
  // we call the bearer-gated /ask endpoint (a bare agent completion) and post the
  // answer back INTO the artifact frame. e.source-guarded to only our artifact frame —
  // the kit's own protoagent:init handshake messages are ignored here.
  window.addEventListener("message", async function(e){
    if(!$frame || e.source!==$frame.contentWindow) return;
    var m=e.data||{};
    // Render verdict from the sandbox (#1458) → relay to /render-status so the agent's
    // create/edit reply + check_artifact can surface a render failure. Best-effort POST —
    // intentionally silent on error (#2885 exempts it): a fire-and-forget status report,
    // not user-facing data, so there's no lying empty state to correct.
    if(m.type==="protoArtifact:render"){
      if(renderingId){ try{ kit.apiFetch("/api/plugins/artifact/render-status",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:renderingId,version:renderingVer,ok:!!m.ok,error:String(m.error||"").slice(0,2000)})}); }catch(_){} kickPoll(); /* the verdict rewrites the store — pick it up from idle promptly (#2256) */ }
      return;
    }
    if(m.type!=="protoArtifact:ask") return;
    function reply(p){ try{ $frame.contentWindow.postMessage(Object.assign({type:"protoArtifact:result",id:m.id},p),"*"); }catch(_){} }
    try{
      var r=await kit.apiFetch("/api/plugins/artifact/ask",
        {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:m.prompt})});
      if(!r.ok){ var t=""; try{ t=await r.text(); }catch(_){} reply({error:("ask failed ("+r.status+") "+t).slice(0,300)}); return; }
      var d=await r.json(); reply({text:(d&&d.text)||""});
    }catch(err){ reply({error:String(err).slice(0,300)}); }
  });

  // Adaptive cadence + conditional requests (#2256): fast (1.5s) while the store is
  // actually changing, decaying to a slow idle tick; unchanged polls are ETag 304s the
  // server answers without reading the store and the panel skips without re-rendering.
  var POLL_FAST_MS = 1500, POLL_IDLE_MS = 8000, POLL_ACTIVE_WINDOW_MS = 15000;
  var lastEtag = "", storeChangedAt = Date.now(), pollTimer = null;
  // Persistent-failure surfacing (#2885): a broken poll endpoint (401, 504, dead store)
  // must NOT masquerade as "no artifacts" — the default empty state on a failing panel is
  // a lie the operator can't distinguish from a genuinely empty store. After
  // POLL_FAIL_LIMIT consecutive failures, drop an honest error strip (DS danger token on
  // a muted ground) over the stage naming the HTTP status; the NEXT successful poll
  // (200 or 304) clears it. One-off misses stay silent — the strip never flashes on a blip.
  var POLL_FAIL_LIMIT = 3, pollFails = 0;
  function showPollError(status){
    var el=document.getElementById("pollerr");
    if(!el){ el=document.createElement("div"); el.id="pollerr";
      el.style.cssText="position:absolute;left:0;right:0;top:0;z-index:3;padding:8px 12px;font-size:12px;"
        + "color:var(--pl-color-danger,#f85149);background:var(--pl-color-bg-inset,rgba(127,127,127,.12));"
        + "border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border,rgba(255,255,255,.12))";
      document.getElementById("stage").appendChild(el); }
    el.textContent="Couldn't load artifacts: "+(status||"network error")+" — retrying.";
  }
  function pollFailed(status){ if(++pollFails >= POLL_FAIL_LIMIT) showPollError(status); }
  function pollOk(){ pollFails=0; var el=document.getElementById("pollerr"); if(el) el.remove(); }
  async function poll() {
    if (document.hidden) return;  // don't poll while the window is hidden/minimized (desktop perf)
    try {
      var r = await kit.apiFetch("/api/plugins/artifact/history",
        lastEtag ? {headers:{"If-None-Match": lastEtag}} : undefined);
      if (r.status === 304) { pollOk(); return; }  // unchanged — no parse, no DOM churn
      // A non-2xx parses as JSON too ({"detail":…} → arts=[]) — the exact empty-state
      // lie #2885 fixes. Count it as a failure instead of feeding it to the store mirror.
      if (!r.ok) { pollFailed(r.status + (r.statusText ? " " + r.statusText : "")); return; }
      lastEtag = (r.headers && r.headers.get && r.headers.get("ETag")) || "";
      storeChangedAt = Date.now();   // fresh payload ⇒ stay on the fast cadence for a bit
      var d = await r.json(); arts = (d && d.artifacts) || []; curId = (d && d.current) || null;
      if (followNewest) { selId = curId || (arts[0] && arts[0].id) || null; selVer = null; }
      // A pinned selection whose artifact was deleted (not in arts) would strand the panel on a
      // dead id — fall back to auto-follow + newest and re-persist so it doesn't linger.
      else if (selId && !arts.some(function(a){ return a.id===selId; })) {
        followNewest = true; selId = curId || (arts[0] && arts[0].id) || null; selVer = null; saveSel();
      }
      rebuildArtSelect(); render();
      pollOk();
    } catch (e) { pollFailed(""); /* network-level — no HTTP status to name */ }
  }
  function schedulePoll(){
    clearTimeout(pollTimer);
    var idle = (Date.now() - storeChangedAt) > POLL_ACTIVE_WINDOW_MS;
    pollTimer = setTimeout(async function(){ await poll(); schedulePoll(); }, idle ? POLL_IDLE_MS : POLL_FAST_MS);
  }
  function kickPoll(){ storeChangedAt = Date.now(); clearTimeout(pollTimer); poll().then(schedulePoll, schedulePoll); }
  // Boot ONCE, on whichever fires first: the handshake (the bearer arrives with
  // protoagent:init, so the gated history poll authenticates) or a short timer
  // for the no-handshake case (standalone page / older host).
  var booted = false;
  function boot(){ if (booted) return; booted = true; loadSel(); poll(); schedulePoll(); }
  kit.initPluginView(boot);
  setTimeout(boot, 800);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden && booted) kickPoll(); }); // refresh on return
