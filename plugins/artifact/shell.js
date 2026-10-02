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
    // @aiden0z/pptx-renderer 1.3.0 (Apache-2.0), re-wrapped as an IIFE → window.PptxRenderer.
    // Slide previews for .pptx file artifacts; notices in vendor/pptx-renderer.LICENSES.txt.
    pptx: ["pptx-renderer.min.js",
      "sha512-MyPAN9XW0LRMqC0rmeaTshNlkb1GW+ODchhz+wvoAT927kKbrkcDGm05cvGa1mVrgAmFlKfA42YE4CFwfzes3g=="],
  };
  // crossorigin="anonymous" is REQUIRED even though the lib is same-origin to the
  // shell: the artifact runs in a no-same-origin sandbox (opaque origin), so its
  // subresource loads are cross-origin — SRI on a cross-origin script without
  // crossorigin can't validate and the browser blocks it. The vendor route sends
  // Access-Control-Allow-Origin:* to satisfy the CORS fetch.
  function cdn(name, nonce){ var c = LIB[name];
    return '<script crossorigin="anonymous" integrity="' + c[1] + '"' + (nonce ? ' nonce="' + nonce + '"' : '')
      + ' src="' + ORIGIN + '/plugins/artifact/vendor/' + c[0] + '"><\/script>'; }
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

  function srcdoc(kind, code, links) {
    if (kind === "html") return htmlDoc(code, dsLink() + base(kind));
    if (kind === "svg") return '<!doctype html>' + base(kind) + viewport(code) + gfxScript({}) +
      '<script>__artVP.full();<\/script></body>';
    // mermaid.run() is async: the viewport + code links mount once the <svg> exists. A rejected
    // run stays UNHANDLED on purpose — ERRBOOT's unhandledrejection hook reports it (#1458).
    if (kind === "mermaid") return '<!doctype html>' + base(kind) + viewport('<pre class="mermaid">' + esc(code) + '</pre>') +
      cdn("mermaid") + gfxScript({links: linkDisplay(links)}) +
      '<script>mermaid.initialize({startOnLoad:false,theme:' + JSON.stringify(mermaidTheme()) + '});'
      + 'mermaid.run().then(function(){__artVP.full();});<\/script></body>';
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
  // .pptx decks render as real slides (the vendored renderer, see slidesDoc); _slides.is_slides
  // is the Python twin of this test (by extension, or the OOXML presentation mime).
  var PPTX_MIME="application/vnd.openxmlformats-officedocument.presentationml.presentation";
  function previewKind(name, mime){
    var n=String(name||"").toLowerCase(), i=n.lastIndexOf("."), ext=i<0?"":n.slice(i+1);
    if(ext==="pptx" || String(mime||"").toLowerCase()===PPTX_MIME) return "slides";
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
  // Does this version get the slide renderer? ONLY a .pptx the save-time preflight (_slides.py)
  // cleared — it inflated every entry under a budget, so the frame never parses bytes the server
  // hasn't measured. A refusal, or a version saved before the preflight existed (no verdict),
  // gets the text outline card.
  function slidesOk(v){
    var f=v.file||{};
    if(previewKind(f.filename, f.mime)!=="slides") return false;
    return !!(f.slides && typeof f.slides==="object" && f.slides.render===true);
  }
  // `note` (optional) forces the text card and says why the slides aren't shown.
  function fileCard(v, note){
    var f=v.file||{}, name=f.filename||"file", mime=f.mime||"application/octet-stream";
    var pk=previewKind(name, mime);
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
    var body, pvl=note ? "Text outline" : "Preview";
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
  var $art=document.getElementById("art"), $vprev=document.getElementById("vprev"),
      $vnext=document.getElementById("vnext"), $vlabel=document.getElementById("vlabel"),
      $dl=document.getElementById("dl"), $del=document.getElementById("del"),
      $bar=document.getElementById("bar"), $empty=document.getElementById("empty"),
      $frame=document.getElementById("frame"), $edit=document.getElementById("edit"),
      $editor=document.getElementById("editor"), $code=document.getElementById("code"),
      $run=document.getElementById("run"), $cancel=document.getElementById("cancel"),
      $estat=document.getElementById("estat"), $dlstat=document.getElementById("dlstat");
  var editing=false;

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
      // A deck's frame asks for its bytes once it boots (pptxNeed) — remember which version it is.
      pptxReset(a.kind==="file" && slidesOk(v) ? {id:a.id, vi:vi, v:v, key:key} : null);
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

  // Slide previews, shell side. The deck's frame is sandboxed (opaque origin, no bearer), so it
  // asks for its bytes ({state:"need"}) and the shell fetches the gated blob and TRANSFERS a copy
  // in. The fetch is size-capped before and after, and cached per version so a re-render (theme,
  // tab back) doesn't download again. The watchdog is the backstop for a frame that never answers
  // (a renderer wedged on a hostile file): it swaps the frame for the plain text outline card.
  var pptxCtx=null, pptxCache={key:"", buf:null}, pptxWatch=0;
  function pptxReset(ctx){ clearTimeout(pptxWatch); pptxCtx=ctx; }
  function pptxFallback(ctx, note){
    if(pptxCtx!==ctx) return;  // the panel moved on to another version
    pptxCtx=null; clearTimeout(pptxWatch);
    $frame.srcdoc=fileCard(ctx.v, note);
  }
  async function pptxBytes(ctx){
    if(pptxCache.key===ctx.key && pptxCache.buf) return pptxCache.buf;
    var size=+((ctx.v.file||{}).size||0);
    if(size>PPTX_CAPS.maxBytes) throw new Error("the file is over the "+Math.round(PPTX_CAPS.maxBytes/1048576)+" MB preview cap");
    var r=await kit.apiFetch("/api/plugins/artifact/artifact/"+encodeURIComponent(ctx.id)+"/blob?version="+(ctx.vi+1));
    if(!r.ok) throw new Error("the file couldn't be fetched ("+r.status+")");
    var buf=await r.arrayBuffer();
    if(buf.byteLength>PPTX_CAPS.maxBytes) throw new Error("the file is over the preview size cap");
    pptxCache={key:ctx.key, buf:buf};
    return buf;
  }
  async function pptxMessage(m){
    var ctx=pptxCtx; if(!ctx) return;
    if(m.state==="rendered"||m.state==="failed"){ clearTimeout(pptxWatch); return; }
    if(m.state!=="need") return;
    clearTimeout(pptxWatch);
    pptxWatch=setTimeout(function(){ pptxFallback(ctx, "Slide preview unavailable — rendering took too long"); }, PPTX_CAPS.watchdogMs);
    try{
      var buf=await pptxBytes(ctx);
      if(pptxCtx!==ctx) return;
      var copy=buf.slice(0);  // transfer a copy; the cache keeps the original
      $frame.contentWindow.postMessage({type:"protoArtifact:pptx:data", buf:copy}, "*", [copy]);
    }catch(err){
      if(pptxCtx!==ctx) return;
      try{ $frame.contentWindow.postMessage({type:"protoArtifact:pptx:error", reason:String((err&&err.message)||err)}, "*"); }catch(_){}
    }
  }

  // Agent-callback bridge: an artifact's window.protoArtifact.ask(prompt) posts here;
  // we call the bearer-gated /ask endpoint (a bare agent completion) and post the
  // answer back INTO the artifact frame. e.source-guarded to only our artifact frame —
  // the kit's own protoagent:init handshake messages are ignored here.
  window.addEventListener("message", async function(e){
    if(!$frame || e.source!==$frame.contentWindow) return;
    var m=e.data||{};
    if(m.type==="protoArtifact:pptx"){ pptxMessage(m); return; }
    // Render verdict from the sandbox (#1458) → relay to /render-status so the agent's
    // create/edit reply + check_artifact can surface a render failure. Best-effort POST —
    // intentionally silent on error (#2885 exempts it): a fire-and-forget status report,
    // not user-facing data, so there's no lying empty state to correct.
    if(m.type==="protoArtifact:render"){
      if(renderingId){ try{ kit.apiFetch("/api/plugins/artifact/render-status",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:renderingId,version:renderingVer,n:renderingN,ts:renderingTs,ok:!!m.ok,error:String(m.error||"").slice(0,2000)})}); }catch(_){} kickPoll(); /* the verdict rewrites the store — pick it up from idle promptly (#2256) */ }
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
    if(m.type!=="protoArtifact:ask") return;
    function reply(p){ try{ $frame.contentWindow.postMessage(Object.assign({type:"protoArtifact:result",id:m.id},p),"*"); }catch(_){} }
    try{
      var r=await kit.apiFetch("/api/plugins/artifact/ask",
        {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:m.prompt})});
      if(!r.ok){ var t=""; try{ t=await r.text(); }catch(_){} reply({error:("ask failed ("+r.status+") "+t).slice(0,300)}); return; }
      var d=await r.json(); reply({text:(d&&d.text)||""});
    }catch(err){ reply({error:String(err).slice(0,300)}); }
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
    if(!m || m.type!=="protoArtifact:select" || !fromEmbedder(e)) return;
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
  // Boot ONCE, on whichever fires first: the handshake (the bearer arrives with
  // protoagent:init, so the gated history poll authenticates) or a short timer
  // for the no-handshake case (standalone page / older host).
  var booted = false;
  // loadSel() restores the operator's last selection — a pending deep-link (#3617) that arrived
  // before boot still wins, applied by the first poll right after.
  function boot(){ if (booted) return; booted = true; loadSel(); poll(); schedulePoll(); }
  kit.initPluginView(boot);
  setTimeout(boot, 800);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden && booted) kickPoll(); }); // refresh on return
