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
# The attribute form is QUOTE-AWARE: a quoted value may hold brackets — Lever names its custom
# questions `cards[<uuid>][field0]`, so `input[name="cards[x][field0]"][value="Yes"]` is one
# compound, not a label (the old `\[[^\]]*\]` stopped at the first `]` inside the quotes).
_CSS_QUALIFIER = re.compile(
    r"""\#[\w-]+|\.[\w-]+|\[(?:"[^"]*"|'[^']*'|[^\]"'])*\]|::?[\w-]+(?:\([^)]*\))?""")


def _scan(s: str):
    """Yield ``(ch, depth)`` for each character of ``s``, where ``depth`` counts the ``[...]`` /
    ``(...)`` nesting OUTSIDE quoted strings — a bracket, space or combinator inside a quoted
    attribute value (``[name="a [b] > c"]``) never changes the depth. Quote characters are
    reported at depth >= 1 when they sit inside brackets (they always do in valid CSS)."""
    depth, quote = 0, ""
    for ch in s:
        if quote:
            if ch == quote:
                quote = ""
            yield ch, max(depth, 1)
            continue
        if ch in "\"'" and depth > 0:
            quote = ch
            yield ch, depth
            continue
        if ch in "[(":
            depth += 1
            yield ch, depth
        elif ch in "])":
            yield ch, depth
            depth = max(0, depth - 1)
        else:
            yield ch, depth


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
    """Split on descendant whitespace, but NOT whitespace inside ``[...]`` / ``(...)`` or a quoted
    value — so an attribute value with a space (``input[aria-label="First name"]``) stays one token."""
    tokens, cur = [], []
    for ch, depth in _scan(s):
        if ch.isspace() and depth == 0:
            if cur:
                tokens.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    return tokens


def _split_combinators(s: str):
    """Split ``s`` on the CSS combinators (``> + ~``) that sit OUTSIDE ``[...]`` / ``(...)`` and
    quotes.

    Returns ``(parts, had_combinator)`` where ``parts`` are the combinator-separated pieces
    (each already whitespace-stripped, kept even when empty) and ``had_combinator`` says a
    combinator character was actually used as a separator. An empty piece means a combinator
    had no compound on one side (``C++``, a leading/trailing/doubled combinator) — the caller
    treats that as NOT a clean selector, so a label like ``"C++ experience"`` is never routed
    to CSS off a bare ``+`` substring (#4032 review)."""
    parts, cur, had = [], [], False
    for ch, depth in _scan(s):
        if depth == 0 and ch in _CSS_COMBINATORS:
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


# ── `#id` selectors whose id holds CSS-special characters ────────────────────────
# Real forms mint ids like `input-candidate.name-3` or `a:b`. As CSS, `#input-candidate.name-3`
# means id `input-candidate` AND class `name-3`, so it matches nothing and the CLI says
# "Element not found" — the jobCoach failure, with the id taken straight from browser_form_read.
# But `#foo.bar` can ALSO be a real id+class compound, so the rewrite is decided IN THE PAGE:
# keep the selector when it matches as CSS, else use `[id="…"]` when an element with that exact
# id exists. Only a `#…` selector that isn't a plain `#[\w-]+` id pays for the probe.
_PLAIN_ID = re.compile(r"#[A-Za-z_][\w-]*")


def id_probe_target(selector: str) -> str:
    """The raw id to probe for when ``selector`` is a single ``#…`` token whose id part holds
    characters outside ``[\\w-]`` (``.`` ``:`` ``[`` ``/`` …) or starts with a digit; ``""``
    when it is a plain id or anything else (no probe needed)."""
    s = (selector or "").strip()
    if len(s) < 2 or not s.startswith("#") or _PLAIN_ID.fullmatch(s):
        return ""
    if any(ch.isspace() for ch in s) or any(c in s for c in ",>+~\"'"):
        return ""  # a list / combinator chain / quoted attribute — a real selector, leave it
    return s[1:]


def id_attr_selector(raw_id: str) -> str:
    """``[id="…"]`` for an exact id — valid CSS for ANY id string."""
    return '[id="' + raw_id.replace("\\", "\\\\").replace('"', '\\"') + '"]'


def id_probe_js(selector: str, raw_id: str) -> str:
    """In-page check for ``id_probe_target``: ``"css"`` if ``selector`` matches as CSS,
    ``"id"`` if it doesn't but an element with id ``raw_id`` exists, else ``"none"``."""
    return ("(function(){var ok=false;try{ok=!!document.querySelector(" + _js(selector) + ");}"
            "catch(e){ok=false;}if(ok)return 'css';"
            "return document.getElementById(" + _js(raw_id) + ")?'id':'none';})()")


def parse_id_probe(output: str) -> str:
    """``"css"`` / ``"id"`` / ``"none"`` from the probe's eval output (the CLI may print the
    returned string JSON-encoded); anything unreadable is ``"none"``."""
    v = (output or "").strip().strip('"').strip()
    return v if v in ("css", "id") else "none"


# Required markers: `*` plus the look-alikes ATS forms render (Lever's `✱` U+2731, `∗` `＊` `⁎` `✳`).
_REQ_MARKS = re.compile("[*\u2731\u2217\uff0a\u204e\u2733]+")


def normalize(s: str) -> str:
    """Collapse whitespace, strip required-marker asterisks, lower-case — the comparison
    key for label matching. Mirrors ``abNorm()`` in the in-page JS."""
    return re.sub(r"\s+", " ", _REQ_MARKS.sub(" ", s or "")).strip().lower()


def _js(value) -> str:
    """A safe JS literal for an embedded string (JSON is a JS-expression subset)."""
    return json.dumps(value)


def _loads(output: str):
    """``json.loads`` tolerant of the CLI's extra string layer.

    Every in-page script here ends ``return JSON.stringify({...})`` — so the value handed
    back to the CLI is a STRING. ``agent-browser eval`` (pinned 0.27.1) serialises the
    completion value for output: a returned *object* prints as JSON, but a returned *string*
    prints JSON-ENCODED — one layer deeper than the mocked unit tests feed (they pass the
    single-encoded JSON verbatim). Live, ``json.loads`` therefore yields the inner JSON text
    as a ``str`` rather than the dict the parsers expect, and every real form op failed as
    "unreadable data" (#4032 A5 — the drift the live fixtures exist to catch).

    Accept both shapes: parse once, and if that yields a lone JSON string, parse it again.
    Raises ``ValueError``/``TypeError`` on genuine garbage, exactly like ``json.loads`` — so
    every caller's existing ``except (ValueError, TypeError)`` still catches it."""
    data = json.loads(output)
    if isinstance(data, str):
        data = json.loads(data)
    return data


# ── the in-page JavaScript (ONE copy, shared by every form tool) ─────────────────
# Defines, in the page: clean/norm (text tidy), candidateLabels (the six label sources in
# precedence order), kindOf / requiredOf / comboValue (field shape), enumerateFields(root)
# (every field in document order, each tagged `data-ab-field="<i>"` so a resolved field has
# a selector the CLI can act on THIS call), and scopeRoot (restrict a read to a container).
_JS_LIB = r"""
// Required markers: `*` and the look-alikes ATS forms draw (Lever's `✱` U+2731, `∗` `＊` `⁎` `✳`).
// Mirrors forms._REQ_MARKS on the Python side.
var AB_REQ_MARKS = /[\*✱∗＊⁎✳]+/g;
function abClean(s){ return String(s==null?'':s).replace(AB_REQ_MARKS,' ').replace(/\s+/g,' ').trim(); }
function abNorm(s){ return abClean(s).toLowerCase(); }
// Never label text: form controls, and the non-text nodes whose fallback text leaks into
// textContent — Workable's inline <svg><title>SVGs not supported by this browser.</title>.
var AB_NONTEXT = 'input,select,textarea,button,svg,script,style,noscript,template';
function abTextNoControls(el){
  var c = el.cloneNode(true);
  var kids = c.querySelectorAll(AB_NONTEXT);
  for(var i=0;i<kids.length;i++){ kids[i].remove(); }
  return abClean(c.textContent);
}
// ── question text: hints, junk, and the question container (#4954 eval) ──────────────
// Hint / status text that sits INSIDE or BESIDE a label but is not the question: descriptions,
// helper lines, validation, live-region status ("Analyzing resume...", "Loading").
var AB_HINT_SEL = '[class*="descr"], [class*="hint"], [class*="help"], [class*="instruction"], ' +
  '[class*="sublabel"], [class*="error"], [class*="status"], [role="tooltip"], [role="alert"], ' +
  '[role="status"], [role="progressbar"], [aria-live]';
function abIsHint(n){ try { return !!(n && n.matches && n.matches(AB_HINT_SEL)); } catch(e){ return false; } }
function abHintFreeText(n){
  // `n`'s text without controls, svg fallbacks or hint/status descendants.
  if(!n) return '';
  if(n.nodeType === 3) return abClean(n.textContent);
  if(n.nodeType !== 1 || abIsHint(n)) return '';
  var c = n.cloneNode(true);
  var kids = c.querySelectorAll(AB_NONTEXT + ',' + AB_HINT_SEL);
  for(var i=0;i<kids.length;i++){ kids[i].remove(); }
  return abClean(c.textContent);
}
function abLabelText(lab){ return abHintFreeText(lab) || abTextNoControls(lab); }
function abLabelLead(lab, el){
  // A WRAPPING <label> often holds the field's whole widget — Lever's location label wraps its
  // "No location found…Loading" dropdown, its résumé label the "Analyzing resume…" status. The
  // question is the text BEFORE the branch that holds the control; a label whose text comes
  // only AFTER the control (`<label><input> English</label>`) keeps its whole text.
  var branch = el;
  while(branch && branch.parentElement && branch.parentElement !== lab) branch = branch.parentElement;
  var parts = [];
  if(branch && branch.parentElement === lab){
    for(var n = branch.previousSibling; n; n = n.previousSibling) parts.unshift(abHintFreeText(n));
  }
  return abClean(parts.join(' ')) || abLabelText(lab);
}
// Placeholder-style text that names no question ("Start typing...", "Type your response",
// "Select...", "Please select one"). Still addressable, but never the primary label while a real
// one exists. BARE placeholders only (any case, optional trailing punctuation): text that names
// a real object — "Please select your state", "Type your full legal name" — is a question and
// must survive. Mirrored by tests/test_agent_browser_form_labels.py (the regex is read from here).
var AB_JUNK = /^(svgs? not supported by this browser|start typing|type (your )?(response|answer)( here)?|type here|type to search|write (your (response|answer) )?here|(please )?(select|choose)( (an?|one))?( (option|value|one|item))?|search|loading|enter (your )?(answer|response|text)( here)?|your (answer|response)( here)?)[\s.…:!?*-]*$/i;
function abIsJunk(s){ return AB_JUNK.test(abClean(s)); }
var AB_FIELD_SEL = 'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]), ' +
  'select, textarea, [role="combobox"], .select__control, [class*="select__control"]';
function abHasForeignField(node, members){
  // Does `node` hold a form field that is NOT one of `members` (or part of one's widget)?
  if(!node || node.nodeType !== 1) return false;
  var fs = [];
  try { if(node.matches(AB_FIELD_SEL)) fs.push(node); } catch(e){}
  try { fs = fs.concat(Array.prototype.slice.call(node.querySelectorAll(AB_FIELD_SEL))); } catch(e){}
  for(var i=0;i<fs.length;i++){
    var f = fs[i], mine = false;
    for(var j=0;j<members.length && !mine;j++){
      var m = members[j];
      if(f === m || (m.contains && m.contains(f)) || (f.contains && f.contains(m))) mine = true;
    }
    if(!mine && !abIsComboShadowInput(f)) return true;
  }
  return false;
}
function abSkipLead(n){
  if(!n || n.nodeType !== 1) return false;
  // controls and page chrome (a tab bar / nav before the form is never the question)
  if(n.matches && n.matches('button, [role="button"], script, style, svg, template, noscript, ' +
                            'nav, a, [role="tablist"], [role="tab"], [role="navigation"], [role="link"], [role="menu"]')) return true;
  if(n.querySelector && n.querySelector('nav, [role="tablist"], [role="navigation"]')) return true;
  return !!(n.getClientRects && !n.getClientRects().length);    // not rendered
}
function abIsLabelish(n){
  if(!n || n.nodeType !== 1 || abIsHint(n)) return false;
  if(/^(LABEL|LEGEND|H[1-6])$/.test(n.tagName)) return true;
  return /label|question|title|heading|legend|prompt/i.test(String(n.className || ''));
}
function abMarked(n){
  // a required marker on a question label: an asterisk look-alike, or a `required` class.
  if(!n) return false;
  if(n.nodeType === 3) return /[\*✱∗＊⁎✳]/.test(n.textContent || '');
  if(/[\*✱∗＊⁎✳]/.test(n.textContent || '')) return true;
  if(/required/i.test(String(n.className || ''))) return true;
  return !!(n.querySelector && n.querySelector('[class*="required"]'));
}
function abWidgetTitle(el){
  // The first heading / label / …title… element inside the field's own widget (the widest
  // ancestor, ≤4 up, that holds no other field), never one that contains the input itself.
  var node = el.parentElement, best = '';
  for(var up = 0; node && up < 4; up++){
    if(node === document.body || node.tagName === 'FORM' || abHasForeignField(node, [el])) break;
    var hs = node.querySelectorAll('h1,h2,h3,h4,h5,h6,label,legend,[class*="title"],[class*="heading"]');
    for(var i = 0; i < hs.length; i++){
      if(hs[i].contains(el) || abIsHint(hs[i])) continue;
      var t = abHintFreeText(hs[i]);
      if(t){ best = t; break; }
    }
    if(best) return best;
    node = node.parentElement;
  }
  return '';
}
function abQuestionText(first, members){
  // The question a field (or a radio/checkbox GROUP) answers when it carries no label of its
  // own — Lever `.application-question`, Ashby's fieldset heading, a bare heading-then-input.
  // Climb from the field (a group's FIRST member); at each level take the siblings BEFORE the
  // branch, stopping at one that holds another field (a neighbour question). A label-like
  // sibling (label/legend/heading/…label… class) wins over hint text; else the joined text.
  members = members || [first];
  var cur = first;
  for(var up = 0; cur && up < 7; up++){
    var parent = cur.parentElement;
    if(!parent || parent === document.body || parent.tagName === 'FORM' || parent.tagName === 'HTML') break;
    var picked = [], blocked = false;
    for(var n = cur.previousSibling; n; n = n.previousSibling){
      if(n.nodeType === 1 && abHasForeignField(n, members)){ blocked = true; break; }
      if(n.nodeType === 1 && abSkipLead(n)) continue;
      if(n.nodeType === 1 || n.nodeType === 3) picked.unshift(n);
    }
    var lab = null;
    for(var i = 0; i < picked.length && !lab; i++){ if(abIsLabelish(picked[i]) && abHintFreeText(picked[i])) lab = picked[i]; }
    var txt = lab ? abHintFreeText(lab)
                  : abClean(picked.map(function(p){ return abHintFreeText(p); }).join(' '));
    if(txt){
      if(txt.length > 400) txt = abClean(txt.slice(0, 400));
      var req = lab ? abMarked(lab) : picked.some(function(p){ return abMarked(p); });
      return {text: txt, required: req};
    }
    if(blocked) break;
    cur = parent;
  }
  return {text: '', required: false};
}
function abFileExt(s){ return /\.\w{2,5}$/.test(String(s==null?'':s)); }
function abRemoveControl(el){
  // The authoritative "a file is actually attached" signal: the field shows a DETACH affordance —
  // a remove / delete button or an × glyph — beside the filename. Arbitrary page text that merely
  // ENDS in a dotted suffix ("jobs@acme.com", "greenhouse.io", "Accepted: .pdf") carries no such
  // control, and neither does a validation / alert message — so requiring it keeps the
  // browser_form_read fallback from reading incidental text as an attached filename (#4032 bug 3
  // review: on a single-upload form the climb never meets a second file input, so the extension
  // test ALONE reported a non-empty value for an unattached required résumé).
  var ctrls = (el && el.querySelectorAll && el.querySelectorAll('button, a, [role="button"]')) || [];
  for(var i=0;i<ctrls.length;i++){
    var b = ctrls[i];
    var t = abNorm(((b.getAttribute && b.getAttribute('aria-label')) || '') + ' ' +
                   ((b.getAttribute && b.getAttribute('title')) || '') + ' ' + (b.textContent || ''));
    if(/remove|delete|detach|discard|clear|cancel|reset|×|✕|✖|⨯/.test(t)) return true;
    if(/(^|\s)x(\s|$)/.test(t)) return true;
  }
  return false;
}
function abChipElement(root, input){
  // The filename CHIP element inside `root`: the one with the SHORTEST text that ENDS in a file
  // extension (the tight chip, not a wrapper that merely contains it). Skip `input` itself, any node
  // that holds a form control, and — crucially — any node that IS, SITS UNDER, or CONTAINS a
  // validation / alert element. closest() (self + ancestors) alone let a plain wrapper AROUND a
  // role=alert through: that wrapper has no alert class yet shares the alert's textContent, so a
  // rejection message that NAMES the file ("Could not upload resume.pdf") read back as a displayed
  // filename and a rejected upload reported as verified — so test descendants too (#4032 bug 3 review).
  var cands = (root && root.querySelectorAll && root.querySelectorAll('*')) || [];
  var sel = '[role="alert"], [aria-invalid="true"], [class*="error"], [class*="invalid"]';
  var best = null, bestLen = 1e9;
  for(var i=0;i<cands.length;i++){
    var c = cands[i];
    if(c === input) continue;
    if(c.querySelector && c.querySelector('input, select, textarea')) continue;
    if(c.closest && c.closest(sel)) continue;
    if(c.querySelector && c.querySelector(sel)) continue;
    var t = abClean(abTextNoControls(c));
    if(t && abFileExt(t) && t.length < bestLen){ best = c; bestLen = t.length; }
  }
  return best;
}
function abChipText(root, input){
  // The filename text shown in `root`. The upload verify also requires it to contain the basename it
  // just uploaded, so the alert exclusion above is a second guard against a rejection message there.
  var el = abChipElement(root, input);
  return el ? abClean(abTextNoControls(el)) : '';
}
function abFileChipName(input){
  // A file widget that RE-RENDERS its <input type=file> empty after an attach (Greenhouse) still
  // shows the attached filename as a chip in the field container. Recover it, but ONLY from THIS
  // field's OWN container (climb a bounded number of ancestors, STOP BEFORE the <form>/<body> or any
  // ancestor that also holds ANOTHER file input — those chips belong to sibling file fields, so an
  // empty input reads back '' rather than borrowing a neighbour's filename) AND ONLY when the chip
  // carries a DETACH control, the authoritative proof a file is attached. Without that gate an empty
  // input on a single-upload form (where the climb never meets a second file input to stop at) read
  // back ANY incidental text ending in a dotted suffix as a filename (#4032 bug 3 review).
  var node = input.parentElement;
  for(var up = 0; node && up < 6; up++){
    if(node.tagName === 'FORM' || node === document.body) break;
    var files = (node.querySelectorAll && node.querySelectorAll('input[type="file"]')) || [];
    var shared = false;
    for(var j=0;j<files.length;j++){ if(files[j] !== input){ shared = true; break; } }
    if(shared) break;
    var el = abChipElement(node, input);
    if(el && (abRemoveControl(el) || abRemoveControl(el.parentElement))) return abClean(abTextNoControls(el));
    node = node.parentElement;
  }
  return '';
}
function abLabelRequired(lab){
  // A required marker on the field's OWN <label>: a '*' in its text, or aria-required="true".
  // Used to carry an ORPHANED file field's required flag when the input (and its `required`
  // attribute) is gone from the DOM but the label survives (#4032 input-removed).
  if(!lab) return false;
  if(/\*/.test(lab.textContent || '')) return true;
  return !!(lab.getAttribute && lab.getAttribute('aria-required') === 'true');
}
function abStripLabelPrefix(chipText, labelText){
  // The displayed value is the BARE filename. When a widget renders the field LABEL into the chip
  // ("Resume/CV* resume.pdf"), drop a leading copy of the label so the value is just "resume.pdf"
  // (#4032 input-removed: the live chip reads "<label> <filename>").
  var c = abClean(chipText), l = abClean(labelText);
  if(l && c.toLowerCase().indexOf(l.toLowerCase()) === 0) c = abClean(c.slice(l.length));
  return c;
}
function abOrphanBox(label){
  // The field wrapper of an ORPHANED file field — a <label for=X> whose #X the widget REMOVED from
  // the DOM after the attach (Greenhouse leaves only a filename chip) — found by climbing from the
  // label until an ancestor shows an attached-file chip (a filename element with a detach control).
  // STOP before any ancestor that holds a live input[type=file]: that chip belongs to a SIBLING
  // file field (e.g. #cover_letter), never this removed one, so it is never borrowed (#4032).
  var node = label && label.parentElement;
  for(var up = 0; node && up < 6; up++){
    if(node.tagName === 'FORM' || node === document.body) break;
    var files = (node.querySelectorAll && node.querySelectorAll('input[type="file"]')) || [];
    if(files.length) break;
    var chip = abChipElement(node, null);
    if(chip && (abRemoveControl(chip) || abRemoveControl(chip.parentElement))) return node;
    node = node.parentElement;
  }
  return null;
}
function abByIds(ids){
  // aria-labelledby text. A referenced BUTTON ("Choose file" beside Workable's "Resume") is only
  // used when nothing else names the field — Greenhouse's résumé is named ONLY by its "Attach".
  var strong = [], weak = [];
  String(ids||'').split(/\s+/).forEach(function(id){
    var n = null; try { n = id && document.getElementById(id); } catch(e){ n = null; }
    if(!n) return;
    var t = abClean(abTextNoControls(n)) || abClean(n.textContent);
    if(!t) return;
    if(n.matches && n.matches('button, [role="button"]')) weak.push(t); else strong.push(t);
  });
  return (strong.length ? strong : weak).join(' ');
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
// ── accessible name of a label-less combobox / phone picker (#4032 bug 2b) ──────
// A phone-country react-select / intl-tel-input often carries NO `<label for>`: its only
// accessible name lives as aria-label / aria-labelledby on the select container or `.iti`
// wrapper, and as aria-label on the listbox it controls (e.g. the live GitLab picker is named
// ONLY by aria-label="Country" on its container). abCandidateLabels adds those as label
// candidates — but ONLY for such a PICKER that has no label of its own, so an explicit
// `<label for>` / wrapping `<label>` / own aria-label always beats an accessible-name-only match.
// `el` is the picker ONLY when it IS the combobox root / `.select__control` / an ARIA combobox,
// or the intl-tel-input COUNTRY BUTTON itself (`.iti__selected-flag` / `.iti__selected-country`).
// It is NOT every node under an `.iti` wrapper: the sibling `<input type=tel>` phone-number field
// shares that wrapper but is its OWN field — it must keep its OWN label and never borrow "Country"
// off the country-list (#4032 bug 2b review: the old `closest('.iti')` matched the tel input too).
function abComboOrIti(el){
  if(abComboRoot(el)) return true;
  if(el.matches && el.matches('.select__control, [class*="select__control"], [role="combobox"]')) return true;
  return !!(el.matches && el.matches(
    '.iti__selected-flag, .iti__selected-country, [class*="iti__selected-flag"], [class*="iti__selected-country"]'));
}
function abWrapperName(el){
  var out = [];
  if(!el || !el.matches) return out;
  // 1) the nearest react-select container / intl-tel-input wrapper, within 3 ancestors — a
  //    far-away container is not this field's name.
  var wrap = null, n = el;
  for(var up=0; n && up<=3; up++){
    if(n.matches && n.matches('.select__container, [class*="select__container"], .iti, [class*="iti--"]')){ wrap = n; break; }
    n = n.parentElement;
  }
  if(wrap){
    var wal = abClean(wrap.getAttribute && wrap.getAttribute('aria-label')); if(wal) out.push(wal);
    var wlb = wrap.getAttribute && wrap.getAttribute('aria-labelledby');
    if(wlb){ var wt = abClean(abByIds(wlb)); if(wt) out.push(wt); }
  }
  // 2) the listbox this combobox controls: its aria-controls / aria-owns target (react-select
  //    PORTALS it, so it exists only while OPEN) or an in-wrapper [role=listbox] /
  //    ul.iti__country-list — whichever is present carries the same aria-label name.
  var combo = (el.matches && el.matches('[role="combobox"]')) ? el
            : (el.querySelector && el.querySelector('[role="combobox"]'));
  var ids = (combo && combo.getAttribute &&
    (combo.getAttribute('aria-controls') || combo.getAttribute('aria-owns'))) || '';
  var box = null;
  String(ids).split(/\s+/).forEach(function(id){
    if(!id || box) return;
    var m = null; try { m = document.getElementById(id); } catch(e){ m = null; }
    if(m) box = m;
  });
  if(!box && wrap && wrap.querySelector){
    box = wrap.querySelector('[role="listbox"], ul.iti__country-list, [class*="iti__country-list"]');
  }
  if(box){ var ba = abClean(box.getAttribute && box.getAttribute('aria-label')); if(ba) out.push(ba); }
  return out;
}
function abCandidateLabels(el, noQuestion){
  var out = [];
  var hasOwnLabel = false;
  var id = el.getAttribute && el.getAttribute('id');
  var forLabels = [], weakFor = [];
  if(id){
    var sel = 'label[for="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]';
    try { forLabels = Array.prototype.slice.call(document.querySelectorAll(sel)); } catch(e){ forLabels = []; }
    // every <label for>: an svg-only one (Workable's dropzone icon) reads empty and is skipped,
    // and a role=button one ("Choose file") ranks after a real one.
    for(var fi = 0; fi < forLabels.length; fi++){
      var ft = abLabelText(forLabels[fi]);
      if(!ft) continue;
      if(forLabels[fi].matches && forLabels[fi].matches('[role="button"]')) weakFor.push(ft); else out.push(ft);
      hasOwnLabel = true;
    }
  }
  var wrap = el.closest && el.closest('label');
  if(wrap && forLabels.indexOf(wrap) < 0){
    var wt = abLabelLead(wrap, el);
    if(wt){ out.push(wt); hasOwnLabel = true; }
  }
  // The element's OWN aria-label / aria-labelledby is a label of its own — so it both ranks as a
  // candidate AND stops the widget from borrowing a container/listbox name below (#4032 bug 2b).
  var al = el.getAttribute && el.getAttribute('aria-label');
  if(al){ var alc = abClean(al); if(alc){ out.push(alc); hasOwnLabel = true; } }
  var lb = el.getAttribute && el.getAttribute('aria-labelledby');
  if(lb){ var lbt = abClean(abByIds(lb)); if(lbt){ out.push(lbt); hasOwnLabel = true; } }
  if(forLabels.length) out = out.concat(weakFor || []);   // a role=button <label for> ranks after aria names
  // A combobox / intl-tel-input PICKER with no label of its own (no `<label for>`, wrapping
  // `<label>`, aria-label or aria-labelledby) is named by the aria-label/aria-labelledby on its
  // container or the aria-label on the listbox it controls — a phone-country picker's only
  // accessible name (#4032 bug 2b). Any label of its own always wins, so these borrowed names are
  // taken ONLY when the picker carries none — and the phone-number `<input type=tel>` beside the
  // picker is NOT a picker (abComboOrIti), so it never borrows its country-list's name.
  if(!hasOwnLabel && abComboOrIti(el)){
    var acc = abWrapperName(el);
    for(var a=0;a<acc.length;a++){ if(acc[a]){ out.push(acc[a]); hasOwnLabel = true; } }
  }
  // No label of its own: the question container names it (Lever custom questions, which carry
  // only `cards[<uuid>][field0]` and a "Type your response" placeholder) — ranked above the
  // placeholder and the machine name.
  if(!hasOwnLabel && !noQuestion){
    // An unlabelled FILE drop zone names itself inside its own widget ("Autofill from resume",
    // after the input) — take that title before climbing out to the page around it.
    var own = ((el.getAttribute('type') || '').toLowerCase() === 'file') ? abWidgetTitle(el) : '';
    var q = own ? {text: own} : abQuestionText(el, [el]);
    if(q.text) out.push(q.text);
  }
  var ph = el.getAttribute && el.getAttribute('placeholder'); if(ph) out.push(abClean(ph));
  var nm = el.getAttribute && el.getAttribute('name'); if(nm) out.push(abClean(nm));
  var seen = {}, res = [], junk = [];
  for(var i=0;i<out.length;i++){
    var s=out[i]; var k=s.toLowerCase();
    if(s && !seen[k]){ seen[k]=1; (abIsJunk(s) ? junk : res).push(s); }
  }
  res = res.concat(junk);       // junk stays addressable, never the primary label
  res.own = hasOwnLabel;
  return res;
}
// ── radio / checkbox GROUPS (#4954 eval) ─────────────────────────────────────────────
// A radio group is its same-name radios; a checkbox group is its same-name checkboxes (Lever:
// 31 language boxes all named `cards[…][field0]`), else the checkboxes of one fieldset /
// role=group holding no other field (Ashby names each box after its option). The group label
// is the legend, else the group container's aria-labelledby / aria-label, else the question
// container's text (abQuestionText from the first member).
var AB_GROUP_CACHE = null;
function abGroupInfo(el){
  var t = ((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();
  if(t !== 'radio' && t !== 'checkbox') return null;
  var cache = AB_GROUP_CACHE || {byEl: [], groups: []};
  for(var ci = 0; ci < cache.byEl.length; ci++){ if(cache.byEl[ci][0] === el) return cache.byEl[ci][1]; }
  var scope = el.form || document;
  var name = (el.getAttribute && el.getAttribute('name')) || '';
  var members = null, key = '';
  if(name){
    var all = scope.querySelectorAll('input[type="' + t + '"]');
    members = Array.prototype.filter.call(all, function(x){ return x.getAttribute('name') === name; });
    key = t + ':n:' + name;
    if(t === 'checkbox' && members.length < 2) members = null;
  }
  if(!members && t === 'checkbox'){
    var box = el.closest && el.closest('fieldset, [role="group"]');
    if(box){
      var cbs = Array.prototype.slice.call(box.querySelectorAll('input[type="checkbox"]'));
      if(cbs.length >= 2 && !abHasForeignField(box, cbs)){
        members = cbs;
        var gid = box.getAttribute('data-ab-group');
        if(!gid){ gid = 'g' + (cache.groups.length + 1) + '-' + Date.now(); try { box.setAttribute('data-ab-group', gid); } catch(e){} }
        key = 'checkbox:g:' + gid;
      }
    }
  }
  if(!members){
    if(t === 'checkbox') return null;           // a lone checkbox stays kind 'checkbox'
    members = [el]; key = '';                   // a name-less radio is its own group
  }
  var label = '', req = false;
  var fs = el.closest && el.closest('fieldset');
  var leg = fs && fs.querySelector('legend');
  if(leg) label = abClean(abTextNoControls(leg));
  if(!label){
    var gc = el.closest && el.closest('[role="radiogroup"], [role="group"], fieldset');
    if(gc){
      label = abClean(abByIds(gc.getAttribute('aria-labelledby'))) || abClean(gc.getAttribute('aria-label') || '');
      if(gc.getAttribute('aria-required') === 'true') req = true;
    }
  }
  if(!label){ var q = abQuestionText(members[0], members); label = q.text; req = req || q.required; }
  for(var mi = 0; mi < members.length; mi++){ if(abRequired(members[mi])) req = true; }
  var info = {kind: t === 'radio' ? 'radio-group' : 'checkbox-group', key: key, members: members,
              label: label, required: req};
  for(var mj = 0; mj < members.length; mj++) cache.byEl.push([members[mj], info]);
  cache.groups.push(info);
  AB_GROUP_CACHE = cache;
  return info;
}
function abOptionLabel(el){
  var labs = abCandidateLabels(el, true);
  return (labs.own && labs[0]) || abClean(el.value) || labs[0] || '';
}
function abIsHiddenHelper(el){
  // A text input the widget keeps for itself: aria-hidden AND tabindex=-1, with no label of its own
  // (Workable's address picker fills hidden `city` / `postcode` / `country` inputs). Not a field.
  if(!el || el.tagName !== 'INPUT') return false;
  var t = (el.getAttribute('type') || 'text').toLowerCase();
  if(['text','search','email','tel','number','url',''].indexOf(t) < 0) return false;
  if(el.getAttribute('aria-hidden') !== 'true' || el.getAttribute('tabindex') !== '-1') return false;
  return !abCandidateLabels(el, true).own;
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
// ── committed-selection identity (phone-country pickers, #4032 bug 2a) ──────────
// A phone-country react-select/iti commits the COUNTRY but renders only a flag + the "+1"
// dial code as visible text, stashing the country NAME in a title/aria-label on a CHILD of the
// single-value / selected-flag. abComboValue (and the read-back in abCombo/abIti) must read that
// NAME, and NEVER treat a bare dial code as the committed value — "+1" is shared by the US and
// Canada, so a dial code alone can never confirm which country was committed.
function abIsDial(s){ var t = abClean(s); return t === '' || /^\+?\d[\d\s-]*$/.test(t); }
function abCountryPart(s){
  // Drop ONLY a trailing phone DIAL CODE — " +1", ": +1", "(+1)" — leaving everything else
  // intact. The literal "+" is REQUIRED, and nothing is split on a bare "(" or ":": otherwise a
  // real parenthetical or a trailing option number would be stripped and two DISTINCT options
  // that merely share a prefix would collapse to the same key — "Level 1"/"Level 2",
  // "Windows 10"/"Windows 11", "Spring 2025"/"Spring 2026", "Yes (full-time)"/"Yes (part-time)",
  // "Virgin Islands (British)"/"Virgin Islands (U.S.)" — which the read-back must still tell apart
  // (the old `split(/[:(]/)[0]` + optional-"+" strip conflated them — #4032 bug 2a review). So
  // "United States: +1" / "United States (+1)" / "United States +1" all -> "United States", while
  // "Virgin Islands (U.S.) +1340" -> "Virgin Islands (U.S.)" (only the "+1340" tail goes).
  var t = abClean(s);
  t = t.replace(/\s*\(\s*\+\d[\d\s-]*\)\s*$/, '');   // a trailing "(+1)" dial code in parens
  t = t.replace(/\s*:?\s*\+\d[\d\s-]*$/, '');        // a trailing " +1" / ": +1" dial code
  return abClean(t);
}
function abAttrTexts(node){
  // title + aria-label on `node` AND every descendant, in document order — a title on a child
  // flag span is what the live GitLab picker carries, which the element-only read missed.
  var out = [];
  if(!node) return out;
  var all = [node];
  if(node.querySelectorAll){ var k = node.querySelectorAll('*'); for(var i=0;i<k.length;i++) all.push(k[i]); }
  for(var j=0;j<all.length;j++){
    var n = all[j]; if(!n.getAttribute) continue;
    var ti = abClean(n.getAttribute('title')); if(ti) out.push(ti);
    var al = abClean(n.getAttribute('aria-label')); if(al) out.push(al);
  }
  return out;
}
function abBestName(cands){
  // the first candidate (or its country part) that is NOT a bare dial code / empty.
  for(var i=0;i<cands.length;i++){
    var cp = abCountryPart(cands[i]);
    if(cp && !abIsDial(cp)) return cp;
    if(cands[i] && !abIsDial(cands[i])) return abClean(cands[i]);
  }
  return '';
}
function abStripSelectedCountry(s){
  // The iti v23 a11y live-text reads e.g. "Selected country United States +1" / "Selected
  // country: United States". Drop that leading screen-reader prefix so only the country name
  // (and a trailing dial code, stripped downstream by abCountryPart) survives (#4032 v23 read-back).
  // The iti v25 selected-country BUTTON's aria-label reads "Change country, selected United States
  // (+1)" — the same idea with a different prefix; strip it too so it reduces to the NAME.
  var t = String(s==null?'':s);
  t = t.replace(/^\s*change\s+country\s*[,:]?\s*selected\s*:?\s*/i, '');
  t = t.replace(/^\s*selected\s+country\s*:?\s*/i, '');
  return abClean(t);
}
function abFlagCountryName(node){
  // The committed country NAME derived from an intl-tel-input flag class — `iti__flag iti__us` —
  // on `node` or any descendant. Greenhouse's 2026 phone-country react-select renders ONLY that
  // flag div + "+1" in its single-value (no title / aria-label), so the ISO-3166 alpha-2 code in
  // the flag class is the one thing that tells the US from Canada. Only a class that is EXACTLY
  // `iti__` + two letters counts (iti__flag / iti__globe / iti__arrow … are modifiers, not
  // countries); the code is mapped to an English name with Intl.DisplayNames, and nothing is
  // returned when that is unsupported or yields no real name.
  if(!node || !node.querySelectorAll) return '';
  // Prefer `node` itself, then the first flag NOT inside a country list / dropdown: a list rendered
  // before the selected flag would otherwise name the FIRST country in the list, not the commit.
  var listSel = '.iti__country-list, .iti__dropdown-content, [role="listbox"]';
  var all = [node].concat(Array.prototype.slice.call(node.querySelectorAll('[class*="iti__flag"]')));
  for(var i=0;i<all.length;i++){
    if(i > 0 && all[i].closest){
      var lst = all[i].closest(listSel);
      if(lst && lst !== node && node.contains(lst)) continue;
    }
    var cl = all[i].classList;
    if(!cl || !cl.contains('iti__flag')) continue;
    for(var j=0;j<cl.length;j++){
      var m = /^iti__([a-z]{2})$/i.exec(cl[j]);
      if(!m) continue;
      var cc = m[1].toUpperCase(), nm = '';
      try { nm = abClean(new Intl.DisplayNames(['en'], {type:'region'}).of(cc)); } catch(e){ nm = ''; }
      if(nm && nm.toUpperCase() !== cc && !/^unknown\b/i.test(nm)) return nm;
    }
  }
  return '';
}
function abSelectedIdentity(root){
  // Candidate texts of the COMMITTED selection, priority order: title/aria-label on the
  // single-value node or ANY descendant, and on the iti selected-flag/-country or ANY
  // descendant; then a hidden native input/select value inside the widget; then the visible text.
  if(!root) return [];
  var out = [];
  var sv = root.querySelector && root.querySelector('.select__single-value, [class*="singleValue"], [class*="single-value"]');
  // The intl-tel-input selected-country element: `root` ITSELF when the field resolved to the
  // button (form_read on the role=combobox button, or a CSS button locator), else a descendant.
  // Prefer the `.iti__selected-country` BUTTON explicitly — climb to it when a descendant such as
  // `.iti__selected-country-primary` matched the loose `[class*="selected-country"]` first — so the
  // committed NAME in its title/aria-label is read, not a dial-code-only child (#4032 v23 read-back).
  var flagSel = '.iti__selected-flag, .iti__selected-country, [class*="selected-flag"], [class*="selected-country"]';
  var flag = (root.matches && root.matches(flagSel)) ? root
           : (root.querySelector ? root.querySelector(flagSel) : null);
  if(flag && flag.closest){ var btn = flag.closest('.iti__selected-country'); if(btn) flag = btn; }
  out = out.concat(abAttrTexts(sv).map(abStripSelectedCountry))
           .concat(abAttrTexts(flag).map(abStripSelectedCountry));
  // The iti v23 a11y live-text ("Selected country United States +1") — a NAME source carried next
  // to a flag + dial code; strip the screen-reader prefix, leave the dial code to abCountryPart.
  var a11y = (flag && flag.querySelector && flag.querySelector('.iti__a11y-text, [class*="iti__a11y-text"]'))
          || (root.querySelector && root.querySelector('.iti__a11y-text, [class*="iti__a11y-text"]'));
  if(a11y){ var at = abStripSelectedCountry(a11y.textContent); if(at) out.push(at); }
  // A flag-class-only single-value / selected flag (`iti__flag iti__us`, no title) — the country
  // NAME derived from its ISO code; after every title/aria-label source, before any visible text.
  var fcn = abFlagCountryName(sv) || abFlagCountryName(flag); if(fcn) out.push(fcn);
  var hid = root.querySelector && root.querySelector('input[type="hidden"], select');
  if(hid){
    if((hid.tagName || '').toLowerCase() === 'select'){
      var so = hid.options && hid.options[hid.selectedIndex];
      var st = so ? abClean(so.textContent) : ''; if(st) out.push(st);
    } else { var hv = abClean(hid.value); if(hv) out.push(hv); }
  }
  if(sv){ var svt = abClean(sv.textContent); if(svt) out.push(svt); }
  if(flag){ var ft = abClean(flag.textContent); if(ft) out.push(ft); }
  var seen = {}, res = [];
  for(var i=0;i<out.length;i++){ var key = out[i].toLowerCase(); if(out[i] && !seen[key]){ seen[key]=1; res.push(out[i]); } }
  return res;
}
function abIdentityMatch(chosen, cands){
  // chosen matches the commit when abNorm(chosen) OR abNorm(countryPart(chosen)) equals abNorm
  // of ANY candidate or its country part. A candidate that is only a dial code / empty is
  // IGNORED — "+1" is shared by US and Canada, so it NEVER matches on its own.
  var targets = [];
  if(!abIsDial(chosen)) targets.push(abNorm(chosen));
  var ccp = abCountryPart(chosen); if(ccp && !abIsDial(ccp)) targets.push(abNorm(ccp));
  if(!targets.length) return false;
  for(var i=0;i<cands.length;i++){
    var c = cands[i], probes = [];
    if(!abIsDial(c)) probes.push(abNorm(c));
    var cp = abCountryPart(c); if(cp && !abIsDial(cp)) probes.push(abNorm(cp));
    for(var p=0;p<probes.length;p++){
      for(var t=0;t<targets.length;t++){ if(probes[p] && probes[p] === targets[t]) return true; }
    }
  }
  return false;
}
var AB_MULTI_SEL = '.select__multi-value, [class*="multiValue"], [class*="multi-value"]';
function abMultiValues(root){
  // The committed chips of a react-select MULTI-select, ONE entry per chip. The loose selector
  // also matches each chip's own children — `.select__multi-value__label` and
  // `.select__multi-value__remove` carry "multi-value" in their class too — so a naive read
  // counted every chip twice (container + label) plus an empty entry for the remove button
  // ("I don't wish to answer, I don't wish to answer, "). Keep only the OUTERMOST match within
  // `root`, read its __label when present, and drop empty texts.
  var out = [];
  if(!root || !root.querySelectorAll) return out;
  var mv = root.querySelectorAll(AB_MULTI_SEL);
  for(var i=0;i<mv.length;i++){
    var n = mv[i];
    var anc = n.parentElement && n.parentElement.closest ? n.parentElement.closest(AB_MULTI_SEL) : null;
    if(anc && anc !== root && root.contains(anc)) continue;      // a chip's label / remove child
    var lab = n.querySelector('.select__multi-value__label, [class*="multi-value__label"], [class*="multiValueLabel"], [class*="MultiValueLabel"]');
    var t = abClean(lab ? lab.textContent : abTextNoControls(n));
    if(t) out.push(t);
  }
  return out;
}
function abComboValue(root){
  var sv = root.querySelector('.select__single-value, [class*="singleValue"], [class*="single-value"]');
  if(sv){
    var vis = abClean(sv.textContent);
    // The visible single-value is only a flag / dial code (a phone-country picker) — recover the
    // committed country NAME from a title/aria-label in the single-value subtree (#4032 bug 2a).
    if(abIsDial(vis)){
      var name = abBestName(abAttrTexts(sv).map(abStripSelectedCountry)) || abFlagCountryName(sv);
      if(name) return name;
    }
    return vis;
  }
  var mv = abMultiValues(root);
  if(mv.length) return mv.join(', ');
  // intl-tel-input v23+: the enumerated field is the `.iti__selected-country` button (role=combobox)
  // whose visible text is ONLY the "+1" dial code. Recover the committed country NAME from its
  // title / aria-label / a11y text (abSelectedIdentity) rather than reporting the bare dial code —
  // which is shared by the US and Canada and must never stand in for the country (#4032 v23 read-back).
  var itiSel = '.iti__selected-flag, .iti__selected-country, [class*="selected-flag"], [class*="selected-country"]';
  if((root.matches && root.matches(itiSel)) || (root.querySelector && root.querySelector(itiSel))){
    var iname = abBestName(abSelectedIdentity(root));
    if(iname) return iname;
  }
  // A READ-ONLY select-like combobox input (Workable's "Select an option…") holds the committed
  // choice as its own value — it can't carry typed-but-uncommitted search text.
  if(root.tagName === 'INPUT' && root.readOnly) return abClean(root.value);
  // A bare ARIA combobox INPUT (no react-select container — Ashby's location box): its committed
  // value is the input's own value once its listbox is closed.
  if(root.tagName === 'INPUT' && root.getAttribute('aria-expanded') !== 'true' &&
     !(root.closest && root.closest('.select__control, [class*="select__control"]'))) return abClean(root.value);
  // last resort: the rendered text with the search input removed, so typed-but-uncommitted
  // text is never reported as the value — and with the PLACEHOLDER removed: an unanswered
  // react-select reads "", never "Select..." (live Greenhouse renders it as plain text).
  var c = root.cloneNode(true);
  var ph = c.querySelectorAll('[class*="placeholder"]');
  for(var pi = 0; pi < ph.length; pi++) ph[pi].remove();
  var rest = abTextNoControls(c);
  return abIsJunk(rest) ? '' : rest;
}
// ── react-select's hidden required "shadow" input (#4032 bug 4) ──────────────────
// When a react-select field is `required`, react-select renders a sibling
// `<input required tabindex="-1" aria-hidden="true">` (styled opacity:0 / 1px) NEXT TO
// `.select__control`, inside the select container — NOT inside the control — to carry the
// field's `required` constraint for native form validation. abInCombo only skips nodes inside
// the control / combobox root, so this sibling slipped through and was enumerated on its own as
// an unlabelled, id-less, required "text" row after EVERY combobox, polluting browser_form_read
// (a required field nobody can answer). abIsComboShadowInput identifies it so abEnumerate folds
// its `required` into the labelled combobox row and never emits it as a field of its own.
function abShadowHidden(el){
  // visually / semantically hidden: aria-hidden / tabindex=-1, OR opacity 0, OR a ≤2px sliver.
  if(el.getAttribute){
    if(el.getAttribute('aria-hidden') === 'true') return true;
    if(el.getAttribute('tabindex') === '-1') return true;
  }
  try {
    var cs = window.getComputedStyle && window.getComputedStyle(el);
    if(cs){
      if(cs.opacity !== '' && parseFloat(cs.opacity) === 0) return true;
      var w = parseFloat(cs.width); if(w >= 0 && w <= 2) return true;
      var h = parseFloat(cs.height); if(h >= 0 && h <= 2) return true;
    }
  } catch(e){}
  return false;
}
function abIsComboShadowInput(el){
  if(!el || !el.tagName || el.tagName.toLowerCase() !== 'input') return false;
  if(abComboRoot(el)) return false;                    // the combobox root itself is a real field
  var t = (el.getAttribute && (el.getAttribute('type') || '').toLowerCase()) || '';
  if(t === 'file') return false;                       // a file input is a real field (#resume)
  if(t === 'checkbox' || t === 'radio') return false;  // a custom-drawn box (Ashby yes/no) is a real field
  // It must have NO label candidates of its own — no `<label for>`, wrapping `<label>`,
  // aria-label / aria-labelledby or placeholder. A REAL unlabelled input keeps whatever it has
  // and stays a field; only a truly nameless input can be a shadow.
  var id = el.getAttribute && el.getAttribute('id');
  if(id){
    var sel = 'label[for="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]';
    var lf = null;
    try { lf = document.querySelector(sel); } catch(e){ lf = null; }
    if(lf) return false;
  }
  if(el.closest && el.closest('label')) return false;
  if(el.getAttribute &&
     (abClean(el.getAttribute('aria-label')) || abClean(abByIds(el.getAttribute('aria-labelledby'))))) return false;
  if(el.getAttribute && abClean(el.getAttribute('placeholder'))) return false;
  if(!abShadowHidden(el)) return false;                // a VISIBLE unlabelled input stays a field
  // and it must sit within the same nearest container (≤3 ancestors) as a `.select__control` /
  // combobox root — otherwise a stray hidden input elsewhere is left alone.
  var n = el;
  for(var up = 0; n && up <= 3; up++){
    if(n.querySelector && n.querySelector(
      '.select__control, [class*="select__control"], [role="combobox"]')) return true;
    n = n.parentElement;
  }
  return false;
}
function abComboShadowRequired(control){
  // react-select carries a required field's `required` on a hidden shadow <input> that is a
  // SIBLING of `.select__control`, inside the SAME select container. Scope the search to THIS
  // control's own container alone — NOT a broad ancestor walk: on a Greenhouse layout
  // (control → .select__container → div.field → form) a ≤3-ancestor walk reaches the whole
  // <form>, and a form-wide querySelectorAll('input') would find a required shadow input
  // belonging to a DIFFERENT (even optional) combobox and falsely mark this one required
  // (#4032 bug 4 review). The container is the control's nearest `.select__container`, falling
  // back to the control's immediate parent (the shadow is a sibling, so it shares that parent).
  var container = (control && control.closest &&
    control.closest('.select__container, [class*="select__container"]')) ||
    (control && control.parentElement);
  if(!container || !container.querySelectorAll) return false;
  var ins = container.querySelectorAll('input');
  for(var i = 0; i < ins.length; i++){
    var inp = ins[i];
    if(!abIsComboShadowInput(inp) || !abRequired(inp)) continue;
    // Fold only a shadow that belongs to THIS control — guard against a container that nests a
    // second react-select: the shadow's nearest select container must be the control's own.
    var owner = inp.closest && inp.closest('.select__container, [class*="select__container"]');
    if(owner && owner !== container) continue;
    return true;
  }
  return false;
}
function abEnumerate(root){
  root = root || document;
  AB_GROUP_CACHE = null;                      // group membership is recomputed every read
  var nodes = Array.prototype.slice.call(root.querySelectorAll(
    'input, textarea, select, [role="combobox"], .select__control, [class*="select__control"]'));
  var out = [], idx = 0;
  var skip = {hidden:1, submit:1, button:1, reset:1, image:1};
  for(var i=0;i<nodes.length;i++){
    var el = nodes[i];
    if(abInCombo(el)) continue;               // the combobox root represents its inner input
    if(el.tagName.toLowerCase()==='input' && skip[(el.getAttribute('type')||'').toLowerCase()]) continue;
    if(abIsComboShadowInput(el)) continue;    // react-select's hidden required sibling — folded below
    if(abIsHiddenHelper(el)) continue;        // a widget's own aria-hidden helper input
    var kind = abKindOf(el);
    var name = (el.getAttribute && el.getAttribute('name')) || '';
    var grp = (kind === 'radio-group' || kind === 'checkbox') ? abGroupInfo(el) : null;
    if(grp && grp.kind === 'checkbox-group') kind = 'checkbox-group';
    var labels = abCandidateLabels(el, kind === 'combobox' || !!grp);
    var comboInner = null;
    // EVERY radio is enumerated and tagged as its own addressable field — not collapsed to the
    // group's first member — so clicking a later option by its own text works (#4032 review).
    // The read tool folds the members back into one radio-group entry (render collapses by the
    // shared group key); the group legend is appended to each member's labels so addressing the
    // group by its legend hits every option (an AMBIGUOUS error that lists them) rather than
    // silently acting on the first.
    var radioLegend = '';
    if(grp){
      // A group member's own label is its OPTION; the group label (legend / aria / question
      // container) is appended so addressing the question hits every member of the group.
      radioLegend = grp.label || '';
      if(radioLegend && labels.indexOf(radioLegend) < 0) labels.push(radioLegend);
    }
    if(kind==='combobox'){
      // The react-select container has no label of its own; the labelled node is its inner
      // search input (its id drives `<label for>`, and it carries aria-label/labelledby).
      // Merge those so the ONE combobox entry is both labelled AND carries the value.
      var ownCombo = !!labels.own;
      comboInner = el.querySelector && el.querySelector('input, select, textarea, [role="combobox"]');
      if(comboInner && comboInner !== el){
        var il = abCandidateLabels(comboInner, true);
        ownCombo = ownCombo || !!il.own;
        var merged = [], jk = [];
        labels.concat(il).forEach(function(x){ if(merged.indexOf(x) < 0 && jk.indexOf(x) < 0) (abIsJunk(x) ? jk : merged).push(x); });
        labels = merged.concat(jk);
      }
      // still nameless (Ashby's "Start typing..." location box): the question container names it.
      if(!ownCombo){
        var cq = abQuestionText(el, comboInner && comboInner !== el ? [el, comboInner] : [el]);
        if(cq.text && labels.indexOf(cq.text) < 0) labels.unshift(cq.text);
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
    } else if(kind==='radio-group' || kind==='checkbox-group'){
      // Per-OPTION descriptor: its own label addresses this one option; the group metadata lets
      // the read renderer fold the members back into one group entry (options + checked).
      var optionLabel = abOptionLabel(el);
      desc.group = (grp && grp.key) || name || ('@@' + idx);     // no name → each radio is its own group
      desc.groupLabel = radioLegend;
      desc.optionLabel = optionLabel;
      desc.checked = !!el.checked;
      desc.value = optionLabel;
      if(grp && grp.required) desc.required = true;
    } else if(kind==='combobox'){
      desc.value = abComboValue(el);          // committed selection, NOT the typed search text
      if(comboInner && comboInner !== el){    // adopt the inner input's identity when the
        if(!desc.name){ desc.name = (comboInner.getAttribute && comboInner.getAttribute('name')) || ''; }
        if(!desc.id){ desc.id = (comboInner.getAttribute && comboInner.getAttribute('id')) || ''; }
        if(!desc.required){ desc.required = abRequired(comboInner); }   // container lacks them
      }
      // react-select stashes the field's `required` on a hidden sibling input of the control, not
      // on the control or its inner input — fold it in so the labelled combobox row carries the
      // question's required flag: required = combobox.required || shadow.required (#4032 bug 4).
      if(!desc.required) desc.required = abComboShadowRequired(el);
    } else if(kind==='checkbox'){
      desc.value = !!el.checked;
    } else if(kind==='file'){
      desc.value = el.files ? Array.prototype.map.call(el.files, function(f){ return f.name; }).join(', ') : '';
      if(!desc.value) desc.value = abFileChipName(el);   // widget re-rendered empty → read the chip
    } else {
      desc.value = (el.value!=null) ? String(el.value) : '';
    }
    out.push(desc);
    idx++;
  }
  // ── orphaned file fields (#4032 input-removed) ──────────────────────────────────
  // A widget that REMOVES its <input type=file> from the DOM after an attach (Greenhouse — it does
  // NOT re-render a fresh input) leaves only a filename chip, with its <label for=X> still pointing
  // at the now-missing #X. No live input carries the file, so the walk above missed the field
  // entirely. Recover it from the label + the displayed chip: for each <label for=X> whose #X no
  // longer exists, take its field wrapper (abOrphanBox — never a sibling file field's container)
  // and, when that wrapper shows an attached-file chip (filename + detach control), emit one
  // kind:'file' field. Skipped when a live #X still exists, so a re-rendering widget (#resume) is
  // NOT duplicated. The value is the BARE filename, with any label prefix stripped.
  var orphanLabels = (root.querySelectorAll && root.querySelectorAll('label[for]')) || [];
  for(var oi = 0; oi < orphanLabels.length; oi++){
    var olab = orphanLabels[oi];
    var forId = olab.getAttribute && olab.getAttribute('for');
    if(!forId) continue;
    var liveEl = null; try { liveEl = document.getElementById(forId); } catch(e){ liveEl = null; }
    if(liveEl) continue;                       // a live input with that id exists — no duplicate
    var obox = abOrphanBox(olab);
    if(!obox) continue;
    var ochip = abChipElement(obox, null);
    if(!ochip || !(abRemoveControl(ochip) || abRemoveControl(ochip.parentElement))) continue;
    var olabelText = abClean(abTextNoControls(olab));
    try { obox.setAttribute('data-ab-field', String(idx)); } catch(e){}
    out.push({
      idx: idx, label: olabelText, labels: olabelText ? [olabelText] : [],
      kind: 'file', name: forId, id: forId, required: abLabelRequired(olab),
      selector: '[data-ab-field="' + idx + '"]',
      value: abStripLabelPrefix(abClean(abTextNoControls(ochip)), olabelText)
    });
    idx++;
  }
  // ── orphaned file fields whose <label for> went WITH the input (live Greenhouse) ──────────
  // Greenhouse keeps `<label for=resume>Attach</label>` beside the input and swaps BOTH for the chip,
  // so the label[for] pass above finds nothing. browser_upload tagged the field's outer wrapper
  // `data-pa-file-for=<id>` before the attach; when that id no longer resolves to a live element and
  // the wrapper shows an attached-file chip, report it under the SAME id so a selector→value verifier
  // (#resume → resume.pdf) matches.
  var seenIds = {};
  for(var si = 0; si < out.length; si++){ if(out[si].id) seenIds[out[si].id] = true; }
  var fboxes = (root.querySelectorAll && root.querySelectorAll('[data-pa-file-for]')) || [];
  for(var bi = 0; bi < fboxes.length; bi++){
    var fbox = fboxes[bi], fid = fbox.getAttribute('data-pa-file-for') || '';
    if(!fid || fid.indexOf('@@') === 0 || seenIds[fid]) continue;   // '@@…': an id-less input
    var fliveEl = null; try { fliveEl = document.getElementById(fid); } catch(e){ fliveEl = null; }
    if(fliveEl && fliveEl.tagName === 'INPUT' && (fliveEl.getAttribute('type') || '').toLowerCase() === 'file') continue;   // live input — the main walk covers it
    if(fbox.querySelector && fbox.querySelector('input[type="file"]')) continue;
    var fchip = abChipElement(fbox, null);
    if(!fchip || !(abRemoveControl(fchip) || abRemoveControl(fchip.parentElement))) continue;
    var flabel = abClean(fbox.getAttribute('data-pa-file-label') || '');
    try { fbox.setAttribute('data-ab-field', String(idx)); } catch(e){}
    out.push({
      idx: idx, label: flabel, labels: flabel ? [flabel] : [],
      kind: 'file', name: fid, id: fid,
      required: fbox.getAttribute('data-pa-file-required') === 'true',
      selector: '[data-ab-field="' + idx + '"]',
      value: abStripLabelPrefix(abClean(abTextNoControls(fchip)), flabel)
    });
    seenIds[fid] = true;
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


# Combobox OPTIONS for the read (#4954 eval): a react-select (or a read-only select-like ARIA
# combobox — Workable's "Select an option…") renders its options only while OPEN. For each such
# field that is still UNANSWERED, open it (pointer sequence, then ArrowDown), read the option
# texts, and close it again with Escape + blur — never typing, never clicking an option, so
# nothing is committed. A type-to-search box (options depend on the query) is skipped, and the
# whole pass is bounded so a long form can't stall the read.
_READ_OPTIONS = r"""
async function abReadComboOptions(fields, budgetMs){
  var start = Date.now();
  for(var i=0;i<fields.length;i++){
    var f = fields[i];
    if(f.kind !== 'combobox' || f.value) continue;
    if(Date.now() - start > budgetMs) break;
    var el = null; try { el = document.querySelector(f.selector); } catch(e){ el = null; }
    if(!el) continue;
    var input = (el.matches && el.matches('[role="combobox"]')) ? el
              : (el.querySelector && el.querySelector('[role="combobox"], input'));
    var control = (el.matches && el.matches('.select__control, [class*="select__control"]')) ? el
                : ((input && input.closest && input.closest('.select__control, [class*="select__control"]')) || el);
    var selectLike = abComboContainer(control) || !!(input && input.readOnly);
    if(!selectLike || !input) continue;
    var wasOpen = abComboOpen(input, control);
    if(!wasOpen){
      if(document.activeElement === input && input.blur){ try { input.blur(); } catch(e){} }
      abTrustedishSeq(control);
      if(!(await abWaitOpen(input, control, 500))){
        if(input.focus){ try { input.focus(); } catch(e){} }
        abArrowDown(input);
        await abWaitOpen(input, control, 400);
      }
    }
    var texts = abComboOptions(input, control).map(function(o){ return abClean(o.textContent); })
                  .filter(function(t){ return t; });
    if(texts.length) f.options = texts;
    if(!wasOpen){
      try { input.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', code:'Escape', keyCode:27, bubbles:true, cancelable:true})); } catch(e){}
      if(input.blur){ try { input.blur(); } catch(e){} }
      await abSleep(60);
    }
  }
}
"""


def read_form_js(scope: str = "", option_budget_ms: int = 5000) -> str:
    """The script to eval for ``browser_form_read``: enumerate every field (optionally
    within ``scope``), read the options of unanswered select-like comboboxes (bounded by
    ``option_budget_ms``), and return ``{ok, fields}`` (or ``{ok:false, error}`` when the scope
    matches nothing)."""
    kind = classify(scope) if scope else "none"
    is_css = "true" if kind == "css" else "false"
    return (_JS_LIB + _SELECT_LIB + _READ_OPTIONS + "\n(async function(){\n"
            "var root = abScopeRoot(" + _js(scope or "") + ", " + is_css + ");\n"
            "if(root === null) return JSON.stringify({ok:false, error:'scope-not-found'});\n"
            "var fields = abEnumerate(root);\n"
            "try { await abReadComboOptions(fields, " + str(int(option_budget_ms)) + "); } catch(e){}\n"
            "return JSON.stringify({ok:true, fields: fields});\n"
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


_GROUP_KINDS = ("radio-group", "checkbox-group")


def _one_group(winners: list) -> bool:
    """True when every winner is an option of the SAME radio/checkbox group."""
    keys = {(w.get("kind"), w.get("group")) for w in winners}
    return len(keys) == 1 and winners[0].get("kind") in _GROUP_KINDS and bool(winners[0].get("group"))


def match_fields(field: str, fields: list, *, group_ok: bool = False) -> Match:
    """Resolve a LABEL ``field`` against enumerated ``fields`` (document order).

    Exact beats prefix beats substring. Exactly one field at the best tier resolves; more
    than one is AMBIGUOUS (an error listing them — never a guess); none is an error naming
    the closest labels. With ``group_ok`` (browser_select), several winners that are all
    options of ONE radio/checkbox group resolve to that group (its first member's selector).
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
    if len(winners) > 1 and _one_group(winners):
        w = winners[0]
        if group_ok:
            return Match(ok=True, selector=w.get("selector", ""), label=w.get("groupLabel") or w.get("label", ""),
                         kind=w.get("kind", ""))
        opts = ", ".join(repr(x.get("optionLabel") or x.get("label") or "") for x in winners[:12])
        what = "checkbox group" if w.get("kind") == "checkbox-group" else "radio group"
        q = w.get("groupLabel") or field
        msg = (f"Error: {field!r} is a {what} (options: {opts}). Pick one with "
               f"browser_select(field={q!r}, option_text=\"<option>\"), or address the option by its own label.")
        return Match(ok=False, error=msg, candidates=[x.get("label", "") for x in winners])
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
        data = _loads(output)
    except (ValueError, TypeError):
        return {"mode": "error", "error": "unreadable resolver output"}
    if not isinstance(data, dict):
        return {"mode": "error", "error": "unexpected resolver output"}
    return data


def resolve_target(field: str, parsed: dict, *, group_ok: bool = False) -> Match:
    """Turn a parsed ``resolve_js`` result into a :class:`Match` the caller acts on."""
    mode = parsed.get("mode")
    if mode == "pass":
        return Match(ok=True, selector=parsed.get("target") or field.strip())
    if mode == "enumerate":
        return match_fields(field, parsed.get("fields") or [], group_ok=group_ok)
    return Match(ok=False, error="Error: could not resolve the field (the page returned unreadable data).")


# ── browser_form_read rendering (public field shape) ─────────────────────────────

# The columns browser_form_read reports, in order. `labels`/`idx`/`selector` are internal
# addressing bookkeeping and never surface.
_PUBLIC_KEYS = ("label", "kind", "name", "id", "required", "value")


# A long option list (Palantir's university dropdown has 3,302) is cut to this many in the read
# view, with `options_more` counting the rest — browser_select still matches ANY option by text.
OPTIONS_SHOWN = 60


def _cap_options(out: dict) -> dict:
    opts = out.get("options")
    if isinstance(opts, list) and len(opts) > OPTIONS_SHOWN:
        out["options"] = opts[:OPTIONS_SHOWN]
        out["options_more"] = len(opts) - OPTIONS_SHOWN
    return out


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
    """Fold the per-option radio / checkbox descriptors ``abEnumerate`` emits back into one
    ``radio-group`` / ``checkbox-group`` object per group — ``options`` is the member labels in
    document order, ``value`` the checked one (a radio group) or the LIST of checked ones (a
    checkbox group) — at the position of the group's first member. Every other field passes
    through unchanged. (Enumeration is per-option so each option is independently addressable;
    the read view is per-group.)"""
    out: list = []
    at: dict = {}
    for f in fields:
        kind = f.get("kind")
        if kind not in _GROUP_KINDS:
            out.append(_cap_options(_public_field(f)))
            continue
        key = (kind, _group_key(f))
        option = f.get("optionLabel") or f.get("label") or ""
        multi = kind == "checkbox-group"
        if key in at:
            g = out[at[key]]
            g["options"].append(option)
            if f.get("checked"):
                if multi:
                    g["value"].append(option)
                else:
                    g["value"] = option
            if f.get("required"):
                g["required"] = True
            continue
        at[key] = len(out)
        checked = bool(f.get("checked"))
        out.append({
            "label": f.get("groupLabel") or f.get("name") or option,
            "kind": kind,
            "name": f.get("name", ""),
            "id": "",
            "required": bool(f.get("required", False)),
            "value": ([option] if checked else []) if multi else (option if checked else ""),
            "options": [option],
        })
    for i in at.values():
        _cap_options(out[i])
    return out


def read_fields(data, scope: str = "") -> tuple[list | None, str]:
    """``read_form_js``'s decoded result → ``(fields, "")``: the public field dicts (label, kind,
    name, id, required, value, and options / options_more where present), radio and checkbox
    options folded into their group — possibly empty. Or ``(None, "Error: …")`` when the scope
    matched nothing or the page answered something else. The ONE parse behind both
    ``browser_form_read`` (rendered for the model) and the plugin client's ``form_read`` (data)."""
    if isinstance(data, dict) and data.get("ok") is False:
        if data.get("error") == "scope-not-found":
            return None, (f"Error: no element matched the scope {scope!r}. Pass a CSS selector or "
                          "a visible container label, or leave scope blank to read the whole page.")
        return None, f"Error: {data.get('error') or 'could not read the form'}."
    fields = data.get("fields") if isinstance(data, dict) else data
    if fields is None:
        fields = []
    if not isinstance(fields, list) or not all(isinstance(f, dict) for f in fields):
        return None, "Error: could not read the form (the page returned unreadable data)."
    return collapse_radio_groups(fields), ""


def render_form_read(output: str, scope: str = "") -> str:
    """Turn ``read_form_js``'s JSON into the model-facing result: a JSON array, one object
    per field in document order (radio options folded into their group), or a readable
    ``Error: …`` / empty note."""
    try:
        data = _loads(output)
    except (ValueError, TypeError):
        return "Error: could not read the form (the page returned unreadable data)."
    fields, error = read_fields(data, scope)
    if error:
        return error
    if not fields:
        where = f" within {scope!r}" if scope else ""
        return f"No form fields found{where}."
    return json.dumps(fields, ensure_ascii=False, indent=2)


# ── browser_fill read-back (#4954 eval) ─────────────────────────────────────────────


def fill_matches(wanted: str, actual: str) -> bool:
    """Does the value read back after ``browser_fill`` agree with what was filled? Exact (after
    trimming / whitespace-collapse) agrees, and so does a widget that only REFORMATS: the same
    digits ("5550100199" → "(555) 010-0199", "150000" → "150,000") or the same text ignoring
    case and punctuation. A blank, truncated or different value is a mismatch."""
    w = re.sub(r"\s+", " ", wanted or "").strip()
    a = re.sub(r"\s+", " ", actual or "").strip()
    if w == a:
        return True
    if not a:
        return False
    wd, ad = re.sub(r"\D", "", w), re.sub(r"\D", "", a)
    if wd and wd == ad and not re.sub(r"[\d\W_]", "", w):
        return True                       # digits-only input, reformatted (phone / amount)
    loose = lambda x: re.sub(r"[\W_]+", "", x.lower())  # noqa: E731
    return bool(loose(w)) and loose(w) == loose(a)


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
#
# The combobox OPENS ITSELF, escalating (#4032 fix round): real react-select's control ignores a
# non-primary / bare untrusted ``mousedown``, opens via ``openAfterFocus`` on the focus a
# mousedown triggers, TOGGLES closed on a second mousedown while open, and opens on an ArrowDown
# keydown — so a single synthetic ``mousedown`` opened the menu only intermittently live (4/10),
# and never on the sponsorship widget. ``abCombo`` now escalates, re-checking THIS control after
# each strategy: (a) ``abOpenCombo`` — a full primary-button pointer sequence on the control and
# its dropdown indicator, then focus + ArrowDown; (b) if those fail and the input is typable, TYPE
# the filter — a TYPE-TO-SEARCH ARIA combobox (``abSelectKind`` routes every ``role=combobox``
# here) renders its listbox ONLY after input, so neither the pointer sequence nor ArrowDown ever
# opens it, and the pre-#4032 driver relied on this typed open; (c) if NOTHING opens it, return
# ``reason:'not-open'`` (with the control selector) so ``browser_select`` can escalate to a REAL,
# trusted CLI click before giving up with an explicit "could not open" error. An already-open menu
# is used as-is — never toggled closed — and the filter is typed once (an empty filtered scan
# clears and rescans unfiltered before declaring no-option).
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
function abWants(want){ return Array.isArray(want) ? want : [want]; }
function abMatchAny(options, wants){
  // ALTERNATIVES ("Decline to self-identify|Prefer not to say"): the FIRST alternative that
  // resolves to one option wins; none resolving is 'ambiguous' if any was, else 'no-option'.
  var amb = false;
  wants = abWants(wants);
  for(var i=0;i<wants.length;i++){
    var m = abMatchOption(options, wants[i]);
    if(!m.error){ m.want = wants[i]; return m; }
    if(m.error === 'ambiguous') amb = true;
  }
  return {error: amb ? 'ambiguous' : 'no-option'};
}
function abTextInput(el){
  // A plain typable text box — browser_select's AUTOCOMPLETE target (suggestions appear only
  // after typing; Lever's location, Google Places).
  if(!el || el.tagName !== 'INPUT') return false;
  var t = (el.getAttribute('type') || 'text').toLowerCase();
  return ['text', 'search', ''].indexOf(t) >= 0 && !el.readOnly && !el.disabled;
}
function abChoiceControl(box){
  // What a real click should land on for a custom-drawn radio/checkbox: its role=radio /
  // role=checkbox wrapper or its <label>, else the input itself.
  var w = box.closest && box.closest('[role="radio"], [role="checkbox"]');
  if(w) return w;
  var lab = box.closest && box.closest('label');
  if(!lab && box.id){
    try { lab = document.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(box.id) : box.id) + '"]'); } catch(e){ lab = null; }
  }
  return lab || box;
}
async function abChoice(g, wants){
  // Tick ONE option of a radio / checkbox GROUP by its visible label, then read `checked` back.
  // A checkbox option that is already ticked is left alone (never toggled off).
  var opts = g.members.map(abOptionLabel);
  var m = abMatchAny(opts, wants);
  if(m.error) return {ok:false, reason:m.error, kind:g.kind, label:g.label, options:opts.slice(0, 40)};
  var box = g.members[m.index], chosen = opts[m.index];
  if(box.checked) return {ok:true, kind:g.kind, label:g.label, chosen:chosen, committed:chosen, already:true};
  try { box.click(); } catch(e){}
  await abWaitFor(function(){ return box.checked ? [1] : []; }, 800);
  if(!box.checked){
    var ctl = abChoiceControl(box);
    if(ctl !== box){ abClickOption(ctl); await abWaitFor(function(){ return box.checked ? [1] : []; }, 800); }
  }
  if(!box.checked)
    return {ok:false, reason:'click-needed', kind:g.kind, label:g.label, wanted:chosen,
            control: abControlSelector(abChoiceControl(box))};
  return {ok:true, kind:g.kind, label:g.label, chosen:chosen, committed:chosen};
}
function abSelectKind(el){
  var tag = el.tagName ? el.tagName.toLowerCase() : '';
  if(tag === 'select') return 'native';
  if(tag === 'input' && /^(radio|checkbox)$/i.test(el.getAttribute('type') || '')) return 'group';
  if(el.matches && el.matches('fieldset, [role="radiogroup"], [role="group"]') &&
     el.querySelector('input[type="radio"], input[type="checkbox"]') &&
     !el.querySelector('select, textarea, [role="combobox"], .select__control')) return 'group';
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
  if(abTextInput(el)) return 'autocomplete';
  return 'other';
}
function abNative(el, want){
  var opts = Array.prototype.map.call(el.options, function(o){ return abClean(o.textContent); });
  var m = abMatchAny(opts, want);
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
function abComboOpen(input, control){
  // THIS control's menu is open when it has rendered options OR its input reports aria-expanded —
  // the two signals react-select exposes, either of which means "don't dispatch another open".
  var opts = abComboOptions(input, control);
  if(opts && opts.length) return true;
  var exp = (input && input.getAttribute) ? input.getAttribute('aria-expanded') : null;
  return exp === 'true';
}
async function abWaitOpen(input, control, timeoutMs){
  var end = Date.now() + timeoutMs;
  for(;;){
    if(abComboOpen(input, control)) return true;
    if(Date.now() >= end) return false;
    await abSleep(80);
  }
}
function abPointerEvt(el, type){
  // A PRIMARY-button pointer/mouse event ({button:0, buttons:1, view:window}) — the open signal
  // real react-select accepts (its onControlMouseDown ignores a non-zero button). PointerEvent for
  // pointer* when the engine has it, MouseEvent otherwise; it is untrusted, so a widget that gates
  // on isTrusted still won't open on it — those escalate to a real CLI click on the Python side.
  if(!el) return;
  var init = {bubbles:true, cancelable:true, button:0, buttons:1, view:window};
  var ev = null;
  try {
    ev = (/^pointer/.test(type) && typeof PointerEvent === 'function')
       ? new PointerEvent(type, init) : new MouseEvent(type, init);
  } catch(e){
    try { ev = new MouseEvent(type, {bubbles:true, cancelable:true, button:0, buttons:1}); }
    catch(e2){ ev = null; }
  }
  if(ev){ try { el.dispatchEvent(ev); } catch(e){} }
}
function abTrustedishSeq(el){
  // The full primary-button sequence a real pointer makes: pointerdown -> mousedown -> pointerup
  // -> mouseup -> click. NEVER a key commit (Enter lands on the highlighted row — the #4032 trap).
  abPointerEvt(el, 'pointerdown'); abPointerEvt(el, 'mousedown');
  abPointerEvt(el, 'pointerup');   abPointerEvt(el, 'mouseup');
  abPointerEvt(el, 'click');
}
function abArrowDown(input){
  // ArrowDown opens a react-select even when its control ignores a synthetic mousedown. This is an
  // OPEN signal, not a commit — the option is still COMMITTED BY CLICK (abClickOption), never Enter.
  if(!input) return;
  var ev = null;
  try {
    ev = new KeyboardEvent('keydown',
      {key:'ArrowDown', code:'ArrowDown', keyCode:40, which:40, bubbles:true, cancelable:true});
  } catch(e){ ev = null; }
  if(ev){ try { input.dispatchEvent(ev); } catch(e){} }
}
function abControlSelector(control){
  // A stable selector the CLI can do a REAL (trusted) click on when every in-page open strategy
  // fails — the field was already tagged data-ab-field by the resolver; otherwise tag it ourselves.
  if(!control || !control.getAttribute) return '';
  var f = control.getAttribute('data-ab-field');
  if(f !== null && f !== '') return '[data-ab-field="' + f + '"]';
  var v = control.getAttribute('data-ab-open');
  if(!v){ v = 'c' + Date.now(); try { control.setAttribute('data-ab-open', v); } catch(e){} }
  return '[data-ab-open="' + v + '"]';
}
async function abOpenCombo(input, control){
  // Escalate open strategies, re-checking THIS control after each — stop at the first that opens,
  // and NEVER dispatch on an already-open menu (a second mousedown toggles react-select closed).
  if(abComboOpen(input, control)) return true;
  // (a) a primary-button pointer sequence on the control, then on its dropdown indicator. Blur the
  //     input first so react-select's openAfterFocus path fires on the focus the mousedown triggers
  //     (a mousedown while already focused TOGGLES — it would close a menu we then race to reopen).
  if(document.activeElement === input && input && input.blur){ try { input.blur(); } catch(e){} }
  abTrustedishSeq(control);
  if(await abWaitOpen(input, control, 800)) return true;
  var indicator = control && control.querySelector && control.querySelector(
    '[class*="indicatorContainer"], [class*="dropdown-indicator"]');
  if(indicator){
    if(document.activeElement === input && input && input.blur){ try { input.blur(); } catch(e){} }
    abTrustedishSeq(indicator);
    if(await abWaitOpen(input, control, 500)) return true;
  }
  // (b) focus the input and press ArrowDown.
  if(input && input.focus){ try { input.focus(); } catch(e){} }
  abArrowDown(input);
  if(await abWaitOpen(input, control, 800)) return true;
  return false;
}
function abTypeFilter(input, want){
  // CLEAR stale text first (never append "YeYess") then type `want`, via the native value setter
  // so a CONTROLLED react-select input actually re-filters. This is ALSO the open signal a
  // TYPE-TO-SEARCH combobox needs — its listbox renders ONLY after input, so neither a pointer
  // sequence nor ArrowDown opens it; the caller polls abComboOptions / abComboOpen afterwards.
  abSetNativeValue(input, ''); abFire(input, 'input');
  abSetNativeValue(input, want); abFire(input, 'input');
}
async function abCombo(el, wantArg){
  var wants = abWants(wantArg);
  if(wants.length > 1) return abComboMulti(el, wants);
  var want = wants[0];
  var input = (el.matches && el.matches('[role="combobox"]')) ? el
            : (el.querySelector && el.querySelector('[role="combobox"], input'));
  var control = (el.matches && el.matches('.select__control, [class*="select__control"]')) ? el
              : ((input && input.closest && input.closest('.select__control, [class*="select__control"]')) || el);
  var label = abFieldLabel(input || el);
  var typable = input && /^(input|textarea)$/i.test(input.tagName || '');
  var filtered = false;                 // have we already typed `want` into the input?
  // MULTI-select: the chips committed BEFORE we touch anything. Clicking an option that is already
  // a chip TOGGLES it off on a react-select that keeps selected options in its menu
  // (hideSelectedOptions=false) — and if the widget commits in a later macrotask, a read-back that
  // still sees the old chip would report a false success after the removal. So an option that is
  // already a chip is NEVER clicked: it is reported as already selected, and a fresh pick only
  // verifies when its chip was absent before the click and present after.
  var preChips = (control && control.querySelector) ? abMultiValues(control) : [];
  function abPreHad(text){
    for(var pi=0; pi<preChips.length; pi++){ if(abNorm(preChips[pi]) === abNorm(text)) return true; }
    return false;
  }
  if(abPreHad(want))
    return {ok:true, kind:'combobox', label:label, chosen:abClean(want), committed:abClean(want), already:true};
  // Open the menu OURSELVES — but if it is ALREADY open on entry, skip straight to the scan so we
  // never toggle it closed (r2). Escalate: abOpenCombo (pointer sequence / ArrowDown), then — for a
  // TYPE-TO-SEARCH combobox whose listbox renders ONLY after input — type the filter to make it
  // render (the pre-#4032 open path, restored). If NOTHING opens it, return reason:'not-open' so
  // browser_select can escalate to a REAL, trusted CLI click before giving up.
  if(!abComboOpen(input, control)){
    var opened = await abOpenCombo(input, control);
    if(!opened && typable){
      if(input.focus){ try { input.focus(); } catch(e){} }
      abTypeFilter(input, want); filtered = true;
      opened = await abWaitOpen(input, control, 3000);
    }
    if(!opened){
      var exp = (input && input.getAttribute) ? (input.getAttribute('aria-expanded') || '') : '';
      return {ok:false, reason:'not-open', kind:'combobox', label:label,
              expanded:exp, control:abControlSelector(control)};
    }
  }
  // Menu is open — type the filter to narrow the options (once; the type-to-search open above may
  // already have), then scan THIS control's menu (abComboMenus scopes it). If the filter hid the
  // wanted option (none left, or none matched), clear it and rescan the UNFILTERED list before
  // declaring no-option — react-select can filter on a value that differs from the visible text.
  if(typable && !filtered){ abTypeFilter(input, want); filtered = true; }
  var opts = await abWaitFor(function(){ return abComboOptions(input, control); }, 3000);
  var texts = (opts || []).map(function(o){ return abClean(o.textContent); });
  var m = texts.length ? abMatchOption(texts, want) : {error:'no-option'};
  if(m.error === 'no-option' && typable){
    abSetNativeValue(input, ''); abFire(input, 'input');               // drop the filter …
    opts = await abWaitFor(function(){ return abComboOptions(input, control); }, 2000);
    texts = (opts || []).map(function(o){ return abClean(o.textContent); });
    m = texts.length ? abMatchOption(texts, want) : {error:'no-option'};   // … and rescan unfiltered
  }
  // No options at all, even after typing: an ASYNC type-to-search box whose results need REAL
  // key events — hand browser_select the input so it can retry as an autocomplete.
  if(!texts.length) return {ok:false, reason:'no-option', kind:'combobox', label:label, options:[],
                            input: typable ? abControlSelector(input) : ''};
  if(m.error) return {ok:false, reason:m.error, kind:'combobox', label:label, options:texts.slice(0,10)};
  return abComboCommit(input, control, label, opts, texts, m, abPreHad, typable);
}
async function abComboMulti(el, wants){
  // ALTERNATIVES on a combobox: scan the UNFILTERED menu for the first alternative that matches;
  // only if none does (or the menu needs typing to render) type each alternative in turn.
  var input = (el.matches && el.matches('[role="combobox"]')) ? el
            : (el.querySelector && el.querySelector('[role="combobox"], input'));
  var control = (el.matches && el.matches('.select__control, [class*="select__control"]')) ? el
              : ((input && input.closest && input.closest('.select__control, [class*="select__control"]')) || el);
  var label = abFieldLabel(input || el);
  var typable = input && /^(input|textarea)$/i.test(input.tagName || '');
  var preChips = (control && control.querySelector) ? abMultiValues(control) : [];
  function abPreHad(text){
    for(var pi=0; pi<preChips.length; pi++){ if(abNorm(preChips[pi]) === abNorm(text)) return true; }
    return false;
  }
  for(var w=0; w<wants.length; w++){
    if(abPreHad(wants[w]))
      return {ok:true, kind:'combobox', label:label, chosen:abClean(wants[w]), committed:abClean(wants[w]), already:true};
  }
  var opened = abComboOpen(input, control) || await abOpenCombo(input, control);
  var opts = [], texts = [], m = {error:'no-option'};
  if(opened){
    if(typable){ abSetNativeValue(input, ''); abFire(input, 'input'); }
    opts = await abWaitFor(function(){ return abComboOptions(input, control); }, 1500);
    texts = (opts || []).map(function(o){ return abClean(o.textContent); });
    if(texts.length) m = abMatchAny(texts, wants);
  }
  if(m.error && typable){
    var amb = m.error === 'ambiguous', seen = texts.slice();
    for(var i=0; i<wants.length; i++){
      if(input.focus){ try { input.focus(); } catch(e){} }
      abTypeFilter(input, wants[i]);
      opts = await abWaitFor(function(){ return abComboOptions(input, control); }, 2000);
      texts = (opts || []).map(function(o){ return abClean(o.textContent); });
      texts.forEach(function(t){ if(seen.indexOf(t) < 0) seen.push(t); });
      var mm = texts.length ? abMatchOption(texts, wants[i]) : {error:'no-option'};
      if(!mm.error){ m = mm; break; }
      if(mm.error === 'ambiguous') amb = true;
    }
    if(m.error){
      abSetNativeValue(input, ''); abFire(input, 'input');
      if(!seen.length && !opened) {
        var exp = (input && input.getAttribute) ? (input.getAttribute('aria-expanded') || '') : '';
        return {ok:false, reason:'not-open', kind:'combobox', label:label, expanded:exp, control:abControlSelector(control)};
      }
      return {ok:false, reason: amb ? 'ambiguous' : 'no-option', kind:'combobox', label:label,
              options: seen.slice(0,10), input: (!seen.length && typable) ? abControlSelector(input) : ''};
    }
  }
  if(m.error){
    if(!opened){
      var ex2 = (input && input.getAttribute) ? (input.getAttribute('aria-expanded') || '') : '';
      return {ok:false, reason:'not-open', kind:'combobox', label:label, expanded:ex2, control:abControlSelector(control)};
    }
    return {ok:false, reason:m.error, kind:'combobox', label:label, options:texts.slice(0,10)};
  }
  return abComboCommit(input, control, label, opts, texts, m, abPreHad, typable);
}
async function abComboCommit(input, control, label, opts, texts, m, abPreHad, typable){
  var chosen = texts[m.index];
  if(abPreHad(chosen)){
    // already a chip — leave it selected: drop our filter text and close the menu we opened.
    if(typable){ abSetNativeValue(input, ''); abFire(input, 'input'); }
    try { input.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', code:'Escape', keyCode:27, bubbles:true, cancelable:true})); } catch(e){}
    return {ok:true, kind:'combobox', label:label, chosen:chosen, committed:chosen, already:true};
  }
  abClickOption(opts[m.index]);                                        // COMMIT by click, not Enter
  // Read-back: a phone-country picker renders only the "+1" dial code, stashing the NAME in a
  // title on a child — so match the chosen option (or its country part) against the committed
  // selection's IDENTITY candidates (#4032 bug 2a). The plain-combobox path is preserved as a
  // fallback: abComboValue now returns the NAME for a dial-code-only value, so comparing against
  // it still works for ordinary (even numeric) comboboxes and NEVER re-matches a bare dial code.
  function abComboOk(){
    var cs = (control && control.querySelector) ? abSelectedIdentity(control) : [];
    var cv = (control && control.querySelector) ? abComboValue(control) : '';
    // A MULTI-select's read-back is a LIST of chips: the chosen option committed when it is one
    // of them (exact, normalized) — never a substring of the joined "A, B" string.
    var mvs = (control && control.querySelector) ? abMultiValues(control) : [];
    if(mvs.length){
      // chips present: the chip list is the whole answer — the joined string / identity fallbacks
      // below must never stand in for it.
      if(abPreHad(chosen)) return false;
      for(var mi=0; mi<mvs.length; mi++){ if(abNorm(mvs[mi]) === abNorm(chosen)) return true; }
      return false;
    }
    return abIdentityMatch(chosen, cs) || (!!cv && abNorm(chosen) === abNorm(cv));
  }
  await abWaitFor(function(){ return abComboOk() ? [1] : []; }, 2000);
  var cands = (control && control.querySelector) ? abSelectedIdentity(control) : [];
  var cval = (control && control.querySelector) ? abComboValue(control) : '';
  if(!abComboOk())
    return {ok:false, reason:'mismatch', kind:'combobox', label:label, wanted:chosen,
            actual: abBestName(cands) || cval};
  return {ok:true, kind:'combobox', label:label, chosen:chosen, committed: abBestName(cands) || cval || chosen};
}
function abItiSelectedName(iti){
  // The committed country NAME — a title/aria-label on the selected-flag OR ANY descendant
  // (e.g. a child flag span), never a bare dial code. (Was: the flag element's own attrs only,
  // so a title on a child was missed and the "+1" dial-code text won — #4032 bug 2a.)
  var cands = abSelectedIdentity(iti);
  return abBestName(cands) || (cands.length ? cands[0] : '');
}
function abShown(node){
  // Is `node` actually RENDERED (not display:none, attached to the layout)? getClientRects() is
  // empty for a display:none / detached element and non-empty otherwise — robust even when an
  // ancestor is position:fixed, where offsetParent would wrongly read null (offsetParent is the
  // ancient fallback). "Rendered" is deliberately NOT "in the viewport": an open list scrolled
  // off-screen still has layout, so it still counts as open.
  if(!node) return false;
  if(node.getClientRects){ var r = node.getClientRects(); return !!(r && r.length); }
  return node.offsetParent !== null;
}
function abItiItems(){
  // The country options of the OPEN list. intl-tel-input v23+ PORTALS the list to a body-level
  // `.iti--container`, so this MUST be a document-wide scan — but it MUST also stay visibility-
  // scoped: the older `.iti__selected-flag` markup (and a v23 widget without a dropdownContainer)
  // keeps its country rows PARKED in the page, hidden by `iti__hide`, while CLOSED, and a page can
  // hold a SECOND phone picker whose own rows sit in the DOM too. An unfiltered scan would (a) read
  // those hidden rows as "a list is already open", skip the button click, then click a hidden row
  // on a CLOSED list (which the real library ignores), and (b) conflate two pickers' rows. So
  // return only rows that are actually RENDERED — the open list's (#4032 v23 read-back, bug 2).
  var all = document.querySelectorAll('.iti__country, li[class*="iti__country"]');
  var out = [];
  for(var i=0;i<all.length;i++){ if(abShown(all[i])) out.push(all[i]); }
  return out;
}
function abItiOpen(iti, btn){
  // Is THIS picker's dropdown already OPEN? Clicking the selected-country button again TOGGLES it
  // closed in intl-tel-input, so a list already open (e.g. resolved via its portalled list) must be
  // used as-is, not toggled shut and raced back open — yet a CLOSED picker MUST still be clicked
  // open even though its hidden rows linger in the DOM, and a sibling picker's rows must not count.
  // "Open" is the button's authoritative aria-expanded="true" (v23+), or a RENDERED country list
  // THIS picker owns: the aria-controls target (the portalled v23 dropdown, or an in-page one) else
  // the list inside the wrapper (older markup). A per-picker signal — never a document-wide count.
  if(btn && btn.getAttribute && btn.getAttribute('aria-expanded') === 'true') return true;
  var boxSel = '[role="listbox"], .iti__country-list, [class*="iti__country-list"]';
  var box = null;
  var cid = btn && btn.getAttribute && btn.getAttribute('aria-controls');
  if(cid){
    var tgt = null;
    try { tgt = document.getElementById(cid); } catch(e){ tgt = null; }
    if(tgt) box = (tgt.matches && tgt.matches(boxSel)) ? tgt
                : (tgt.querySelector ? tgt.querySelector(boxSel) : null);
  }
  if(!box && iti && iti.querySelector) box = iti.querySelector(boxSel);
  return abShown(box);
}
function abItiWrapper(el){
  // The `.iti` wrapper that OWNS the selected-country button — the correct root for opening and
  // read-back. In v23+ the dropdown (search input + country list) is PORTALLED to a body-level
  // `div.iti.iti--container`; when `el` resolved to THAT container / its list / its search input
  // (bd-qgrz made its aria-label="Country" resolve), `el.closest('.iti')` is the CONTAINER, which
  // holds no `.iti__selected-country`, so the read-back saw only "+1" / nothing (#4032 v23 read-back,
  // wrong root). Resolve the owning wrapper:
  //   (1) a wrapper around `el` that actually holds a selected-country button;
  //   (2) else the button whose aria-controls targets this dropdown's content / list / container id;
  //   (3) else any `.iti` wrapper on the page that holds a selected-country button.
  if(!el) return el;
  var sel = '.iti__selected-flag, .iti__selected-country, [class*="iti__selected-flag"], [class*="iti__selected-country"]';
  var n = el.closest && el.closest('.iti, [class*="iti--"]');
  while(n){
    if(n.querySelector && n.querySelector(sel)) return n;
    var p = n.parentElement;
    n = (p && p.closest) ? p.closest('.iti, [class*="iti--"]') : null;
  }
  var cont = el.closest && el.closest('.iti, [class*="iti--"]');
  var ids = [];
  var dc = el.closest && el.closest('.iti__dropdown-content, [class*="iti__dropdown-content"]');
  if(dc && dc.id) ids.push(dc.id);
  var box = (el.matches && el.matches('[role="listbox"], .iti__country-list, [class*="iti__country-list"]')) ? el
          : (el.querySelector && el.querySelector('[role="listbox"], .iti__country-list, [class*="iti__country-list"]'));
  if(box && box.id) ids.push(box.id);
  if(cont && cont.id) ids.push(cont.id);
  for(var i=0;i<ids.length;i++){
    var esc = (window.CSS && CSS.escape) ? CSS.escape(ids[i]) : ids[i];
    var btn = null;
    try { btn = document.querySelector(
      '.iti__selected-country[aria-controls="' + esc + '"], [class*="iti__selected-country"][aria-controls="' + esc + '"]'); }
    catch(e){ btn = null; }
    if(btn){ var w = btn.closest('.iti, [class*="iti--"]'); if(w) return w; }
  }
  var all = document.querySelectorAll('.iti, [class*="iti--"]');
  for(var j=0;j<all.length;j++){ if(all[j].querySelector && all[j].querySelector(sel)) return all[j]; }
  return cont || el;
}
async function abIti(el, want){
  var iti = abItiWrapper(el);
  var label = abFieldLabel(el);
  var btn = iti && iti.querySelector && iti.querySelector('.iti__selected-flag, .iti__selected-country, [class*="selected-flag"], [class*="selected-country"]');
  // Open the country list — but NOT when THIS picker is already open: clicking the button again
  // TOGGLES it closed in intl-tel-input, so a dropdown already open (e.g. resolved via its portalled
  // list) is used as-is rather than toggled shut and raced back open. `abItiOpen` is a PER-PICKER
  // check (aria-expanded / a rendered owned list), not a document-wide row count — the older markup
  // and a second picker both park hidden rows in the DOM, and those must NOT suppress the open.
  if(btn && !abItiOpen(iti, btn)) abClickOption(btn);
  var items = await abWaitFor(abItiItems, 3000);
  if(!items.length) return {ok:false, reason:'no-option', kind:'iti', label:label, options:[]};
  var names = items.map(function(li){
    var nm = li.querySelector('.iti__country-name, [class*="country-name"]');
    return abClean(nm ? nm.textContent : li.textContent);
  });
  var m = abMatchAny(names, want);
  if(m.error) return {ok:false, reason:m.error, kind:'iti', label:label, options:names.slice(0,10)};
  var chosen = names[m.index];
  abClickOption(items[m.index]);
  function abItiOk(){
    return abIdentityMatch(chosen, abSelectedIdentity(iti)) ||
           abNorm(chosen) === abNorm(abItiSelectedName(iti));
  }
  await abWaitFor(function(){ return abItiOk() ? [1] : []; }, 2000);
  var cands = abSelectedIdentity(iti);                   // read back the selected flag's identity
  var iname = abItiSelectedName(iti);
  if(!abItiOk())
    return {ok:false, reason:'mismatch', kind:'iti', label:label, wanted:chosen,
            actual: abBestName(cands) || iname};
  return {ok:true, kind:'iti', label:label, chosen:chosen, committed: abBestName(cands) || iname || chosen};
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
    if(kind === 'group'){
      var first = /^input$/i.test(el.tagName) ? el : el.querySelector('input[type="radio"], input[type="checkbox"]');
      var g = abGroupInfo(first) || {kind:'checkbox-group', key:'', members:[first], label:abFieldLabel(first), required:false};
      return JSON.stringify(await abChoice(g, WANT));
    }
    if(kind === 'autocomplete')
      return JSON.stringify({ok:false, reason:'autocomplete', input:abControlSelector(el),
                             value:String(el.value || ''), label:abFieldLabel(el)});
    return JSON.stringify({ok:false, reason:'unsupported', kind:kind, label:abFieldLabel(el)});
  } catch(e){
    return JSON.stringify({ok:false, reason:'exception', message:String(e && e.message || e)});
  }
})()
"""


def alternatives(option_text: str) -> list:
    """``"A|B|C"`` → ``["A", "B", "C"]`` (tried in order, first match wins); a text without
    ``|`` is the single alternative. Blank pieces are dropped."""
    parts = [p.strip() for p in (option_text or "").split("|")]
    parts = [p for p in parts if p]
    return parts or [option_text or ""]


def select_js(selector: str, option_text: str) -> str:
    """The script ``browser_select`` evals: detect the widget at ``selector`` and set it to
    ``option_text`` in the page, reading back the committed value. ``selector`` is the
    locator the bd-12mo.1 resolver already turned into something ``querySelector`` can act on
    (a ``[data-ab-field="N"]`` tag for a label, or a raw CSS selector). ``option_text`` may list
    ALTERNATIVES separated by ``|``. Rides ``eval --stdin`` like every other form script (#3689)."""
    wants = alternatives(option_text)
    return (_JS_LIB + _SELECT_LIB + "\n(function(){\n"
            "var SEL = " + _js(selector) + ";\n"
            "var WANT = " + _js(wants if len(wants) > 1 else wants[0]) + ";\n"
            "return " + _SELECT_DRIVER.strip() + ";\n})()")


def select_reopen_target(output: str) -> str:
    """If ``select_js`` reported the dropdown would not open in-page (``reason:'not-open'`` — every
    synthetic open strategy failed), or a radio/checkbox option would not tick from a synthetic
    click (``reason:'click-needed'``), return the CONTROL selector ``browser_select`` should do a
    REAL, trusted CLI click on before retrying; otherwise "". A trusted click opens a react-select
    that gates on ``isTrusted`` where a synthetic mousedown cannot. Never raises."""
    try:
        data = _loads(output)
    except (ValueError, TypeError):
        return ""
    if isinstance(data, dict) and data.get("reason") in ("not-open", "click-needed"):
        return data.get("control") or ""
    return ""


def autocomplete_target(output: str) -> dict | None:
    """``{input, value, label}`` when ``select_js`` handed the field back for AUTOCOMPLETE — a
    plain text input (``reason:'autocomplete'``), or a type-to-search combobox that showed no
    options at all after in-page typing (``no-option`` with an ``input``). Else ``None``."""
    try:
        data = _loads(output)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data.get("input"):
        return None
    if data.get("reason") == "autocomplete" or (data.get("reason") == "no-option" and not data.get("options")):
        return {"input": data["input"], "value": str(data.get("value") or ""), "label": data.get("label") or ""}
    return None


def render_select(output: str, field: str, option_text: str, *, fresh: bool = False) -> str:
    """Turn ``select_js``'s JSON into the model-facing result. Success is ``Selected "…" in
    <label>``; every failure is an ``Error: …`` — an ambiguous/absent option lists the choices,
    a dropdown that never opened says so (never the misleading "no options were found"), and a
    read-back that disagrees with what was chosen is a hard mismatch error (never a silent
    success). Never raises."""
    try:
        data = _loads(output)
    except (ValueError, TypeError):
        return "Error: could not select — the page returned unreadable data."
    if not isinstance(data, dict):
        return "Error: could not select — unexpected data from the page."
    label = data.get("label") or field
    if data.get("ok"):
        chosen = data.get("chosen") or option_text
        if data.get("already") and not fresh:
            return f'Selected "{chosen}" in {label} (it was already selected; left as is)'
        return f'Selected "{chosen}" in {label}'
    reason = data.get("reason")
    if reason == "not-open":
        exp = data.get("expanded") or "false"
        return (f"Error: could not open the dropdown for {label} (aria-expanded stayed {exp}) after "
                "JS, keyboard and a real click — call browser_snapshot to inspect it.")
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
    if reason == "click-needed":
        return (f'Error: "{data.get("wanted", option_text)}" in {label} did not tick from a click — '
                "call browser_snapshot to inspect the control.")
    if reason == "unsupported":
        return (f"Error: {field!r} is a {data.get('kind', 'plain')!r} field, not a choice "
                "field browser_select can set. Use browser_fill for text.")
    if reason == "exception":
        return f"Error: selecting in {label} failed in the page: {str(data.get('message', ''))[:200]}"
    return f"Error: could not select {option_text!r} in {label}."


# ── browser_select AUTOCOMPLETE: suggestions that appear only after typing (#4954 eval) ─
# Lever's "Current location" and Google-Places-style boxes are plain text inputs whose listbox
# renders only after REAL typing — Lever searches on keyup, which the CLI's insertText-based
# `fill` never fires, so browser_select used to report "unsupported" and in-page typing found
# nothing. The tool (tools.browser_select) does the typing with the CLI — `fill`, then a
# harmless real `End` keypress — and these scripts do the rest: wait (bounded) for THIS field's
# suggestions, match the wanted text against their visible text, tag the unique match for a
# trusted CLI click, and read the committed value back. No/ambiguous match lists the
# suggestions and the tool restores the field's previous value.
_AC_LIB = r"""
function abAcKey(s){ return abNorm(s).replace(/[^\p{L}\p{N}]+/gu, ''); }
function abAcText(n){ return abClean((n && (n.innerText || n.textContent)) || ''); }
var AB_AC_STATUS = /no[-_ ]?results?|loading|empty|status|spinner|header|footer|hint|powered/i;
function abAcStatusText(t){ return /^(loading|searching|no (results|matches|options|location)|type to search)/i.test(t); }
function abAcItemsIn(box, input){
  var out = [];
  var kids = box.children || [];
  for(var i=0;i<kids.length;i++){
    var k = kids[i];
    if(!abShown(k) || k.contains(input)) continue;
    if(k.querySelector && k.querySelector('input, select, textarea')) continue;
    if(AB_AC_STATUS.test(String(k.className || '')) || abIsHint(k)) continue;
    var t = abAcText(k);
    if(!t || abAcStatusText(t)) continue;
    out.push(k);
  }
  return out;
}
function abAcItems(input){
  // 1) aria wiring — the input (or its combobox) names its listbox.
  var owner = (input.closest && input.closest('[role="combobox"]')) || input;
  var ids = ((input.getAttribute('aria-controls') || input.getAttribute('aria-owns') || '') + ' ' +
             (owner.getAttribute('aria-controls') || owner.getAttribute('aria-owns') || '')).trim();
  var out = [];
  ids.split(/\s+/).forEach(function(id){
    var lb = null; try { lb = id && document.getElementById(id); } catch(e){ lb = null; }
    if(!lb) return;
    var os = lb.querySelectorAll('[role="option"]');
    for(var i=0;i<os.length;i++){ if(abShown(os[i]) && out.indexOf(os[i]) < 0) out.push(os[i]); }
  });
  if(out.length) return out;
  // 2) Google Places: a body-level .pac-container per input; only the open one is rendered.
  var pac = document.querySelectorAll('.pac-container .pac-item');
  for(var p=0;p<pac.length;p++){ if(abShown(pac[p])) out.push(pac[p]); }
  if(out.length) return out;
  // 3) THIS field's own container (never crossing into a neighbour field): a rendered
  //    role=option list, else the suggestion box with the most items (deepest on a tie).
  var node = input.parentElement, best = [], scope = null;
  for(var up=0; node && up<5; up++){
    if(node === document.body || node.tagName === 'FORM') break;
    if(abHasForeignField(node, [input])) break;
    scope = node; node = node.parentElement;
  }
  if(!scope) return out;
  var opts = scope.querySelectorAll('[role="option"]');
  for(var o=0;o<opts.length;o++){ if(abShown(opts[o]) && abAcText(opts[o])) out.push(opts[o]); }
  if(out.length) return out;
  var boxes = scope.querySelectorAll('[role="listbox"], [class*="result"], [class*="suggest"], ' +
    '[class*="autocomplete"], [class*="dropdown"], [class*="menu"], [class*="option"], ul');
  for(var b=0;b<boxes.length;b++){
    var bx = boxes[b];
    if(bx.contains(input) || !abShown(bx) || AB_AC_STATUS.test(String(bx.className || ''))) continue;
    var items = abAcItemsIn(bx, input);
    if(items.length && items.length >= best.length) best = items;
  }
  return best;
}
function abAcMatch(texts, want){
  var m = abMatchOption(texts, want);
  if(m.index !== undefined || m.error === 'ambiguous') return m;
  // Loose: punctuation/space-blind ("PortlandOR, USA" — a pac-item's spans — vs "Portland, OR").
  var kw = abAcKey(want), exact = [], prefix = [];
  if(!kw) return {error:'no-option'};
  for(var i=0;i<texts.length;i++){
    var k = abAcKey(texts[i]);
    if(k === kw) exact.push(i); else if(k.indexOf(kw) === 0) prefix.push(i);
  }
  if(exact.length === 1) return {index: exact[0]};
  if(exact.length > 1 || prefix.length > 1) return {error:'ambiguous'};
  if(prefix.length === 1) return {index: prefix[0]};
  return {error:'no-option'};
}
"""

_AC_PICK = r"""
(async function(){
  var input = null; try { input = document.querySelector(SEL); } catch(e){ input = null; }
  if(!input) return JSON.stringify({ok:false, reason:'not-found'});
  var label = abFieldLabel(input);
  var end = Date.now() + TIMEOUT, items = [];
  for(;;){
    items = abAcItems(input);
    if(items.length) break;
    if(Date.now() >= end) break;
    await abSleep(120);
  }
  if(items.length){ await abSleep(300); var again = abAcItems(input); if(again.length) items = again; }
  if(!items.length) return JSON.stringify({ok:false, reason:'no-suggestions', label:label});
  var texts = items.map(abAcText);
  var m = abAcMatch(texts, WANT);
  if(m.error) return JSON.stringify({ok:false, reason:m.error, label:label, options:texts.slice(0,10)});
  try { items[m.index].setAttribute('data-ab-opt', NONCE); } catch(e){}
  return JSON.stringify({ok:true, option:'[data-ab-opt="' + NONCE + '"]', chosen:texts[m.index], label:label});
})()
"""

_AC_VERIFY = r"""
(async function(){
  var input = null; try { input = document.querySelector(SEL); } catch(e){ input = null; }
  if(!input) return JSON.stringify({ok:false, reason:'not-found'});
  var want = abAcKey(CHOSEN);
  function ok(){ var v = abAcKey(input.value); return !!v && (v === want || v.indexOf(want) === 0); }
  await abWaitFor(function(){ return ok() ? [1] : []; }, 2000);
  return JSON.stringify({ok:ok(), value:String(input.value || '')});
})()
"""


def _ac_script(body: str, **values) -> str:
    head = "".join(f"var {k} = {_js(v)};\n" for k, v in values.items())
    return (_JS_LIB + _SELECT_LIB + _AC_LIB + "\n(function(){\n" + head
            + "return " + body.strip() + ";\n})()")


def autocomplete_pick_js(selector: str, want: str, nonce: str, timeout_ms: int = 5000) -> str:
    """After the tool typed ``want`` into ``selector``: wait ≤ ``timeout_ms`` for THIS field's
    suggestions, match ``want`` by visible text (exact, unique prefix, then punctuation-blind) and
    tag the match ``data-ab-opt=<nonce>`` for a trusted CLI click. Changes nothing itself."""
    return _ac_script(_AC_PICK, SEL=selector, WANT=want, NONCE=nonce, TIMEOUT=int(timeout_ms))


def autocomplete_verify_js(selector: str, chosen: str) -> str:
    """After the click: wait ≤2s for the input's value to read ``chosen`` (or a fuller form of it)."""
    return _ac_script(_AC_VERIFY, SEL=selector, CHOSEN=chosen)


def parse_json(output: str) -> dict:
    """A form script's JSON result as a dict; ``{}`` on anything unreadable. Never raises."""
    try:
        data = _loads(output)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def render_autocomplete_miss(data: dict, field: str, typed: list, restored: str) -> str:
    """The ``Error:`` for an autocomplete that found nothing to pick (the field was restored)."""
    label = data.get("label") or field
    tried = " / ".join(repr(t) for t in typed)
    back = f"restored to {restored!r}" if restored else "cleared again"
    reason = data.get("reason")
    if reason in ("no-option", "ambiguous"):
        opts = ", ".join(repr(o) for o in (data.get("options") or [])[:10])
        head = ("matches more than one suggestion" if reason == "ambiguous" else "matches no suggestion")
        return (f"Error: {tried} {head} in {label}. Suggestions: {opts}. Nothing was picked; the "
                f"field was {back}. Retry with the exact suggestion text.")
    if reason == "no-suggestions":
        return (f"Error: no suggestions appeared in {label} after typing {tried}; the field was {back}. "
                "If it is a plain text field, use browser_fill.")
    if reason == "not-found":
        return f"Error: the input for {label} vanished while picking a suggestion."
    return f"Error: could not pick a suggestion in {label}; the field was {back}."


# ── browser_upload: attach a fenced local file to a file input, then PROVE it (#4032) ─
# Ashby/Greenhouse application forms want a résumé FILE, and hide the real `input[type=file]`
# behind an "Attach" button — so a label/CSS locator often lands on the wrapper or the button,
# not the input itself. The driver resolves to the file input (a `#id` / bare-id locator resolves
# DIRECTLY via getElementById, with no climbing, even when the input is hidden behind an "Attach"
# button), or to the SINGLE file input inside the located element's field container (none / more
# than one is an error, never a guess), and tags it `data-pa-upload="<nonce>"` so the CLI acts on
# a STABLE selector — never a snapshot ref a re-render would invalidate.
#
# It ALSO returns the input's `id`, `name`, its own `<label for>` (`label_for` / `label_text`) and a
# stable field-container selector, because a Greenhouse-style widget may either RE-RENDER its file
# input after the attach (bug 3 — fresh input, same id/name) or REMOVE it from the DOM ENTIRELY
# (input-removed — leaving only a filename chip, the `<label for>` still pointing at the now-missing
# id). In both cases the nonce tag rode off with the discarded node. The verify re-finds the input
# by `#<id>` / `input[type=file][name=…]`; failing that (a removal) it re-finds the field CONTAINER
# — the surviving wrapper, then the wrapper of `label[for=<id>]`, then an element whose text starts
# with the label — and reads the filename chip there, so an attach that survives EITHER behavior is
# confirmed, not reported as "could not be found". The container is chosen to SURVIVE the removal:
# the ancestor that holds both the `<label>` and the input, never FORM/body.
# Shares abClean / abCandidateLabels / abTextNoControls with _JS_LIB (one tidy/label copy).
_UPLOAD_DRIVER = r"""
(function(){
  function abIsFile(n){
    return !!(n && n.tagName === 'INPUT' && (n.getAttribute('type')||'').toLowerCase() === 'file');
  }
  // A `#id` / bare-id locator that names an input[type=file] resolves to it DIRECTLY — no
  // climbing — even when the input is hidden behind an "Attach" button (#4032 bug 3). Any other
  // selector (a CSS wrapper, a [data-ab-field] tag from the label resolver) goes through
  // querySelector and the container climb below.
  var el = null;
  var idm = /^#?([A-Za-z][\w-]*)$/.exec(SEL || '');
  if(idm){ try { el = document.getElementById(idm[1]); } catch(e){ el = null; } }
  if(!el){ try { el = document.querySelector(SEL); } catch(e){ el = null; } }
  if(!el) return JSON.stringify({ok:false, reason:'not-found'});
  var labs = abCandidateLabels(el), label = labs.length ? labs[0] : '';
  var input = null;
  if(abIsFile(el)){
    input = el;
  } else {
    // The located element is a wrapper / label / "Attach" button — climb a bounded number of
    // ancestors and take the file input from the FIRST container that holds any. Exactly one
    // resolves; more than one is ambiguous (never a guess); none is an error.
    var node = el, picked = null, many = 0;
    for(var up = 0; node && up < 6; up++){
      var found = (node.querySelectorAll && node.querySelectorAll('input[type="file"]')) || [];
      if(found.length === 1){ picked = found[0]; break; }
      if(found.length > 1){ many = found.length; break; }
      if(node.tagName === 'FORM' || node === document.body) break;
      node = node.parentElement;
    }
    if(many) return JSON.stringify({ok:false, reason:'multiple', label:label, count:many});
    if(!picked) return JSON.stringify({ok:false, reason:'no-file-input', label:label});
    input = picked;
  }
  if(!label){ var il = abCandidateLabels(input); label = il.length ? il[0] : ''; }
  try { input.setAttribute('data-pa-upload', NONCE); } catch(e){}
  // This field's own <label for> — its id and text. When the widget REMOVES the input from the DOM
  // (not just re-renders it empty), the verify step and form_read re-find the field by this label,
  // since neither #id nor the nonce resolves to anything any more (#4032 input-removed).
  var labelFor = '', labelText = '', labelEl = null;
  var inId = input.getAttribute && input.getAttribute('id');
  if(inId){
    var lsel = 'label[for="' + (window.CSS && CSS.escape ? CSS.escape(inId) : inId) + '"]';
    try { labelEl = document.querySelector(lsel); } catch(e){ labelEl = null; }
    if(labelEl){ labelFor = inId; labelText = abClean(abTextNoControls(labelEl)); }
  }
  if(!labelEl && input.closest){
    labelEl = input.closest('label');
    if(labelEl && !labelText) labelText = abClean(abTextNoControls(labelEl));
  }
  // A stable container that SURVIVES the input's removal: PREFER the tightest ancestor that holds
  // BOTH the input and its <label> as SEPARATE descendants (the field wrapper the widget leaves in
  // place — e.g. Greenhouse's <label for> beside the input), never FORM/body. Use its id, else tag
  // it. This preference is tried ONLY for a SEPARATE label — one that does NOT itself wrap the
  // input. A WRAPPING <label> (input.closest('label'), no `for`) or no label at all falls straight
  // through to the nearest ancestor-with-id — then a tagged parent — exactly the way it re-found the
  // container BEFORE this change, so nothing is narrowed (#4032: additive). (Were a wrapping label
  // allowed in, the climb would stop AT the label — an element contains itself — and pin the
  // container to it; a widget that removes the input and draws its chip OUTSIDE the label would then
  // read as "nothing is attached", and the broader surviving ancestor-with-id the removal case
  // relies on would be shadowed. Likewise pinning to input.parentElement — which the widget may
  // remove with the input — would shadow that same ancestor-with-id fallback.)
  var container = '';
  var wrapper = null;
  if(labelEl && !(labelEl.contains && labelEl.contains(input))){
    var wn = input.parentElement;
    for(var wu = 0; wn && wu < 6; wu++){
      if(wn.tagName === 'FORM' || wn === document.body) break;
      // never the label itself (it trivially contains itself) — only a PROPER wrapper of both.
      if(wn !== labelEl && wn.contains && wn.contains(labelEl)){ wrapper = wn; break; }
      wn = wn.parentElement;
    }
  }
  if(wrapper){
    var wid = wrapper.getAttribute && wrapper.getAttribute('id');
    if(wid){ container = '#' + (window.CSS && CSS.escape ? CSS.escape(wid) : wid); }
    else { try { wrapper.setAttribute('data-pa-upbox', NONCE); container = '[data-pa-upbox="' + NONCE + '"]'; } catch(e){} }
  }
  if(!container){
    var cnode = input.parentElement;
    for(var cu = 0; cnode && cu < 6; cu++){
      if(cnode.tagName === 'FORM' || cnode === document.body) break;
      var cid = cnode.getAttribute && cnode.getAttribute('id');
      if(cid){ container = '#' + (window.CSS && CSS.escape ? CSS.escape(cid) : cid); break; }
      cnode = cnode.parentElement;
    }
  }
  if(!container){
    var box = input.parentElement;
    if(box && box.tagName !== 'FORM' && box !== document.body){
      try { box.setAttribute('data-pa-upbox', NONCE); container = '[data-pa-upbox="' + NONCE + '"]'; } catch(e){}
    }
  }
  // The FIELD BOX — the widest ancestor that is still THIS field's alone, captured BEFORE the attach.
  // Live Greenhouse keeps its <label for=resume> ("Attach") in the SAME small <div> as the input and
  // REPLACES that whole button container with the filename chip — so the label-anchored `container`
  // above, `label[for]` and its "Attach" text all vanish with the input, and nothing re-found the
  // field. Climb (bounded, never FORM/body) while the ancestor holds no OTHER file input and no other
  // field's box; the outer `role=group` wrapper ("Resume/CV*") survives the swap and shows the chip.
  // Tagged `data-pa-file-for=<id>` (plus label / required) so browser_form_read can still report the
  // field under its id once the input is gone (#resume: input AND label removed).
  // Stop BEFORE any ancestor that holds another field: (a) another live file input; (b) a box an
  // earlier upload tagged for a DIFFERENT field (its input may already be gone) — a tag that
  // CONTAINS this input is this field's own, from an earlier attempt; (c) a filename CHIP with a
  // detach control — before the attach this field shows none, so any chip is a neighbour's whose
  // input was already swapped away (attached by hand, or by an earlier upload). Without (b)/(c)
  // the box swallowed such a neighbour and its chip was read as this field's filename.
  var fileKey = inId || ('@@pa-' + NONCE);     // '@@' — never a real id; form_read skips these
  var fieldBox = '', fbNode = null, fn = input.parentElement;
  for(var fu = 0; fn && fu < 6; fu++){
    if(fn.tagName === 'FORM' || fn === document.body) break;
    var ff = (fn.querySelectorAll && fn.querySelectorAll('input[type="file"]')) || [], other = false;
    for(var fj = 0; fj < ff.length; fj++){ if(ff[fj] !== input){ other = true; break; } }
    if(!other && fn.querySelectorAll){
      var fb = fn.querySelectorAll('[data-pa-file-for], [data-pa-filebox], [data-pa-upbox]');
      for(var fk = 0; fk < fb.length; fk++){
        var ptag = fb[fk];
        if(ptag.contains && ptag.contains(input)) continue;      // this field's own (earlier) tag
        var tFor = ptag.getAttribute('data-pa-file-for');
        var tNonce = ptag.getAttribute('data-pa-filebox') || ptag.getAttribute('data-pa-upbox');
        if((tFor && tFor !== fileKey) || (!tFor && tNonce && tNonce !== NONCE)){ other = true; break; }
      }
    }
    if(!other){
      var nchip = abChipElement(fn, input);
      if(nchip && (abRemoveControl(nchip) || abRemoveControl(nchip.parentElement))) other = true;
    }
    if(other) break;
    fbNode = fn;
    fn = fn.parentElement;
  }
  var groupLabel = '';
  var grp = input.closest && input.closest('[role="group"][aria-labelledby], [role="group"][aria-label]');
  if(grp){
    groupLabel = abByIds(grp.getAttribute('aria-labelledby')) || abClean(grp.getAttribute('aria-label') || '');
  }
  if(!groupLabel) groupLabel = labelText || label;
  if(fbNode){
    try {
      // Always tagged (a later upload's climb stops at it); form_read reports only a real id.
      fbNode.setAttribute('data-pa-file-for', fileKey);
      fbNode.setAttribute('data-pa-file-label', abClean(groupLabel));
      var req = !!input.required || (input.getAttribute('aria-required') === 'true') ||
                !!(grp && grp.getAttribute('aria-required') === 'true') ||
                !!(grp && /\*/.test(String(grp.getAttribute('aria-labelledby') || '').split(/\s+/).map(function(i){
                  var n = i && document.getElementById(i); return n ? n.textContent : ''; }).join(' ')));
      fbNode.setAttribute('data-pa-file-required', req ? 'true' : 'false');
      fbNode.setAttribute('data-pa-filebox', NONCE);
      fieldBox = '[data-pa-filebox="' + NONCE + '"]';
    } catch(e){}
  }
  return JSON.stringify({ok:true, selector:'[data-pa-upload="' + NONCE + '"]', label:label,
                         name:(input.getAttribute && input.getAttribute('name')) || '',
                         id:(input.getAttribute && input.getAttribute('id')) || '',
                         container:container, label_for:labelFor, label_text:labelText,
                         field_box:fieldBox, group_label:abClean(groupLabel)});
})()
"""

# The verify, in order: (a) the nonce-tagged node, if it survived the attach; (b) else the fresh
# input the widget re-rendered — re-found by `#<id>`, then `input[type=file][name=…]`; (c) the
# field container's visible text, where a Greenhouse widget leaves a filename chip. `name` is the
# `input.files[0].name` read from whichever of (a)/(b) still carries the file; `displayed` says the
# container's text (controls stripped) contains the uploaded basename; `source` records which of
# (a)/(b) read the name. Any field-level validation text ("must be a PDF") is surfaced either way.
# When the widget REMOVED the input (no live node, no fresh input), the container is RE-FOUND in
# order — (1) the driver's stable CONTAINER; (2) the wrapper of `label[for=LABEL_FOR]` climbing to
# a chip (never a sibling file field's); (3) an element whose text starts with LABEL_TEXT — then
# scanned for the chip (#4032 input-removed).
_UPLOAD_VERIFY = r"""
(function(){
  function abIsFile(n){
    return !!(n && n.tagName === 'INPUT' && (n.getAttribute('type')||'').toLowerCase() === 'file');
  }
  function abFiles(n){ return (n && n.files && n.files.length) ? String(n.files[0].name || '') : ''; }
  function abRefindBox(){
    // (1) the stable wrapper the driver stored (survives a re-render AND an input removal).
    var b = null;
    if(CONTAINER){ try { b = document.querySelector(CONTAINER); } catch(e){ b = null; } }
    if(b) return b;
    // (1b) the FIELD BOX the driver tagged before the attach — the widest ancestor holding only this
    //      field. Live Greenhouse swaps out the input's button container, <label for> and all, so
    //      (1) and (2) both vanish; the outer field wrapper survives and holds the chip.
    if(FIELD_BOX){ try { b = document.querySelector(FIELD_BOX); } catch(e){ b = null; } }
    if(b) return b;
    // (2) the wrapper of this field's own label[for=LABEL_FOR], climbing to its attached-file chip
    //     without ever crossing into a sibling file field's container (abOrphanBox).
    if(LABEL_FOR){
      var lab = null;
      try { lab = document.querySelector('label[for="' +
        (window.CSS && CSS.escape ? CSS.escape(LABEL_FOR) : LABEL_FOR) + '"]'); } catch(e){ lab = null; }
      var ob = abOrphanBox(lab);
      if(ob) return ob;
    }
    // (3) last resort: the tightest element whose text STARTS WITH the field label (the <label for>
    //     text, then the field's group label, e.g. "Resume/CV") and that holds a chip, skipping any
    //     element that still holds a file input (a sibling field).
    var texts = [LABEL_TEXT, GROUP_LABEL];
    for(var ti = 0; ti < texts.length; ti++){
      if(!texts[ti]) continue;
      var want = abNorm(texts[ti]), all = document.querySelectorAll('*');
      var best = null, bestLen = 1e9;
      for(var i = 0; i < all.length; i++){
        var el = all[i];
        if(el.querySelector && el.querySelector('input[type="file"]')) continue;
        var t = abNorm(abTextNoControls(el));
        if(t && t.indexOf(want) === 0 && t.length < bestLen && abChipElement(el, null)){ best = el; bestLen = t.length; }
      }
      if(best) return best;
    }
    return null;
  }
  // (a) the nonce-tagged node, if the re-render did not discard it.
  var node = null;
  try { node = document.querySelector(SEL); } catch(e){ node = null; }
  var name = '', source = '';
  if(abIsFile(node)){ name = abFiles(node); if(name) source = 'nonce'; }
  // (b) the widget re-rendered its input (the nonce rode off with the old node) — re-find a fresh
  //     input by its committed id, then by name, and read ITS files.
  if(!name && ID){
    var byId = null; try { byId = document.getElementById(ID); } catch(e){ byId = null; }
    if(abIsFile(byId)){ name = abFiles(byId); if(name) source = 'id'; if(!node) node = byId; }
  }
  if(!name && NAME){
    var byName = null;
    try {
      byName = document.querySelector('input[type="file"][name="' +
        (window.CSS && CSS.escape ? CSS.escape(NAME) : NAME) + '"]');
    } catch(e){ byName = null; }
    if(abIsFile(byName)){ name = abFiles(byName); if(name) source = 'name'; if(!node) node = byName; }
  }
  // (c) the field container often keeps the attached filename visible as a chip even after the
  //     input is re-rendered empty OR removed from the DOM — success if the container shows the
  //     basename. The container is RE-FOUND (abRefindBox) so a widget that discarded the input
  //     entirely, nonce and all, is still resolved to THIS field's wrapper, never a sibling's.
  var box = abRefindBox();
  if(!box) box = (node && node.parentElement) || node;
  // (c) the attached basename shown as a filename CHIP (a tight element whose text ends in an
  //     extension — controls excluded, and any element that IS/sits-under/CONTAINS an alert
  //     excluded) — NOT merely present somewhere in the container's text, which would read a
  //     rejection message that names the file ("resume.pdf exceeds 5MB", or a plain wrapper around a
  //     role=alert) as a success (#4032 bug 3 review). Prefer the re-found input's OWN field scope
  //     (abFileChipName: a chip with a detach control, never crossing into a sibling file field);
  //     else scan the container, where requiring the basename still rejects a neighbour's chip.
  var chip = abIsFile(node) ? abFileChipName(node) : '';
  if(!chip) chip = abChipText(box, node);
  var displayed = !!(chip && BASENAME && chip.indexOf(BASENAME) >= 0);
  // Surface any visible validation/error text in the field's container (bounded climb), so a
  // form that rejected the file ("must be a PDF") tells the agent rather than reading as a win.
  var err = '', scan = box || node;
  for(var up = 0; scan && up < 6; up++){
    var alert = scan.querySelector && scan.querySelector(
      '[role="alert"], [aria-invalid="true"], [class*="error"], [class*="invalid"]');
    if(alert){ var t = abClean(abTextNoControls(alert)); if(t){ err = t; break; } }
    if(scan.tagName === 'FORM' || scan === document.body) break;
    scan = scan.parentElement;
  }
  return JSON.stringify({ok:true, name:name, source:source, displayed:displayed, error:err});
})()
"""


def upload_js(selector: str, nonce: str) -> str:
    """Script ``browser_upload`` evals to RESOLVE the upload target: find the file input at
    ``selector`` (or the single one inside its field container) and tag it
    ``data-pa-upload="<nonce>"`` so the CLI acts on a stable selector, not a stale ref.
    ``selector`` is what the bd-12mo.1 resolver already produced (a ``[data-ab-field="N"]`` tag
    for a label, or raw CSS). Rides ``eval --stdin`` like every form script (#3689)."""
    return (_JS_LIB + "\n(function(){\n"
            "var SEL = " + _js(selector) + ";\n"
            "var NONCE = " + _js(nonce) + ";\n"
            "return " + _UPLOAD_DRIVER.strip() + ";\n})()")


def upload_verify_js(selector: str, input_id: str = "", input_name: str = "",
                     container: str = "", basename: str = "", label_for: str = "",
                     label_text: str = "", field_box: str = "", group_label: str = "") -> str:
    """Script ``browser_upload`` evals AFTER the attach. Reads ``input.files[0].name`` back from
    the nonce-tagged node (``selector``) if it survived, else from the input the widget
    re-rendered (re-found by ``input_id``, then ``input_name``), and reports whether the field
    ``container``'s visible text shows ``basename`` (the filename chip) — plus any field-level
    validation text. ``label_for`` / ``label_text`` re-find the field's container when the widget
    REMOVED the input from the DOM entirely, so neither the nonce nor ``input_id`` resolves;
    ``field_box`` (the field's outer wrapper, tagged before the attach) and ``group_label`` cover a
    widget that removes the ``<label for>`` along with the input (live Greenhouse)."""
    return (_JS_LIB + "\n(function(){\n"
            "var SEL = " + _js(selector) + ";\n"
            "var ID = " + _js(input_id) + ";\n"
            "var NAME = " + _js(input_name) + ";\n"
            "var CONTAINER = " + _js(container) + ";\n"
            "var BASENAME = " + _js(basename) + ";\n"
            "var LABEL_FOR = " + _js(label_for) + ";\n"
            "var LABEL_TEXT = " + _js(label_text) + ";\n"
            "var FIELD_BOX = " + _js(field_box) + ";\n"
            "var GROUP_LABEL = " + _js(group_label) + ";\n"
            "return " + _UPLOAD_VERIFY.strip() + ";\n})()")


def parse_upload(output: str, field: str) -> tuple[dict | None, str]:
    """Parse ``upload_js``'s JSON → ``(info, error)``. On success ``info`` is a dict
    ``{selector, label, id, name, container, label_for, label_text, field_box, group_label}`` — ``selector`` is the
    ``[data-pa-upload=…]`` tag the CLI uploads to, and ``id``/``name``/``container`` /
    ``label_for``/``label_text`` let the verify step re-find the field after a re-render drops the
    tag OR the widget removes the input entirely. On failure ``info`` is ``None`` and ``error`` is a
    ready-to-return ``Error: …``. Never raises."""
    try:
        data = _loads(output)
    except (ValueError, TypeError):
        return None, "Error: could not locate the upload field — the page returned unreadable data."
    if not isinstance(data, dict):
        return None, "Error: could not locate the upload field — unexpected data from the page."
    if data.get("ok"):
        info = {
            "selector": data.get("selector") or "",
            "label": data.get("label") or "",
            "id": data.get("id") or "",
            "name": data.get("name") or "",
            "container": data.get("container") or "",
            "label_for": data.get("label_for") or "",
            "label_text": data.get("label_text") or "",
            "field_box": data.get("field_box") or "",
            "group_label": data.get("group_label") or "",
        }
        return info, ""
    reason, label = data.get("reason"), (data.get("label") or field)
    if reason == "not-found":
        return None, (f"Error: could not find a field matching {field!r} to upload to. Call "
                      "browser_form_read to list the fields (a file field shows kind 'file'), "
                      "or pass a CSS selector.")
    if reason == "no-file-input":
        return None, (f"Error: {label} is not a file input, and no file input sits in its "
                      "container to upload to. Point `field` at the form's file field "
                      "(browser_form_read lists them; a file field shows kind 'file').")
    if reason == "multiple":
        return None, (f"Error: {label}'s container holds {data.get('count', 'several')} file "
                      "inputs, so which one to upload to is ambiguous. Point `field` directly "
                      "at the one you mean (its own label or a CSS selector).")
    return None, f"Error: could not locate a file input for {field!r}."


def upload_settling(output: str) -> bool:
    """True while ``upload_verify_js`` found the field but NO evidence of the attach yet — no
    filename read back and none displayed. Live Greenhouse renders its filename chip a beat AFTER
    the change event (on a cold page often after the first verify), so ``browser_upload`` re-runs
    the verify while this holds, for a bounded wait, before calling it "nothing is attached".
    Anything else — a name, a chip, an unreadable or not-found result — is final. Never raises."""
    try:
        data = _loads(output)
    except (ValueError, TypeError):
        return False
    return (isinstance(data, dict) and bool(data.get("ok"))
            and not str(data.get("name") or "").strip() and not data.get("displayed"))


def render_upload(output: str, field: str, basename: str, label: str = "") -> str:
    """Turn ``upload_verify_js``'s JSON into the result. Success — ``Uploaded <name> to <label>``
    — when a re-found input reads ``basename`` back via ``input.files[0].name``, OR when the
    field's container shows the filename (a re-rendered widget keeps a chip); the latter line says
    ``(verified via the field's displayed filename)``. A non-empty read-back of a DIFFERENT name,
    or no evidence at all, is a hard ``Error:`` (never a silent non-attach), and any field-level
    validation text is surfaced either way. Never raises."""
    where = label or field
    try:
        data = _loads(output)
    except (ValueError, TypeError):
        return "Error: could not verify the upload — the page returned unreadable data."
    if not isinstance(data, dict):
        return "Error: could not verify the upload — unexpected data from the page."
    if not data.get("ok"):
        if data.get("reason") == "not-found":
            return (f"Error: the file input for {where} could not be found to verify the upload, "
                    "so nothing was confirmed attached.")
        return f"Error: could not verify the upload to {where}."
    name = str(data.get("name") or "").strip()
    displayed = bool(data.get("displayed"))
    err = str(data.get("error") or "").strip()
    tail = f" The field reports: {err}" if err else ""
    # (a)/(b) a re-found input carries the file — its name must match what we uploaded.
    if name and name == basename:
        return f"Uploaded {name} to {where}{tail}"
    if name and name != basename:
        return (f"Error: {where} reads back {name!r}, but {basename!r} was uploaded — the "
                f"attachment does not match, so it was not confirmed.{tail}")
    # (c) the input re-rendered empty but the field still SHOWS the filename (a chip) — that is
    #     proof the attach took, even though input.files no longer carries it.
    if displayed:
        return (f"Uploaded {basename} to {where} (verified via the field's displayed filename)"
                f"{tail}")
    return (f"Error: nothing is attached to {where} after the upload — the file input reads "
            f"empty.{tail}")


# ── browser_click JS fallback: a CLI click that reports success but does nothing (#4032) ─
# On Greenhouse the résumé "Enter manually" button opened its textarea only after a
# JS-dispatched click: a normal CLI click exited 0 and NOTHING happened. So
# ``browser_click(js_fallback=True)`` takes a cheap DOM fingerprint, does the CLI click,
# re-fingerprints, and ONLY when nothing moved dispatches an in-page click. The fallback
# never fires when the first click already worked (the fingerprint changed) or when the
# fingerprint can't be read (a doubt is treated as "something happened" — never a second
# click). Both scripts ride ``eval --stdin`` like every form script (#3689).
_FINGERPRINT_DRIVER = r"""
(function(){
  var n = document.getElementsByTagName('*').length;
  var ae = document.activeElement;
  var aeDesc = ae ? ((ae.tagName||'') + '#' + (ae.getAttribute ? (ae.getAttribute('id')||'') : '')
                     + '.' + String(ae.className||'')
                     + ':' + (ae.getAttribute ? (ae.getAttribute('name')||'') : '')) : '';
  var t = null;
  try { t = SEL ? document.querySelector(SEL) : null; } catch(e){ t = null; }
  var exp = (t && t.getAttribute) ? (t.getAttribute('aria-expanded')||'') : '';
  return JSON.stringify({n:n, ae:aeDesc, exp:exp});
})()
"""

_JS_CLICK_DRIVER = r"""
(function(){
  var el = null;
  try { el = document.querySelector(SEL); } catch(e){ el = null; }
  if(!el) return JSON.stringify({ok:false, reason:'not-found'});
  function abFire(type){
    try { el.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true})); } catch(e){}
  }
  // A bubbling mousedown/mouseup PLUS el.click() — the sequence Greenhouse's "Enter manually"
  // button needed. Never a keyboard commit (that lands on the wrong highlighted row, #4032 A2).
  abFire('mousedown'); abFire('mouseup');
  if(typeof el.click === 'function'){ try { el.click(); } catch(e){ abFire('click'); } }
  else abFire('click');
  return JSON.stringify({ok:true});
})()
"""


def fingerprint_js(selector: str) -> str:
    """The script ``browser_click`` evals to fingerprint the page for its JS fallback: the
    element count, a descriptor of ``document.activeElement``, and ``selector``'s
    ``aria-expanded`` — cheap, but enough to tell whether a CLI click actually DID anything.
    ``selector`` is what the bd-12mo.1 resolver produced — a ``[data-ab-field="N"]`` tag or raw
    CSS (``browser_click`` refuses a ``@ref`` on the fallback path, so the in-page
    ``querySelector`` always has something resolvable to read ``aria-expanded`` from). Rides
    ``eval --stdin`` (#3689)."""
    return ("(function(){\nvar SEL = " + _js(selector) + ";\n"
            "return " + _FINGERPRINT_DRIVER.strip() + ";\n})()")


def js_click_js(selector: str) -> str:
    """The script ``browser_click`` evals for its JS fallback: resolve ``selector`` in the page
    and dispatch a bubbling ``mousedown``/``mouseup`` plus ``el.click()``. Returns
    ``{ok:true}``, or ``{ok:false, reason:'not-found'}`` when the element can't be resolved
    in-page (it vanished between the fingerprint and the dispatch). ``browser_click`` refuses a
    ``@ref`` before reaching here — a ref is invalid CSS in-page and would never resolve. Rides
    ``eval --stdin`` (#3689)."""
    return ("(function(){\nvar SEL = " + _js(selector) + ";\n"
            "return " + _JS_CLICK_DRIVER.strip() + ";\n})()")


def fingerprint_changed(before: str, after: str) -> bool:
    """True if two ``fingerprint_js`` outputs differ — i.e. the CLI click DID something. An
    empty or ``Error:`` fingerprint on EITHER side also returns True: "something happened, or
    we can't tell", which SUPPRESSES the fallback so a doubt never triggers a second click."""
    b, a = (before or "").strip(), (after or "").strip()
    if not b or not a or b.startswith("Error:") or a.startswith("Error:"):
        return True
    return b != a


def render_js_click(output: str, selector: str, cli_result: str) -> str:
    """Turn ``js_click_js``'s JSON into the result. A dispatched click reports
    ``Clicked <selector> (JS fallback)``; if the element could not be resolved in the page
    the CLI's own result stands (nothing better to say). Never raises."""
    try:
        data = _loads(output)
    except (ValueError, TypeError):
        return cli_result
    if isinstance(data, dict) and data.get("ok"):
        return f"Clicked {selector} (JS fallback)"
    return cli_result
