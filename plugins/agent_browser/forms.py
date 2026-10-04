"""Form-field addressing that survives re-renders (#4032).

A ``browser_snapshot`` hands the model compact ``@eN`` refs, but those are resolved
ONCE and go stale the instant the page re-renders — a reload, a React state change, a
validation error redrawing the field. On a long Greenhouse application form that is a
treadmill: every reflow invalidates the refs and the next action fails "Unknown ref".

The fix here is to address a field by something that is **re-resolved in the page, fresh,
on every call** — its visible label — rather than by a cached handle. All of the in-page
JavaScript lives in this module as string constants so the read tool, and the later
``browser_select`` / ``browser_upload`` tools, share ONE resolver; the ranking that turns
a label into a single field lives on the Python side (``match_fields``) so it is pure and
host-free to unit-test.

A ``field`` string is classified (``classify``) and resolved in this order:

* ``@e…``  → a snapshot ref, passed through to the CLI UNCHANGED (back-compat).
* ``#`` / ``.`` / ``[`` prefix, or a CSS combinator (``>`` ``+`` ``~``) → a CSS selector.
* anything else → a LABEL: matched case-insensitively, whitespace-collapsed, asterisks
  (required markers) ignored, against — in source order — the ``<label for>`` text, a
  wrapping ``<label>``, ``aria-label``, ``aria-labelledby`` text, ``placeholder`` and
  ``name``. Exact beats prefix beats substring; a tie at the best tier is AMBIGUOUS and is
  an error (never guess), and zero matches is an error naming the closest labels.

The scripts always ride ``agent-browser eval --stdin`` (never argv, #3689).
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field as _dc_field

# ── field classification (Python side — pure string inspection) ──────────────────
# A dash-and-letter is the CLI's option grammar, but classify() runs on a locator, not an
# argv element; the argv guard (runtime.bad_operand) still covers anything that reaches the
# command line. Descendant whitespace is deliberately NOT treated as a combinator: real
# field labels contain spaces ("First name"), so only the explicit combinators count.
_CSS_START = ("#", ".", "[")
_CSS_COMBINATORS = (">", "+", "~")


def classify(field: str) -> str:
    """``"ref"`` (``@e…``), ``"css"`` (selector) or ``"label"`` (visible text)."""
    s = (field or "").strip()
    if s.startswith("@e"):
        return "ref"
    if s[:1] in _CSS_START or any(c in s for c in _CSS_COMBINATORS):
        return "css"
    return "label"


def is_ref(field: str) -> bool:
    return classify(field) == "ref"


def is_css(field: str) -> bool:
    return classify(field) == "css"


def normalize(s: str) -> str:
    """Collapse whitespace, strip required-marker asterisks, lower-case — the comparison
    key for label matching. Mirrors ``norm()`` in the in-page JS."""
    return re.sub(r"\s+", " ", re.sub(r"\*+", " ", s or "")).strip().lower()


def _js(value) -> str:
    """A safe JS literal for an embedded string (JSON is a JS-expression subset)."""
    return json.dumps(value)


# ── the in-page JavaScript (ONE copy, shared by every form tool) ─────────────────
# Defines, in the page: clean/norm (text tidy), candidateLabels (the six label sources in
# precedence order), kindOf / requiredOf / comboValue (field shape), enumerateFields(root)
# (every field in document order, each tagged `data-ab-field="<i>"` so a resolved field has
# a selector the CLI can act on THIS call), and scopeRoot (restrict a read to a container).
_JS_LIB = r"""
function abClean(s){ return String(s==null?'':s).replace(/\*+/g,' ').replace(/\s+/g,' ').trim(); }
function abNorm(s){ return abClean(s).toLowerCase(); }
function abTextNoControls(el){
  var c = el.cloneNode(true);
  var kids = c.querySelectorAll('input,select,textarea,button');
  for(var i=0;i<kids.length;i++){ kids[i].remove(); }
  return abClean(c.textContent);
}
function abByIds(ids){
  return String(ids||'').split(/\s+/).map(function(id){
    var n = id && document.getElementById(id); return n ? abClean(n.textContent) : '';
  }).filter(Boolean).join(' ');
}
// The react-select container (`.select__control`) is the canonical combobox root — it holds
// the committed `.select__single-value`. Its inner search INPUT ALSO carries
// role="combobox", but it is NOT a field of its own: enumerating it surfaced the dropdown
// twice — an unlabelled entry with the real value AND a labelled entry whose value was
// always '' (abComboValue found no single-value node on a bare <input>) (#4032 review).
function abComboContainer(el){
  return !!(el.matches && el.matches('.select__control, [class*="select__control"]'));
}
function abComboRoot(el){
  if(abComboContainer(el)) return true;
  // A bare ARIA combobox is a root only when it is NOT the inner input of a react-select
  // container — that inner input is represented by the container above it.
  if(el.matches && el.matches('[role="combobox"]')){
    return !(el.closest && el.closest('.select__control, [class*="select__control"]'));
  }
  return false;
}
function abInCombo(el){
  if(abComboRoot(el)) return false;
  // The react-select inner search input matches `[role="combobox"]` itself, so closest() on
  // that selector would return the input and miss the skip; test the CONTAINER ancestor
  // explicitly (never a self-match) before falling back to a bare ARIA combobox ancestor.
  var cont = el.closest && el.closest('.select__control, [class*="select__control"]');
  if(cont && cont !== el) return true;
  var combo = el.closest && el.closest('[role="combobox"]');
  return !!(combo && combo !== el);
}
function abCandidateLabels(el){
  var out = [];
  var id = el.getAttribute && el.getAttribute('id');
  if(id){
    var sel = 'label[for="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]';
    var lf = null;
    try { lf = document.querySelector(sel); } catch(e){ lf = null; }
    if(lf) out.push(abTextNoControls(lf));
  }
  var wrap = el.closest && el.closest('label');
  if(wrap) out.push(abTextNoControls(wrap));
  var al = el.getAttribute && el.getAttribute('aria-label'); if(al) out.push(abClean(al));
  var lb = el.getAttribute && el.getAttribute('aria-labelledby'); if(lb) out.push(abClean(abByIds(lb)));
  var ph = el.getAttribute && el.getAttribute('placeholder'); if(ph) out.push(abClean(ph));
  var nm = el.getAttribute && el.getAttribute('name'); if(nm) out.push(abClean(nm));
  var seen = {}, res = [];
  for(var i=0;i<out.length;i++){ var s=out[i]; var k=s.toLowerCase(); if(s && !seen[k]){ seen[k]=1; res.push(s); } }
  return res;
}
function abKindOf(el){
  if(abComboRoot(el)) return 'combobox';
  var tag = el.tagName.toLowerCase();
  if(tag==='textarea') return 'textarea';
  if(tag==='select') return 'native-select';
  if(tag==='input'){
    var t = (el.getAttribute('type')||'text').toLowerCase();
    if(t==='email') return 'email';
    if(t==='tel') return 'tel';
    if(t==='number') return 'number';
    if(t==='checkbox') return 'checkbox';
    if(t==='radio') return 'radio-group';
    if(t==='file') return 'file';
    if(['text','search','url','password',''].indexOf(t)>=0) return 'text';
    return 'other';
  }
  return 'other';
}
function abRequired(el){
  if(el.required) return true;
  var r = el.getAttribute && el.getAttribute('aria-required');
  if(r==='true') return true;
  return !!(el.hasAttribute && el.hasAttribute('required'));
}
function abComboValue(root){
  var sv = root.querySelector('.select__single-value, [class*="singleValue"], [class*="single-value"]');
  if(sv) return abClean(sv.textContent);
  var mv = root.querySelectorAll('.select__multi-value, [class*="multiValue"], [class*="multi-value"]');
  if(mv && mv.length){
    return Array.prototype.map.call(mv, function(n){ return abClean(n.textContent); }).join(', ');
  }
  // last resort: the rendered text with the search input removed, so typed-but-uncommitted
  // text is never reported as the value.
  return abTextNoControls(root);
}
function abEnumerate(root){
  root = root || document;
  var nodes = Array.prototype.slice.call(root.querySelectorAll(
    'input, textarea, select, [role="combobox"], .select__control, [class*="select__control"]'));
  var out = [], seenRadio = {}, idx = 0;
  var skip = {hidden:1, submit:1, button:1, reset:1, image:1};
  for(var i=0;i<nodes.length;i++){
    var el = nodes[i];
    if(abInCombo(el)) continue;               // the combobox root represents its inner input
    if(el.tagName.toLowerCase()==='input' && skip[(el.getAttribute('type')||'').toLowerCase()]) continue;
    var kind = abKindOf(el);
    var name = (el.getAttribute && el.getAttribute('name')) || '';
    if(kind==='radio-group'){
      var key = name || ('@@' + idx);
      if(seenRadio[key]) continue;            // collapse a radio group to its first member
      seenRadio[key] = true;
    }
    var labels = abCandidateLabels(el);
    var comboInner = null;
    if(kind==='radio-group'){
      var fs = el.closest && el.closest('fieldset');
      var leg = fs && fs.querySelector('legend');
      if(leg){ var lt = abClean(abTextNoControls(leg)); if(lt) labels.unshift(lt); }
    }
    if(kind==='combobox'){
      // The react-select container has no label of its own; the labelled node is its inner
      // search input (its id drives `<label for>`, and it carries aria-label/labelledby).
      // Merge those so the ONE combobox entry is both labelled AND carries the value.
      comboInner = el.querySelector && el.querySelector('input, select, textarea, [role="combobox"]');
      if(comboInner && comboInner !== el){
        var il = abCandidateLabels(comboInner);
        for(var li=0; li<il.length; li++){ if(labels.indexOf(il[li])<0) labels.push(il[li]); }
      }
    }
    try { el.setAttribute('data-ab-field', String(idx)); } catch(e){}
    var desc = {
      idx: idx, label: labels[0] || '', labels: labels, kind: kind, name: name,
      id: (el.getAttribute && el.getAttribute('id')) || '', required: abRequired(el),
      selector: '[data-ab-field="' + idx + '"]'
    };
    if(kind==='native-select'){
      desc.options = Array.prototype.map.call(el.options, function(o){ return abClean(o.textContent); });
      var so = el.options[el.selectedIndex];
      desc.value = so ? abClean(so.textContent) : '';
    } else if(kind==='radio-group'){
      var radios = Array.prototype.slice.call(root.querySelectorAll('input[type="radio"]')).filter(
        function(r){ return ((r.getAttribute('name')||'')===name); });
      if(!name){ radios = [el]; }
      desc.options = radios.map(function(r){ var l = abCandidateLabels(r); return l[0] || abClean(r.value) || ''; });
      var chk = radios.filter(function(r){ return r.checked; })[0];
      if(chk){ var cl = abCandidateLabels(chk); desc.value = cl[0] || abClean(chk.value) || ''; }
      else { desc.value = ''; }
    } else if(kind==='combobox'){
      desc.value = abComboValue(el);          // committed selection, NOT the typed search text
      if(comboInner && comboInner !== el){    // adopt the inner input's identity when the
        if(!desc.name){ desc.name = (comboInner.getAttribute && comboInner.getAttribute('name')) || ''; }
        if(!desc.id){ desc.id = (comboInner.getAttribute && comboInner.getAttribute('id')) || ''; }
        if(!desc.required){ desc.required = abRequired(comboInner); }   // container lacks them
      }
    } else if(kind==='checkbox'){
      desc.value = !!el.checked;
    } else if(kind==='file'){
      desc.value = el.files ? Array.prototype.map.call(el.files, function(f){ return f.name; }).join(', ') : '';
    } else {
      desc.value = (el.value!=null) ? String(el.value) : '';
    }
    out.push(desc);
    idx++;
  }
  return out;
}
function abScopeRoot(q, isCss){
  if(!q) return document;
  if(isCss){ try { return document.querySelector(q); } catch(e){ return null; } }
  var conts = Array.prototype.slice.call(document.querySelectorAll(
    'form, fieldset, section, [role="group"], [role="form"], [role="region"], [aria-label], [aria-labelledby]'));
  var nq = abNorm(q), best = null, bestTier = 9;
  for(var i=0;i<conts.length;i++){
    var c = conts[i], names = [];
    var al = c.getAttribute('aria-label'); if(al) names.push(al);
    var lb = c.getAttribute('aria-labelledby'); if(lb) names.push(abByIds(lb));
    var leg = c.querySelector(':scope > legend'); if(leg) names.push(leg.textContent);
    for(var j=0;j<names.length;j++){
      var n = abNorm(names[j]); if(!n) continue;
      var tier = n===nq ? 0 : (n.indexOf(nq)===0 ? 1 : (n.indexOf(nq)>=0 ? 2 : 9));
      if(tier < bestTier){ bestTier = tier; best = c; }
    }
  }
  return bestTier < 9 ? best : null;
}
"""

_RESOLVE_RETURN = (
    "(function(){return JSON.stringify({mode:'enumerate', fields: abEnumerate(document)});})()"
)


def resolve_js(field: str) -> str:
    """The script to eval to resolve ``field`` to an element, fresh, this call.

    A ref or a CSS selector is a pass-through — the CLI resolves those itself — so the
    script just echoes the target without touching the DOM. A label enumerates every field
    (tagging each so it has a usable selector) and hands the list to the Python ranker
    (``parse_resolve`` + ``resolve_target``). ONE implementation, reused by every form tool.
    """
    kind = classify(field)
    if kind in ("ref", "css"):
        return "(function(){return JSON.stringify({mode:'pass',target:" + _js(field.strip()) + "});})()"
    return _JS_LIB + "\n" + _RESOLVE_RETURN


def read_form_js(scope: str = "") -> str:
    """The script to eval for ``browser_form_read``: enumerate every field (optionally
    within ``scope``) and return ``{ok, fields}`` (or ``{ok:false, error}`` when the scope
    matches nothing)."""
    kind = classify(scope) if scope else "none"
    is_css = "true" if kind == "css" else "false"
    return (_JS_LIB + "\n(function(){\n"
            "var root = abScopeRoot(" + _js(scope or "") + ", " + is_css + ");\n"
            "if(root === null) return JSON.stringify({ok:false, error:'scope-not-found'});\n"
            "return JSON.stringify({ok:true, fields: abEnumerate(root)});\n"
            "})()")


# ── the Python-side ranker (pure, host-free; the single source of match truth) ───


@dataclass
class Match:
    """Outcome of resolving one ``field``. ``ok`` with a ``selector`` the CLI can act on,
    or not-``ok`` with a ready-to-return ``Error: …`` string."""

    ok: bool
    selector: str = ""
    label: str = ""
    kind: str = ""
    error: str = ""
    candidates: list = _dc_field(default_factory=list)


def _tier(candidate: str, query: str) -> int:
    """0 exact · 1 prefix · 2 substring · 9 no match (lower wins)."""
    if not candidate or not query:
        return 9
    if candidate == query:
        return 0
    if candidate.startswith(query):
        return 1
    if query in candidate:
        return 2
    return 9


def _field_labels(f: dict) -> list:
    labels = f.get("labels")
    if labels:
        return labels
    lab = f.get("label")
    return [lab] if lab else []


def _best_tier(f: dict, query: str) -> int:
    return min((_tier(normalize(lab), query) for lab in _field_labels(f)), default=9)


def closest_labels(fields: list, query: str, n: int = 5) -> list:
    """The ``n`` field labels most similar to ``query`` (difflib ratio) — what a zero-match
    error offers the model instead of a dead end."""
    qn = normalize(query)
    labs, seen = [], set()
    for f in fields:
        lab = (f.get("label") or "").strip()
        key = lab.lower()
        if lab and key not in seen:
            seen.add(key)
            labs.append(lab)
    labs.sort(key=lambda lab: difflib.SequenceMatcher(None, qn, normalize(lab)).ratio(), reverse=True)
    return labs[:n]


def _describe(f: dict) -> str:
    label = repr(f.get("label") or "(unlabelled)")
    tail = f.get("kind") or "?"
    if f.get("name"):
        tail += f", name={f['name']}"
    return f"{label} ({tail})"


def match_fields(field: str, fields: list) -> Match:
    """Resolve a LABEL ``field`` against enumerated ``fields`` (document order).

    Exact beats prefix beats substring. Exactly one field at the best tier resolves; more
    than one is AMBIGUOUS (an error listing them — never a guess); none is an error naming
    the closest labels.
    """
    query = normalize(field)
    scored = [(_best_tier(f, query), f) for f in fields]
    best = min((t for t, _ in scored), default=9)
    if best == 9:
        cands = closest_labels(fields, field)
        if cands:
            msg = (f"Error: no form field matches {field!r}. Closest labels: "
                   + ", ".join(repr(c) for c in cands)
                   + ". Call browser_form_read to list every field, or pass a CSS selector or @ref.")
        else:
            msg = (f"Error: no form field matches {field!r}, and the page exposes no labelled "
                   "fields. Call browser_form_read to inspect it, or pass a CSS selector or @ref.")
        return Match(ok=False, error=msg, candidates=cands)
    winners = [f for t, f in scored if t == best]
    if len(winners) > 1:
        listing = "; ".join(_describe(w) for w in winners)
        tier_name = {0: "exactly", 1: "as a prefix", 2: "as a substring"}[best]
        msg = (f"Error: {field!r} matches {len(winners)} fields {tier_name}: {listing}. "
               "Narrow it with a more specific label, a CSS selector or an @ref — "
               "browser_form_read lists every field.")
        return Match(ok=False, error=msg, candidates=[w.get("label", "") for w in winners])
    w = winners[0]
    return Match(ok=True, selector=w.get("selector", ""), label=w.get("label", ""), kind=w.get("kind", ""))


def parse_resolve(output: str) -> dict:
    """Parse the JSON ``resolve_js`` prints. Never raises — a garbled page returns an
    ``{"mode": "error"}`` marker ``resolve_target`` turns into a readable error."""
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        return {"mode": "error", "error": "unreadable resolver output"}
    if not isinstance(data, dict):
        return {"mode": "error", "error": "unexpected resolver output"}
    return data


def resolve_target(field: str, parsed: dict) -> Match:
    """Turn a parsed ``resolve_js`` result into a :class:`Match` the caller acts on."""
    mode = parsed.get("mode")
    if mode == "pass":
        return Match(ok=True, selector=parsed.get("target") or field.strip())
    if mode == "enumerate":
        return match_fields(field, parsed.get("fields") or [])
    return Match(ok=False, error="Error: could not resolve the field (the page returned unreadable data).")


# ── browser_form_read rendering (public field shape) ─────────────────────────────

# The columns browser_form_read reports, in order. `labels`/`idx`/`selector` are internal
# addressing bookkeeping and never surface.
_PUBLIC_KEYS = ("label", "kind", "name", "id", "required", "value")


def _public_field(f: dict) -> dict:
    out = {
        "label": f.get("label", ""),
        "kind": f.get("kind", "other"),
        "name": f.get("name", ""),
        "id": f.get("id", ""),
        "required": bool(f.get("required", False)),
        "value": f.get("value", ""),
    }
    if f.get("options") is not None:
        out["options"] = f["options"]
    return out


def render_form_read(output: str, scope: str = "") -> str:
    """Turn ``read_form_js``'s JSON into the model-facing result: a JSON array, one object
    per field in document order, or a readable ``Error: …`` / empty note."""
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        return "Error: could not read the form (the page returned unreadable data)."
    if isinstance(data, dict) and data.get("ok") is False:
        if data.get("error") == "scope-not-found":
            return (f"Error: no element matched the scope {scope!r}. Pass a CSS selector or a "
                    "visible container label, or leave scope blank to read the whole page.")
        return f"Error: {data.get('error') or 'could not read the form'}."
    fields = data.get("fields") if isinstance(data, dict) else data
    if not fields:
        where = f" within {scope!r}" if scope else ""
        return f"No form fields found{where}."
    return json.dumps([_public_field(f) for f in fields], ensure_ascii=False, indent=2)
