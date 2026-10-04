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
# command line.
_CSS_START = ("#", ".", "[")
_CSS_COMBINATORS = (">", "+", "~")

# The HTML tag names a CSS selector may lead with. A label that happens to be one of these
# words is still routed to the CLI as a selector ONLY when it is a bare lowercase tag (a
# capitalised "Address"/"Time"/"Select" stays a LABEL — tag names are matched case-sensitively
# against this lowercase set), which is why real field labels ("Address", "First name") keep
# resolving by label while `textarea`, `select#country`, `button[type=submit]` and `form input`
# route to CSS the way they did before this plugin learned to address by label (#4032 review).
_HTML_TAGS = frozenset({
    "a", "abbr", "address", "area", "article", "aside", "audio", "b", "base", "bdi", "bdo",
    "blockquote", "body", "br", "button", "canvas", "caption", "cite", "code", "col",
    "colgroup", "data", "datalist", "dd", "del", "details", "dfn", "dialog", "div", "dl",
    "dt", "em", "embed", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "head", "header", "hgroup", "hr", "html", "i", "iframe", "img",
    "input", "ins", "kbd", "label", "legend", "li", "link", "main", "map", "mark", "menu",
    "meta", "meter", "nav", "noscript", "object", "ol", "optgroup", "option", "output", "p",
    "param", "picture", "pre", "progress", "q", "rp", "rt", "ruby", "s", "samp", "script",
    "search", "section", "select", "slot", "small", "source", "span", "strong", "style",
    "sub", "summary", "sup", "table", "tbody", "td", "template", "textarea", "tfoot", "th",
    "thead", "time", "title", "tr", "track", "u", "ul", "var", "video", "wbr",
})

# One simple-selector qualifier: `#id`, `.class`, `[attr]`/`[attr=val]`, `:pseudo`/`::pseudo`.
_CSS_QUALIFIER = re.compile(r"\#[\w-]+|\.[\w-]+|\[[^\]]*\]|::?[\w-]+(?:\([^)]*\))?")


def _compound_ok(tok: str) -> bool:
    """True if ``tok`` is one CSS compound selector: an optional type selector (``*`` or a
    known HTML tag) followed by any number of ``#``/``.``/``[``/``:`` qualifiers — e.g.
    ``textarea``, ``select#country``, ``button[type=submit]``, ``input:checked``. A leading
    word that is NOT a known tag (``First``, ``Email``) disqualifies it, so a label is never
    mistaken for a bare type selector."""
    if not tok:
        return False
    m = re.match(r"\*|[A-Za-z][A-Za-z0-9]*", tok)
    i = 0
    has_type = False
    if m:
        lead = m.group(0)
        if lead != "*" and lead not in _HTML_TAGS:
            return False
        has_type = True
        i = m.end()
    has_qualifier = False
    while i < len(tok):
        qm = _CSS_QUALIFIER.match(tok, i)
        if not qm:
            return False
        i = qm.end()
        has_qualifier = True
    return has_type or has_qualifier


def _split_selector(s: str) -> list:
    """Split on descendant whitespace, but NOT whitespace inside ``[...]`` / ``(...)`` — so an
    attribute value with a space (``input[aria-label="First name"]``) stays one token."""
    tokens, depth, cur = [], 0, []
    for ch in s:
        if ch in "[(":
            depth += 1
            cur.append(ch)
        elif ch in "])":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch.isspace() and depth == 0:
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    return tokens


def _split_combinators(s: str):
    """Split ``s`` on the CSS combinators (``> + ~``) that sit OUTSIDE ``[...]`` / ``(...)``.

    Returns ``(parts, had_combinator)`` where ``parts`` are the combinator-separated pieces
    (each already whitespace-stripped, kept even when empty) and ``had_combinator`` says a
    combinator character was actually used as a separator. An empty piece means a combinator
    had no compound on one side (``C++``, a leading/trailing/doubled combinator) — the caller
    treats that as NOT a clean selector, so a label like ``"C++ experience"`` is never routed
    to CSS off a bare ``+`` substring (#4032 review)."""
    parts, depth, cur, had = [], 0, [], False
    for ch in s:
        if ch in "[(":
            depth += 1
            cur.append(ch)
        elif ch in "])":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif depth == 0 and ch in _CSS_COMBINATORS:
            had = True
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur).strip())
    return parts, had


def _is_tag_led_chain(s: str) -> bool:
    """True if ``s`` is a (possibly descendant) chain whose every bracket-aware token is a CSS
    compound led by a known HTML tag/``*`` or a ``#``/``.``/``[``/``:`` qualifier."""
    tokens = _split_selector(s)
    return bool(tokens) and all(_compound_ok(t) for t in tokens)


def _is_css_selector(s: str) -> bool:
    """Decide whether ``s`` is a CSS selector rather than a visible label.

    Unambiguous starts (``#``/``.``/``[``) route to CSS. A combinator (``> + ~``) routes to CSS
    only when it ANCHORS two real compound chains — ``div > input``, ``input+label``,
    ``li ~ a`` — never off a bare combinator character, so ``"C++ experience"`` stays a LABEL.
    Beyond that, a tag-led chain — one whose every (bracket-aware) token is a CSS compound whose
    type selector is a known HTML tag — is CSS too, so a descendant selector (``form input``)
    and a bare/qualified tag (``textarea``, ``select#country``,
    ``input[aria-label="First name"]``) reach the CLI instead of failing a label match.
    Descendant whitespace is a combinator ONLY when every token parses as a tag-led compound, so
    a multi-word label ("First name") stays a LABEL (``First`` is not a tag)."""
    if not s:
        return False
    if s[:1] in _CSS_START:
        return True
    parts, had_combinator = _split_combinators(s)
    if had_combinator:
        # a real combinator joins non-empty compound chains on BOTH sides (no empty piece,
        # which would mean "C++"/"~x"/"x>"), and each piece is itself a tag-led compound chain.
        return all(parts) and all(_is_tag_led_chain(p) for p in parts)
    return _is_tag_led_chain(s)


def classify(field: str) -> str:
    """``"ref"`` (``@e…``), ``"css"`` (selector) or ``"label"`` (visible text)."""
    s = (field or "").strip()
    if s.startswith("@e"):
        return "ref"
    if _is_css_selector(s):
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
  var out = [], idx = 0;
  var skip = {hidden:1, submit:1, button:1, reset:1, image:1};
  for(var i=0;i<nodes.length;i++){
    var el = nodes[i];
    if(abInCombo(el)) continue;               // the combobox root represents its inner input
    if(el.tagName.toLowerCase()==='input' && skip[(el.getAttribute('type')||'').toLowerCase()]) continue;
    var kind = abKindOf(el);
    var name = (el.getAttribute && el.getAttribute('name')) || '';
    var labels = abCandidateLabels(el);
    var comboInner = null;
    // EVERY radio is enumerated and tagged as its own addressable field — not collapsed to the
    // group's first member — so clicking a later option by its own text works (#4032 review).
    // The read tool folds the members back into one radio-group entry (render collapses by the
    // shared group key); the group legend is appended to each member's labels so addressing the
    // group by its legend hits every option (an AMBIGUOUS error that lists them) rather than
    // silently acting on the first.
    var radioLegend = '';
    if(kind==='radio-group'){
      var fs = el.closest && el.closest('fieldset');
      var leg = fs && fs.querySelector('legend');
      if(leg) radioLegend = abClean(abTextNoControls(leg));
      if(radioLegend && labels.indexOf(radioLegend) < 0) labels.push(radioLegend);
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
      // Per-OPTION descriptor: its own label addresses this one radio; the group metadata lets
      // the read renderer fold the members back into one radio-group entry (options + checked).
      var optionLabel = labels[0] || abClean(el.value) || '';
      desc.group = name || ('@@' + idx);     // no name → each radio is its own group
      desc.groupLabel = radioLegend;
      desc.optionLabel = optionLabel;
      desc.checked = !!el.checked;
      desc.value = optionLabel;
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


def _group_key(f: dict) -> str:
    """The key that folds a radio group's per-option descriptors into one read entry."""
    return f.get("group") or f.get("name") or f.get("selector") or f.get("label") or ""


def collapse_radio_groups(fields: list) -> list:
    """Fold the per-option radio descriptors ``abEnumerate`` emits back into one ``radio-group``
    object per group — ``options`` is the member labels in document order, ``value`` the checked
    one — at the position of the group's first member. Every other field passes through
    unchanged. (Enumeration is per-option so each radio is independently addressable; the read
    view is per-group.)"""
    out: list = []
    at: dict = {}
    for f in fields:
        if f.get("kind") != "radio-group":
            out.append(_public_field(f))
            continue
        key = _group_key(f)
        option = f.get("optionLabel") or f.get("label") or ""
        if key in at:
            g = out[at[key]]
            g["options"].append(option)
            if f.get("checked"):
                g["value"] = option
            if f.get("required"):
                g["required"] = True
            continue
        at[key] = len(out)
        out.append({
            "label": f.get("groupLabel") or f.get("name") or option,
            "kind": "radio-group",
            "name": f.get("name", ""),
            "id": "",
            "required": bool(f.get("required", False)),
            "value": option if f.get("checked") else "",
            "options": [option],
        })
    return out


def render_form_read(output: str, scope: str = "") -> str:
    """Turn ``read_form_js``'s JSON into the model-facing result: a JSON array, one object
    per field in document order (radio options folded into their group), or a readable
    ``Error: …`` / empty note."""
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
    return json.dumps(collapse_radio_groups(fields), ensure_ascii=False, indent=2)


# ── browser_select: set a choice field correctly, then PROVE it (#4032) ───────────
# The two failure modes on the Greenhouse form (#4032) are removed by construction:
#   * the react-select dropdown committed the HIGHLIGHTED option on type+Enter (visa became
#     "Yes, Ireland Highly Skilled Worker Visa") — so this NEVER presses Enter; it CLICKS the
#     matched `role=option` element;
#   * typed text was appended to stale input ("YeYess") — so it CLEARS the combobox input
#     before typing, never appends.
# The committed value is read back and compared to what was chosen; a mismatch is a hard
# error (never a silent wrong submission). The match itself is case-insensitive and
# whitespace-collapsed (``abNorm``), an exact match wins, and a unique prefix is the only
# non-exact that resolves — zero or several candidates is an error that lists the options.
#
# One async script, three in-page branches dispatched by ``abSelectKind``: a native
# ``<select>``, a react-select-style combobox (``role=combobox`` input in a ``select__`` /
# ``-container`` wrapper, or ``aria-autocomplete=list``), and an intl-tel-input (``.iti``)
# country picker. The combobox/iti branches poll (≤3s) for the menu to render — the CLI's
# ``eval`` awaits the returned Promise. Helpers are shared with ``_JS_LIB`` (``abClean`` /
# ``abNorm`` / ``abComboValue`` / ``abCandidateLabels``) so there is one tidy/label copy.
_SELECT_LIB = r"""
function abFieldLabel(el){
  if(!el) return '';
  var labs = abCandidateLabels(el);
  return labs.length ? labs[0] : '';
}
function abSetNativeValue(input, value){
  // A react-select search input is a CONTROLLED <input>; assigning `.value` directly does not
  // notify React. Call the native value setter off the prototype, then dispatch `input`, so the
  // clear (and the typed filter) actually take — and we never APPEND to stale text ("YeYess").
  try {
    var proto = Object.getPrototypeOf(input);
    var desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
    if(desc && desc.set){ desc.set.call(input, value); return; }
  } catch(e){}
  try { input.value = value; } catch(e){}
}
function abFire(el, type){ try { el.dispatchEvent(new Event(type, {bubbles:true})); } catch(e){} }
function abMouse(el, type){ try { el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true})); } catch(e){} }
function abClickOption(el){
  // A full pointer sequence, NEVER Enter: react-select commits an option on mousedown, an
  // intl-tel country on click — dispatch both so either reacts, and no keyboard commit can
  // land on the wrong highlighted row (the #4032 visa bug).
  abMouse(el, 'mousedown'); abMouse(el, 'mouseup');
  if(typeof el.click === 'function'){ try { el.click(); } catch(e){ abMouse(el, 'click'); } }
  else abMouse(el, 'click');
}
function abSleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
async function abWaitFor(fn, timeoutMs){
  var end = Date.now() + timeoutMs;
  for(;;){
    var v = fn();
    if(v && v.length) return v;
    if(Date.now() >= end) return v || [];
    await abSleep(80);
  }
}
function abMatchOption(options, want){
  // Case-insensitive, whitespace-collapsed. Exactly one EXACT match wins; else exactly one
  // PREFIX match wins; zero or several at the winning tier is an error (never a guess).
  var nw = abNorm(want), exact = [], prefix = [];
  for(var i=0;i<options.length;i++){
    var no = abNorm(options[i]);
    if(!no) continue;
    if(no === nw) exact.push(i);
    else if(no.indexOf(nw) === 0) prefix.push(i);
  }
  if(exact.length === 1) return {index: exact[0]};
  if(exact.length > 1) return {error: 'ambiguous'};
  if(prefix.length === 1) return {index: prefix[0]};
  return {error: prefix.length > 1 ? 'ambiguous' : 'no-option'};
}
function abSelectKind(el){
  var tag = el.tagName ? el.tagName.toLowerCase() : '';
  if(tag === 'select') return 'native';
  if(el.closest && el.closest('.iti, [class*="iti--"]')) return 'iti';
  if(el.matches && el.matches('.select__control, [class*="select__control"]')) return 'combobox';
  var combo = (el.matches && el.matches('[role="combobox"]')) ? el
            : (el.querySelector && el.querySelector('[role="combobox"]'));
  if(combo){
    // The react-select signature: a role=combobox input inside a `select__` / `-container`
    // wrapper, or one declaring aria-autocomplete=list. A bare ARIA combobox still routes
    // here — the two conditions are the canonical cases, not an exclusive gate.
    var container = (combo.closest && combo.closest('[class*="select__"], [class*="-container"]'))
                 || (el.closest && el.closest('[class*="select__"], [class*="-container"]'));
    var aa = combo.getAttribute && combo.getAttribute('aria-autocomplete');
    if(container || aa === 'list') return 'combobox';
    return 'combobox';
  }
  return 'other';
}
function abNative(el, want){
  var opts = Array.prototype.map.call(el.options, function(o){ return abClean(o.textContent); });
  var m = abMatchOption(opts, want);
  var label = abFieldLabel(el);
  if(m.error) return {ok:false, reason:m.error, kind:'native-select', label:label, options:opts.slice(0,10)};
  var chosen = opts[m.index];
  el.selectedIndex = m.index;
  try { el.value = el.options[m.index].value; } catch(e){}
  abFire(el, 'input'); abFire(el, 'change');            // bubbling, so React/jQuery listeners fire
  var so = el.options[el.selectedIndex];
  var actual = so ? abClean(so.textContent) : '';
  if(abNorm(actual) !== abNorm(chosen))
    return {ok:false, reason:'mismatch', kind:'native-select', label:label, wanted:chosen, actual:actual};
  return {ok:true, kind:'native-select', label:label, chosen:chosen, committed:actual};
}
function abComboMenus(input, control){
  // Find THIS control's dropdown — never the whole document, so a menu already open
  // elsewhere (a hidden listbox, a sibling widget's list) can't win the poll and get clicked
  // (#4032 review: an unscoped `[role="option"]` matched another control's "Afghanistan").
  // 1) aria wiring — the input names its listbox by id (react-select AND bare ARIA comboboxes
  //    both do), which locates the menu even when react-select PORTALS it out of the container.
  var menus = [];
  var ids = (input && input.getAttribute &&
    (input.getAttribute('aria-controls') || input.getAttribute('aria-owns'))) || '';
  String(ids).split(/\s+/).forEach(function(id){
    if(!id) return;
    var m = null; try { m = document.getElementById(id); } catch(e){ m = null; }
    if(m && menus.indexOf(m) < 0) menus.push(m);
  });
  if(menus.length) return menus;
  // 2) fallback — react-select renders the menu as a sibling of the control inside their
  //    shared wrapper; climb to the nearest ancestor that actually CONTAINS a react-select
  //    menu and scope to it (bounded, so the search never widens to the whole page).
  var node = control || input;
  for(var up = 0; node && up < 6; up++){
    var m2 = node.querySelector && node.querySelector(
      '.select__menu, [class*="select__menu"], [class*="-menu"]');
    if(m2) return [m2];
    if(node.tagName === 'FORM' || node === document.body) break;
    node = node.parentElement;
  }
  // 3) last resort — a bare ARIA combobox whose listbox lives inside its own container.
  var cont = (input && input.closest && input.closest('[class*="select__"], [class*="-container"]'))
          || control;
  var lb = cont && cont.querySelector && cont.querySelector('[role="listbox"]');
  if(lb) return [lb];
  return cont ? [cont] : [];
}
function abComboOptions(input, control){
  var menus = abComboMenus(input, control), out = [];
  for(var i=0;i<menus.length;i++){
    var found = (menus[i].querySelectorAll && menus[i].querySelectorAll('[role="option"]')) || [];
    for(var j=0;j<found.length;j++){ if(out.indexOf(found[j]) < 0) out.push(found[j]); }
  }
  return out;
}
async function abCombo(el, want){
  var input = (el.matches && el.matches('[role="combobox"]')) ? el
            : (el.querySelector && el.querySelector('[role="combobox"], input'));
  var control = (el.matches && el.matches('.select__control, [class*="select__control"]')) ? el
              : ((input && input.closest && input.closest('.select__control, [class*="select__control"]')) || el);
  var label = abFieldLabel(input || el);
  var typable = input && /^(input|textarea)$/i.test(input.tagName || '');
  if(input && input.focus){ try { input.focus(); } catch(e){} }
  if(typable){ abSetNativeValue(input, ''); abFire(input, 'input'); }   // CLEAR first — never append
  abMouse(control, 'mousedown');                                       // open the menu
  if(typable){ abSetNativeValue(input, want); abFire(input, 'input'); } // type a filter prefix
  // Scope the option scan to THIS control's menu (abComboMenus) — the poll returns on its
  // first non-empty read, so an unscoped scan would seize whatever `[role="option"]` is
  // already on the page before this menu renders.
  var opts = await abWaitFor(function(){ return abComboOptions(input, control); }, 3000);
  if(!opts.length) return {ok:false, reason:'no-option', kind:'combobox', label:label, options:[]};
  var texts = opts.map(function(o){ return abClean(o.textContent); });
  var m = abMatchOption(texts, want);
  if(m.error) return {ok:false, reason:m.error, kind:'combobox', label:label, options:texts.slice(0,10)};
  var chosen = texts[m.index];
  abClickOption(opts[m.index]);                                        // COMMIT by click, not Enter
  await abWaitFor(function(){
    var sv = control.querySelector && control.querySelector(
      '.select__single-value, [class*="singleValue"], [class*="single-value"]');
    var t = sv ? abClean(sv.textContent) : '';
    return (t && abNorm(t) === abNorm(chosen)) ? [t] : [];
  }, 2000);
  var actual = (control && control.querySelector) ? abComboValue(control) : '';
  if(abNorm(actual) !== abNorm(chosen))
    return {ok:false, reason:'mismatch', kind:'combobox', label:label, wanted:chosen, actual:actual};
  return {ok:true, kind:'combobox', label:label, chosen:chosen, committed:actual};
}
function abItiSelectedName(iti){
  var f = iti.querySelector('.iti__selected-flag, .iti__selected-country, [class*="selected-flag"], [class*="selected-country"]');
  if(!f) return '';
  var t = f.getAttribute('title') || f.getAttribute('aria-label') || abClean(f.textContent) || '';
  return abClean(String(t).split(':')[0]);               // "United States: +1 201" -> "United States"
}
async function abIti(el, want){
  var iti = (el.closest && el.closest('.iti, [class*="iti--"]')) || el;
  var label = abFieldLabel(el);
  var btn = iti.querySelector('.iti__selected-flag, .iti__selected-country, [class*="selected-flag"], [class*="selected-country"]');
  if(btn) abClickOption(btn);                             // open the country list
  var items = await abWaitFor(function(){
    return Array.prototype.slice.call(document.querySelectorAll('.iti__country, li[class*="iti__country"]'));
  }, 3000);
  if(!items.length) return {ok:false, reason:'no-option', kind:'iti', label:label, options:[]};
  var names = items.map(function(li){
    var nm = li.querySelector('.iti__country-name, [class*="country-name"]');
    return abClean(nm ? nm.textContent : li.textContent);
  });
  var m = abMatchOption(names, want);
  if(m.error) return {ok:false, reason:m.error, kind:'iti', label:label, options:names.slice(0,10)};
  var chosen = names[m.index];
  abClickOption(items[m.index]);
  await abWaitFor(function(){
    var got = abItiSelectedName(iti);
    return (got && abNorm(got) === abNorm(chosen)) ? [got] : [];
  }, 2000);
  var actual = abItiSelectedName(iti);                   // read back the selected flag's title
  if(abNorm(actual) !== abNorm(chosen))
    return {ok:false, reason:'mismatch', kind:'iti', label:label, wanted:chosen, actual:actual};
  return {ok:true, kind:'iti', label:label, chosen:chosen, committed:actual};
}
"""

_SELECT_DRIVER = r"""
(async function(){
  var el = null;
  try { el = document.querySelector(SEL); } catch(e){ el = null; }
  if(!el) return JSON.stringify({ok:false, reason:'not-found'});
  var kind = abSelectKind(el);
  try {
    if(kind === 'native') return JSON.stringify(abNative(el, WANT));
    if(kind === 'combobox') return JSON.stringify(await abCombo(el, WANT));
    if(kind === 'iti') return JSON.stringify(await abIti(el, WANT));
    return JSON.stringify({ok:false, reason:'unsupported', kind:kind, label:abFieldLabel(el)});
  } catch(e){
    return JSON.stringify({ok:false, reason:'exception', message:String(e && e.message || e)});
  }
})()
"""


def select_js(selector: str, option_text: str) -> str:
    """The script ``browser_select`` evals: detect the widget at ``selector`` and set it to
    ``option_text`` in the page, reading back the committed value. ``selector`` is the
    locator the bd-12mo.1 resolver already turned into something ``querySelector`` can act on
    (a ``[data-ab-field="N"]`` tag for a label, or a raw CSS selector). Rides ``eval --stdin``
    like every other form script (#3689)."""
    return (_JS_LIB + _SELECT_LIB + "\n(function(){\n"
            "var SEL = " + _js(selector) + ";\n"
            "var WANT = " + _js(option_text) + ";\n"
            "return " + _SELECT_DRIVER.strip() + ";\n})()")


def render_select(output: str, field: str, option_text: str) -> str:
    """Turn ``select_js``'s JSON into the model-facing result. Success is ``Selected "…" in
    <label>``; every failure is an ``Error: …`` — an ambiguous/absent option lists the choices,
    and a read-back that disagrees with what was chosen is a hard mismatch error (never a
    silent success). Never raises."""
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        return "Error: could not select — the page returned unreadable data."
    if not isinstance(data, dict):
        return "Error: could not select — unexpected data from the page."
    label = data.get("label") or field
    if data.get("ok"):
        chosen = data.get("chosen") or option_text
        return f'Selected "{chosen}" in {label}'
    reason = data.get("reason")
    if reason == "mismatch":
        wanted = data.get("wanted") or option_text
        return (f'Error: {label} reads "{data.get("actual", "")}" after selecting "{wanted}" — '
                "the committed value does not match what was chosen, so nothing was submitted.")
    if reason in ("no-option", "ambiguous"):
        opts = data.get("options") or []
        listing = ", ".join(repr(o) for o in opts[:10])
        head = (f"Error: {option_text!r} matches more than one option in {label}"
                if reason == "ambiguous"
                else f"Error: no option matching {option_text!r} in {label}")
        if listing:
            return f"{head}. Available options: {listing}."
        return (f"{head} — no options were found. Is the control open and populated? "
                "Call browser_form_read to inspect the field.")
    if reason == "not-found":
        return (f"Error: could not find a field matching {field!r} to select in. Call "
                "browser_form_read to list the fields, or pass a CSS selector. (browser_select "
                "addresses by LABEL or CSS, not a @ref — a ref can't be resolved in the page.)")
    if reason == "unsupported":
        return (f"Error: {field!r} is a {data.get('kind', 'plain')!r} field, not a choice "
                "field browser_select can set. Use browser_fill for text, or browser_click for "
                "a checkbox/radio.")
    if reason == "exception":
        return f"Error: selecting in {label} failed in the page: {str(data.get('message', ''))[:200]}"
    return f"Error: could not select {option_text!r} in {label}."
