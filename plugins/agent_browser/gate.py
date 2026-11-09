"""The browser action gate: an operator approval before a consequential browser action.

The CLI's own ``--confirm-actions`` gates by VERB (every ``click``, every ``eval``) and, in the
orchestrator mode a subprocess wrapper runs it in, answers a gated command with a
``confirmation_required`` id that has to be confirmed within 60 s — which nothing here ever did,
so a gated action could only fail. And a verb is the wrong unit anyway: "click" is both the
cookie banner and the Submit button. Only something that resolves the TARGET can tell them
apart; careercoach's submit gate, which matches words in the tool's argument text, lets
``browser_click("@e42")`` on the real Submit button straight through.

So the gate lives here, in core, and decides on what the action will actually hit:

* **Categories** (``confirm_actions``, a comma list): ``submit`` (a form submission, or a control
  whose name commits something — submit / send / place order / pay / delete …; NOT a job board's
  "Apply now" that opens a form, NOT a search or filter), ``eval`` (any ``browser_eval``),
  ``upload`` (any ``browser_upload``), ``download`` (a link carrying ``download``). ``all``,
  ``none``. A MISSING key means the default, ``submit``; an empty value means off.
* **Target resolution** (the tools do the CLI calls; this module supplies the probe script and
  reads its answer). A CSS selector or a label is resolved in-page — one ``eval``. A ``@eN`` ref
  can't be (the CLI owns the ref map), so the tools ask the CLI for its box (``get box``) and
  probe ``elementFromPoint`` at the box centre — which is where the CLI's click lands
  (``interaction.rs::click`` dispatches the mouse at the box-model centre), so the probe names
  what the click will really hit. When the box doesn't line up with what's there (a ref inside
  an iframe, an overlay, off-screen), the tools fall back to the CLI's ``get text``/``get attr
  type`` on the ref and decide by name. Nothing is resolved — zero extra calls — unless a
  configured category (or a plugin policy) could apply to the verb.
* **Approval** is the shell tool's ``interrupt()`` approval card, in plain words ("Submit the
  form on boards.greenhouse.io — button “Submit application”"). Approve → the action runs;
  decline → an ``Error:`` telling the model not to retry. Where nobody could answer — a
  delegated subagent (a nested graph), or a turn that isn't an operator chat turn (scheduled,
  inbox, A2A, …) — the action is refused at once rather than parked on a card no one will see.
* **Plugin policies** (:func:`add_policy`, also offered as the ``agent_browser.add_gate_policy``
  service): a callback sees each resolved action + core's verdict and may allow, ask or deny it.

The policy half (classification, description, decisions) is pure and host-free; the approval
half imports langgraph lazily.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

log = logging.getLogger("protoagent.plugins.agent_browser")

SUBMIT, EVAL, UPLOAD, DOWNLOAD = "submit", "eval", "upload", "download"
CATEGORIES = (SUBMIT, EVAL, UPLOAD, DOWNLOAD)
DEFAULT_CATEGORIES = frozenset({SUBMIT})
_OFF_WORDS = frozenset({"none", "off", "false", "no", "0", "disabled"})
# Legacy ``--confirm-actions`` verb names that TRIGGER a submit (a click, or an Enter/Space
# keypress). The old flag gated by verb and, in this orchestrator, was never answerable — so no
# config relied on them working. Map these to ``submit`` so an operator who kept an old value
# still gets the submit gate; every other legacy verb (``nav``/``navigate``/``fill``) is unknown
# and falls back to the safe default (see :func:`parse_categories`).
_LEGACY_CATEGORIES = {"click": SUBMIT, "press": SUBMIT, "type": SUBMIT, "key": SUBMIT,
                      "enter": SUBMIT, "return": SUBMIT}

# Which categories can apply to which tool verb — the gate resolves a target only when one of
# these is configured (or a plugin policy asks for it), so an ungated verb pays nothing.
VERB_CATEGORIES = {
    "click": frozenset({SUBMIT, DOWNLOAD}),
    "press": frozenset({SUBMIT}),
    "type": frozenset({SUBMIT}),  # only when the text carries a newline (an Enter)
    "eval": frozenset({EVAL, SUBMIT}),
    "upload": frozenset({UPLOAD}),
}


def parse_categories(raw) -> frozenset[str]:
    """``confirm_actions`` → the set of gated categories.

    ``None`` (key absent) is the default (``submit``). Only an EXPLICIT off-word —
    ``""``/``none``/``off``/``false``/``no``/``0``/``disabled`` — turns the gate off; ``all`` is
    every category. A value that names no known category — a legacy CLI verb (``click``, ``nav``,
    ``navigate``), a typo, or a mix that resolves to nothing — FAILS CLOSED to the safe default
    (``submit``) with a warning, so an operator who opted into confirmation never silently loses
    all protection (including on real Submit buttons). Legacy submit-triggering verbs
    (``click``/``press``/``type``…) map to ``submit``; an unknown word inside an otherwise-valid
    list is warned about and dropped, but never empties the gate."""
    if raw is None:
        return DEFAULT_CATEGORIES
    if isinstance(raw, (list, tuple, set, frozenset)):
        words = [str(w) for w in raw]
    else:
        words = re.split(r"[,\s]+", str(raw))
    words = [w.strip().lower() for w in words if w and w.strip()]
    if not words or all(w in _OFF_WORDS for w in words):
        return frozenset()  # cleared / all off-words → the only way to disable the gate
    out: set[str] = set()
    legacy: list[str] = []
    unknown: list[str] = []
    for w in words:
        if w == "all":
            out.update(CATEGORIES)
        elif w in CATEGORIES:
            out.add(w)
        elif w == "default":
            out.update(DEFAULT_CATEGORIES)
        elif w in _LEGACY_CATEGORIES:
            out.add(_LEGACY_CATEGORIES[w])
            legacy.append(w)
        elif w not in _OFF_WORDS:
            unknown.append(w)
    if legacy:
        log.warning("[agent_browser] confirm_actions: legacy verb(s) %s now map to the 'submit' category",
                    ", ".join(repr(w) for w in legacy))
    if unknown:
        log.warning("[agent_browser] confirm_actions: ignoring unrecognised word(s) %s (known: %s, all, none)",
                    ", ".join(repr(w) for w in unknown), ", ".join(CATEGORIES))
    if not out:
        # The value named only unrecognised words (a legacy verb with no category, or a typo).
        # Fail closed to the default rather than silently leaving the gate off.
        log.warning("[agent_browser] confirm_actions=%r named no known category; failing closed to the "
                    "default (%s) so the gate stays on", raw, ", ".join(sorted(DEFAULT_CATEGORIES)))
        return DEFAULT_CATEGORIES
    return frozenset(out)


# ── the resolved target ──────────────────────────────────────────────────────────────────────


@dataclass
class Target:
    """What a click/press will act on, as the in-page probe (or the CLI fallback) saw it."""
    found: bool = False
    resolved: bool = True     # False: identified only by the ref's text/type (frame, overlay, off-screen)
    tag: str = ""
    type: str = ""
    role: str = ""
    name: str = ""
    submits: bool = False     # a submit control associated with a form (button default type, input[type=submit|image])
    form_action: str = ""
    form_method: str = ""
    form_search: bool = False
    implicit_submit: bool = False  # Enter in this field submits its form (focus probe only)
    default_button: str = ""       # that form's default button name
    multiline: bool = False        # textarea / contenteditable: Enter types a newline
    href: str = ""
    download: bool = False
    page_url: str = ""
    note: str = ""
    via: str = ""             # css | point | focus | fallback

    def to_dict(self) -> dict:
        return asdict(self)


def _s(v, n: int = 300) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def parse_probe(out: str, via: str = "") -> Target | None:
    """The probe's JSON line → a ``Target``; ``None`` when the output isn't the probe's answer."""
    text = (out or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, str):  # the CLI prints an eval's string result JSON-quoted
            data = json.loads(data)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    form = data.get("form") if isinstance(data.get("form"), dict) else {}
    return Target(
        found=bool(data.get("found")), resolved=bool(data.get("resolved", data.get("found"))),
        tag=_s(data.get("tag"), 40).lower(), type=_s(data.get("type"), 40).lower(),
        role=_s(data.get("role"), 40).lower(), name=_s(data.get("name"), 160),
        submits=bool(data.get("submits")),
        form_action=_s(form.get("action"), 500), form_method=_s(form.get("method"), 20).lower(),
        form_search=bool(form.get("search")),
        implicit_submit=bool(data.get("implicit_submit")), default_button=_s(data.get("default_button"), 160),
        multiline=bool(data.get("multiline")),
        href=_s(data.get("href"), 500), download=bool(data.get("download")),
        page_url=_s(data.get("page"), 500), note=_s(data.get("note"), 60), via=via,
    )


def fallback_target(text: str, type_attr: str, page_url: str = "", note: str = "") -> Target:
    """A ref the probe couldn't line up (frame / overlay / off-screen), known only by the CLI's
    ``get text`` and ``get attr type`` on it."""
    def clean(v: str) -> str:
        v = (v or "").strip()
        if v.startswith("Error:") or v in ("(ok)", "null"):
            return ""
        try:  # `get attr` prints JSON-ish values on some builds
            j = json.loads(v)
            v = j if isinstance(j, str) else v
        except ValueError:
            pass
        return v
    return Target(found=True, resolved=False, name=_s(clean(text), 160), type=_s(clean(type_attr), 40).lower(),
                  page_url=page_url, note=note or "unresolved", via="fallback")


# The probe. MODE is css (SEL), point (BOX from `get box`) or focus (document.activeElement).
# It walks up to the nearest interactive element (the CLI clicks a child <span> of a button just
# the same) across shadow roots, and reports the facts the policy needs. It never changes the
# page: no focus, no scroll, no events.
_PROBE_JS = r"""(() => {
  const MODE = __MODE__, SEL = __SEL__, BOX = __BOX__;
  const norm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().slice(0, 160);
  const up = (n) => n && (n.parentElement || (n.parentNode && n.parentNode.host) || null);
  const INTERACTIVE = 'button,a[href],input,select,textarea,summary,[role=button],[role=link],[role=menuitem],[role=tab],[role=option],[role=switch],[role=checkbox],[role=radio]';
  const interactive = (el) => { for (let n = el; n; n = up(n)) { if (n.nodeType === 1 && n.matches && n.matches(INTERACTIVE)) return n; } return el; };
  const deep = (x, y) => { let el = document.elementFromPoint(x, y); while (el && el.shadowRoot) { const i = el.shadowRoot.elementFromPoint(x, y); if (!i || i === el) break; el = i; } return el; };
  const out = { ok: true, found: false, resolved: false, page: location.href, note: '' };
  let el = null;
  if (MODE === 'css') {
    try { el = document.querySelector(SEL); } catch (e) { out.note = 'bad-selector'; return JSON.stringify(out); }
    if (!el) { out.note = 'not-found'; return JSON.stringify(out); }
  } else if (MODE === 'focus') {
    el = document.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
    if (!el || el === document.body || el === document.documentElement) { out.note = 'nothing-focused'; return JSON.stringify(out); }
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') { out.found = true; out.note = 'frame'; return JSON.stringify(out); }
  } else {
    const cx = BOX.x + BOX.width / 2, cy = BOX.y + BOX.height / 2;
    if (!(BOX.width > 0 && BOX.height > 0) || cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) { out.note = 'offscreen'; return JSON.stringify(out); }
    const hit = deep(cx, cy);
    const near = (r) => Math.abs(r.x - BOX.x) <= 2 && Math.abs(r.y - BOX.y) <= 2 && Math.abs(r.width - BOX.width) <= 2 && Math.abs(r.height - BOX.height) <= 2;
    for (let n = hit; n; n = up(n)) { if (n.nodeType === 1 && near(n.getBoundingClientRect())) { el = n; break; } }
    if (!el) { out.note = hit && (hit.tagName === 'IFRAME' || hit.tagName === 'FRAME') ? 'frame' : 'covered'; return JSON.stringify(out); }
  }
  const t = MODE === 'focus' ? el : interactive(el);
  const tag = t.tagName.toLowerCase();
  const typeAttr = (t.getAttribute('type') || '').toLowerCase();
  const type = tag === 'button' ? (['submit', 'reset', 'button'].includes(typeAttr) ? typeAttr : 'submit') : typeAttr;
  const ids = (v) => v.split(/\s+/).map((i) => { const n = document.getElementById(i); return n ? (n.innerText || n.textContent || '') : ''; }).join(' ');
  let name = t.getAttribute('aria-label') || (t.getAttribute('aria-labelledby') ? ids(t.getAttribute('aria-labelledby')) : '');
  if (!name && tag === 'input' && ['submit', 'button', 'reset', 'image'].includes(type)) name = t.value || t.getAttribute('alt') || (type === 'submit' ? 'Submit' : '');
  if (!name && t.labels && t.labels.length) name = t.labels[0].innerText || t.labels[0].textContent;
  if (!name && tag !== 'input' && tag !== 'textarea' && tag !== 'select') name = t.innerText || t.textContent;
  if (!name) { const img = t.querySelector && t.querySelector('img[alt]'); name = t.getAttribute('title') || t.getAttribute('placeholder') || (img ? img.alt : '') || t.getAttribute('name') || ''; }
  const assoc = ('form' in t) ? t.form : null;
  const submits = !!assoc && ((tag === 'button' && type === 'submit') || (tag === 'input' && (type === 'submit' || type === 'image')));
  const f = assoc || (t.closest ? t.closest('form') : null);
  let form = null;
  if (f) {
    const action = submits && t.hasAttribute('formaction') ? t.formAction : f.action;
    const method = submits && t.hasAttribute('formmethod') ? t.formMethod : f.method;
    const search = (f.getAttribute('role') || '').toLowerCase() === 'search' || !!(f.closest && f.closest('[role=search]')) ||
      !!f.querySelector('input[type=search]') || /search/i.test(f.getAttribute('action') || '');
    form = { action: action || '', method: (method || 'get').toLowerCase(), search };
  }
  const isBtn = (e) => (e.tagName === 'BUTTON' && e.type === 'submit') || (e.tagName === 'INPUT' && (e.type === 'submit' || e.type === 'image'));
  const TEXTISH = ['', 'text', 'email', 'search', 'tel', 'url', 'password', 'number', 'date', 'datetime-local', 'month', 'week', 'time'];
  let implicit = false, defName = '';
  if (assoc && tag === 'input' && TEXTISH.includes(typeAttr)) {
    const els = Array.from(assoc.elements || []);
    const def = els.find(isBtn);
    const singles = els.filter((e) => e.tagName === 'INPUT' && TEXTISH.includes((e.getAttribute('type') || '').toLowerCase()));
    if (def && !def.disabled) { implicit = true; defName = def.getAttribute('aria-label') || (def.tagName === 'INPUT' ? (def.value || 'Submit') : (def.innerText || def.textContent)); }
    else if (!def && singles.length === 1) implicit = true;
  }
  Object.assign(out, {
    found: true, resolved: true, tag, type, role: (t.getAttribute('role') || '').toLowerCase(), name: norm(name),
    submits, form, implicit_submit: implicit, default_button: norm(defName),
    multiline: tag === 'textarea' || !!t.isContentEditable,
    href: tag === 'a' ? (t.href || '') : '', download: tag === 'a' && t.hasAttribute('download'),
  });
  return JSON.stringify(out);
})()"""


def probe_js(mode: str, *, selector: str = "", box: dict | None = None) -> str:
    """The in-page probe for ``mode`` (``css`` / ``point`` / ``focus``). Values are JSON-embedded."""
    b = {k: float((box or {}).get(k) or 0) for k in ("x", "y", "width", "height")}
    return (_PROBE_JS.replace("__MODE__", json.dumps(mode))
            .replace("__SEL__", json.dumps(selector or ""))
            .replace("__BOX__", json.dumps(b)))


def parse_box(out: str) -> dict | None:
    """``agent-browser --json get box`` → ``{x, y, width, height}`` or ``None``."""
    try:
        data = json.loads((out or "").strip())
    except (TypeError, ValueError):
        return None
    box = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(box, dict) or not all(k in box for k in ("x", "y", "width", "height")):
        return None
    try:
        return {k: float(box[k]) for k in ("x", "y", "width", "height")}
    except (TypeError, ValueError):
        return None


# ── the policy ───────────────────────────────────────────────────────────────────────────────
# Names that COMMIT something on the user's behalf. Careercoach's well-tested send verbs
# (submit / send application / complete application / finish) plus the other irreversible ones.
# Deliberately NO bare "apply": on a job board "Apply" / "Apply now" OPENS the form, and "Apply
# filters" is a listing control.
_COMMIT = re.compile(
    r"\bsubmit\b|\bsend\b|\bfinish\b"
    r"|\bcomplete\s+(?:your\s+|my\s+|the\s+)?(?:application|order|purchase|payment|booking|checkout|registration)\b"
    r"|\bplace\s+(?:your\s+|my\s+|the\s+)?order\b|\bpay\b|\bpurchase\b|\bbuy\b"
    r"|\bconfirm\b|\bdelete\b|\bpublish\b|\bpost\b|\bdonate\b|\btransfer\b|\breserve\b"
    r"|\bbook\s+(?:now|it|this|appointment|flight|room|ticket)s?\b"
    r"|\bsign\s+up\b|\bcreate\s+(?:an?\s+|my\s+)?account\b|\bregister\b"
    r"|\bunsubscribe\b|\bcancel\s+(?:my\s+|your\s+|the\s+)?(?:subscription|order|membership|account|booking)\b",
    re.IGNORECASE,
)
# Whole names that are navigation, search, sign-in or housekeeping — never a commitment, even
# when the control happens to submit a form (a filter panel's "Apply", a login form, a wizard's
# "Next"). Matched against the WHOLE normalised name, so "Submit search" isn't one.
_BENIGN = re.compile(
    r"^(?:apply|apply now|easy apply|quick apply|apply (?:for|to) (?:this|the) (?:job|role|position)|apply on company (?:site|website)"
    r"|apply filters?|filters?|filter results|search|search jobs|find|find jobs|go|sort|refine|update results"
    r"|sign in|log ?in|login|next|continue|back|previous|cancel|close|done|ok|got it|dismiss"
    r"|save|save draft|save (?:and|&) continue|accept|accept all|accept cookies|reject all|allow all|agree"
    r"|accept (?:and|&) continue|confirm (?:my |your )?(?:choices|selection|preferences)|save (?:my )?(?:choices|preferences|settings)"
    r"|load more|show more|see more|view|view all|view job|details|clear|reset|"
    r"(?:re)?send (?:me )?(?:a |the |my )?(?:code|link|verification code|otp|magic link|one-time code))$",
    re.IGNORECASE,
)
_ENTER_KEYS = frozenset({"enter", "return", "numpadenter"})
_SPACE_KEYS = frozenset({"space", " ", "spacebar"})
_CLICKY_ROLES = frozenset({"button", "link", "menuitem", "tab", "option", "switch"})


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s&'-]+", " ", name or "")).strip().lower()


def is_benign_name(name: str) -> bool:
    return bool(_BENIGN.match(_norm_name(name)))


def is_commit_name(name: str) -> bool:
    return bool(_COMMIT.search(name or "")) and not is_benign_name(name)


@dataclass
class Verdict:
    """Core's reading of one action: the categories it falls in (empty = nothing to gate), why,
    and the one-line, human description the approval card leads with."""
    categories: frozenset = field(default_factory=frozenset)
    reason: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return {"categories": sorted(self.categories), "reason": self.reason, "description": self.description}


def host_of(url: str) -> str:
    try:
        return urlparse(url or "").hostname or ""
    except ValueError:
        return ""


def _where(t: Target | None, page_url: str = "") -> str:
    host = host_of((t.page_url if t else "") or page_url)
    return f" on {host}" if host else ""


def _control(t: Target) -> str:
    kind = "link" if (t.tag == "a" or t.role == "link") else "field" if t.tag in ("input", "textarea", "select") and not t.submits else "button"
    return f"{kind} “{t.name}”" if t.name else f"an unlabelled {kind}"


def _submit_desc(t: Target, *, lead: str) -> str:
    where = _where(t)
    dest = host_of(t.form_action)
    to = f", sending it to {dest}" if dest and dest != host_of(t.page_url) else ""
    return f"{lead}{where} — {_control(t)}{to}"


def classify_click(t: Target | None) -> Verdict:
    """What clicking ``t`` would do. A form submission counts unless its name or its form says
    it's a search/filter/navigation; a GET form is a query, not a commitment. A control whose name
    commits something counts even with no form (a React "Place order" div)."""
    if t is None or not t.found:
        return Verdict()
    if t.download:
        return Verdict(frozenset({DOWNLOAD}), "a download link",
                       f"Download a file{_where(t)} — {_control(t)}" + (f" ({t.href})" if t.href else ""))
    if is_benign_name(t.name):
        return Verdict(reason=f"“{t.name}” reads as navigation, search or sign-in")
    if t.form_search and (t.submits or is_commit_name(t.name)):
        return Verdict(reason="a search form")
    if is_commit_name(t.name):
        reason = f"its name “{t.name}” commits something"
        if t.submits:
            return Verdict(frozenset({SUBMIT}), reason, _submit_desc(t, lead="Submit the form"))
        return Verdict(frozenset({SUBMIT}), reason,
                       f"Click the {_control(t)}{_where(t)}, which looks like it sends or commits something")
    if t.submits or (not t.resolved and t.type == "submit"):
        if t.form_method == "get":
            return Verdict(reason="a GET form (a query, not a submission)")
        return Verdict(frozenset({SUBMIT}), "it submits a form", _submit_desc(t, lead="Submit the form"))
    return Verdict()


def _key_name(key: str) -> str:
    if key == " ":
        return "space"
    k = (key or "").strip().lower()
    return k.rsplit("+", 1)[-1] if len(k) > 1 else k  # a chord: Control+Enter → enter


def is_activation_key(key: str) -> bool:
    """Enter / Space (alone or as a chord's last key) — the keys that can activate or submit."""
    k = _key_name(key)
    return k in _ENTER_KEYS or k in _SPACE_KEYS


def classify_press(key: str, t: Target | None) -> Verdict:
    """What pressing ``key`` on the focused element ``t`` would do: Enter/Space on a button or
    link is a click; Enter in a single-line field submits its form when the form has a default
    button (or is a lone field) — HTML's implicit submission."""
    if t is None or not t.found:
        return Verdict()
    k = _key_name(key)
    enter, space = k in _ENTER_KEYS, k in _SPACE_KEYS
    if not (enter or space):
        return Verdict()
    # Focus is inside an IFRAME/FRAME the probe can't see into (``found`` but nothing read back —
    # no tag, no form). An Enter/Space there could submit an embedded form and we can't tell, so
    # fail closed: ask for approval rather than let it through unseen.
    if t.note == "frame" and not t.tag:
        return Verdict(frozenset({SUBMIT}), "focus is inside a frame the gate can't read into",
                       f"Press {'Enter' if enter else 'Space'} inside an embedded frame{_where(t)}, "
                       f"which could submit a form")
    clicky = t.tag in ("button", "summary") or (t.tag == "input" and t.type in ("submit", "image", "button", "reset")) \
        or t.role in _CLICKY_ROLES or (enter and t.tag == "a")
    if clicky:
        v = classify_click(t)
        if v.categories:
            v.description = f"Press {'Enter' if enter else 'Space'} on {v.description[0].lower()}{v.description[1:]}"
        return v
    if enter and t.implicit_submit and not t.multiline:
        btn = Target(**{**t.to_dict(), "name": t.default_button, "submits": True, "tag": "button", "role": ""})
        v = classify_click(btn)
        if v.categories:
            field_name = f"the “{t.name}” field" if t.name else "a field"
            via = f" (its button “{t.default_button}”)" if t.default_button else ""
            v.description = f"Press Enter in {field_name}, which submits the form{_where(t)}{via}"
        return v
    return Verdict()


def classify_type(text: str, t: Target | None) -> Verdict:
    """Typing text that contains a newline presses Enter in the field."""
    if "\n" not in (text or "") and "\r" not in (text or ""):
        return Verdict()
    return classify_press("Enter", t)


def _looks_like_js_submit(script: str) -> bool:
    """Careercoach's eval heuristic: ``.submit(`` / ``requestSubmit``, or a ``.click()`` aimed at
    a submit-ish control."""
    low = (script or "").lower()
    if ".submit(" in low or "requestsubmit" in low:
        return True
    return ".click(" in low and (bool(_COMMIT.search(script)) or bool(re.search(r"""type\s*=\s*["']?\s*submit""", script, re.I)))


def classify_eval(expression: str, page_url: str = "") -> Verdict:
    cats = {EVAL}
    if _looks_like_js_submit(expression):
        cats.add(SUBMIT)
    code = (expression or "").strip()
    excerpt = code if len(code) <= 240 else code[:240] + " …"
    lead = "Run JavaScript that looks like it submits a form" if SUBMIT in cats else "Run JavaScript"
    host = host_of(page_url)
    return Verdict(frozenset(cats), "arbitrary JavaScript in the page",
                   f"{lead}{f' on {host}' if host else ''}: {excerpt}")


def classify_upload(field: str, file_name: str, page_url: str = "") -> Verdict:
    host = host_of(page_url)
    return Verdict(frozenset({UPLOAD}), "a local file leaves this machine",
                   f"Attach {file_name} to the “{field}” field{f' on {host}' if host else ''}")


# ── decisions + plugin policies ──────────────────────────────────────────────────────────────

ALLOW, ASK, DENY = "allow", "ask", "deny"


@dataclass
class Decision:
    kind: str                 # allow | ask | deny
    reason: str = ""
    description: str = ""
    category: str = ""
    by: str = "core"


@dataclass
class _Policy:
    fn: object
    categories: frozenset
    name: str


_POLICIES: list[_Policy] = []
_POLICIES_LOCK = threading.Lock()


def add_policy(fn, categories=(SUBMIT,), name: str = ""):
    """Layer a plugin's own rule over the gate. Returns a function that removes it.

    ``fn(action: dict) -> dict | None`` runs inside the browser tool, after core resolved the
    target, for every action whose verb can fall in one of ``categories`` — whether or not the
    operator gates those categories (so a plugin keeps its rule when ``confirm_actions`` is off;
    registering is what makes the tools resolve targets at all on such an install).

    ``action`` is plain data: ``{"tool", "verb", "selector", "key", "page_url", "target": {...}
    | None, "verdict": {"categories", "reason", "description"}, "gated": [categories the operator
    gates that this action falls in]}``. Return ``None`` for no opinion, or
    ``{"decision": "allow" | "ask" | "deny", "reason": "...", "description": "..."}``:
    ``allow`` skips core's card (e.g. the operator already approved this exact submit through the
    plugin's own card), ``ask`` raises the card even if core wouldn't, ``deny`` refuses with
    ``reason``. Deny beats ask beats allow across policies; an exception is logged and ignored."""
    p = _Policy(fn=fn, categories=frozenset(categories or ()) or frozenset(CATEGORIES),
                name=name or getattr(fn, "__qualname__", "policy"))
    with _POLICIES_LOCK:
        _POLICIES.append(p)

    def remove() -> None:
        with _POLICIES_LOCK:
            if p in _POLICIES:
                _POLICIES.remove(p)
    return remove


def policy_categories() -> frozenset[str]:
    """Every category some plugin policy wants resolved."""
    with _POLICIES_LOCK:
        return frozenset().union(*(p.categories for p in _POLICIES)) if _POLICIES else frozenset()


def describe_target(target) -> str:
    """A plain-words name for a resolved target (a ``Target`` or its dict), e.g. ``button
    “Submit application” on boards.greenhouse.io``. Public: plugins label things the same way."""
    t = target if isinstance(target, Target) else Target(**{k: v for k, v in dict(target or {}).items()
                                                            if k in Target.__dataclass_fields__})
    if not t.found:
        return "nothing identifiable"
    return f"{_control(t)}{_where(t)}"


def decide(gated: frozenset[str], action: dict, verdict: Verdict) -> Decision:
    """Core's verdict, then each matching plugin policy. ``gated`` = operator categories."""
    hit = sorted(verdict.categories & gated)
    decision = Decision(ASK if hit else ALLOW, verdict.reason, verdict.description, hit[0] if hit else "")
    with _POLICIES_LOCK:
        policies = list(_POLICIES)
    if not policies:
        return decision
    payload = {**action, "verdict": verdict.to_dict(), "gated": hit}
    rank = {ALLOW: 0, ASK: 1, DENY: 2}
    votes: list[Decision] = []
    for p in policies:
        if not (p.categories & VERB_CATEGORIES.get(action.get("verb", ""), frozenset())):
            continue
        try:
            got = p.fn(payload)
        except Exception:  # noqa: BLE001 — a plugin's bug must not break (or open) the gate
            log.exception("[agent_browser] gate policy %s failed; ignoring it", p.name)
            continue
        if not isinstance(got, dict) or got.get("decision") not in rank:
            continue
        votes.append(Decision(got["decision"], str(got.get("reason") or ""),
                              str(got.get("description") or "") or verdict.description,
                              hit[0] if hit else (sorted(verdict.categories)[0] if verdict.categories else ""), p.name))
    if not votes:
        return decision
    top = max(votes, key=lambda d: rank[d.kind])
    if top.kind == ALLOW:
        log.info("[agent_browser] gate: %s allowed by policy %s (%s)", verdict.description or action.get("verb"),
                 top.by, top.reason)
    if top.kind == ASK and not top.description:
        top.description = f"{action.get('verb', 'act')} {action.get('selector') or action.get('key') or ''}".strip()
    return top


# ── approval (the shell tool's interrupt card) ───────────────────────────────────────────────

# What an autonomous turn's driver resumes an interrupt with (server/turn_control.py
# _AUTONOMOUS_HITL_SENTINEL); matched by prefix, since a plugin can't import server/.
_NO_OPERATOR_PREFIX = "[no interactive operator available]"
_APPROVE_WORDS = frozenset({"approve", "approved", "allow", "allowed", "yes", "y", "true", "ok"})


def approved(answer) -> bool:
    """Default-deny parse of an interrupt answer (bool, word, or ``{approved|decision|choice|value}``)."""
    if isinstance(answer, bool):
        return answer
    if isinstance(answer, dict):
        if isinstance(answer.get("approved"), bool):
            return answer["approved"]
        for k in ("decision", "choice", "value", "answer", "action"):
            if answer.get(k) not in (None, ""):
                answer = answer[k]
                break
        else:
            return False
        if isinstance(answer, bool):
            return answer
    return str(answer or "").strip().lower() in _APPROVE_WORDS


def interrupt_context(state) -> tuple[bool, str, str]:
    """``(can_ask, why_not, key)``. An approval card can only be answered on an operator chat
    turn of the LEAD graph: the server stamps ``operator_chat_turn`` from the authenticated tier
    + the turn's origin on every pass (the same check ``record_directive`` makes), a subagent's
    state has no stamp, and a subagent's tool runs in a NESTED graph (``checkpoint_ns`` holds a
    ``|``) whose interrupt the lead's runner can't resume. ``key`` names this tool call across the
    resume, so the card that was shown can be checked against what's there now."""
    try:
        from langgraph.config import get_config
        configurable = (get_config() or {}).get("configurable") or {}
    except Exception:  # noqa: BLE001 — outside a graph run: nobody to ask
        return False, "no-graph", ""
    ns = str(configurable.get("checkpoint_ns") or "")
    if "|" in ns or (isinstance(state, dict) and state.get("subagent_fence")):
        return False, "subagent", ""
    if not isinstance(state, dict) or state.get("operator_chat_turn") is not True:
        return False, "unattended", ""
    thread = str(configurable.get("thread_id") or "")
    if not thread:
        return False, "no-graph", ""
    return True, "", f"{thread}|{ns}"


_WHY = {
    "subagent": "this step runs as a delegated subagent, which can't show the operator an approval card",
    "unattended": "this turn isn't an operator chat turn (a scheduled, inbox, background, or remote/A2A turn), "
                  "so nobody can approve it",
    "no-graph": "there is no chat turn to show an approval card in",
    "no-operator": "no operator is available to approve it (this turn is running autonomously)",
}


def refusal(desc: str, why: str, category: str) -> str:
    # A subagent (a task() delegation or a workflow step on the `browser` worker) answers to the
    # lead or the workflow, not to the user: it should end its step and say so, and the lead —
    # on an operator chat turn — can take the action itself and get the approval card.
    handoff = ("End this step here and report that the page is ready and this action is waiting for the "
               "operator, who can do or approve it from chat."
               if why == "subagent" else
               "Leave the page as it is and tell the user it's ready for them to do or approve from chat.")
    return (f"Error: blocked by the browser action gate ({category}) — {desc}. This needs the operator's "
            f"approval and {_WHY.get(why, why)}. Don't retry it or work around it (no JS submit, no Enter key). "
            f"{handoff}")


def declined(desc: str) -> str:
    return (f"Error: the operator declined this browser action — not done: {desc}. Don't retry it or try "
            f"another way; tell the user it wasn't done and ask how they'd like to proceed.")


def denied_by_policy(desc: str, reason: str) -> str:
    text = reason or "blocked by a browser gate policy"
    return f"Error: {text}" + (f" ({desc})" if desc and desc not in text else "")


def approval_card(decision: Decision, *, tool: str, selector: str = "", page_url: str = "") -> dict:
    titles = {SUBMIT: "Let the agent submit this?", EVAL: "Let the agent run JavaScript in the page?",
              UPLOAD: "Let the agent upload this file?", DOWNLOAD: "Let the agent download this file?"}
    lines = [f"Tool: {tool}" + (f" {selector}" if selector else "")]
    if page_url:
        lines.append(f"Page: {page_url}")
    if decision.reason:
        lines.append(f"Why you're being asked: {decision.reason}"
                     + (f" (gate: {decision.category})" if decision.category else ""))
    return {
        "kind": "approval",
        "type": "agent_browser_action_gate",
        "title": titles.get(decision.category, "Approve this browser action?"),
        "description": decision.description,
        "detail": "\n".join(lines),
        "options": [{"label": "Allow", "value": "approve", "primary": True},
                    {"label": "Don't allow", "value": "deny"}],
        "session_allow": False,
        "category": decision.category,
    }


# The description shown on each pending card, keyed by thread + checkpoint namespace (stable
# across the resume of one tool call). The tool re-runs from the top on resume and re-resolves
# the target, so the approval is honoured only if the page still shows the SAME thing.
_PENDING: dict[str, tuple[str, float]] = {}
_PENDING_LOCK = threading.Lock()
_PENDING_TTL_S = 6 * 3600.0


def ask(decision: Decision, state, *, tool: str, selector: str = "", page_url: str = "") -> str | None:
    """Raise the approval card (or refuse where none can be answered). ``None`` = approved, run
    the action; otherwise the ``Error:`` to return. ``GraphInterrupt`` propagates (it IS the pause)."""
    can, why, key = interrupt_context(state)
    if not can:
        log.info("[agent_browser] gate refused (%s): %s", why, decision.description)
        return refusal(decision.description, why, decision.category)
    now = time.monotonic()
    with _PENDING_LOCK:
        for k in [k for k, (_d, at) in _PENDING.items() if now - at > _PENDING_TTL_S]:
            _PENDING.pop(k, None)
        shown = _PENDING.setdefault(key, (decision.description, now))[0]
    from langgraph.types import interrupt

    answer = interrupt(approval_card(decision, tool=tool, selector=selector, page_url=page_url))
    with _PENDING_LOCK:
        _PENDING.pop(key, None)
    if isinstance(answer, str) and answer.strip().startswith(_NO_OPERATOR_PREFIX):
        return refusal(decision.description, "no-operator", decision.category)
    if not approved(answer):
        log.info("[agent_browser] gate: operator declined: %s", decision.description)
        return declined(decision.description)
    if shown != decision.description:
        return (f"Error: the page changed while the approval card was open — you approved “{shown}”, but the "
                f"target is now “{decision.description}”. Nothing was done. Take a fresh browser_snapshot and "
                f"try again; the operator will be asked again.")
    log.warning("[agent_browser] gate: operator APPROVED: %s", decision.description)
    return None
