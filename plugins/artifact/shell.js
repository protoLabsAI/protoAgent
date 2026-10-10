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
    kerr.style.cssText = "padding:14px 16px;color:var(--pl-color-status-error,#f85149);font:13px/1.5 var(--pl-font-sans,system-ui)";
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
  // The version in the frame, for render-status (#1458): its id, 1-based position, and its identity —
  // lifetime number + ts — which the route resolves even after a trim has shifted the position.
  var renderingId = null, renderingVer = 0, renderingN = 0, renderingTs = 0;
  var EXT = { html: "html", svg: "svg", mermaid: "mmd", react: "jsx", "vega-lite": "vl.json" };
  function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;"); }
  // The NESTED artifact iframe (sandboxed, no stylesheet access) gets the live theme
  // injected as literal colors — read the kit-managed tokens at render time.
  // Injected into EVERY artifact (the window.claude.complete analog): the artifact
  // calls window.protoArtifact.ask(prompt) → a Promise that round-trips via the shell
  // (postMessage) to the gated /ask endpoint → the agent → back. parent.postMessage
  // works from the sandbox; the shell validates e.source and calls the bearer-gated
  // endpoint. ask() rejects if the operator hasn't enabled it (ARTIFACT_ASK_ENABLED).
  // send(text) / openLink(url) (ADR 0118 D4) sit next to ask: the shim posts them up to the
  // shell, which SANITY-checks and relays them to the console host (its embedder); the HOST
  // owns the gates (gesture / length / busy / rate for send, https + origin allowlist for
  // openLink) and posts a verdict back down. Each returns a Promise that resolves on accept
  // and rejects with the host's reason (e.g. "the agent is busy"), so the frame can show it.
  // ask stays opt-in and unchanged; send is on by default (D4).
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
    + 'setTimeout(function(){if(w[id]){delete w[id];rej(new Error("ask timed out"));}},60000);});},'
    + 'send:function(text){return new Promise(function(res,rej){var id=++s;w[id]={resolve:res,reject:rej};'
    + 'parent.postMessage({type:"protoArtifact:send",id:id,text:String(text)},"*");'
    + 'setTimeout(function(){if(w[id]){delete w[id];rej(new Error("send timed out"));}},60000);});},'
    + 'openLink:function(url){return new Promise(function(res,rej){var id=++s;w[id]={resolve:res,reject:rej};'
    + 'parent.postMessage({type:"protoArtifact:openLink",id:id,url:String(url)},"*");'
    + 'setTimeout(function(){if(w[id]){delete w[id];rej(new Error("openLink timed out"));}},60000);});}};'
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
  // confirms via the no-mount guard's firstChild check instead (mount is async, post-load), and
  // vega-lite via its embed promise (the chart is drawn after load, and can still fail then).
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
    + 'addEventListener("load",function(){if(W.__artKind!=="react"&&W.__artKind!=="vega-lite")setTimeout(function(){if(!W.__artRep)W.__artOk();},80);});'
    + '})();<\/script>';
  // Height reporting for the embed placement (ADR 0118 D2). Injected ONLY into embed frames
  // (by embedSuffix below) — the panel carries no reporter, so its frames are untouched. A
  // ResizeObserver posts the frame's CONTENT height up to the shell on every size change (plus on
  // load and when the shell asks via protoArtifact:measure); the shell sizes the frame to it and
  // relays it to the console host, which clamps it to [80,1200].
  // Measures the BOX height of <html>/<body> — which embedSuffix's CSS frees to size to content
  // (flow kinds) or pins to a width-proportional box (fill kinds) — NOT
  // documentElement.scrollHeight, which is floored at the viewport (the frame's OWN height) and so
  // could only ever GROW, never shrink, and left the overflow:hidden svg/mermaid frames reporting
  // their own squashed height. Deduped so an unchanged measure doesn't thrash the host. No `</`
  // inside: rides a srcdoc <script>.
  var HEIGHTJS = '<script>(function(){var W=window,D=document,last=-1;'
    + 'function h(){var d=D.documentElement,b=D.body,'
    + 'dh=d&&d.getBoundingClientRect?d.getBoundingClientRect().height:0,'
    + 'bh=b?Math.max(b.scrollHeight||0,b.getBoundingClientRect?b.getBoundingClientRect().height:0):0;'
    + 'return Math.ceil(Math.max(dh,bh));}'
    + 'function send(){var v=h();if(v===last)return;last=v;'
    + 'try{W.parent.postMessage({type:"protoArtifact:height",height:v},"*");}catch(_){}}'
    + 'if(W.ResizeObserver){var ro=new W.ResizeObserver(send);ro.observe(D.documentElement);if(D.body)ro.observe(D.body);}'
    + 'W.addEventListener("load",send);'
    + 'W.addEventListener("message",function(ev){if(((ev.data)||{}).type==="protoArtifact:measure")send();});'
    + 'send();'
    + '})();<\/script>';
  // The CSS reset embedSuffix pairs with HEIGHTJS (ADR 0118 D2). FLOW kinds (html, markdown, react,
  // charts, .md file previews) size to their content, so <html>/<body> are freed to shrink-wrap.
  // FILL kinds (navigable svg/mermaid diagrams, paged decks/PDF/Word, and the scroll-box file
  // cards) have no intrinsic content height — their panel CSS deliberately fills the stage — so
  // they get a width-proportional box clamped to a sane range instead of being squashed to the
  // 150px iframe default. !important so it wins over the kind's own html,body rule regardless of
  // cascade order. No `</` inside: rides a srcdoc <style>.
  var EMBED_FLOW_CSS = 'html,body{height:auto !important;min-height:0 !important}';
  var EMBED_FILL_CSS = 'html,body{height:clamp(260px,62vw,760px) !important;min-height:0 !important}';
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
    // @aiden0z/pptx-renderer 1.3.0 (Apache-2.0), re-wrapped as an IIFE → window.PptxRenderer.
    // Slide previews for .pptx file artifacts; notices in vendor/pptx-renderer.LICENSES.txt.
    pptx: ["pptx-renderer.min.js",
      "sha512-MyPAN9XW0LRMqC0rmeaTshNlkb1GW+ODchhz+wvoAT927kKbrkcDGm05cvGa1mVrgAmFlKfA42YE4CFwfzes3g=="],
    // Vega 6.4.0 / Vega-Lite 6.4.3 / vega-embed 7.3.0 (BSD-3-Clause): the packages' own UMD
    // builds, byte-for-byte (window.vega / window.vegaLite / window.vegaEmbed). `vega-lite`
    // chart artifacts (ADR 0116); notices in vendor/vega.LICENSES.txt.
    vega: ["vega.min.js",
      "sha512-liroOUtDzitgul3BVykJa+eaVI7LaAPY+R+CwBwOmZrJEkcHT7eMi6aFC2u6B7NQaosshIS/wM/yhkNAbj0lTA=="],
    vegaLite: ["vega-lite.min.js",
      "sha512-Tq8tvzbZ581gvQ426FLV3Aq2TnZrQt6TCuvqosm8aB5GJ8NviU6SKrwVkWcXUR7V6WlEtmf7HU8qUDxhQLMbQA=="],
    vegaEmbed: ["vega-embed.min.js",
      "sha512-Z+cCCqLMktM+IFtuAdRPVEwNQiFln47hxAh3zidS/1Aic/Ky0frcJsqn69mhMhIdlou2FgoUoPAm0i0Y5aS44A=="],
    // pdf.js — pdfjs-dist 6.4.299 legacy build (Apache-2.0), byte-for-byte ES modules (loaded with
    // cdnModule). The lib sets globalThis.pdfjsLib; the worker module only sets
    // globalThis.pdfjsWorker, which makes pdf.js run on the frame's main thread — the sandbox CSP
    // forbids workers. PDF page previews; notices in vendor/pdfjs.LICENSES.txt.
    pdfjs: ["pdfjs.min.mjs",
      "sha512-Z/QvVhTGIViDuuSHyCgvsZVOuYQa8CpYD3lis7cG8c17vDGEO8fkstjdeW/58o1qjWEa8IPH3efCurYfgw3PSQ=="],
    // docx-preview 0.4.1 (Apache-2.0) on JSZip 3.10.2 (MIT): the packages' own UMD builds,
    // byte-for-byte (window.JSZip, then window.docx). .docx page previews; notices in
    // vendor/docx-preview.LICENSES.txt.
    jszip: ["jszip.min.js",
      "sha512-/ICos1xr6gGjsbC2d7Z6D0hmKAnNbkCv5Pp803Q75xgHowbEcgI4CVOHMHUux/ZorCuod6dzuJOjfNoCKV0tRw=="],
    docxPreview: ["docx-preview.min.js",
      "sha512-CToErkDzmSle4BCcUm0qqqWrjXJuUd2g0On8SLez8p9Bf6rZHE7oxMdgARb0dOYKeZkH9wblI+J5PF6fxRttYQ=="],
    pdfjsWorker: ["pdfjs-worker.min.mjs",
      "sha512-cgsoOrm2N2zEbj1vccst4py/Wf4vyUBwoMXCSaT7WI/vGKCYc33zBWj2TPeYFpdtESAOHCvzzxKe+lxMDatk9Q=="],
    // three.js r170 (MIT): the package's own self-contained minified ESM build
    // (build/three.module.min.js), byte-for-byte. Resolved via the `three` import-map
    // specifier below for 3D `html`/`react` artifacts; SRI-pinned here like every other
    // vendored module. Notices in vendor/three.LICENSES.txt.
    three: ["three.module.min.js",
      "sha512-zTnt1Hf43YVf2to5DC6GE6cPRSC/xgPJDf3PLQussTsaDak1uHdbnWtIYnOQiL40AIa2OZfFkayQXVdzL1/DqA=="],
  };
  // crossorigin="anonymous" is REQUIRED even though the lib is same-origin to the
  // shell: the artifact runs in a no-same-origin sandbox (opaque origin), so its
  // subresource loads are cross-origin — SRI on a cross-origin script without
  // crossorigin can't validate and the browser blocks it. The vendor route sends
  // Access-Control-Allow-Origin:* to satisfy the CORS fetch.
  function cdn(name, nonce){ var c = LIB[name];
    return '<script crossorigin="anonymous" integrity="' + c[1] + '"' + (nonce ? ' nonce="' + nonce + '"' : '')
      + ' src="' + ORIGIN + '/plugins/artifact/vendor/' + c[0] + '"><\/script>'; }
  // The same, as an ES module script (pdf.js ships only as modules). Module scripts run in
  // document order after parsing, so a later inline module sees what these ones set up.
  function cdnModule(name, nonce){ var c = LIB[name];
    return '<script type="module" crossorigin="anonymous" integrity="' + c[1] + '" nonce="' + nonce + '"'
      + ' src="' + ORIGIN + '/plugins/artifact/vendor/' + c[0] + '"><\/script>'; }
  // Curated ESM import map for `react` artifacts (offline-vendored, served same-origin with
  // CORS). Bare specifiers resolve to the vendored modules: react/react-dom via tiny shims that
  // re-export the UMD globals (so the artifact, the @pl/ui wrappers, and any lib share ONE
  // React instance), plus d3 / chart.js / lucide / three and the authored @pl/ui DS wrappers.
  var V = ORIGIN + "/plugins/artifact/vendor/";
  var IMPORTMAP = JSON.stringify({ imports: {
    "react": V + "react.shim.mjs",
    "react-dom": V + "react-dom-client.shim.mjs",
    "react-dom/client": V + "react-dom-client.shim.mjs",
    "@pl/ui": V + "pl-ui.mjs",
    "d3": V + "d3.mjs",
    "chart.js": V + "chartjs.mjs",
    "chart.js/auto": V + "chartjs.mjs",
    "lucide": V + "lucide.mjs",
    "three": V + "three.module.min.js"
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

  // Navigable viewport for the GRAPHIC kinds (svg + mermaid, and mermaid fences in markdown).
  // Zoom and pan move the root <svg>'s VIEWBOX — the browser re-lays the vector out at every
  // zoom level, so it stays sharp. Never a CSS transform: #1517 removed the old transform zoom
  // because WKWebView (the desktop app) rasterized the SVG at 1x and GPU-scaled the bitmap, which
  // blurred on zoom-in. Wheel / pinch zoom to the cursor, drag to pan, double-click to zoom in,
  // keyboard (+ − 0 f and arrows), and a small toolbar. Big diagrams open fitted to the frame;
  // small ones at 1:1 (never blown up). Self-contained (needs no DS stylesheet) — styled off the
  // base() token carry, so a live re-theme restyles it too.
  var VP_CSS = '<style>html,body{margin:0;height:100%;overflow:hidden}'
    + '#__vp{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden;touch-action:none}'
    + '#__vp pre.mermaid{display:contents}'
    // Before the controller mounts (mermaid still laying out) the svg just fits, as it always did.
    + '#__vp>svg,#__vp>pre>svg{max-width:100%;max-height:100%}'
    + '</style>';
  // Chrome shared by the full-frame viewport and markdown's inline diagram boxes, plus the code
  // link affordances (ADR 0038 amendment). No `</` inside: it rides a srcdoc <style>.
  var GFX_CSS = '<style>'
    + '.__vpsvg{position:absolute;left:0;top:0;width:100% !important;height:100% !important;max-width:none !important;max-height:none !important;display:block;cursor:grab}'
    + '.__vpdrag .__vpsvg{cursor:grabbing}'
    + '.__vpbox{position:relative;overflow:hidden;margin:12px 0;border:1px solid var(--pl-color-border,rgba(255,255,255,.12));border-radius:8px;touch-action:pan-y}'
    + '.__vpbox:focus-visible,#__vp:focus-visible{outline:2px solid var(--pl-color-accent,#9b87f2);outline-offset:-2px}'
    + '.__vptb{position:absolute;top:8px;right:8px;z-index:10;display:flex;align-items:center;gap:1px;padding:2px;border-radius:7px;'
    + 'background:var(--pl-color-bg,#0a0a0c);color:var(--pl-color-fg,#ededed);border:1px solid var(--pl-color-border,rgba(255,255,255,.12));'
    + 'font:12px/1 var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif);box-shadow:0 2px 8px rgba(0,0,0,.25)}'
    + '.__vptb button{all:unset;box-sizing:border-box;min-width:26px;height:24px;padding:0 6px;border-radius:5px;text-align:center;cursor:pointer}'
    + '.__vptb button:hover{background:rgba(127,127,127,.18)}'
    + '.__vptb button:focus-visible{outline:2px solid var(--pl-color-accent,#9b87f2)}'
    + '.__vptb .__vpz{min-width:44px;text-align:center;opacity:.75;font-variant-numeric:tabular-nums}'
    + '.__lk{cursor:pointer}'
    + '.__lk text,.__lk .nodeLabel,.__lk .label{text-decoration:underline dotted;text-underline-offset:3px}'
    + '.__lk:hover :is(rect,polygon,circle,ellipse,path,line):not(.__lkhit),.__lk:focus-visible :is(rect,polygon,circle,ellipse,path,line):not(.__lkhit),'
    + '.__lkhl :is(rect,polygon,circle,ellipse,path,line):not(.__lkhit){stroke:var(--pl-color-accent,#9b87f2) !important;stroke-width:2px !important}'
    + '.__lk:hover text,.__lk:focus-visible text,.__lkhl text{fill:var(--pl-color-accent,#9b87f2) !important;text-decoration-style:solid}'
    + '.__lk:hover .nodeLabel,.__lk:focus-visible .nodeLabel,.__lkhl .nodeLabel{color:var(--pl-color-accent,#9b87f2) !important;text-decoration-style:solid}'
    + '.__lk:focus{outline:none}'
    + '.__lkhit{fill:transparent;stroke:none;pointer-events:all}'
    + '.__lktip{position:fixed;z-index:2147483646;pointer-events:none;max-width:min(440px,calc(100vw - 16px));padding:6px 9px;border-radius:6px;'
    + 'background:var(--pl-color-bg,#0a0a0c);color:var(--pl-color-fg,#ededed);border:1px solid var(--pl-color-border,rgba(255,255,255,.14));'
    + 'box-shadow:0 4px 14px rgba(0,0,0,.35);font:12px/1.45 var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif)}'
    + '.__lktip b{display:block;font:600 12px/1.45 var(--pl-font-mono,ui-monospace,Menlo,monospace);word-break:break-all}'
    + '</style>';

  // The in-frame controller. Authored here as a real function and injected into the sandboxed
  // artifact frame as SOURCE (`'(' + artGraphics + ')(cfg)'`) — it never runs in the shell, so it
  // may reference nothing outside itself. cfg = {links: {key: {where, note}} | null}: DISPLAY data
  // only (tooltips); a click posts just the KEY up, and the shell resolves the target from the
  // version's stored links — the frame (model-authored code) can't name a path of its own.
  // Keep `</` and `<!` out of this function: its source rides a srcdoc <script>.
  function artGraphics(cfg){
    var D=document, W=window, NS="http://www.w3.org/2000/svg";
    var links=(cfg&&cfg.links)||null, own=Object.prototype.hasOwnProperty;
    var reduce=false; try{ reduce=W.matchMedia("(prefers-reduced-motion: reduce)").matches; }catch(_){}
    var views=[], byKey={};

    // The svg's own coordinate box — viewBox, else width/height, else its drawn bbox.
    function natural(svg){
      var vb=svg.viewBox&&svg.viewBox.baseVal;
      if(vb&&vb.width>0&&vb.height>0) return {x:vb.x,y:vb.y,w:vb.width,h:vb.height};
      var w=parseFloat(svg.getAttribute("width")), h=parseFloat(svg.getAttribute("height"));
      if(w>0&&h>0&&!/%/.test(svg.getAttribute("width")+svg.getAttribute("height"))) return {x:0,y:0,w:w,h:h};
      try{ var b=svg.getBBox(); if(b.width>0&&b.height>0) return {x:b.x,y:b.y,w:b.width,h:b.height}; }catch(_){}
      return {x:0,y:0,w:300,h:150};
    }
    // Rendered text below this many CSS px counts as unreadable (see home()).
    var READABLE_PX=11, TB_CLEAR=34;
    // The diagram's typical label size in its own units (svg px at 1:1) — the median of a few
    // labels' font sizes; 0 when it has no text (a pure drawing: fit it whole).
    function textUnits(svg){
      var els=[].slice.call(svg.querySelectorAll("text,.nodeLabel,.label span")).slice(0,24), fs=[];
      els.forEach(function(el){ var v=parseFloat(getComputedStyle(el).fontSize); if(v>0&&(el.textContent||"").trim()) fs.push(v); });
      if(!fs.length) return 0;
      fs.sort(function(a,b){ return a-b; }); return fs[Math.floor(fs.length/2)];
    }
    function btn(label, title, fn){
      var b=D.createElement("button"); b.type="button"; b.textContent=label; b.title=title; b.setAttribute("aria-label",title);
      b.addEventListener("click",function(e){ e.stopPropagation(); fn(); });
      b.addEventListener("pointerdown",function(e){ e.stopPropagation(); });
      b.addEventListener("dblclick",function(e){ e.stopPropagation(); });
      return b;
    }

    // Mount one navigable view on `svg` inside `box`. inline = a diagram inside a markdown
    // document: plain wheel keeps scrolling the page (zoom needs ctrl/⌘ or a pinch).
    function mount(svg, box, inline){
      if(!svg||svg.__vp) return null;
      var vb0=natural(svg), view=null, anim=0, fontUnits=textUnits(svg);
      if(inline){
        var h=Math.round(Math.min(Math.max(vb0.h+24,140), W.innerHeight*0.7));
        box.style.height=h+"px";
      }
      svg.removeAttribute("width"); svg.removeAttribute("height");
      svg.setAttribute("preserveAspectRatio","xMidYMid meet");
      svg.classList.add("__vpsvg");
      if(!box.hasAttribute("tabindex")) box.setAttribute("tabindex","0");
      box.setAttribute("role","group");
      box.setAttribute("aria-label","Diagram — scroll or pinch to zoom, drag to pan, + − 0 and arrow keys");
      function size(){ var r=box.getBoundingClientRect(); return {w:Math.max(1,r.width),h:Math.max(1,r.height)}; }
      function k(v){ return size().w/(v||view).w; }  // screen px per svg unit
      function fitK(cap){ var s=size(), p=12, f=Math.min((s.w-2*p)/vb0.w,(s.h-2*p)/vb0.h); if(!(f>0)) f=1; return cap?Math.min(f,1):f; }
      function around(kk, cx, cy){ var s=size(), w=s.w/kk, h=s.h/kk; return {x:cx-w/2,y:cy-h/2,w:w,h:h}; }
      // The START view. Fit-to-contain (never above 1:1) when that keeps text readable; a tall or
      // wide diagram in a narrow dock would shrink its labels to a smudge (a sequence diagram
      // opened at 34%), so then it opens at fit-to-WIDTH — or at the smallest readable zoom —
      // anchored at the top-left, where the first participants / messages are. "Fit" still
      // shows the whole diagram.
      function home(){
        var kc=fitK(true), fu=fontUnits;
        if(!fu || kc*fu>=READABLE_PX) return around(kc, vb0.x+vb0.w/2, vb0.y+vb0.h/2);
        var s=size(), p=12, kw=Math.min(1,(s.w-2*p)/vb0.w), kk=Math.min(1,Math.max(kw, READABLE_PX/fu));
        var x=vb0.x-p/kk;
        if(kk===kw) x=vb0.x+vb0.w/2-s.w/(2*kk);  // the width fits: centre it
        return {x:x, y:vb0.y-(p+TB_CLEAR)/kk, w:s.w/kk, h:s.h/kk};  // clear the zoom toolbar
      }
      function fit(){ return around(fitK(false), vb0.x+vb0.w/2, vb0.y+vb0.h/2); }
      function clampK(kk){ var lo=Math.min(fitK(false)*0.25,0.5), hi=Math.max(32,fitK(false)*4); return Math.min(hi,Math.max(lo,kk)); }
      function set(v){ view=v; svg.setAttribute("viewBox",v.x+" "+v.y+" "+v.w+" "+v.h); if(zl) zl.textContent=Math.round(k(v)*100)+"%"; }
      function go(v, animate){
        if(anim){ cancelAnimationFrame(anim); anim=0; }
        if(!animate||reduce||!view){ set(v); return; }
        var a=view, t0=performance.now(), dur=160;
        function step(t){ var p=Math.min(1,(t-t0)/dur), e=1-Math.pow(1-p,3);
          set({x:a.x+(v.x-a.x)*e,y:a.y+(v.y-a.y)*e,w:a.w+(v.w-a.w)*e,h:a.h+(v.h-a.h)*e});
          anim=p<1?requestAnimationFrame(step):0; }
        anim=requestAnimationFrame(step);
      }
      // Zoom by `f` keeping the svg point under box-local (px,py) fixed.
      function zoomAt(f, px, py, animate){
        var kk=k(), nk=clampK(kk*f); if(nk===kk) return;
        var ux=view.x+px/kk, uy=view.y+py/kk, s=size();
        go({x:ux-px/nk,y:uy-py/nk,w:s.w/nk,h:s.h/nk}, animate);
      }
      function zoomCenter(f){ var s=size(); zoomAt(f, s.w/2, s.h/2, true); }
      function pan(dx, dy){ var kk=k(); set({x:view.x-dx/kk,y:view.y-dy/kk,w:view.w,h:view.h}); }
      function local(e){ var r=box.getBoundingClientRect(); return {x:e.clientX-r.left,y:e.clientY-r.top}; }

      var tb=D.createElement("div"); tb.className="__vptb"; tb.setAttribute("role","toolbar"); tb.setAttribute("aria-label","Zoom");
      var zl=D.createElement("span"); zl.className="__vpz"; zl.setAttribute("aria-live","polite");
      tb.appendChild(btn("−","Zoom out (−)",function(){ zoomCenter(1/1.25); }));
      tb.appendChild(zl);
      tb.appendChild(btn("+","Zoom in (+)",function(){ zoomCenter(1.25); }));
      tb.appendChild(btn("Fit","Fit the whole diagram (f)",function(){ go(fit(),true); }));
      tb.appendChild(btn("Reset","Reset to the start view (0)",function(){ go(home(),true); }));
      box.appendChild(tb);

      box.addEventListener("wheel",function(e){
        var dx=e.deltaX, dy=e.deltaY, m=e.deltaMode===1?16:e.deltaMode===2?size().h:1;
        dx*=m; dy*=m;
        var pinch=e.ctrlKey||e.metaKey;
        if(inline&&!pinch) return;  // let the document scroll
        e.preventDefault();
        var p=local(e);
        if(pinch) zoomAt(Math.exp(-dy*0.01), p.x, p.y, false);
        else if(e.shiftKey||Math.abs(dx)>Math.abs(dy)) pan(-(dx||dy), 0);
        else zoomAt(Math.exp(-dy*0.0015), p.x, p.y, false);
      },{passive:false});
      // Safari / WKWebView trackpad pinch arrives as gesture events, not ctrl+wheel.
      var g0=1;
      box.addEventListener("gesturestart",function(e){ e.preventDefault(); g0=1; });
      box.addEventListener("gesturechange",function(e){ e.preventDefault(); var p=local(e); zoomAt(e.scale/g0, p.x, p.y, false); g0=e.scale; });

      // Drag to pan (mouse / pen / one finger); two fingers pinch. A drag past a few px
      // swallows the click that ends it, so panning never fires a code link.
      var ptrs={}, dragged=false, start=null, pinch0=0;
      function count(){ return Object.keys(ptrs).length; }
      box.addEventListener("pointerdown",function(e){
        if(e.button!==0&&e.pointerType==="mouse") return;
        if(inline&&e.pointerType==="touch") return;
        ptrs[e.pointerId]={x:e.clientX,y:e.clientY};
        if(count()===1){ start={x:e.clientX,y:e.clientY}; dragged=false; }
        if(count()===2){ var a=Object.keys(ptrs).map(function(i){return ptrs[i];}); pinch0=Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y); }
      });
      box.addEventListener("pointermove",function(e){
        var p=ptrs[e.pointerId]; if(!p) return;
        var dx=e.clientX-p.x, dy=e.clientY-p.y; p.x=e.clientX; p.y=e.clientY;
        if(count()>=2){
          var a=Object.keys(ptrs).map(function(i){return ptrs[i];}), d=Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y);
          if(pinch0>0&&d>0){ var r=box.getBoundingClientRect(); zoomAt(d/pinch0,(a[0].x+a[1].x)/2-r.left,(a[0].y+a[1].y)/2-r.top,false); }
          pinch0=d; dragged=true; return;
        }
        if(!dragged&&start&&Math.hypot(e.clientX-start.x,e.clientY-start.y)<4) return;
        if(!dragged){ dragged=true; box.classList.add("__vpdrag"); try{ box.setPointerCapture(e.pointerId); }catch(_){} }
        pan(dx, dy);
      });
      function up(e){ delete ptrs[e.pointerId]; if(!count()){ box.classList.remove("__vpdrag"); start=null; } }
      box.addEventListener("pointerup",up); box.addEventListener("pointercancel",up);
      box.addEventListener("click",function(e){ if(dragged){ e.stopPropagation(); e.preventDefault(); dragged=false; } },true);
      box.addEventListener("dblclick",function(e){
        if(e.target&&e.target.closest&&e.target.closest(".__lk")) return;  // a link, not a zoom
        var p=local(e); zoomAt(e.shiftKey?0.5:2, p.x, p.y, true);
      });
      // Keyboard: the whole frame for a full-frame diagram, the box itself when inline.
      (inline?box:D).addEventListener("keydown",function(e){
        if(e.defaultPrevented||e.altKey||e.ctrlKey||e.metaKey) return;
        var t=e.target; if(t&&(t.isContentEditable||/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
        var s=size(), step=0.1;
        switch(e.key){
          case "+": case "=": zoomCenter(1.25); break;
          case "-": case "_": zoomCenter(1/1.25); break;
          case "0": go(home(),true); break;
          case "f": case "F": go(fit(),true); break;
          case "ArrowLeft": pan(s.w*step,0); break;
          case "ArrowRight": pan(-s.w*step,0); break;
          case "ArrowUp": pan(0,s.h*step); break;
          case "ArrowDown": pan(0,-s.h*step); break;
          default: return;
        }
        e.preventDefault();
      });
      // A resized frame keeps the same centre and zoom level.
      var last=size();
      if(W.ResizeObserver) new ResizeObserver(function(){
        var s=size(); if(!view||(s.w===last.w&&s.h===last.h)) return;
        var kk=last.w/view.w, cx=view.x+view.w/2, cy=view.y+view.h/2; last=s;
        set(around(kk,cx,cy));
      }).observe(box);
      set(home());
      var api={svg:svg,box:box,reveal:function(el){
        try{ var b=el.getBBox(), inside=b.x>=view.x&&b.y>=view.y&&b.x+b.width<=view.x+view.w&&b.y+b.height<=view.y+view.h;
          if(!inside) go(around(k(),b.x+b.width/2,b.y+b.height/2),true); }catch(_){}
      }};
      svg.__vp=api; views.push(api);
      return api;
    }

    // ── code links (mermaid) ─────────────────────────────────────────────────────────
    var tip=null;
    function tipFor(key){ var t=links[key]||{}; return t; }
    function showTip(key, x, y){
      if(!tip){ tip=D.createElement("div"); tip.className="__lktip"; tip.setAttribute("role","tooltip"); D.body.appendChild(tip); }
      var t=tipFor(key); tip.textContent="";
      var b=D.createElement("b"); b.textContent=String(t.where||""); tip.appendChild(b);
      if(t.note){ var n=D.createElement("span"); n.textContent=String(t.note); tip.appendChild(n); }
      tip.style.display="block";
      var r=tip.getBoundingClientRect(), vw=W.innerWidth, vh=W.innerHeight;
      tip.style.left=Math.max(8,Math.min(x+12,vw-r.width-8))+"px";
      tip.style.top=(y+16+r.height>vh-8?Math.max(8,y-r.height-10):y+16)+"px";
    }
    function hideTip(){ if(tip) tip.style.display="none"; }
    function open(key){ hideTip(); try{ parent.postMessage({type:"protoArtifact:openCode",key:key},"*"); }catch(_){} }
    function mark(el, key, label){
      (byKey[key]=byKey[key]||[]).push(el);
      if(el.__lkKey) return;  // one element, several keys (msg:3 + msg:<label>): one handler, the first key
      el.__lkKey=key;
      el.classList.add("__lk"); el.setAttribute("tabindex","0"); el.setAttribute("role","link");
      el.setAttribute("data-lk",key);
      var t=tipFor(key);
      el.setAttribute("aria-label",(label?label+" — ":"")+"open "+String(t.where||"")+(t.note?" — "+String(t.note):""));
      el.addEventListener("click",function(e){ e.stopPropagation(); open(key); });
      el.addEventListener("keydown",function(e){ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); e.stopPropagation(); open(key); } });
      el.addEventListener("pointerenter",function(e){ showTip(key,e.clientX,e.clientY); });
      el.addEventListener("pointermove",function(e){ showTip(key,e.clientX,e.clientY); });
      el.addEventListener("pointerleave",hideTip);
      el.addEventListener("focus",function(){ var r=el.getBoundingClientRect(); showTip(key,r.left,r.bottom-8); });
      el.addEventListener("blur",hideTip);
    }
    function textOf(el){ return String(el.textContent||"").replace(/​/g,"").replace(/\s+/g," ").trim(); }
    function norm(s){ return String(s).replace(/\s+/g," ").trim(); }
    function attach(svg){
      var found={};
      function hit(key, el, label){ if(!own.call(links,key)||found[key]&&found[key].el===el) return; mark(el,key,label); if(!found[key]) found[key]={el:el,label:label}; }
      // Flowchart / class / state nodes (mermaid 10: g.node#flowchart-<id>-<n>, #classId-<id>-<n>,
      // #state-<id>-<n>) and flowchart subgraphs (g.cluster#<id>). Ids are stable per source.
      svg.querySelectorAll("g.node,g.cluster").forEach(function(g){
        var id=g.id||"", m=/^(?:flowchart|classId|state)-(.+)-\d+$/.exec(id);
        var key=m?m[1]:id; if(key) hit(key,g,textOf(g));
      });
      // Sequence participants: rect.actor[name] (its <g> holds the label) and actor-man g[name];
      // matched by id (`participant:U`) or by display alias (`participant:User`). Top + bottom boxes.
      svg.querySelectorAll("rect.actor[name],g.actor-man[name]").forEach(function(el){
        var g=el.tagName.toLowerCase()==="rect"?el.parentNode:el, name=el.getAttribute("name")||"", label=textOf(g);
        hit("participant:"+name,g,label); if(label&&label!==name) hit("participant:"+label,g,label);
      });
      // Sequence messages, in drawing order: each message is its label line(s) (text.messageText)
      // followed by its arrow (.messageLine0/1). msg:<n> is 1-based; msg:<label> only if unique.
      var msgs=[], cur=[];
      svg.querySelectorAll("text.messageText,.messageLine0,.messageLine1").forEach(function(el){
        if(el.tagName.toLowerCase()==="text"){ cur.push(el); return; }
        msgs.push({texts:cur,line:el}); cur=[];
      });
      var counts={};
      msgs.forEach(function(m){ m.label=norm(m.texts.map(textOf).join(" ")); counts[m.label]=(counts[m.label]||0)+1; });
      msgs.forEach(function(m,i){
        var keys=["msg:"+(i+1)]; if(m.label&&counts[m.label]===1) keys.push("msg:"+m.label);
        Object.keys(links).forEach(function(k){ if(k.indexOf("msg:")===0&&!/^msg:\d+$/.test(k)&&norm(k.slice(4))===m.label&&counts[m.label]===1&&keys.indexOf(k)<0) keys.push(k); });
        var want=keys.filter(function(k){ return own.call(links,k); }); if(!want.length) return;
        var els=m.texts.concat([m.line]), parentEl=m.line.parentNode, same=els.every(function(e){ return e.parentNode===parentEl; });
        var target=m.line;
        if(same){
          var bb=null; els.forEach(function(e){ try{ var b=e.getBBox(); if(!bb) bb={x:b.x,y:b.y,x2:b.x+b.width,y2:b.y+b.height};
            else { bb.x=Math.min(bb.x,b.x); bb.y=Math.min(bb.y,b.y); bb.x2=Math.max(bb.x2,b.x+b.width); bb.y2=Math.max(bb.y2,b.y+b.height); } }catch(_){} });
          var g=D.createElementNS(NS,"g"); parentEl.insertBefore(g,els[0]);
          if(bb){ var r=D.createElementNS(NS,"rect"); r.setAttribute("class","__lkhit");
            r.setAttribute("x",bb.x-4); r.setAttribute("y",bb.y-6); r.setAttribute("width",bb.x2-bb.x+8); r.setAttribute("height",bb.y2-bb.y+12); g.appendChild(r); }
          els.forEach(function(e){ g.appendChild(e); });
          target=g;
        }
        want.forEach(function(k){ hit(k,target,(i+1)+". "+m.label); });
      });
      return found;
    }
    // Tell the shell which keys landed (for the Links list) — labels only, never targets.
    function report(){
      if(!links) return;
      var matched={}; Object.keys(byKey).forEach(function(k){ var el=byKey[k][0]; matched[k]=String(el.__lkLabel||""); });
      try{ parent.postMessage({type:"protoArtifact:linkmap",matched:matched},"*"); }catch(_){}
    }
    // The shell's Links list highlights (and scrolls to) an element on hover / focus.
    W.addEventListener("message",function(e){
      if(e.source!==parent) return;
      var m=e.data||{}; if(m.type!=="protoArtifact:highlight") return;
      D.querySelectorAll(".__lkhl").forEach(function(el){ el.classList.remove("__lkhl"); });
      var els=typeof m.key==="string"&&own.call(byKey,m.key)?byKey[m.key]:null; if(!els) return;
      els.forEach(function(el){ el.classList.add("__lkhl"); });
      var svg=els[0].ownerSVGElement; while(svg&&svg.ownerSVGElement) svg=svg.ownerSVGElement;
      if(svg&&svg.__vp&&m.reveal) svg.__vp.reveal(els[0]);
    });

    W.__artVP={
      // The full-frame kinds: the first root <svg> in #__vp (mermaid draws it inside its <pre>).
      full:function(){
        var vp=D.getElementById("__vp"); if(!vp) return;
        var svg=vp.querySelector(":scope>svg,:scope>pre.mermaid>svg"); if(!svg) return;
        mount(svg,vp,false);
        if(links){ var f=attach(svg); Object.keys(f).forEach(function(k){ byKey[k].forEach(function(el){ el.__lkLabel=f[k].label; }); }); report(); }
      },
      // markdown: every rendered ```mermaid fence gets its own inline box (no code links there).
      inline:function(){
        D.querySelectorAll("pre.mermaid>svg").forEach(function(svg){
          var pre=svg.parentNode, box=D.createElement("div"); box.className="__vpbox";
          pre.parentNode.insertBefore(box,pre); box.appendChild(svg); pre.remove();
          mount(svg,box,true);
        });
      }
    };
  }
  // Mermaid's own palette follows the console's ground: "dark" on a dark theme, "default" on a
  // light one — the hardcoded "dark" drew light-grey message labels on a light panel.
  function mermaidTheme(){
    var bg=(getComputedStyle(document.documentElement).getPropertyValue("--pl-color-bg")||"").trim(), m, l=0;
    if((m=/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(bg))){ var h=m[1].length===3?m[1].replace(/./g,"$&$&"):m[1];
      l=(0.299*parseInt(h.slice(0,2),16)+0.587*parseInt(h.slice(2,4),16)+0.114*parseInt(h.slice(4,6),16))/255; }
    else if((m=/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i.exec(bg))) l=(0.299*m[1]+0.587*m[2]+0.114*m[3])/255;
    else return "dark";
    return l>0.5 ? "default" : "dark";
  }
  // The controller + its config as one srcdoc script. JSON is made script-safe (`<` escaped),
  // so a note or key holding a script close tag can't end the tag early.
  function gfxScript(cfg){
    return '<script>(' + artGraphics.toString() + ')(' + JSON.stringify(cfg||{}).replace(/</g,"\\u003c") + ');<\/script>';
  }
  // Display data for the frame's tooltips: {key: {where:"project/path:line[-end]", note}}.
  function linkDisplay(links){
    if(!links || typeof links!=="object") return null;
    var out={}, n=0;
    Object.keys(links).forEach(function(k){ var t=links[k]; if(!t||typeof t!=="object") return;
      out[k]={where: linkWhere(t), note: String(t.note||"")}; n++; });
    return n ? out : null;
  }
  function linkWhere(t){
    var w=String(t.project||"")+"/"+String(t.path||"")+":"+(+t.line||1);
    if(+t.end_line && +t.end_line!==+t.line) w+="-"+(+t.end_line);
    return w;
  }
  // Wrap graphic content in the navigable viewport (svg + mermaid share this).
  function viewport(inner){ return VP_CSS + GFX_CSS + '<body><div id="__vp">' + inner + '</div>'; }

  // An `html` artifact that is a FULL document must keep its own prologue FIRST. Prepending the
  // DS link + base() ahead of its `<!doctype …>` put content before the doctype, and a doctype —
  // or a `<head>` tag — that follows content is a parse error the parser DISCARDS: the panel
  // showed the document with no doctype (document.doctype null) and without its <head>
  // attributes. (It was never quirks mode: a srcdoc document is always no-quirks.)
  // A document therefore gets the injection INSIDE its head: right after `<head>`, else after
  // `<html>`, else after the doctype — still ahead of the author's own styles and scripts, so
  // they override the base exactly as before. The match is ANCHORED at the start (a BOM,
  // whitespace, comments or an XML prolog may precede, as the parser allows), so markup a
  // document merely mentions later is never mistaken for its prologue. A fragment (no leading
  // doctype or `<html>`) keeps the plain prepend. Unit-tested from Python off this regex source.
  var DOC_PROLOGUE = /^\uFEFF?(?:\s|<!--[\s\S]*?-->|<\?[^>]*>)*(<!doctype[^>]*>)?(?:\s|<!--[\s\S]*?-->)*(<html(?:\s[^>]*)?>)?(?:\s|<!--[\s\S]*?-->)*(<head(?:\s[^>]*)?>)?/i;
  function htmlDoc(code, inject){
    var m = DOC_PROLOGUE.exec(code);
    if (!m || !(m[1] || m[2])) return inject + code;  // a fragment
    return m[0] + inject + code.slice(m[0].length);
  }

  // An html artifact may ship its OWN <script type="importmap">. Only the first import map in a
  // document takes effect, so stacking the shell's ahead of it would silently shadow the
  // author's. Merge instead: the author's entries win, the shell's fill in the curated
  // specifiers the author didn't name, and the author's tag is lifted out so ONE map remains.
  // An author map that isn't valid JSON is left exactly as written and the shell's is not
  // injected, which is the pre-ADR-0118 behavior for that artifact.
  var AUTHOR_IMPORTMAP = /<script\b[^>]*\btype\s*=\s*["']?importmap["']?[^>]*>([\s\S]*?)<\/script\s*>/i;
  function htmlImportMap(code){
    var m = AUTHOR_IMPORTMAP.exec(code);
    if (!m) return {code: code, map: IMPORTMAP};
    var own;
    try { own = JSON.parse(m[1]); } catch (e) { return {code: code, map: null}; }
    if (!own || typeof own !== "object" || Array.isArray(own)) return {code: code, map: null};
    var merged = {imports: Object.assign({}, JSON.parse(IMPORTMAP).imports, own.imports || {})};
    if (own.scopes) merged.scopes = own.scopes;
    if (own.integrity) merged.integrity = own.integrity;
    return {code: code.slice(0, m.index) + code.slice(m.index + m[0].length), map: JSON.stringify(merged)};
  }

  function srcdoc(kind, code, links) {
    // `html` gets the SAME curated ESM import map as `react` (injected ahead of the author's
    // own markup by htmlDoc, so it precedes any `<script type="module">` the artifact ships):
    // a plain html artifact can then `import * as THREE from "three"` (or d3 / chart.js / lucide)
    // and the bare specifier resolves to the same-origin vendored module — the three.js (ADR 0118
    // D6) support promised in the changelog/LICENSES only works because of this map, not base().
    // An author's own import map is merged into it, never shadowed (htmlImportMap).
    if (kind === "html") {
      var im = htmlImportMap(code);
      return htmlDoc(im.code, dsLink() + base(kind) + (im.map ? '<script type="importmap">' + im.map + '<\/script>' : ''));
    }
    if (kind === "svg") return '<!doctype html>' + base(kind) + viewport(code) + gfxScript({}) +
      '<script>__artVP.full();<\/script></body>';
    // mermaid.run() is async: the viewport + code links mount once the <svg> exists. A rejected
    // run stays UNHANDLED on purpose — ERRBOOT's unhandledrejection hook reports it (#1458).
    if (kind === "mermaid") return '<!doctype html>' + base(kind) + viewport('<pre class="mermaid">' + esc(code) + '</pre>') +
      cdn("mermaid") + gfxScript({links: linkDisplay(links)}) +
      '<script>mermaid.initialize({startOnLoad:false,theme:' + JSON.stringify(mermaidTheme()) + '});'
      + 'mermaid.run().then(function(){__artVP.full();});<\/script></body>';
    if (kind === "markdown") return mdDoc(code);
    if (kind === "vega-lite") return vegaDoc(code);
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
        + 'if(window.mermaid){mermaid.initialize({startOnLoad:false,theme:' + JSON.stringify(mermaidTheme()) + '});mermaid.run().then(function(){__artVP.inline();});}'
      : "";
    return '<!doctype html>' + dsLink() + base("markdown") + '<style>' + MD_CSS + '</style>' +
      (hasMermaid ? GFX_CSS : "") +
      '<body><div id="md" class="pl-prose"></div>' +
      '<script type="importmap">{"imports":{"marked":"' + V + 'marked.mjs"}}<\/script>' +
      (hasMermaid ? cdn("mermaid") + gfxScript({}) : "") +
      '<script type="module">import { marked } from "marked";' +
      'document.getElementById("md").innerHTML = marked.parse(decodeURIComponent(escape(atob("' + b64 + '"))));' +
      mmRun + '<\/script></body>';
  }
  // `file` artifacts (ADR 0092 D2) don't iframe generated code — they show a static,
  // themed DOWNLOAD CARD whose preview is TYPED by the file's extension: csv/tsv parse
  // into a real table, .xlsx into one table per sheet, .json pretty-prints, .md renders
  // through mdDoc (the same sandboxed machinery as the markdown kind), everything else stays a
  // text scroll box. The table/json/text srcdocs carry no scripts, so those sandboxes stay
  // inert; only the .md path runs script, and it's the already-trusted markdown renderer.
  // .docx / .pdf / .pptx get their own vendored renderers (docxDoc / pdfDoc / slidesDoc).
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
  // .pptx decks render as real slides (the vendored renderer, see slidesDoc); _slides.is_slides
  // is the Python twin of this test (by extension, or the OOXML presentation mime). PDFs render
  // as real pages (vendored pdf.js, see pdfDoc); _pdfview.is_pdf is the twin of that test.
  var PPTX_MIME="application/vnd.openxmlformats-officedocument.presentationml.presentation";
  var DOCX_MIME="application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  function previewKind(name, mime){
    var n=String(name||"").toLowerCase(), i=n.lastIndexOf("."), ext=i<0?"":n.slice(i+1);
    if(ext==="pptx" || String(mime||"").toLowerCase()===PPTX_MIME) return "slides";
    if(ext==="pdf" || String(mime||"").toLowerCase()==="application/pdf") return "pdf";
    if(ext==="docx" || String(mime||"").toLowerCase()===DOCX_MIME) return "docx";
    if(ext==="xlsx") return "sheets";
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
  // .csv delimiter: comma unless the header row clearly uses ; (European Excel), tab or |.
  // Counted outside quotes on the first non-empty line only — cheap, and right for real files.
  function sniffDelim(text){
    var line=(text.split("\n").find(function(l){ return l.trim()!==""; })||""), q=false, n={",":0,";":0,"\t":0,"|":0};
    for(var i=0;i<line.length;i++){ var c=line[i]; if(c==='"') q=!q; else if(!q && n.hasOwnProperty(c)) n[c]++; }
    var best=",";
    [";","\t","|"].forEach(function(d){ if(n[d]>n[best]) best=d; });
    return best;
  }
  // A parsed DSV → {html, label}: header row, then up to TABLE_MAX_ROWS rows. A column whose
  // every non-empty cell is a number is right-aligned (prices, counts read down the column).
  function dsvTable(rows, truncated){
    var head=rows[0]||[], data=rows.slice(1), shown=data.slice(0,TABLE_MAX_ROWS), w=head.length;
    shown.forEach(function(r){ if(r.length>w) w=r.length; });
    var num=[];
    for(var c=0;c<w;c++){
      var any=false, all=true;
      shown.forEach(function(r){ var v=(r[c]||"").trim(); if(v===""){ return; } any=true;
        if(v==="-"||v==="\u2013"||v==="\u2014") return;  // a dash is "none", not text
        if(!/^[-+]?[$£€]?\(?[\d,]*\.?\d+\)?%?$/.test(v)) all=false; });
      num.push(any&&all);
    }
    function cell(tag, v, c){ return "<"+tag+(num[c]?' class="n"':"")+">"+esc(v)+"</"+tag+">"; }
    var html='<div class="tw"><table><thead><tr>'
      + Array.from({length:w}, function(_,c){ return cell("th", head[c]||"", c); }).join("")
      + '</tr></thead><tbody>'
      + shown.map(function(r){ return "<tr>"+Array.from({length:w}, function(_,c){ return cell("td", r[c]||"", c); }).join("")+"</tr>"; }).join("")
      + '</tbody></table></div>';
    var label=w+" columns × "+data.length+(truncated?"+":"")+" rows"
      + (data.length>shown.length||truncated ? " · first "+shown.length+" shown — download for all" : "");
    return {html:html, label:label};
  }
  // The .xlsx preview is CSV per sheet, each opened by a "### sheet: <name>" line (_preview.py).
  function splitSheets(code){
    var out=[], cur=null;
    code.split("\n").forEach(function(line){
      var m=/^### sheet: (.*)$/.exec(line);
      if(m){ cur={name:m[1], lines:[]}; out.push(cur); }
      else if(cur) cur.lines.push(line);
    });
    return out.map(function(s){ return {name:s.name, csv:s.lines.join("\n").replace(/\n+$/,"")}; });
  }
  // Does this version get the slide renderer? ONLY a .pptx the save-time preflight (_slides.py)
  // cleared — it inflated every entry under a budget, so the frame never parses bytes the server
  // hasn't measured. A refusal, or a version saved before the preflight existed (no verdict),
  // gets the text outline card.
  function slidesOk(v){
    var f=v.file||{};
    if(previewKind(f.filename, f.mime)!=="slides") return false;
    return !!(f.slides && typeof f.slides==="object" && f.slides.render===true);
  }
  // Does this version get the page renderer? ONLY a PDF the save-time preflight (_pdfview.py)
  // cleared — it decoded every stream the renderer will decode under a budget. A refusal, or a
  // version saved before the preflight existed (no verdict), gets the extracted-text card.
  function pdfOk(v){
    var f=v.file||{};
    if(previewKind(f.filename, f.mime)!=="pdf") return false;
    return !!(f.pdf && typeof f.pdf==="object" && f.pdf.render===true);
  }
  // Does this version get the Word renderer? ONLY a .docx the save-time preflight (_docx.py)
  // cleared — it inflated every entry and measured every image under a budget.
  function docxOk(v){
    var f=v.file||{};
    if(previewKind(f.filename, f.mime)!=="docx") return false;
    return !!(f.docx && typeof f.docx==="object" && f.docx.render===true);
  }
  // `note` (optional) forces the text card and says why the slides/pages aren't shown.
  function fileCard(v, note){
    var f=v.file||{}, name=f.filename||"file", mime=f.mime||"application/octet-stream";
    var pk=previewKind(name, mime);
    if(pk==="pdf" && !note){
      if(pdfOk(v)) return pdfDoc(v);
      note="Page preview unavailable — "+(f.pdf&&typeof f.pdf==="object"
        ? String(f.pdf.reason||"the file failed the safety checks")
        : "this version was saved before page previews; re-save the file to render its pages");
    }
    if(pk==="docx" && !note){
      if(docxOk(v)) return docxDoc(v);
      note="Page preview unavailable — "+(f.docx&&typeof f.docx==="object"
        ? String(f.docx.reason||"the file failed the safety checks")
        : "this version was saved before document previews; re-save the file to render its pages");
    }
    if(pk==="slides" && !note){
      if(slidesOk(v)) return slidesDoc(v);
      note="Slide preview unavailable — "+(f.slides&&typeof f.slides==="object"
        ? String(f.slides.reason||"the file failed the safety checks")
        : "this version was saved before slide previews; re-save the file to render its slides");
    }
    var cs=getComputedStyle(document.documentElement);
    function tok(n,d){ return (cs.getPropertyValue(n)||d).trim(); }
    var bg=tok("--pl-color-bg","#0a0a0c"), fg=tok("--pl-color-fg","#ededed"),
        muted=tok("--pl-color-fg-muted","#9aa0aa"), border=tok("--pl-color-border","rgba(255,255,255,.12)");
    var st=stripTrunc(v.code||""), code=st.code, truncated=st.truncated;
    if(pk==="md")
      return mdDoc(truncated ? code+"\n\n> *(preview truncated — download the file for the full document)*" : code);
    var thumb = f.thumb
      ? '<img src="'+f.thumb+'" alt="" style="max-width:200px;max-height:200px;border-radius:8px;border:1px solid '+border+'">'
      : '<div style="font-size:44px;line-height:1">📄</div>';
    var body, pvl=note ? (pk==="pdf"||pk==="docx" ? "Extracted text" : "Text outline") : "Preview";
    if(pk==="table"){
      code=code.replace(/^\uFEFF/, "");  // Excel's UTF-8 BOM would otherwise prefix the first header
      var rows=parseDsv(code, name.slice(-4).toLowerCase()===".tsv" ? "\t" : sniffDelim(code));
      if(truncated && rows.length>1) rows=rows.slice(0,-1); // last row may be mid-cut
      var tb=dsvTable(rows, truncated);
      body=tb.html; pvl=tb.label;
    } else if(pk==="sheets"){
      var sheets=splitSheets(code), parts=[];
      sheets.forEach(function(sh){
        var rows=parseDsv(sh.csv, ",");
        if(truncated && sh===sheets[sheets.length-1] && rows.length>1) rows=rows.slice(0,-1);
        var tb=dsvTable(rows, truncated && sh===sheets[sheets.length-1]);
        parts.push('<div class="sh">'+esc(sh.name)+' <span>'+esc(tb.label)+'</span></div>'+tb.html);
      });
      body=parts.length ? '<div class="sheets">'+parts.join("")+'</div>' : '<pre class="pv">'+esc(code)+'</pre>';
      pvl=sheets.length+" sheet"+(sheets.length===1?"":"s");
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
      + '.tw th.n,.tw td.n{text-align:right;font-variant-numeric:tabular-nums}'
      + '.sheets{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:6px}'
      + '.sheets .tw{flex:none;max-height:none}'
      + '.wrap>.tw{flex:0 1 auto}'
      + '.sh{font-size:12px;font-weight:600;margin-top:8px}.sh span{font-weight:400;color:'+muted+'}'
      + '.tw tbody tr:nth-child(even){background:rgba(127,127,127,.06)}'
      + '.note{font-size:12px;color:'+fg+';padding:8px 10px;border-radius:6px;border:1px solid '+border+';background:rgba(127,127,127,.1)}'
      + '</style><div class="wrap"><div class="hd">'+thumb
      + '<div class="meta"><div class="nm">'+esc(name)+'</div><div class="mt">'+esc(mime)+' · '+fmtSize(f.size)+'</div></div></div>'
      + (note ? '<div class="note" role="status">'+esc(note)+'</div>' : '')
      + '<div class="pvl">'+esc(pvl)+'</div>'+body+'</div>';
  }
  // ── .pptx slide previews ──────────────────────────────────────────────────────────────
  // A deck renders as REAL slides — the vendored @aiden0z/pptx-renderer (window.PptxRenderer)
  // lays each slide out as HTML/SVG, so it stays sharp at any panel width or zoom. It runs in the
  // same no-same-origin sandbox as every artifact, under a nonce CSP (no inline handlers, no
  // javascript: URLs, no network: connect/img/media/font are blob:/data: only). The frame can't
  // authenticate, so the SHELL fetches the gated blob and transfers the bytes in by postMessage.
  //
  // Caps — the mirror of _slides.py (drift-guarded by a test). The save-time preflight checks the
  // zip's directory; the frame re-checks the ACTUAL inflated bytes (a hostile directory can lie):
  // file size, entry count, per-entry / total / media inflation, slide count, and image pixels
  // (an over-cap image is swapped for a placeholder, never decoded). parseMs bounds the frame's
  // own parse; watchdogMs is the shell's backstop that swaps in the text outline.
  var PPTX_CAPS={
    maxBytes: 41943040,         // 40 MB — _slides.MAX_BYTES
    maxEntries: 4000,           // _slides.MAX_ENTRIES
    maxEntryBytes: 33554432,    // 32 MB — _slides.MAX_ENTRY_BYTES
    maxTotalBytes: 268435456,   // 256 MB — _slides.MAX_TOTAL_BYTES
    maxMediaBytes: 201326592,   // 192 MB of inflated media
    maxImagePixels: 50000000,   // _slides.MAX_IMAGE_PIXELS
    maxDeckPixels: 150000000,   // _slides.MAX_DECK_PIXELS — all images together
    maxSlides: 1000,            // _slides.MAX_SLIDES
    parseMs: 20000,
    watchdogMs: 45000
  };
  function cspNonce(){
    var a=new Uint8Array(18); crypto.getRandomValues(a);
    return btoa(String.fromCharCode.apply(null, a)).replace(/[^A-Za-z0-9]/g, "");
  }
  // Script-free fallback is the text outline (the same projection the card always showed),
  // kept in a <details> under the slides: a screen-reader-friendly read of the deck, and the
  // whole view when the renderer can't parse the file.
  function slidesDoc(v){
    var cs=getComputedStyle(document.documentElement);
    function tok(n,d){ return (cs.getPropertyValue(n)||d).trim(); }
    var f=v.file||{}, name=f.filename||"deck.pptx", mime=f.mime||PPTX_MIME, nonce=cspNonce();
    var st=stripTrunc(v.code||"");
    var csp="default-src 'none'; script-src 'nonce-"+nonce+"'; style-src 'unsafe-inline'; img-src blob: data:; "
      + "media-src blob:; font-src blob: data:; connect-src 'none'; worker-src 'none'; frame-src 'none'; "
      + "object-src 'none'; base-uri 'none'; form-action 'none'";
    var tokens=":root{--pl-color-bg:"+tok("--pl-color-bg","#0a0a0c")+";--pl-color-fg:"+tok("--pl-color-fg","#ededed")
      + ";--pl-color-fg-muted:"+tok("--pl-color-fg-muted","#9aa0aa")+";--pl-color-border:"+tok("--pl-color-border","rgba(255,255,255,.12)")
      + ";--pl-color-accent:"+tok("--pl-color-accent","#9b87f2")+"}";
    var cfg={caps:PPTX_CAPS, count:+((f.slides&&f.slides.count)||0)};
    return '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'+csp+'">'
      + '<style>'+tokens+SLIDES_CSS+'</style>'
      + '<div class="wrap"><div class="hd"><div class="ic" aria-hidden="true">PPTX</div>'
      + '<div class="meta"><div class="nm">'+esc(name)+'</div><div class="mt">'+esc(mime)+' · '+fmtSize(f.size)+'</div></div>'
      + '<div class="nav" id="nav" hidden><button id="prev" type="button" aria-label="Previous slide" title="Previous slide (←)">‹</button>'
      + '<span id="pos" aria-live="polite"></span>'
      + '<button id="next" type="button" aria-label="Next slide" title="Next slide (→)">›</button></div></div>'
      + '<div class="stage" id="stage" tabindex="0" role="group" aria-roledescription="slide deck" aria-label="Slides">'
      + '<div id="main"></div><div id="status" class="st" role="status">Rendering slides…</div></div>'
      + '<div class="strip" id="strip" role="tablist" aria-label="Slides" hidden></div>'
      + '<details id="ol"><summary>Text outline</summary><pre class="pv">'+esc(st.code)
      + (st.truncated?'\n… (preview truncated — download the file for the full content)':'')+'</pre></details></div>'
      + '<div id="host" aria-hidden="true"></div>'
      + cdn("pptx", nonce)
      + '<script nonce="'+nonce+'">(' + artSlides.toString() + ')(' + JSON.stringify(cfg).replace(/</g,"\\u003c") + ');<\/script>';
  }
  var SLIDES_CSS='html,body{margin:0;height:100%;background:var(--pl-color-bg);color:var(--pl-color-fg);'
    + 'font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif);overflow:hidden}'
    + '.wrap{display:flex;flex-direction:column;height:100%;box-sizing:border-box;padding:14px 16px;gap:10px}'
    + '.hd{display:flex;gap:12px;align-items:center;flex:none}.meta{min-width:0;flex:1}'
    + '.ic{flex:none;width:38px;height:38px;border-radius:8px;display:flex;align-items:center;justify-content:center;'
    + 'font:700 9px/1 var(--pl-font-sans,system-ui);letter-spacing:.04em;color:#fff;background:#c4532d}'
    + '.nm{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.mt{color:var(--pl-color-fg-muted);font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.nav{display:flex;align-items:center;gap:4px;flex:none}.nav[hidden]{display:none}'
    + '.nav button{all:unset;box-sizing:border-box;width:28px;height:28px;border-radius:6px;text-align:center;font-size:18px;line-height:26px;'
    + 'cursor:pointer;border:1px solid var(--pl-color-border)}'
    + '.nav button:hover{background:rgba(127,127,127,.16)}.nav button[disabled]{opacity:.35;cursor:default}'
    + '.nav button:focus-visible,.th:focus-visible,.stage:focus-visible,summary:focus-visible{outline:2px solid var(--pl-color-accent);outline-offset:2px}'
    + '#pos{min-width:56px;text-align:center;font-size:12px;color:var(--pl-color-fg-muted);font-variant-numeric:tabular-nums}'
    + '.stage{flex:1;min-height:0;position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;outline:none}'
    + '.stage.failed{flex:none}'
    + '#main{line-height:0;border-radius:6px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.25),0 8px 28px rgba(0,0,0,.28);cursor:pointer}'
    + '#main:empty{display:none}'
    + '.st{font-size:12px;color:var(--pl-color-fg-muted);padding:6px 0}.st:empty{display:none}'
    + '.st.err{color:var(--pl-color-fg);padding:8px 10px;border-radius:6px;border:1px solid var(--pl-color-border);background:rgba(127,127,127,.1);align-self:stretch}'
    + '.strip{flex:none;display:flex;gap:8px;overflow-x:auto;overflow-y:hidden;padding:4px 2px 8px;scrollbar-width:thin}'
    + '.strip[hidden]{display:none}'
    + '.th{all:unset;box-sizing:border-box;position:relative;flex:none;width:112px;border-radius:5px;cursor:pointer;overflow:hidden;'
    + 'line-height:0;background:rgba(127,127,127,.14);outline:1px solid var(--pl-color-border);outline-offset:0}'
    + '.th[aria-selected="true"]{outline:2px solid var(--pl-color-accent);outline-offset:1px}'
    + '.th .num{position:absolute;left:4px;bottom:4px;z-index:2;font:600 10px/14px var(--pl-font-sans,system-ui);padding:0 4px;border-radius:3px;'
    + 'background:rgba(0,0,0,.55);color:#fff}'
    + '.th .tb{pointer-events:none}'
    + 'details{flex:none;font-size:12px;min-height:0}details[open]{flex:1;display:flex;flex-direction:column}'
    + 'summary{cursor:pointer;color:var(--pl-color-fg-muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding:2px 0}'
    + 'pre.pv{flex:1;min-height:0;max-height:100%;overflow:auto;margin:6px 0 0;padding:12px;border:1px solid var(--pl-color-border);border-radius:8px;'
    + 'background:rgba(127,127,127,.08);white-space:pre-wrap;word-break:break-word;'
    + 'font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-size:12px;line-height:1.5}'
    + '#host{position:absolute;left:-100000px;top:0;width:960px;height:540px;overflow:hidden;visibility:hidden}';

  // The in-frame slide controller. Like artGraphics it's authored here and injected as SOURCE, so it
  // may reference nothing outside itself. Keep `</` and `<!` out of it: it rides a srcdoc <script>.
  function artSlides(cfg){
    var D=document, W=window, P=W.PptxRenderer, caps=(cfg&&cfg.caps)||{};
    function $(id){ return D.getElementById(id); }
    var stage=$("stage"), main=$("main"), strip=$("strip"), status=$("status"), ol=$("ol"),
        nav=$("nav"), pos=$("pos"), prev=$("prev"), next=$("next"), host=$("host");
    var viewer=null, cur=0, count=0, ratio=9/16, mainHandle=null, thumbs=[], failed=false, timer=0, lastW=0;
    function post(m){ try{ W.parent.postMessage(m, "*"); }catch(_){} }
    function mb(n){ return Math.round(n/1048576)+" MB"; }
    function fail(reason){
      if(failed) return; failed=true; clearTimeout(timer);
      status.textContent="Couldn't render these slides: "+reason+". The text outline is below.";
      status.className="st err"; main.textContent=""; strip.hidden=true; nav.hidden=true;
      stage.classList.add("failed"); ol.open=true;
      post({type:"protoArtifact:pptx", state:"failed", reason:String(reason).slice(0,300)});
    }
    // A deck can carry hyperlinks; only plain web links stay clickable, and even those can't open
    // anything (the sandbox grants no popups or top navigation). Anything else loses its href.
    function tidy(root){
      var as=root.querySelectorAll ? root.querySelectorAll("a[href]") : [];
      for(var i=0;i<as.length;i++){ var h=String(as[i].getAttribute("href")||"");
        if(!/^https?:/i.test(h)) as[i].removeAttribute("href"); }
    }
    new MutationObserver(function(ms){ ms.forEach(function(m){ m.addedNodes.forEach(function(n){
      if(n.nodeType===1){ if(n.matches && n.matches("a[href]")) tidy(n.parentNode||n); else tidy(n); } }); }); })
      .observe(D.body, {childList:true, subtree:true});

    // Pixel dimensions from an image's header bytes (PNG / GIF / BMP / WebP / JPEG), or null.
    function dims(b){
      if(!b || b.length<30) return null;
      function be16(i){ return (b[i]<<8)|b[i+1]; }
      function be32(i){ return b[i]*16777216+((b[i+1]<<16)|(b[i+2]<<8)|b[i+3]); }
      function le16(i){ return b[i]|(b[i+1]<<8); }
      function le24(i){ return b[i]|(b[i+1]<<8)|(b[i+2]<<16); }
      function le32s(i){ return b[i]|(b[i+1]<<8)|(b[i+2]<<16)|(b[i+3]<<24); }
      if(b[0]===0x89&&b[1]===0x50&&b[2]===0x4E&&b[3]===0x47) return [be32(16), be32(20)];
      if(b[0]===0x47&&b[1]===0x49&&b[2]===0x46) return [le16(6), le16(8)];
      if(b[0]===0x42&&b[1]===0x4D) return [Math.abs(le32s(18)), Math.abs(le32s(22))];
      if(b[0]===0x52&&b[1]===0x49&&b[2]===0x46&&b[3]===0x46&&b[8]===0x57&&b[9]===0x45&&b[10]===0x42&&b[11]===0x50){
        var k=String.fromCharCode(b[12],b[13],b[14],b[15]);
        if(k==="VP8X") return [le24(24)+1, le24(27)+1];
        if(k==="VP8L"){ var bits=(b[21]|(b[22]<<8)|(b[23]<<16))+b[24]*16777216; return [(bits&0x3FFF)+1, (Math.floor(bits/16384)&0x3FFF)+1]; }
        if(k==="VP8 ") return [le16(26)&0x3FFF, le16(28)&0x3FFF];
        return null;
      }
      if(b[0]===0xFF&&b[1]===0xD8){
        var i=2;
        while(i+9<b.length){
          if(b[i]!==0xFF){ i++; continue; }
          var m=b[i+1];
          if(m===0xD8||m===0x01||(m>=0xD0&&m<=0xD7)){ i+=2; continue; }
          if(m>=0xC0&&m<=0xCF&&m!==0xC4&&m!==0xC8&&m!==0xCC) return [be16(i+7), be16(i+5)];
          i+=2+be16(i+2);
        }
      }
      return null;
    }
    var PLACEHOLDER="iVBORw0KGgoAAAANSUhEUgAAABAAAAAJCAIAAAC0SDtlAAAAGklEQVR4nGNsaOhgIAUwkaSaYVQDcYDkYAUAsYgBmnyVf/4AAAAASUVORK5CYII=";
    function placeholder(){ var s=atob(PLACEHOLDER), u=new Uint8Array(s.length); for(var i=0;i<s.length;i++) u[i]=s.charCodeAt(i); return u; }

    function fitWidth(){
      var r=stage.getBoundingClientRect(), sh=status.textContent ? status.getBoundingClientRect().height : 0;
      var w=Math.max(160, r.width-4), h=Math.max(90, r.height-sh-8);
      return Math.floor(Math.min(w, h/ratio));
    }
    function show(i, focusThumb){
      if(!viewer) return;
      i=Math.max(0, Math.min(count-1, i)); cur=i;
      if(mainHandle){ try{ mainHandle.dispose(); }catch(_){} mainHandle=null; }
      main.textContent="";
      lastW=fitWidth();
      try{ mainHandle=viewer.renderThumbnailToContainer(i, main, {width:lastW}); }
      catch(e){ main.textContent=""; status.textContent="Slide "+(i+1)+" couldn't be drawn: "+String((e&&e.message)||e).slice(0,160); }
      pos.textContent=(i+1)+" / "+count;
      stage.setAttribute("aria-label", "Slide "+(i+1)+" of "+count+". Arrow keys change slides.");
      prev.disabled=i<=0; next.disabled=i>=count-1;
      thumbs.forEach(function(t, j){ t.setAttribute("aria-selected", j===i ? "true" : "false"); t.tabIndex = j===i ? 0 : -1; });
      var t=thumbs[i];
      if(t){ try{ t.scrollIntoView({block:"nearest", inline:"nearest"}); }catch(_){} if(focusThumb) t.focus(); }
    }
    // Filmstrip: one cheap button per slide; a slide is laid out only while its button is near
    // the visible strip and is disposed again when it scrolls away, so a 500-slide deck costs
    // what the few visible thumbnails cost.
    function buildStrip(){
      var tw=112, th=Math.round(tw*ratio);
      var io=new IntersectionObserver(function(es){ es.forEach(function(e){
        var b=e.target, i=+b.getAttribute("data-i");
        if(e.isIntersecting && !b.__h){
          var box=D.createElement("div"); box.className="tb"; b.appendChild(box);
          try{ b.__h=viewer.renderThumbnailToContainer(i, box, {width:tw}); }catch(_){ b.__h=null; }
        } else if(!e.isIntersecting && b.__h){
          try{ b.__h.dispose(); }catch(_){} b.__h=null;
          var bx=b.querySelector(".tb"); if(bx) bx.remove();
        }
      }); }, {root:strip, rootMargin:"0px 360px"});
      for(var i=0;i<count;i++){
        var b=D.createElement("button"); b.type="button"; b.className="th"; b.style.height=th+"px";
        b.setAttribute("role", "tab"); b.setAttribute("aria-label", "Slide "+(i+1)); b.setAttribute("data-i", String(i));
        var n=D.createElement("span"); n.className="num"; n.textContent=String(i+1); b.appendChild(n);
        b.addEventListener("click", function(ev){ show(+ev.currentTarget.getAttribute("data-i"), true); });
        strip.appendChild(b); thumbs.push(b); io.observe(b);
      }
      strip.hidden = count<2;
    }

    async function load(buf){
      if(failed || viewer) return;
      if(!(buf instanceof ArrayBuffer)) return fail("no file data reached the preview");
      if(buf.byteLength>caps.maxBytes) return fail("the file is over the "+mb(caps.maxBytes)+" preview cap");
      timer=setTimeout(function(){ fail("it took longer than "+Math.round(caps.parseMs/1000)+"s to read"); }, caps.parseMs);
      try{
        var files=await P.parseZip(buf, {maxEntries:caps.maxEntries, maxEntryUncompressedBytes:caps.maxEntryBytes,
          maxTotalUncompressedBytes:caps.maxTotalBytes, maxMediaBytes:caps.maxMediaBytes, maxConcurrency:1});  // one entry at a time: the first cap hit ends it
        if(failed) return;
        var big=0, pixels=0;
        if(files.media && files.media.forEach) files.media.forEach(function(bytes, key){
          var d=dims(bytes), px=d ? d[0]*d[1] : 0;
          if(px>caps.maxImagePixels || pixels+px>caps.maxDeckPixels){ files.media.set(key, placeholder()); big++; }
          else pixels+=px;
        });
        var pres=P.buildPresentation(files, {lazySlides:true});
        count=(pres && pres.slides && pres.slides.length) || 0;
        if(!count) return fail("the deck has no slides");
        if(count>caps.maxSlides) return fail(count+" slides is over the "+caps.maxSlides+"-slide preview cap");
        viewer=new P.PptxViewer(host, {pdfjs:false, fitMode:"none"});
        viewer.load(pres);
        if(viewer.slideWidth>0 && viewer.slideHeight>0) ratio=viewer.slideHeight/viewer.slideWidth;
        clearTimeout(timer);
        status.textContent = big ? big+" oversized image"+(big>1?"s were":" was")+" replaced with a placeholder." : "";
        nav.hidden=false; buildStrip(); show(0);
        post({type:"protoArtifact:pptx", state:"rendered", count:count});
      }catch(e){ fail(String((e&&e.message)||e).slice(0,200)); }
    }

    prev.addEventListener("click", function(){ show(cur-1); });
    next.addEventListener("click", function(){ show(cur+1); });
    // Click the slide: left third goes back, the rest goes forward (a presenter's clicker).
    main.addEventListener("click", function(e){
      var r=main.getBoundingClientRect(); show(e.clientX-r.left < r.width/3 ? cur-1 : cur+1);
    });
    D.addEventListener("keydown", function(e){
      if(!viewer || e.altKey || e.ctrlKey || e.metaKey) return;
      var t=e.target, inStrip=t && t.classList && t.classList.contains("th");
      if(t && t.tagName==="SUMMARY") return;
      var k=e.key, to=null;
      if(k==="ArrowRight"||k==="ArrowDown"||k==="PageDown"||(k===" "&&!(t&&t.tagName==="BUTTON"))) to=cur+1;
      else if(k==="ArrowLeft"||k==="ArrowUp"||k==="PageUp") to=cur-1;
      else if(k==="Home") to=0; else if(k==="End") to=count-1;
      if(to===null) return;
      e.preventDefault(); show(to, inStrip);
    });
    if(W.ResizeObserver){ var rt=0; new ResizeObserver(function(){ clearTimeout(rt); rt=setTimeout(function(){
      if(viewer && Math.abs(fitWidth()-lastW)>2) show(cur); }, 120); }).observe(stage); }

    W.addEventListener("message", function(e){
      if(e.source!==W.parent) return;
      var m=e.data||{};
      if(m.type==="protoArtifact:theme" && m.tokens && typeof m.tokens==="object"){
        Object.keys(m.tokens).forEach(function(k){ if(/^--pl-color-[a-z-]+$/.test(k)) D.documentElement.style.setProperty(k, String(m.tokens[k])); });
        return;
      }
      if(m.type==="protoArtifact:pptx:data"){ load(m.buf); return; }
      if(m.type==="protoArtifact:pptx:error"){ fail(String(m.reason||"the file couldn't be fetched")); }
    });
    if(!P || typeof P.parseZip!=="function"){ fail("the slide renderer didn't load"); return; }
    post({type:"protoArtifact:pptx", state:"need"});
  }
  // ── theme tokens for a frame ────────────────────────────────────────────────────────────
  // The live theme as literal values, read off the kit-managed --pl-* tokens on this page: a
  // nested frame has no stylesheet access, so this is how it matches the console. The chart
  // tokens are the design system's own data-viz palette (--pl-color-chart-axis / -grid /
  // -series1…8), so a chart is drawn in the active theme's colours, dark or light. Pushed into
  // every frame on a re-theme (pushTheme) and baked into a chart's srcdoc (vegaDoc).
  var THEME_TOKENS={"--pl-color-bg":"#0a0a0c","--pl-color-fg":"#ededed","--pl-color-fg-muted":"#9aa0aa",
    "--pl-color-accent":"#9b87f2","--pl-color-border":"rgba(255,255,255,.08)","--pl-font-sans":"",
    "--pl-color-chart-axis":"","--pl-color-chart-grid":""};
  for(var _s=1;_s<=8;_s++) THEME_TOKENS["--pl-color-chart-series"+_s]="";
  function themeTokens(){
    var cs=getComputedStyle(document.documentElement), out={};
    Object.keys(THEME_TOKENS).forEach(function(k){ var v=(cs.getPropertyValue(k)||"").trim()||THEME_TOKENS[k]; if(v) out[k]=v; });
    return out;
  }
  // ── vega-lite charts (ADR 0116) ─────────────────────────────────────────────────────────
  // A chart is a Vega-Lite spec with its data INLINE (data.values): the model writes a small spec
  // and a query — the data plugin's data_chart runs the query and inlines the rows — instead of
  // hand-writing a component, which is what made "chart my week" slow. Drawn by the vendored
  // vega / vega-lite / vega-embed in the same no-same-origin sandbox as every artifact, under a
  // nonce CSP with NO network (connect-src 'none', img/font data: only). Two more locks, because
  // CSP alone would surface them as opaque failures rather than refuse them up front:
  //   • Vega's expression language runs as the CSP-safe interpreter (ast:true — no eval/Function);
  //   • Vega's loader refuses EVERY load, so data.url, a spec-by-URL and image marks can't fetch,
  //     and a spec's usermeta.embedOptions (which vega-embed lets override the embed options,
  //     loader included) is stripped before embedding.
  // Themed from the console's --pl-* tokens and re-drawn on a live theme switch.
  function vegaDoc(code){
    var nonce=cspNonce();
    var csp="default-src 'none'; script-src 'nonce-"+nonce+"'; style-src 'unsafe-inline'; img-src data: blob:; "
      + "font-src data:; media-src 'none'; connect-src 'none'; worker-src 'none'; frame-src 'none'; "
      + "object-src 'none'; base-uri 'none'; form-action 'none'";
    var cfg={spec:String(code||""), tokens:themeTokens()};
    return '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'+csp+'">'
      + base("vega-lite").replace(/<script>/g, '<script nonce="'+nonce+'">')
      + '<style>'+VEGA_CSS+'</style><body><div id="wrap"><div id="vis"></div></div>'
      + cdn("vega", nonce) + cdn("vegaLite", nonce) + cdn("vegaEmbed", nonce)
      + '<script nonce="'+nonce+'">(' + artVega.toString() + ')(' + JSON.stringify(cfg).replace(/</g,"\\u003c") + ');<\/script></body>';
  }
  var VEGA_CSS='html,body{margin:0;min-height:100%;background:var(--pl-color-bg);color:var(--pl-color-fg);'
    + 'font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif)}'
    + '#wrap{box-sizing:border-box;padding:16px 18px}#vis{width:100%}#vis.vega-embed{display:block}'
    + '#vis svg,#vis canvas{max-width:100%;height:auto}';
  // The in-frame controller. Authored as a real function and injected via toString(), like
  // artGraphics / artSlides — so it must carry no literal script-close or comment-open sequence.
  function artVega(cfg){
    var W=window, tok=cfg.tokens||{}, spec=null, view=null, gen=0;
    function fail(m){ if(W.__artErr) W.__artErr("⚠ "+m); }
    function t(k, d){ var v=tok[k]; return (typeof v==="string" && v.trim()) ? v.trim() : d; }
    // Luminance of the ground, for the tooltip theme. Themes are often oklch(), so the browser
    // resolves the colour (a 1px canvas accepts any CSS colour); hex/rgb are parsed directly
    // where there's no canvas.
    function isDark(){
      var bg=t("--pl-color-bg","#0a0a0c"), m, l;
      try{ var cv=W.document.createElement("canvas"), x; cv.width=cv.height=1; x=cv.getContext("2d");
        x.fillStyle="#000"; x.fillStyle=bg; x.fillRect(0,0,1,1); var px=x.getImageData(0,0,1,1).data;
        return (0.299*px[0]+0.587*px[1]+0.114*px[2])/255<=0.5; }catch(_){}
      if((m=/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(bg))){ var h=m[1].length===3?m[1].replace(/./g,"$&$&"):m[1];
        l=(0.299*parseInt(h.slice(0,2),16)+0.587*parseInt(h.slice(2,4),16)+0.114*parseInt(h.slice(4,6),16))/255; }
      else if((m=/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i.exec(bg))) l=(0.299*m[1]+0.587*m[2]+0.114*m[3])/255;
      else return true;
      return l<=0.5;
    }
    // The Vega config the console theme implies. A spec's own `config` still wins (vega-embed
    // merges it over this), so an author who means a colour keeps it.
    function theme(){
      var fg=t("--pl-color-fg","#ededed"), muted=t("--pl-color-fg-muted",fg),
          font=t("--pl-font-sans","ui-sans-serif, system-ui, sans-serif"),
          axis=t("--pl-color-chart-axis",muted), grid=t("--pl-color-chart-grid",t("--pl-color-border","rgba(127,127,127,.25)")),
          series=[], i, c;
      for(i=1;i<=8;i++){ c=t("--pl-color-chart-series"+i,""); if(c) series.push(c); }
      if(!series.length) series.push(t("--pl-color-accent","#9b87f2"));
      function guide(x){ x.labelColor=muted; x.titleColor=fg; x.labelFont=font; x.titleFont=font; return x; }
      var out={ background:t("--pl-color-bg","#0a0a0c"), font:font, padding:4,
        view:{stroke:null},
        axis:guide({domainColor:axis, tickColor:axis, gridColor:grid, labelFontSize:11, titleFontSize:12, titleFontWeight:500}),
        legend:guide({labelFontSize:11, titleFontSize:12, titleFontWeight:500}),
        header:guide({labelFontSize:11, titleFontSize:12}),
        title:{color:fg, subtitleColor:muted, font:font, subtitleFont:font, anchor:"start", fontSize:14, fontWeight:600, offset:12},
        mark:{color:series[0]}, text:{color:fg}, rule:{color:axis} };
      if(series.length>1) out.range={category:series};
      return out;
    }
    // Any `url` inside a data definition — refused up front with a reason, rather than left to
    // fail inside the dataflow as a bare "Loading failed". Inline rows (values / datasets) are
    // never walked: a column named `url` is data, not a load. Iterative and bounded (the mirror of
    // _tools._remote_data), so a hostile nesting depth can't blow the stack: "remote" | "deep" | "".
    function remote(spec){
      var stack=[[spec,false,0]], nodes=0, top, o, inData, d, k, v, i;
      while(stack.length){
        top=stack.pop(); o=top[0]; inData=top[1]; d=top[2];
        if(d>64 || ++nodes>100000) return "deep";
        if(Array.isArray(o)){ for(i=0;i<o.length;i++) if(o[i] && typeof o[i]==="object") stack.push([o[i],inData,d+1]); continue; }
        for(k in o){
          if(!Object.prototype.hasOwnProperty.call(o, k)) continue;
          if(inData && k==="url") return "remote";
          if(k==="values" || k==="datasets") continue;
          v=o[k]; if(v && typeof v==="object") stack.push([v, inData || k==="data", d+1]);
        }
      }
      return "";
    }
    function deny(){ return Promise.reject(new Error("a chart's data must be inline (data.values) — loading is disabled")); }
    var LOADER={load:deny, sanitize:deny, http:deny, file:deny};
    // Errors raised INSIDE the dataflow (after embed resolved) go to the logger, not the promise.
    var logger=null;
    function makeLogger(){
      var b=W.vega.logger(W.vega.Warn), lg={
        level:function(l){ if(arguments.length){ b.level(l); return lg; } return b.level(); },
        error:function(){ fail([].slice.call(arguments).map(function(x){ return (x&&x.message)||String(x); }).join(" ")); return lg; },
        warn:function(){ b.warn.apply(b, arguments); return lg; },
        info:function(){ return lg; }, debug:function(){ return lg; } };
      return lg;
    }
    function prep(){
      var s=JSON.parse(JSON.stringify(spec));
      if(s.usermeta && typeof s.usermeta==="object") delete s.usermeta.embedOptions;
      var single=!!(s.mark || s.layer) && !s.facet && !s.repeat;
      if(single && s.width===undefined) s.width="container";
      if(single && s.autosize===undefined) s.autosize={type:"fit-x", contains:"padding"};
      return s;
    }
    function draw(){
      var my=++gen;
      if(view){ view.finalize(); view=null; }
      return W.vegaEmbed("#vis", prep(), {mode:"vega-lite", renderer:"svg", actions:false, ast:true,
          loader:LOADER, logger:logger, config:theme(), tooltip:{theme:isDark()?"dark":"light"}})
        .then(function(res){ if(my!==gen){ res.finalize(); return; } view=res; if(W.__artOk) W.__artOk(); })
        .catch(function(e){ if(my===gen) fail((e && e.message) || String(e)); });
    }
    W.addEventListener("message", function(e){
      if(e.source!==W.parent) return;
      var m=e.data||{};
      if(m.type!=="protoArtifact:theme" || !m.tokens || typeof m.tokens!=="object") return;
      var nt={};
      Object.keys(m.tokens).forEach(function(k){ if(/^--pl-[a-z0-9-]+$/.test(k)) nt[k]=String(m.tokens[k]); });
      if(JSON.stringify(nt)===JSON.stringify(tok)) return;  // the on-load push repeats what's baked in
      tok=nt; if(spec) draw();
    });
    try{ spec=JSON.parse(cfg.spec); }catch(e){ fail("the chart spec isn't valid JSON: "+e.message); return; }
    if(!spec || typeof spec!=="object" || Array.isArray(spec)){ fail("a Vega-Lite spec is a JSON object"); spec=null; return; }
    var why=remote(spec);
    if(why==="deep"){ fail("the chart spec is too deeply nested or too large (at most 64 levels)"); spec=null; return; }
    if(why){ fail("a chart's data must be inline (data.values) — data.url is not loaded"); spec=null; return; }
    if(!W.vega || !W.vegaLite || !W.vegaEmbed){ fail("the chart renderer didn't load"); spec=null; return; }
    logger=makeLogger();
    draw();
  }
  var $art=document.getElementById("art"), $vprev=document.getElementById("vprev"),
      $vnext=document.getElementById("vnext"), $vlabel=document.getElementById("vlabel"),
      $dl=document.getElementById("dl"), $del=document.getElementById("del"),
      $bar=document.getElementById("bar"), $empty=document.getElementById("empty"),
      $frame=document.getElementById("frame"), $edit=document.getElementById("edit"),
      $editor=document.getElementById("editor"), $code=document.getElementById("code"),
      $run=document.getElementById("run"), $cancel=document.getElementById("cancel"),
      $estat=document.getElementById("estat"), $dlstat=document.getElementById("dlstat");
  var editing=false;

  // Embed placement (ADR 0118 D2): /plugins/artifact/view?embed=<id>&v=<version> renders ONE
  // version with no panel chrome. EMBED is {id, ver} (ver = the LIFETIME version number the
  // artifact-ref chip carries; 0/absent = latest) or null for the normal panel. Parsed off the
  // query the browser keeps — the view route serves the same static page either way.
  function embedParams(search){
    var m=/[?&]embed=([^&]*)/.exec(search||""); if(!m) return null;
    var id=""; try{ id=decodeURIComponent(m[1]||""); }catch(_){ id=m[1]||""; }
    if(!id) return null;
    var vm=/[?&]v=([^&]*)/.exec(search||""), ver=0;
    if(vm){ var raw=vm[1]; try{ raw=decodeURIComponent(raw); }catch(_){} ver=parseInt(raw,10); }
    return {id:id, ver:(isFinite(ver)&&ver>0)?ver:0};
  }
  var EMBED = embedParams(location.search);

  // Persist the user's selection (artifact + version + whether to auto-follow the newest) so it
  // STICKS across a tab-away/back — the plugin-view iframe unmounts on tab switch, so the shell
  // reloads fresh; without this it snapped back to the latest artifact. Same-origin page → plain
  // localStorage; best-effort (a blocked/full store just no-ops).
  var SEL_KEY = "protoartifact.sel";
  function saveSel(){ try{ localStorage.setItem(SEL_KEY, JSON.stringify({selId:selId, selVer:selVer, followNewest:followNewest})); }catch(_){} }
  function loadSel(){ try{ var s=JSON.parse(localStorage.getItem(SEL_KEY)||"null");
    if(s&&typeof s==="object"){ selId=s.selId||null; selVer=(s.selVer==null?null:s.selVer); followNewest=(s.followNewest!==false); } }catch(_){} }

  function total(a){ return (a && (a.version_count||a.versions.length)) || 0; }
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
    // LIFETIME numbers (#3617) — the ones the chat's artifact-ref chips carry. Past the
    // max_versions cap the oldest versions are trimmed, so a position would drift from the
    // number the agent reported; "v48 of 52" stays true. Identical to positions until then.
    var vtot=total(a);
    $vlabel.textContent="v"+(vtot-a.versions.length+vi+1)+" of "+vtot;
    $vprev.disabled = vi<=0; $vnext.disabled = vi>=a.versions.length-1;
    $empty.style.display="none";
    $edit.style.display = a.kind==="file" ? "none" : "";  // a file's preview isn't user-editable
    // Re-srcdoc only when the shown version actually changes. Keyed by ts as well as position: at
    // the max_versions cap every new version lands in the SAME slot (the trim shifts the rest down),
    // so a position-only key never re-rendered past the cap.
    var key=a.id+"@"+vi+"@"+v.ts;
    if(key!==lastRendered){ lastRendered=key; renderingId=a.id; renderingVer=vi+1; renderingTs=v.ts;
      renderingN=(a.version_count||a.versions.length)-a.versions.length+vi+1;  // lifetime number (_store._version_key)
      // The version's STORED code links (ADR 0038 amendment) — the only targets a click in the
      // frame can open. Captured with the srcdoc so they always belong to what's on screen.
      renderingLinks = (a.kind==="mermaid" && v.links && typeof v.links==="object" && !Array.isArray(v.links)) ? v.links : null;
      linkLabels = null;
      // A deck's / PDF's frame asks for its bytes once it boots (pptxNeed) — remember which version it is.
      pptxReset(a.kind==="file" && (slidesOk(v)||pdfOk(v)||docxOk(v))
        ? {id:a.id, vi:vi, v:v, key:key, kind:pdfOk(v) ? "pdf" : docxOk(v) ? "docx" : "pptx"} : null);
      $frame.srcdoc = a.kind==="file" ? fileCard(v) : srcdoc(a.kind, v.code, renderingLinks); $frame.style.display="block";
      renderLinks(); }
  }

  // ── narrow-dock toolbar ───────────────────────────────────────────────────────────────
  // Below COMPACT_PX the secondary actions (Edit / Download / Delete) move — the SAME elements,
  // so their handlers and the delete confirm keep working — into a ⋯ menu, and back when the
  // dock widens. The bar stays one clean row instead of wrapping or clipping a button.
  var COMPACT_PX = 560;
  var $acts=document.getElementById("acts"), $more=document.getElementById("more"), $menu=document.getElementById("moremenu");
  var ACT_IDS=["edit","dl","del"];
  function setMenu(open){ $menu.classList.toggle("open", open); $more.setAttribute("aria-expanded", open?"true":"false");
    if(open){ var f=[].slice.call($menu.querySelectorAll("button")).filter(function(b){ return b.style.display!=="none"; })[0]; if(f) f.focus(); } }
  function layoutBar(){
    var compact = $bar.getBoundingClientRect().width < COMPACT_PX;
    if(compact===$bar.classList.contains("compact")) return;
    $bar.classList.toggle("compact", compact);
    var dest = compact ? $menu : $acts;
    ACT_IDS.forEach(function(id){ var b=document.getElementById(id); dest.appendChild(b);
      if(compact) b.setAttribute("role","menuitem"); else b.removeAttribute("role"); });
    $acts.style.display = compact ? "none" : "";
    if(!compact) setMenu(false);
  }
  $more.addEventListener("click", function(e){ e.stopPropagation(); setMenu(!$menu.classList.contains("open")); });
  // Picking an action closes the menu — except the first Delete click, which arms "Confirm?".
  $menu.addEventListener("click", function(e){ var b=e.target.closest("button"); if(b && b.id!=="del") setMenu(false); });
  $menu.addEventListener("keydown", function(e){
    if(e.key==="Escape"){ e.preventDefault(); setMenu(false); $more.focus(); return; }
    if(e.key!=="ArrowDown" && e.key!=="ArrowUp") return;
    var bs=[].slice.call($menu.querySelectorAll("button")).filter(function(b){ return b.style.display!=="none"; }), i=bs.indexOf(document.activeElement);
    e.preventDefault(); bs[(i+(e.key==="ArrowDown"?1:bs.length-1))%bs.length].focus();
  });
  document.addEventListener("click", function(e){ if(!$menu.contains(e.target) && e.target!==$more) setMenu(false); });
  if(window.ResizeObserver) new ResizeObserver(layoutBar).observe($bar);
  window.addEventListener("resize", layoutBar);

  // ── code links (ADR 0038 amendment) ──────────────────────────────────────────────────────
  // A mermaid version may carry `links` {key: {project, path, line, end_line, note}}, validated
  // server-side against the fs fence. The frame posts only a KEY; everything that leaves this
  // page for the console is looked up HERE, in the rendered version's stored links — so model-
  // authored code in the sandbox can open exactly the targets the tool validated, nothing else.
  var renderingLinks = null, linkLabels = null;  // linkLabels: null until the frame reports its matches
  var $links=document.getElementById("links"), $linkpanel=document.getElementById("linkpanel"),
      $linklist=document.getElementById("linklist");
  function linkTarget(key){
    if(!renderingLinks || typeof key!=="string" || !Object.prototype.hasOwnProperty.call(renderingLinks, key)) return null;
    var t=renderingLinks[key];
    if(!t || typeof t.project!=="string" || typeof t.path!=="string") return null;
    var line=Math.floor(+t.line)||0; if(line<1) return null;
    var end=Math.floor(+t.end_line)||line;
    return {project:t.project, path:t.path, line:line, end_line:Math.max(end,line), note:typeof t.note==="string"?t.note:""};
  }
  // Hand a target to the console, which opens the code pane (or the editor). Posted only to the
  // page that embeds this one, at its origin where the browser tells us (see fromEmbedder).
  function openTarget(t){
    if(!t || window.parent===window) return;
    var anc=location.ancestorOrigins, origin=(anc && anc.length) ? anc[0] : "*";
    try{ window.parent.postMessage({type:"protoagent:code:open", project:t.project, path:t.path,
      line:t.line, end_line:t.end_line, note:t.note}, origin); }catch(_){}
  }
  function linkKeys(){ return renderingLinks ? Object.keys(renderingLinks).filter(function(k){ return !!linkTarget(k); }) : []; }
  function msgOrder(k){ var m=/^msg:(\d+)$/.exec(k); return m ? +m[1] : Infinity; }
  function renderLinks(){
    var keys=linkKeys();
    $links.style.display = keys.length ? "" : "none";
    document.getElementById("linkn").textContent = String(keys.length);
    $links.setAttribute("aria-label", "Code links ("+keys.length+")");
    if(!keys.length) closeLinks(false);
    $linklist.textContent="";
    keys.sort(function(a,b){ return msgOrder(a)-msgOrder(b); }).forEach(function(k){
      var t=linkTarget(k), li=document.createElement("li"), b=document.createElement("button");
      b.type="button"; b.className="lk"; b.setAttribute("data-key", k);
      var name=document.createElement("span"); name.className="lk-name";
      name.textContent = (linkLabels && linkLabels[k]) || k;   // textContent only — never HTML
      var where=document.createElement("span"); where.className="lk-where";
      where.textContent = t.project+"/"+t.path+":"+t.line+(t.end_line!==t.line?"-"+t.end_line:"");
      b.appendChild(name); b.appendChild(where);
      if(t.note){ var n=document.createElement("span"); n.className="lk-note"; n.textContent=t.note; b.appendChild(n); }
      if(linkLabels && !Object.prototype.hasOwnProperty.call(linkLabels, k)){
        b.classList.add("lk-miss"); var miss=document.createElement("span"); miss.className="lk-note"; miss.textContent="not found in the diagram"; b.appendChild(miss);
      }
      b.addEventListener("click", function(){ openTarget(linkTarget(k)); });
      b.addEventListener("mouseenter", function(){ highlight(k, false); });
      b.addEventListener("focus", function(){ highlight(k, true); });
      b.addEventListener("mouseleave", function(){ highlight(null, false); });
      li.appendChild(b); $linklist.appendChild(li);
    });
  }
  function highlight(key, reveal){
    try{ $frame.contentWindow.postMessage({type:"protoArtifact:highlight", key:key, reveal:!!reveal}, "*"); }catch(_){}
  }
  function openLinks(){ $linkpanel.style.display="flex"; $links.setAttribute("aria-expanded","true");
    var f=$linklist.querySelector("button"); if(f) f.focus(); }
  function closeLinks(refocus){ if($linkpanel.style.display==="none") return;
    $linkpanel.style.display="none"; $links.setAttribute("aria-expanded","false"); highlight(null,false);
    if(refocus) $links.focus(); }
  $links.addEventListener("click", function(){ $linkpanel.style.display==="flex" ? closeLinks(false) : openLinks(); });
  document.getElementById("linkclose").addEventListener("click", function(){ closeLinks(true); });
  $linkpanel.addEventListener("keydown", function(e){
    if(e.key==="Escape"){ e.preventDefault(); closeLinks(true); return; }
    if(e.key!=="ArrowDown" && e.key!=="ArrowUp") return;
    var bs=[].slice.call($linklist.querySelectorAll("button")), i=bs.indexOf(document.activeElement);
    if(i<0) return; e.preventDefault();
    var j=e.key==="ArrowDown" ? Math.min(bs.length-1,i+1) : Math.max(0,i-1); bs[j].focus();
  });

  // Live re-theme (#1872): base() bakes the theme tokens into the srcdoc as literal
  // colors, so an app-theme switch after render left the artifact in the stale
  // palette. The DS plugin-kit re-themes THIS page by rewriting the --pl-* tokens on
  // the root element; observe that and push the fresh tokens into the frame (the
  // SHIM applies them in place — no re-srcdoc, so interactive artifact state survives).
  function pushTheme(){
    if(!$frame || !$frame.contentWindow || $frame.style.display==="none") return;
    try{ $frame.contentWindow.postMessage({type:"protoArtifact:theme",tokens:themeTokens()},"*"); }catch(_){}
  }
  new MutationObserver(pushTheme).observe(document.documentElement,{attributes:true,attributeFilter:["style","class","data-theme"]});
  // A theme switch can race a render — re-push once the fresh srcdoc has loaded.
  $frame.addEventListener("load", pushTheme);
  // Embed only: wake the frame's dormant height reporter (HEIGHTJS) so it starts posting its
  // content height up on every size change. A no-op in the panel (never sent).
  $frame.addEventListener("load", function(){
    if(!EMBED) return;
    try{ $frame.contentWindow.postMessage({type:"protoArtifact:measure"}, "*"); }catch(_){}
  });

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
  // Transient download acknowledgement, sharing the failure-button pattern. The browser owns
  // the actual save and its completion isn't observable from here, so a success says the
  // download STARTED — it must not claim a file reached disk. Both outcomes also land on the
  // aria-live status region ($dlstat): a lone label swap on an unfocused button isn't announced
  // to assistive tech. The label restores to "Download" after a short flash either way.
  var dlFlashT=null;
  function dlFlash(label, status){
    clearTimeout(dlFlashT); $dl.textContent=label;
    if($dlstat) $dlstat.textContent=status;
    dlFlashT=setTimeout(function(){ $dl.textContent="Download"; },1800);
  }
  $dl.addEventListener("click", async function(){
    var a=selArt(); if(!a)return; var vi=verIdx(a), v=a.versions[vi];
    if(a.kind==="file"){  // download the STORED BYTES via the gated blob route (ADR 0092 D2)
      try{ var r=await kit.apiFetch("/api/plugins/artifact/artifact/"+encodeURIComponent(a.id)+"/blob?version="+(vi+1));
        if(!r.ok) throw 0; saveBlob(await r.blob(), (v.file&&v.file.filename)||("artifact-"+a.id)); dlFlash("Started","Download started"); }
      catch(e){ dlFlash("Failed","Download failed"); }
      return;
    }
    saveBlob(new Blob([v.code],{type:"text/plain"}), "artifact-"+a.id+"-v"+(vi+1)+"."+(EXT[a.kind]||"txt"));
    dlFlash("Started","Download started");
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

  // ── .pdf page previews ────────────────────────────────────────────────────────────────
  // A PDF renders as REAL pages — the vendored pdf.js (window.pdfjsLib), drawn to canvases on the
  // frame's main thread (the worker module only registers its message handler; the CSP forbids
  // workers), in the same no-same-origin sandbox under a nonce CSP with no network. The pages
  // stack in one scroll, fitted to the panel width; a page is drawn only while it's near the
  // visible area and its canvas is freed when it scrolls away, so a 2000-page PDF costs what the
  // few visible pages cost. Canvas size is capped (maxCanvasPixels) whatever the zoom.
  //
  // Caps — the mirror of _pdfview.py (drift-guarded by a test). The save-time preflight decoded
  // every stream under a budget; the frame bounds its own parse (parseMs), and the shell's
  // watchdog swaps in the extracted-text card if the frame never answers.
  var PDF_CAPS={
    maxBytes: 41943040,         // 40 MB — _pdfview.MAX_BYTES
    maxPages: 2000,             // _pdfview.MAX_PAGES
    maxCanvasPixels: 16777216,  // one drawn page's canvas (4096 × 4096)
    parseMs: 20000,
    watchdogMs: 45000
  };
  function pdfDoc(v){
    var cs=getComputedStyle(document.documentElement);
    function tok(n,d){ return (cs.getPropertyValue(n)||d).trim(); }
    var f=v.file||{}, name=f.filename||"document.pdf", mime=f.mime||"application/pdf", nonce=cspNonce();
    var st=stripTrunc(v.code||"");
    var csp="default-src 'none'; script-src 'nonce-"+nonce+"'; style-src 'unsafe-inline'; img-src blob: data:; "
      + "media-src 'none'; font-src blob: data:; connect-src 'none'; worker-src 'none'; frame-src 'none'; "
      + "object-src 'none'; base-uri 'none'; form-action 'none'";
    var tokens=":root{--pl-color-bg:"+tok("--pl-color-bg","#0a0a0c")+";--pl-color-fg:"+tok("--pl-color-fg","#ededed")
      + ";--pl-color-fg-muted:"+tok("--pl-color-fg-muted","#9aa0aa")+";--pl-color-border:"+tok("--pl-color-border","rgba(255,255,255,.12)")
      + ";--pl-color-accent:"+tok("--pl-color-accent","#9b87f2")+"}";
    var cfg={caps:PDF_CAPS, pages:+((f.pdf&&f.pdf.pages)||0)};
    return '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'+csp+'">'
      + '<style>'+tokens+PDF_CSS+'</style>'
      + '<div class="wrap"><div class="hd"><div class="ic" aria-hidden="true">PDF</div>'
      + '<div class="meta"><div class="nm">'+esc(name)+'</div><div class="mt">'+esc(mime)+' · '+fmtSize(f.size)+'</div></div>'
      + '<div class="nav" id="nav" hidden>'
      + '<button id="prev" type="button" aria-label="Previous page" title="Previous page">‹</button>'
      + '<span id="pos" aria-live="polite"></span>'
      + '<button id="next" type="button" aria-label="Next page" title="Next page">›</button>'
      + '<span class="sep"></span>'
      + '<button id="zout" type="button" aria-label="Zoom out" title="Zoom out (−)">−</button>'
      + '<button id="zfit" type="button" class="wide" aria-label="Fit to width" title="Fit to width (0)">Fit</button>'
      + '<button id="zin" type="button" aria-label="Zoom in" title="Zoom in (+)">+</button></div></div>'
      + '<div class="stage" id="stage" tabindex="0" role="document" aria-label="PDF pages">'
      + '<div id="pages"></div><div id="status" class="st" role="status">Rendering pages…</div></div>'
      + '<details id="ol"><summary>Extracted text</summary><pre class="pv">'+esc(st.code)
      + (st.truncated?'\n… (preview truncated — download the file for the full content)':'')+'</pre></details></div>'
      + cdnModule("pdfjsWorker", nonce) + cdnModule("pdfjs", nonce)
      + '<script type="module" nonce="'+nonce+'">(' + artPdf.toString() + ')(' + JSON.stringify(cfg).replace(/</g,"\\u003c") + ');<\/script>';
  }
  var PDF_CSS='html,body{margin:0;height:100%;background:var(--pl-color-bg);color:var(--pl-color-fg);'
    + 'font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif);overflow:hidden}'
    + '.wrap{display:flex;flex-direction:column;height:100%;box-sizing:border-box;padding:14px 16px;gap:10px}'
    + '.hd{display:flex;gap:12px;align-items:center;flex:none}.meta{min-width:0;flex:1}'
    + '.ic{flex:none;width:38px;height:38px;border-radius:8px;display:flex;align-items:center;justify-content:center;'
    + 'font:700 9px/1 var(--pl-font-sans,system-ui);letter-spacing:.04em;color:#fff;background:#b3261e}'
    + '.nm{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.mt{color:var(--pl-color-fg-muted);font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.nav{display:flex;align-items:center;gap:4px;flex:none}.nav[hidden]{display:none}'
    + '.nav .sep{width:1px;height:18px;background:var(--pl-color-border);margin:0 4px}'
    + '.nav button{all:unset;box-sizing:border-box;min-width:28px;height:28px;padding:0 6px;border-radius:6px;text-align:center;font-size:16px;line-height:26px;'
    + 'cursor:pointer;border:1px solid var(--pl-color-border)}.nav button.wide{font-size:12px}'
    + '.nav button:hover{background:rgba(127,127,127,.16)}.nav button[disabled]{opacity:.35;cursor:default}'
    + '.nav button:focus-visible,.stage:focus-visible,summary:focus-visible{outline:2px solid var(--pl-color-accent);outline-offset:2px}'
    + '#pos{min-width:64px;text-align:center;font-size:12px;color:var(--pl-color-fg-muted);font-variant-numeric:tabular-nums}'
    + '.stage{flex:1;min-height:0;position:relative;display:flex;flex-direction:column;outline:none}'
    + '.stage.failed{flex:none}'
    + '#pages{flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;align-items:center;gap:12px;padding:4px 0 12px}'
    + '#pages[hidden]{display:none}'
    + '.pg{flex:none;position:relative;background:#fff;border-radius:2px;box-shadow:0 1px 2px rgba(0,0,0,.25),0 8px 28px rgba(0,0,0,.28);line-height:0}'
    + '.pg canvas{display:block;width:100%;height:100%}'
    + '.pg[data-err]::after{content:attr(data-err);position:absolute;inset:0;display:flex;align-items:center;justify-content:center;'
    + 'font:12px/1.4 var(--pl-font-sans,system-ui);color:#555}'
    + '.st{font-size:12px;color:var(--pl-color-fg-muted);padding:6px 0}.st:empty{display:none}'
    + '.st.err{color:var(--pl-color-fg);padding:8px 10px;border-radius:6px;border:1px solid var(--pl-color-border);background:rgba(127,127,127,.1);align-self:stretch}'
    + 'details{flex:none;font-size:12px;min-height:0}details[open]{flex:1;display:flex;flex-direction:column}'
    + 'summary{cursor:pointer;color:var(--pl-color-fg-muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;padding:2px 0}'
    + 'pre.pv{flex:1;min-height:0;max-height:100%;overflow:auto;margin:6px 0 0;padding:12px;border:1px solid var(--pl-color-border);border-radius:8px;'
    + 'background:rgba(127,127,127,.08);white-space:pre-wrap;word-break:break-word;'
    + 'font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-size:12px;line-height:1.5}';

  // The in-frame page controller. Like artSlides it's authored here and injected as SOURCE, so it
  // may reference nothing outside itself. Keep `</` and `<!` out of it: it rides a srcdoc <script>.
  function artPdf(cfg){
    var D=document, W=window, L=W.pdfjsLib, caps=(cfg&&cfg.caps)||{};
    function $(id){ return D.getElementById(id); }
    var stage=$("stage"), box=$("pages"), status=$("status"), ol=$("ol"), nav=$("nav"), pos=$("pos"),
        prev=$("prev"), next=$("next"), zin=$("zin"), zout=$("zout"), zfit=$("zfit");
    var doc=null, task=null, count=0, failed=false, timer=0, slots=[], io=null, zoom=1, cur=1, baseW=612, lastFit=0;
    var ZMIN=0.5, ZMAX=4;
    function post(m){ try{ W.parent.postMessage(m, "*"); }catch(_){} }
    function mb(n){ return Math.round(n/1048576)+" MB"; }
    function fail(reason){
      if(failed) return; failed=true; clearTimeout(timer);
      if(task){ try{ task.destroy(); }catch(_){} }
      status.textContent="Couldn't render this PDF: "+reason+". The extracted text is below.";
      status.className="st err"; box.textContent=""; box.hidden=true; nav.hidden=true;
      stage.classList.add("failed"); ol.open=true;
      post({type:"protoArtifact:pdf", state:"failed", reason:String(reason).slice(0,300)});
    }
    // Page width in CSS px: the panel width at zoom 1 ("fit"), scaled by the zoom factor. The page
    // box stays laid out while it's still empty (it's only hidden on failure): a display:none box
    // measures 0 wide, which sized the FIRST page at the 160px floor.
    function fitW(){ return Math.max(160, box.clientWidth-24); }
    function pageW(){ return Math.round(fitW()*zoom); }
    function size(s){ var w=pageW(); s.el.style.width=w+"px"; s.el.style.height=Math.round(w*s.ratio)+"px"; }
    function release(s){
      if(s.canvas){ s.canvas.width=0; s.canvas.height=0; s.canvas.remove(); s.canvas=null; }
      s.drawnW=0;
    }
    async function draw(s){
      if(s.busy || failed) return;
      var w=pageW(); if(s.canvas && s.drawnW===w) return;
      s.busy=true;
      try{
        var page=await doc.getPage(s.n);
        var vp1=page.getViewport({scale:1});
        if(vp1.width>0 && vp1.height>0 && Math.abs(vp1.height/vp1.width-s.ratio)>0.001){ s.ratio=vp1.height/vp1.width; size(s); }
        var dpr=Math.min(W.devicePixelRatio||1, 2), scale=w/vp1.width*dpr;
        var px=vp1.width*vp1.height*scale*scale;
        if(px>caps.maxCanvasPixels) scale*=Math.sqrt(caps.maxCanvasPixels/px);
        var vp=page.getViewport({scale:scale});
        var c=D.createElement("canvas"); c.width=Math.max(1, Math.floor(vp.width)); c.height=Math.max(1, Math.floor(vp.height));
        c.setAttribute("aria-hidden", "true");
        await page.render({canvas:c, viewport:vp}).promise;
        if(!s.visible){ c.width=0; c.height=0; }
        else { if(s.canvas) release(s); s.el.appendChild(c); s.canvas=c; s.drawnW=w; s.el.removeAttribute("data-err"); }
        try{ page.cleanup(); }catch(_){}
      }catch(e){ s.el.setAttribute("data-err", "Page "+s.n+" couldn't be drawn"); }
      s.busy=false;
      if(s.visible && s.drawnW!==pageW()) draw(s);  // the zoom changed while it was drawing
    }
    function current(){
      var top=box.getBoundingClientRect().top, best=1, bd=1e9;
      for(var i=0;i<slots.length;i++){ var r=slots[i].el.getBoundingClientRect(), d=Math.abs(r.top-top);
        if(r.bottom>top+8 && d<bd){ bd=d; best=slots[i].n; } if(r.top>top+box.clientHeight) break; }
      return best;
    }
    function update(){
      cur=current(); pos.textContent=cur+" / "+count;
      prev.disabled=cur<=1; next.disabled=cur>=count;
      zout.disabled=zoom<=ZMIN; zin.disabled=zoom>=ZMAX;
      stage.setAttribute("aria-label", "PDF page "+cur+" of "+count);
    }
    function go(n){
      n=Math.max(1, Math.min(count, n)); var s=slots[n-1]; if(!s) return;
      box.scrollTop=s.el.offsetTop-box.offsetTop-4; update();
    }
    function setZoom(z){
      var anchor=cur, frac=0, s=slots[anchor-1];
      if(s){ frac=(box.scrollTop-(s.el.offsetTop-box.offsetTop))/Math.max(1, s.el.offsetHeight); }
      zoom=Math.max(ZMIN, Math.min(ZMAX, z));
      slots.forEach(size);
      if(s){ box.scrollTop=s.el.offsetTop-box.offsetTop+frac*s.el.offsetHeight; }
      slots.forEach(function(t){ if(t.visible) draw(t); });
      update();
    }
    async function load(buf){
      if(failed || doc) return;
      if(!(buf instanceof ArrayBuffer)) return fail("no file data reached the preview");
      if(buf.byteLength>caps.maxBytes) return fail("the file is over the "+mb(caps.maxBytes)+" preview cap");
      timer=setTimeout(function(){ fail("it took longer than "+Math.round(caps.parseMs/1000)+"s to read"); }, caps.parseMs);
      try{
        task=L.getDocument({data:new Uint8Array(buf), useWasm:false, useSystemFonts:true, enableXfa:false,
          disableAutoFetch:true, disableStream:true, disableRange:true, isOffscreenCanvasSupported:false, verbosity:0});
        task.onPassword=function(){ fail("the PDF is password-protected"); };
        doc=await task.promise;
        if(failed) return;
        count=doc.numPages||0;
        if(!count) return fail("the PDF has no pages");
        if(count>caps.maxPages) return fail(count+" pages is over the "+caps.maxPages+"-page preview cap");
        var first=(await doc.getPage(1)).getViewport({scale:1}), ratio=first.height/first.width;
        baseW=first.width;
        clearTimeout(timer);
        io=new IntersectionObserver(function(es){ es.forEach(function(e){
          var s=e.target.__slot; s.visible=e.isIntersecting;
          if(e.isIntersecting) draw(s); else release(s);
        }); update(); }, {root:box, rootMargin:"600px 0px"});
        for(var i=1;i<=count;i++){
          var el=D.createElement("div"); el.className="pg"; el.setAttribute("role", "img"); el.setAttribute("aria-label", "Page "+i);
          var s={n:i, el:el, ratio:ratio, canvas:null, drawnW:0, busy:false, visible:false}; el.__slot=s;
          slots.push(s); size(s); box.appendChild(el); io.observe(el);
        }
        status.textContent=""; nav.hidden=false; lastFit=fitW(); update();
        post({type:"protoArtifact:pdf", state:"rendered", count:count});
      }catch(e){
        var msg=String((e&&e.message)||e);
        fail(e && e.name==="PasswordException" ? "the PDF is password-protected" : msg.slice(0,200));
      }
    }

    prev.addEventListener("click", function(){ go(cur-1); });
    next.addEventListener("click", function(){ go(cur+1); });
    zin.addEventListener("click", function(){ setZoom(zoom*1.25); });
    zout.addEventListener("click", function(){ setZoom(zoom/1.25); });
    zfit.addEventListener("click", function(){ setZoom(1); });
    box.addEventListener("scroll", function(){ if(count) update(); }, {passive:true});
    D.addEventListener("keydown", function(e){
      if(!doc || e.altKey || e.ctrlKey || e.metaKey) return;
      var t=e.target; if(t && (t.tagName==="SUMMARY" || t.tagName==="BUTTON")) return;
      var k=e.key;
      if(k==="+"||k==="=") { e.preventDefault(); setZoom(zoom*1.25); }
      else if(k==="-"||k==="_") { e.preventDefault(); setZoom(zoom/1.25); }
      else if(k==="0") { e.preventDefault(); setZoom(1); }
      else if(k==="Home") { e.preventDefault(); go(1); }
      else if(k==="End") { e.preventDefault(); go(count); }
    });
    if(W.ResizeObserver){ var rt=0; new ResizeObserver(function(){ clearTimeout(rt); rt=setTimeout(function(){
      if(!doc || Math.abs(fitW()-lastFit)<=2) return;
      lastFit=fitW(); setZoom(zoom); }, 120); }).observe(box); }

    W.addEventListener("message", function(e){
      if(e.source!==W.parent) return;
      var m=e.data||{};
      if(m.type==="protoArtifact:theme" && m.tokens && typeof m.tokens==="object"){
        Object.keys(m.tokens).forEach(function(k){ if(/^--pl-color-[a-z-]+$/.test(k)) D.documentElement.style.setProperty(k, String(m.tokens[k])); });
        return;
      }
      if(m.type==="protoArtifact:pdf:data"){ load(m.buf); return; }
      if(m.type==="protoArtifact:pdf:error"){ fail(String(m.reason||"the file couldn't be fetched")); }
    });
    if(!L || typeof L.getDocument!=="function" || !W.pdfjsWorker){ fail("the PDF renderer didn't load"); return; }
    post({type:"protoArtifact:pdf", state:"need"});
  }
  // ── .docx page previews ───────────────────────────────────────────────────────────────
  // A Word document renders as REAL pages — the vendored docx-preview (window.docx) on JSZip
  // (window.JSZip): styles, headings, lists, tables, images, headers/footers and footnotes, laid out
  // as HTML at the document's own page size and scaled to fit the panel (− / Fit / + zoom). Same
  // no-same-origin sandbox and nonce CSP as slides/pages, no network. renderAltChunks stays OFF:
  // an altChunk is raw HTML embedded in the document, and it never renders. Hyperlinks keep an
  // href only when it's plain http(s), and even those can't open anything from the sandbox.
  //
  // Caps — the mirror of _docx.py (drift-guarded by a test). The save-time preflight inflated every
  // entry and measured every image under a budget; the frame bounds its own parse (parseMs), and
  // the shell's watchdog swaps in the extracted-text card if the frame never answers.
  var DOCX_CAPS={
    maxBytes: 41943040,         // 40 MB — _docx.MAX_BYTES
    parseMs: 20000,
    watchdogMs: 45000
  };
  function docxDoc(v){
    var cs=getComputedStyle(document.documentElement);
    function tok(n,d){ return (cs.getPropertyValue(n)||d).trim(); }
    var f=v.file||{}, name=f.filename||"document.docx", mime=f.mime||DOCX_MIME, nonce=cspNonce();
    var st=stripTrunc(v.code||"");
    var csp="default-src 'none'; script-src 'nonce-"+nonce+"'; style-src 'unsafe-inline'; img-src blob: data:; "
      + "media-src 'none'; font-src blob: data:; connect-src 'none'; worker-src 'none'; frame-src 'none'; "
      + "object-src 'none'; base-uri 'none'; form-action 'none'";
    var tokens=":root{--pl-color-bg:"+tok("--pl-color-bg","#0a0a0c")+";--pl-color-fg:"+tok("--pl-color-fg","#ededed")
      + ";--pl-color-fg-muted:"+tok("--pl-color-fg-muted","#9aa0aa")+";--pl-color-border:"+tok("--pl-color-border","rgba(255,255,255,.12)")
      + ";--pl-color-accent:"+tok("--pl-color-accent","#9b87f2")+"}";
    var cfg={caps:DOCX_CAPS};
    return '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'+csp+'">'
      + '<style>'+tokens+PDF_CSS+DOCX_CSS+'</style>'
      + '<div class="wrap"><div class="hd"><div class="ic dx" aria-hidden="true">DOCX</div>'
      + '<div class="meta"><div class="nm">'+esc(name)+'</div><div class="mt">'+esc(mime)+' · '+fmtSize(f.size)+'</div></div>'
      + '<div class="nav" id="nav" hidden>'
      + '<span id="pos" aria-live="polite"></span>'
      + '<span class="sep"></span>'
      + '<button id="zout" type="button" aria-label="Zoom out" title="Zoom out (−)">−</button>'
      + '<button id="zfit" type="button" class="wide" aria-label="Fit to width" title="Fit to width (0)">Fit</button>'
      + '<button id="zin" type="button" aria-label="Zoom in" title="Zoom in (+)">+</button></div></div>'
      + '<div class="stage" id="stage" tabindex="0" role="document" aria-label="Document pages">'
      + '<div id="pages"><div id="doc"></div></div><div id="dstyle"></div><div id="status" class="st" role="status">Rendering document…</div></div>'
      + '<details id="ol"><summary>Extracted text</summary><pre class="pv">'+esc(st.code)
      + (st.truncated?'\n… (preview truncated — download the file for the full content)':'')+'</pre></details></div>'
      + cdn("jszip", nonce) + cdn("docxPreview", nonce)
      + '<script nonce="'+nonce+'">(' + artDocx.toString() + ')(' + JSON.stringify(cfg).replace(/</g,"\\u003c") + ');<\/script>';
  }
  var DOCX_CSS='.ic.dx{background:#2b579a}'
    + '#pages{display:block;padding:4px 0 12px}'
    + '#doc .docx-wrapper{background:transparent!important;padding:0!important;display:flex;flex-direction:column;align-items:flex-start;gap:12px}'
    + '#doc .docx-wrapper>section.docx{margin:0!important;box-shadow:0 1px 2px rgba(0,0,0,.25),0 8px 28px rgba(0,0,0,.28)}';

  // The in-frame document controller. Like artPdf it's authored here and injected as SOURCE, so
  // it may reference nothing outside itself. Keep `</` and `<!` out of it: it rides a srcdoc <script>.
  function artDocx(cfg){
    var D=document, W=window, L=W.docx, caps=(cfg&&cfg.caps)||{};
    function $(id){ return D.getElementById(id); }
    var stage=$("stage"), box=$("pages"), doc=$("doc"), dstyle=$("dstyle"), status=$("status"), ol=$("ol"),
        nav=$("nav"), pos=$("pos"), zin=$("zin"), zout=$("zout"), zfit=$("zfit");
    var done=false, failed=false, timer=0, zoom=1, pageW=816, pages=[], lastFit=0;
    var ZMIN=0.5, ZMAX=3;
    function post(m){ try{ W.parent.postMessage(m, "*"); }catch(_){} }
    function mb(n){ return Math.round(n/1048576)+" MB"; }
    function fail(reason){
      if(failed) return; failed=true; clearTimeout(timer);
      status.textContent="Couldn't render this document: "+reason+". The extracted text is below.";
      status.className="st err"; doc.textContent=""; box.hidden=true; nav.hidden=true;
      stage.classList.add("failed"); ol.open=true;
      post({type:"protoArtifact:docx", state:"failed", reason:String(reason).slice(0,300)});
    }
    function tidy(root){
      var as=root.querySelectorAll ? root.querySelectorAll("a[href]") : [];
      for(var i=0;i<as.length;i++){ var h=String(as[i].getAttribute("href")||"");
        if(!/^https?:/i.test(h) && h.charAt(0)!=="#") as[i].removeAttribute("href"); }
    }
    // Word writes list bullets as private-use code points meant for the Symbol / Wingdings fonts,
    // which a browser doesn't have (they draw as boxes). Swap the common ones for real Unicode.
    var GLYPHS={"\uf0b7":"\u2022", "\uf0a7":"\u25aa", "\uf0d8":"\u27a2", "\uf0fc":"\u2713",
      "\uf076":"\u2756", "\uf0a8":"\u25c6", "\uf06c":"\u25cf", "\uf06e":"\u25a0", "\uf0e0":"\u27a4"};
    function fixGlyphs(root){
      var styles=root.querySelectorAll("style");
      for(var i=0;i<styles.length;i++){
        var t=styles[i].textContent, u=t.replace(/[\uf000-\uf0ff]/g, function(c){ return GLYPHS[c]||"\u2022"; });
        if(u!==t) styles[i].textContent=u;
      }
    }
    // Fit the rendered pages to the panel with CSS zoom (zoom 1 = the widest page fits the frame).
    // zoom re-lays the text out at the new size, so it stays sharp; a scaling transform would be
    // rasterized at 1x and blurred by WKWebView (#1517).
    function fitScale(){ return Math.max(0.1, (box.clientWidth-8)/pageW); }
    function apply(){
      doc.style.zoom=String(fitScale()*zoom);
      zout.disabled=zoom<=ZMIN; zin.disabled=zoom>=ZMAX;
      update();
    }
    function update(){
      if(!pages.length) return;
      var top=box.getBoundingClientRect().top+4, cur=1;
      for(var i=0;i<pages.length;i++){ if(pages[i].getBoundingClientRect().top<=top) cur=i+1; else break; }
      pos.textContent=cur+" / "+pages.length;
      stage.setAttribute("aria-label", "Document page "+cur+" of "+pages.length);
    }
    async function load(buf){
      if(failed || done) return;
      if(!(buf instanceof ArrayBuffer)) return fail("no file data reached the preview");
      if(buf.byteLength>caps.maxBytes) return fail("the file is over the "+mb(caps.maxBytes)+" preview cap");
      timer=setTimeout(function(){ fail("it took longer than "+Math.round(caps.parseMs/1000)+"s to read"); }, caps.parseMs);
      try{
        await L.renderAsync(buf, doc, dstyle, {className:"docx", inWrapper:true, ignoreWidth:false, ignoreHeight:false,
          ignoreFonts:false, breakPages:true, ignoreLastRenderedPageBreak:true, experimental:false,
          trimXmlDeclaration:true, useBase64URL:true, renderChanges:false, renderHeaders:true, renderFooters:true,
          renderFootnotes:true, renderEndnotes:true, renderComments:false, renderAltChunks:false, debug:false});
        if(failed) return;
        clearTimeout(timer); done=true;
        tidy(doc); fixGlyphs(dstyle); fixGlyphs(doc);
        pages=Array.prototype.slice.call(doc.querySelectorAll("section.docx"));
        if(!pages.length) return fail("the document has no pages");
        pageW=Math.max.apply(null, pages.map(function(p){ return p.offsetWidth||816; }));
        status.textContent=""; nav.hidden=false; lastFit=box.clientWidth; apply();
        post({type:"protoArtifact:docx", state:"rendered", count:pages.length});
      }catch(e){ fail(String((e&&e.message)||e).slice(0,200)); }
    }
    zin.addEventListener("click", function(){ zoom=Math.min(ZMAX, zoom*1.25); apply(); });
    zout.addEventListener("click", function(){ zoom=Math.max(ZMIN, zoom/1.25); apply(); });
    zfit.addEventListener("click", function(){ zoom=1; apply(); });
    box.addEventListener("scroll", update, {passive:true});
    D.addEventListener("keydown", function(e){
      if(!done || e.altKey || e.ctrlKey || e.metaKey) return;
      var t=e.target; if(t && (t.tagName==="SUMMARY" || t.tagName==="BUTTON")) return;
      var k=e.key;
      if(k==="+"||k==="="){ e.preventDefault(); zoom=Math.min(ZMAX, zoom*1.25); apply(); }
      else if(k==="-"||k==="_"){ e.preventDefault(); zoom=Math.max(ZMIN, zoom/1.25); apply(); }
      else if(k==="0"){ e.preventDefault(); zoom=1; apply(); }
    });
    if(W.ResizeObserver){ var rt=0; new ResizeObserver(function(){ clearTimeout(rt); rt=setTimeout(function(){
      if(!done || Math.abs(box.clientWidth-lastFit)<=2) return; lastFit=box.clientWidth; apply(); }, 120); }).observe(box); }
    W.addEventListener("message", function(e){
      if(e.source!==W.parent) return;
      var m=e.data||{};
      if(m.type==="protoArtifact:theme" && m.tokens && typeof m.tokens==="object"){
        Object.keys(m.tokens).forEach(function(k){ if(/^--pl-color-[a-z-]+$/.test(k)) D.documentElement.style.setProperty(k, String(m.tokens[k])); });
        return;
      }
      if(m.type==="protoArtifact:docx:data"){ load(m.buf); return; }
      if(m.type==="protoArtifact:docx:error"){ fail(String(m.reason||"the file couldn't be fetched")); }
    });
    if(!L || typeof L.renderAsync!=="function" || !W.JSZip){ fail("the document renderer didn't load"); return; }
    post({type:"protoArtifact:docx", state:"need"});
  }
  // Slide previews, shell side. The deck's frame is sandboxed (opaque origin, no bearer), so it
  // asks for its bytes ({state:"need"}) and the shell fetches the gated blob and TRANSFERS a copy
  // in. The fetch is size-capped before and after, and cached per version so a re-render (theme,
  // tab back) doesn't download again. The watchdog is the backstop for a frame that never answers
  // (a renderer wedged on a hostile file): it swaps the frame for the plain text outline card.
  var pptxCtx=null, pptxCache={key:"", buf:null}, pptxWatch=0;
  function pptxReset(ctx){ clearTimeout(pptxWatch); pptxCtx=ctx; }
  // `ctx.kind` is "pptx" (slides) or "pdf" (pages): one byte-feed for both renderers.
  function pptxFallback(ctx, note){
    if(pptxCtx!==ctx) return;  // the panel moved on to another version
    pptxCtx=null; clearTimeout(pptxWatch);
    $frame.srcdoc=fileCard(ctx.v, note);
  }
  // One byte feed serves every in-frame renderer; each kind brings its own caps.
  function feedCaps(kind){ return kind==="pdf" ? PDF_CAPS : kind==="docx" ? DOCX_CAPS : PPTX_CAPS; }
  async function pptxBytes(ctx){
    if(pptxCache.key===ctx.key && pptxCache.buf) return pptxCache.buf;
    var size=+((ctx.v.file||{}).size||0), maxBytes=feedCaps(ctx.kind).maxBytes;
    if(size>maxBytes) throw new Error("the file is over the "+Math.round(maxBytes/1048576)+" MB preview cap");
    var r=await kit.apiFetch("/api/plugins/artifact/artifact/"+encodeURIComponent(ctx.id)+"/blob?version="+(ctx.vi+1));
    if(!r.ok) throw new Error("the file couldn't be fetched ("+r.status+")");
    var buf=await r.arrayBuffer();
    if(buf.byteLength>maxBytes) throw new Error("the file is over the preview size cap");
    pptxCache={key:ctx.key, buf:buf};
    return buf;
  }
  async function pptxMessage(m){
    var ctx=pptxCtx; if(!ctx || m.type!=="protoArtifact:"+ctx.kind) return;
    if(m.state==="rendered"||m.state==="failed"){ clearTimeout(pptxWatch); return; }
    if(m.state!=="need") return;
    clearTimeout(pptxWatch);
    pptxWatch=setTimeout(function(){
      pptxFallback(ctx, (ctx.kind==="pptx" ? "Slide" : "Page")+" preview unavailable — rendering took too long");
    }, feedCaps(ctx.kind).watchdogMs);
    try{
      var buf=await pptxBytes(ctx);
      if(pptxCtx!==ctx) return;
      var copy=buf.slice(0);  // transfer a copy; the cache keeps the original
      $frame.contentWindow.postMessage({type:"protoArtifact:"+ctx.kind+":data", buf:copy}, "*", [copy]);
    }catch(err){
      if(pptxCtx!==ctx) return;
      try{ $frame.contentWindow.postMessage({type:"protoArtifact:"+ctx.kind+":error", reason:String((err&&err.message)||err)}, "*"); }catch(_){}
    }
  }

  // Agent-callback bridge: an artifact's window.protoArtifact.ask(prompt) posts here;
  // we call the bearer-gated /ask endpoint (a bare agent completion) and post the
  // answer back INTO the artifact frame. e.source-guarded to only our artifact frame —
  // the kit's own protoagent:init handshake messages are ignored here.
  window.addEventListener("message", async function(e){
    if(!$frame || e.source!==$frame.contentWindow) return;
    var m=e.data||{};
    if(m.type==="protoArtifact:pptx"||m.type==="protoArtifact:pdf"||m.type==="protoArtifact:docx"){ pptxMessage(m); return; }
    // Embed only: the frame's measured content height → size the frame and relay it to the host.
    if(m.type==="protoArtifact:height"){ if(EMBED) embedHeight(m.height); return; }
    // Render verdict from the sandbox (#1458) → relay to /render-status so the agent's
    // create/edit reply + check_artifact can surface a render failure. Best-effort POST —
    // intentionally silent on error (#2885 exempts it): a fire-and-forget status report,
    // not user-facing data, so there's no lying empty state to correct.
    if(m.type==="protoArtifact:render"){
      if(renderingId){ try{ kit.apiFetch("/api/plugins/artifact/render-status",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:renderingId,version:renderingVer,n:renderingN,ts:renderingTs,ok:!!m.ok,error:String(m.error||"").slice(0,2000)})}); }catch(_){} if(!EMBED) kickPoll(); /* the verdict rewrites the store — pick it up from idle promptly (#2256); embed has no standing poll */ }
      return;
    }
    // A click on a linked diagram element (ADR 0038 amendment). Only the KEY is read from the
    // frame — the target comes from the rendered version's stored links; an unknown key, or a
    // post without a user gesture behind it (a script firing on its own), opens nothing.
    if(m.type==="protoArtifact:openCode"){
      var ua=navigator.userActivation;
      if(ua && !ua.isActive) return;
      openTarget(linkTarget(m.key));
      return;
    }
    // Which keys the diagram actually matched (+ their visible labels) — for the Links list.
    if(m.type==="protoArtifact:linkmap"){
      var mm=m.matched, keep={};
      if(mm && typeof mm==="object") Object.keys(mm).forEach(function(k){ if(linkTarget(k)) keep[k]=String(mm[k]||"").slice(0,160); });
      linkLabels=keep; renderLinks();
      return;
    }
    // send-to-chat / openLink (ADR 0118 D4) — relayed UP to the console host, which gates them.
    if(m.type==="protoArtifact:send" || m.type==="protoArtifact:openLink"){ relayBridge(m); return; }
    if(m.type!=="protoArtifact:ask") return;
    function reply(p){ try{ $frame.contentWindow.postMessage(Object.assign({type:"protoArtifact:result",id:m.id},p),"*"); }catch(_){} }
    try{
      var r=await kit.apiFetch("/api/plugins/artifact/ask",
        {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:m.prompt})});
      if(!r.ok){ var t=""; try{ t=await r.text(); }catch(_){} reply({error:("ask failed ("+r.status+") "+t).slice(0,300)}); return; }
      var d=await r.json(); reply({text:(d&&d.text)||""});
    }catch(err){ reply({error:String(err).slice(0,300)}); }
  });

  // ── send-to-chat / openLink bridge (ADR 0118 D4) ───────────────────────────────────────
  // The in-frame shim's send()/openLink() post up here; the shell SANITY-checks and relays the
  // request to the console host (its embedder), which owns the real gates — a user gesture,
  // length, "the agent is busy", the 1-per-2s rate (send) / https + origin allowlist
  // (openLink) — in S10. The host posts its verdict back down (`protoArtifact:bridgeResult`)
  // and we relay it into the frame as a `protoArtifact:result`, resolving/rejecting the frame's
  // Promise. Trust lives in the host: the shell never decides whether a send is allowed; it
  // only tags the request with which artifact/version it came from (D4 origin metadata) and
  // correlates the reply. Works in panel AND embed mode — the embedder is the console host
  // either way.
  var bridgeSeq = 0, bridgePending = {};  // host-correlation id (cid) → the frame's own call id
  function relayBridge(m){
    var frameId = m.id, kind = (m.type === "protoArtifact:send") ? "send" : "openLink";
    function deny(msg){ if(!$frame) return; try{ $frame.contentWindow.postMessage({type:"protoArtifact:result", id:frameId, error:msg}, "*"); }catch(_){} }
    // No embedder to gate the request (a standalone page load, not embedded in the console) →
    // refuse locally so the frame's Promise doesn't hang until its timeout. The host is the
    // only thing trusted to allow it, so without one the answer is no.
    if(window.parent === window){ deny(kind + " is unavailable here"); return; }
    var cid = ++bridgeSeq;
    bridgePending[cid] = frameId;
    var payload = {type:m.type, cid:cid, via:"artifact", artifact_id:renderingId||null, version:renderingN||0};
    // Coarse sanity bound only — the host enforces the real 1–4000 / https limits. Kept well
    // above 4000 so a too-long send still reaches the host and is rejected there, not silently
    // trimmed into range here.
    if(kind === "send") payload.text = String(m.text||"").slice(0, 16384);
    else payload.url = String(m.url||"").slice(0, 4096);
    try{ window.parent.postMessage(payload, "*"); }
    catch(_){ delete bridgePending[cid]; deny(kind + " failed"); }
  }
  // The host's verdict for a relayed send/openLink. Trusted only from the embedder (fromEmbedder,
  // defined below) — never from the nested artifact frame (model code) or a sibling — and matched
  // to a pending request by its correlation id, so a stray post can't resolve an unrelated call.
  window.addEventListener("message", function(e){
    var m = e.data;
    if(!m || m.type !== "protoArtifact:bridgeResult" || !fromEmbedder(e)) return;
    var frameId = bridgePending[m.cid];
    if(frameId === undefined) return;  // unknown / already-settled correlation id
    delete bridgePending[m.cid];
    if(!$frame) return;
    var reply = {type:"protoArtifact:result", id:frameId};
    if(m.ok) reply.text = String(m.text||""); else reply.error = String(m.error||"rejected").slice(0,300);
    try{ $frame.contentWindow.postMessage(reply, "*"); }catch(_){}
  });

  // Deep-link from the console (#3617): a chat `artifact-ref` chip asks the panel to show ONE
  // artifact at ONE version — `{type:"protoArtifact:select", id, ver}`, ver = the LIFETIME
  // version number the agent's tool reported. Trusted only from the window that EMBEDS this
  // page (the console, via its PluginView bridge): never from the nested artifact frame (model
  // code), never from a sibling or an opener. The origin is checked against the embedder's
  // origin where the browser tells us (location.ancestorOrigins — Chromium/WebKit, which is
  // every console host; the desktop embeds from tauri://, a different origin than this page).
  // Low-stakes by design either way: a select only changes which stored artifact is on screen.
  function fromEmbedder(e){
    if(window.parent===window || e.source!==window.parent) return false;
    var anc=location.ancestorOrigins;
    if(anc && anc.length) return e.origin===anc[0];
    return true;
  }
  // The request waits for the store mirror: the panel may still be booting (a collapsed dock
  // mounts it on this very open), or its last poll may predate the artifact the agent just
  // made. Applied after every poll until it resolves; an id still absent after a fresh poll
  // was deleted/evicted, so the request is dropped and the panel keeps what it showed.
  var pendingSel=null;
  function applyPendingSel(fresh){
    if(!pendingSel) return;
    var a=null; for(var i=0;i<arts.length;i++) if(arts[i].id===pendingSel.id){ a=arts[i]; break; }
    if(!a){ if(fresh) pendingSel=null; return; }
    var want=pendingSel.ver; pendingSel=null;
    var vtot=total(a), idx=want-(vtot-a.versions.length)-1;
    selId=a.id;
    // The newest version → follow it (selVer null), and follow newest ARTIFACTS only when this
    // is also the panel's current one (the picker's own rule). An older version pins it: auto-
    // follow off, so the next agent edit doesn't yank the operator off the version they asked
    // for. A version trimmed at the cap falls back to the oldest one still kept.
    if(idx>=a.versions.length-1){ selVer=null; followNewest=(a.id===(curId||(arts[0]&&arts[0].id))); }
    else { selVer=Math.max(0,idx); followNewest=false; }
    if(editing) exitEdit();
    saveSel(); rebuildArtSelect(); render();
  }
  window.addEventListener("message", function(e){
    var m=e.data;
    if(!m || m.type!=="protoArtifact:select" || !fromEmbedder(e) || EMBED) return;  // embed shows a fixed version
    var id=typeof m.id==="string" ? m.id.slice(0,64) : "";
    var ver=(typeof m.ver==="number" && isFinite(m.ver)) ? Math.floor(m.ver) : 0;
    if(!id || ver<1) return;
    pendingSel={id:id, ver:ver};
    if(booted){ applyPendingSel(false); if(pendingSel) kickPoll(); }
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
        + "color:var(--pl-color-status-error,#f85149);background:var(--pl-color-bg-inset,rgba(127,127,127,.12));"
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
      if (r.status === 304) { pollOk(); applyPendingSel(true); return; }  // unchanged — no parse, no DOM churn
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
      applyPendingSel(true);  // a chip's deep-link (#3617) — after the follow rules, so it wins
    } catch (e) { pollFailed(""); /* network-level — no HTTP status to name */ }
  }
  function schedulePoll(){
    clearTimeout(pollTimer);
    var idle = (Date.now() - storeChangedAt) > POLL_ACTIVE_WINDOW_MS;
    pollTimer = setTimeout(async function(){ await poll(); schedulePoll(); }, idle ? POLL_IDLE_MS : POLL_FAST_MS);
  }
  function kickPoll(){ storeChangedAt = Date.now(); clearTimeout(pollTimer); poll().then(schedulePoll, schedulePoll); }

  // ── embed placement (ADR 0118 D2) ───────────────────────────────────────────────────────
  // renders exactly ONE version with no panel chrome, through the SAME srcdoc()/fileCard()
  // builder render() uses — so its nonce CSP, vendored SRI LIB map, theme tokens + live
  // re-theme, loader lockdown and render-verdict reporting are all inherited (there is no
  // second frame builder). The frame reports its content height (HEIGHTJS, woken by the
  // protoArtifact:measure below) and the shell relays it to the console host, which clamps it.
  var EMBED_TRIES = 8, EMBED_RETRY_MS = 1200, EMBED_MIN_H = 80, EMBED_MAX_H = 1200;
  // Resolve {art, idx, v} for id + LIFETIME version in the store mirror, or null when the
  // artifact is absent or the version is out of range / trimmed at the cap → the inert
  // "unavailable" state. ver 0/absent follows the latest.
  function embedLocate(list, id, ver){
    var a=null, i;
    for(i=0;i<list.length;i++) if(list[i].id===id){ a=list[i]; break; }
    if(!a || !a.versions || !a.versions.length) return null;
    var n=a.versions.length, vtot=total(a), idx;
    if(!ver) idx=n-1;
    else { idx=ver-(vtot-n)-1; if(idx<0 || idx>n-1) return null; }
    return {art:a, idx:idx, v:a.versions[idx]};
  }
  function showUnavail(){
    var el=document.getElementById("unavail"); if(el) el.style.display="flex";
    if($frame){ $frame.style.display="none"; $frame.removeAttribute("srcdoc"); }
    lastRendered=""; embedPostParent({type:"protoArtifact:height", height:EMBED_MIN_H});
  }
  function hideUnavail(){ var el=document.getElementById("unavail"); if(el) el.style.display="none"; }
  // The content height the frame measured → size the frame to it and relay it to the host.
  function embedHeight(h){
    h=Math.floor(+h||0); if(h<=0) return;
    // Clamp to the console host's own [EMBED_MIN_H, EMBED_MAX_H] range so a frame whose content is
    // sized in viewport units (e.g. 100vh + padding) can't drive the frame — and the measurement
    // that follows it — upward without bound; it converges at the cap instead of growing forever.
    h=Math.max(EMBED_MIN_H, Math.min(EMBED_MAX_H, h));
    if($frame) $frame.style.height=h+"px";
    embedPostParent({type:"protoArtifact:height", height:h});
  }
  // Which embed sizing a kind gets: FILL (a width-proportional box) for the navigable svg/mermaid
  // viewports, the paged deck/PDF/Word frames, and the scroll-box file cards — none has a content
  // height to follow; FLOW (size-to-content) for html, markdown, react, charts, and .md previews.
  function embedFill(a, v){
    if(a.kind==="svg" || a.kind==="mermaid") return true;
    if(a.kind!=="file") return false;   // html, markdown, react, vega-lite
    return previewKind((v.file||{}).filename, (v.file||{}).mime) !== "md";  // .md renders as flowing prose
  }
  // Append the height reporter + its CSS reset to a built frame doc WITHOUT touching the doc the
  // panel builder produced (so there is no second builder and the panel stays byte-identical). The
  // reporter is injected here, in the embed path only — NOT in base() — so it rides EVERY embed
  // frame, including the script-free file cards (table/json/text/sheets) and the nonce-CSP
  // decks/PDF/Word frames that never call base(). Its inline <script> reuses the doc's existing CSP
  // nonce when it has one (vega / slides / PDF / Word run under a nonce CSP, which would otherwise
  // block a bare inline script and leave those embeds never reporting); the nonce-free kinds take a
  // bare <script>.
  function embedSuffix(doc, fill){
    var m = /script-src 'nonce-([A-Za-z0-9]+)'/.exec(doc);
    var tag = m ? '<script nonce="' + m[1] + '">' : '<script>';
    return '<style>' + (fill ? EMBED_FILL_CSS : EMBED_FLOW_CSS) + '</style>'
      + HEIGHTJS.replace('<script>', tag);
  }
  // Post UP to the embedder (the console host). Targeted at its origin where the browser tells
  // us (ancestorOrigins), like openTarget — a height int is low-stakes, but stay a good citizen.
  function embedPostParent(msg){
    if(window.parent===window) return;
    var anc=location.ancestorOrigins, origin=(anc && anc.length) ? anc[0] : "*";
    try{ window.parent.postMessage(msg, origin); }catch(_){}
  }
  // Render the one requested version through the shared builder; returns false when it isn't in
  // the store mirror yet (the caller retries, then settles on the unavailable state).
  function renderEmbed(){
    var r = EMBED && embedLocate(arts, EMBED.id, EMBED.ver);
    if(!r) return false;
    hideUnavail();
    var a=r.art, vi=r.idx, v=r.v;
    // Same render-verdict plumbing as render() so a failed inline render still reaches the agent.
    renderingId=a.id; renderingVer=vi+1; renderingTs=v.ts;
    renderingN=(a.version_count||a.versions.length)-a.versions.length+vi+1;
    renderingLinks = (a.kind==="mermaid" && v.links && typeof v.links==="object" && !Array.isArray(v.links)) ? v.links : null;
    linkLabels = null;
    var key="embed:"+a.id+"@"+vi+"@"+v.ts;
    if(key!==lastRendered){
      lastRendered=key;
      pptxReset(a.kind==="file" && (slidesOk(v)||pdfOk(v)||docxOk(v))
        ? {id:a.id, vi:vi, v:v, key:key, kind:pdfOk(v) ? "pdf" : docxOk(v) ? "docx" : "pptx"} : null);
      // SAME builder as the panel; the embed-only tail (height reporter + sizing CSS) is appended
      // AFTER it, so the frame's own doc — its CSP, vendored libs and SRI — is byte-identical.
      var doc = a.kind==="file" ? fileCard(v) : srcdoc(a.kind, v.code, renderingLinks);
      $frame.srcdoc = doc + embedSuffix(doc, embedFill(a, v)); $frame.style.display="block";
    }
    return true;
  }
  // One bounded fetch of the gated store (the kit's bearer arrives with the handshake, so an
  // early try before it lands retries rather than stranding). A fixed inline version never
  // changes, so there's no standing poll — stop once rendered, settle on "unavailable" if the
  // id/version never appears.
  function embedBoot(){
    var tries=0;
    (function attempt(){
      tries++;
      kit.apiFetch("/api/plugins/artifact/history").then(function(r){
        if(!r.ok) throw new Error(String(r.status));
        return r.json();
      }).then(function(d){
        arts=(d && d.artifacts) || []; curId=(d && d.current) || null;
        if(renderEmbed()) return;
        if(tries<EMBED_TRIES){ setTimeout(attempt, EMBED_RETRY_MS); return; }
        showUnavail();
      }).catch(function(){
        if(tries<EMBED_TRIES){ setTimeout(attempt, EMBED_RETRY_MS); return; }
        showUnavail();
      });
    })();
  }

  // Boot ONCE, on whichever fires first: the handshake (the bearer arrives with
  // protoagent:init, so the gated history poll authenticates) or a short timer
  // for the no-handshake case (standalone page / older host).
  var booted = false;
  // loadSel() restores the operator's last selection — a pending deep-link (#3617) that arrived
  // before boot still wins, applied by the first poll right after.
  function boot(){ if (booted) return; booted = true;
    if (EMBED) { embedBoot(); return; }   // one version, no chrome, no standing poll
    loadSel(); poll(); schedulePoll(); }
  kit.initPluginView(boot);
  setTimeout(boot, 800);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden && booted && !EMBED) kickPoll(); }); // refresh on return (panel only)
