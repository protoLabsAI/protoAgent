"""Bot-wall and sign-in-wall detection — is the page we landed on the page we asked for?

``browser_open`` used to report ``✓ Just a moment...`` (a Cloudflare challenge's title) as a
successful navigation, so the agent went on to "read" an interstitial. This module answers
one question after a navigation: did a bot check, a block page or a sign-in wall answer
instead of the site? Nothing here tries to get past one — no solving, no evasion. A wall is
handed to the operator, who clears it in the console Browser panel.

Kept dependency-free (no langchain, no host imports) so another plugin can reuse it: run
:data:`PROBE_JS` with ``agent-browser eval --stdin``, feed the output to :func:`parse_probe`,
then :func:`classify` (or call :func:`check` with an already-parsed dict). The signals are
generalised from what has held up live in ebay-plugin (``/splashui/`` + "Pardon Our
Interruption" for its challenge, a ``signin.`` redirect for its sign-in wall).

Precision over recall: a false wall stops a working task, so each class needs a signal a
normal page doesn't carry — a vendor's challenge markup or title, a challenge iframe on a
page with almost nothing else, a 403/429 status, a redirect onto a login URL. A page that
merely says "sign in" (every site header) is not a wall.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

#: The one in-page read. Cheap (no layout beyond a few bounding boxes) and bounded: at most
#: 2,000 chars of text and 20 iframe srcs come back. Returns a JSON string.
PROBE_JS = r"""(() => {
  const vis = (el) => { try { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'; } catch (e) { return false; } };
  const body = document.body;
  const text = body ? (body.innerText || '') : '';
  const inputs = Array.from(document.querySelectorAll('input, textarea, select')).filter(vis);
  const pw = inputs.filter((i) => (i.type || '').toLowerCase() === 'password').length;
  const other = inputs.filter((i) => !['password', 'hidden', 'submit', 'button', 'checkbox', 'radio'].includes((i.type || '').toLowerCase())).length;
  const frames = Array.from(document.querySelectorAll('iframe')).slice(0, 20)
    .map((f) => ({ src: String(f.src || '').slice(0, 300), visible: vis(f) }));
  const has = (sel) => { try { return !!document.querySelector(sel); } catch (e) { return false; } };
  let status = 0;
  try { const n = performance.getEntriesByType('navigation')[0]; status = (n && n.responseStatus) || 0; } catch (e) {}
  return JSON.stringify({
    url: location.href, title: document.title || '', status,
    text: text.slice(0, 2000), text_len: text.length,
    password_fields: pw, other_fields: other,
    links: document.querySelectorAll('a[href]').length,
    frames,
    markers: {
      cf: has('#challenge-running, #challenge-form, #cf-challenge-running, .cf-browser-verification, #cf-wrapper #cf-error-details, form#challenge-form'),
      cf_cp: has('script[src*="/cdn-cgi/challenge-platform/"]'),
      px: has('#px-captcha, [id^="px-captcha"]'),
      dd: has('script[src*="captcha-delivery.com"], script[src*="datadome"]'),
    },
  });
})()"""

#: Kinds a few seconds can clear on their own (a JS challenge that redirects once it has
#: run). ``browser_open`` re-probes these before calling it a wall.
TRANSIENT = frozenset({"cloudflare", "browser_check"})

_LABEL = {
    "cloudflare": "a Cloudflare bot check",
    "cloudflare_block": "a Cloudflare block page",
    "browser_check": "a \"checking your browser\" bot check",
    "datadome": "a DataDome bot check",
    "perimeterx": "a PerimeterX (HUMAN) bot check",
    "akamai": "an Akamai access-denied page",
    "google_sorry": "Google's unusual-traffic page",
    "captcha": "a CAPTCHA challenge",
    "blocked_by_client": "the browser blocking the request (ERR_BLOCKED_BY_CLIENT)",
    "http_403": "an HTTP 403 Forbidden response",
    "http_429": "an HTTP 429 Too Many Requests response",
    "signin": "a sign-in wall",
}

_CF_TITLES = re.compile(r"^\s*(just a moment\.*|attention required!?(\s*\|\s*cloudflare)?|one more step)\s*$", re.I)
_CF_TEXT = re.compile(r"checking if the site connection is secure|verify(ing)? you are human|"
                      r"needs to review the security of your connection|ray id:", re.I)
_BROWSER_CHECK = re.compile(r"checking your browser( before accessing)?|ddos protection by|"
                            r"please wait while we verify|please stand by, while we are checking", re.I)
_PX_TEXT = re.compile(r"press\s*&\s*hold|access to this page has been denied", re.I)
_AKAMAI_TEXT = re.compile(r"you don'?t have permission to access .* on this server", re.I | re.S)
_GOOGLE_SORRY = re.compile(r"our systems have detected unusual traffic", re.I)
_CAPTCHA_SRC = re.compile(r"google\.com/recaptcha/|recaptcha\.net/recaptcha/|hcaptcha\.com/|"
                          r"challenges\.cloudflare\.com/", re.I)
_DATADOME_SRC = re.compile(r"captcha-delivery\.com", re.I)
#: Human-check pages that aren't a named vendor's: Amazon's robot check, Imperva's "Pardon Our
#: Interruption" (what ebay-plugin meets as /splashui/), generic "security check" pages.
_CAPTCHA_TEXT = re.compile(r"not a robot|enter the characters you see below|pardon our interruption|"
                           r"complete the security check|verify (that )?you are (a )?human", re.I)
_CAPTCHA_URL = re.compile(r"/errors/validatecaptcha|/splashui/(captcha|challenge)|/captcha(/|\?|$)", re.I)
#: A login URL: a path segment like /login, /signin, /sign-in, /sign_in, /auth/…/login,
#: /ap/signin (Amazon), /uas/login (LinkedIn), /accounts/login, or a well-known identity host.
_LOGIN_PATH = re.compile(r"(^|/)(log[-_]?in|sign[-_]?in|signon|sso|ap/signin|uas/login|"
                         r"checkpoint|authwall|session/new|users/sign_in|oauth2?/authorize)(/|$|\.|\?)", re.I)
_LOGIN_HOST = re.compile(r"^(login|signin|auth|sso|accounts|account|id|identity)\.", re.I)
_LOGIN_TITLE = re.compile(r"\b(sign[ -]?in|log[ -]?in|log on|sign on)\b", re.I)
_SIGNUP = re.compile(r"\b(sign[ -]?up|register|create (an |your )?account|join)\b", re.I)


@dataclass(frozen=True)
class Wall:
    """A detected wall. ``kind`` is a stable key (see ``_LABEL``); ``url`` where it was seen."""

    kind: str
    url: str
    detail: str = ""

    @property
    def label(self) -> str:
        return _LABEL.get(self.kind, self.kind)

    @property
    def transient(self) -> bool:
        return self.kind in TRANSIENT


def parse_probe(output: str) -> dict | None:
    """The dict :data:`PROBE_JS` returned, or None. Tolerates the CLI's extra string layer
    (a returned JSON string prints JSON-encoded — see ``forms._loads``)."""
    try:
        data = json.loads(output)
        if isinstance(data, str):
            data = json.loads(data)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _with_scheme(url: str) -> str:
    """``urlparse`` only fills ``hostname`` when the URL has a scheme: ``accounts.google.com``
    parses with ``hostname=None`` (the whole thing lands in ``path``), so an identity-host check
    never fires. Prepend ``https://`` when there's no scheme (``//host`` → ``https://host`` too),
    leaving already-schemed URLs — ``http:``, ``https:``, ``chrome-error:``, ``about:`` — alone."""
    u = (url or "").strip()
    if not u or u.startswith("//"):
        return "https:" + u if u else u
    return u if re.match(r"^[a-z][a-z0-9+.\-]*:", u, re.I) else "https://" + u


def is_login_url(url: str) -> bool:
    """True when ``url`` looks like a sign-in page (path or identity host)."""
    try:
        p = urlparse(_with_scheme(url))
    except ValueError:
        return False
    return bool(_LOGIN_PATH.search(p.path or "") or _LOGIN_HOST.match(p.hostname or ""))


def classify(probe: dict | None, requested_url: str = "") -> Wall | None:
    """Classify a parsed probe. ``requested_url`` is what the agent asked to open: landing
    on a login page you navigated to on purpose is not a wall; being REDIRECTED to one is.
    Returns None for a normal page (and for a probe that couldn't be read)."""
    if not isinstance(probe, dict):
        return None
    url = str(probe.get("url") or "")
    title = str(probe.get("title") or "").strip()
    text = str(probe.get("text") or "")
    text_len = int(probe.get("text_len") or len(text))
    markers = probe.get("markers") if isinstance(probe.get("markers"), dict) else {}
    frames = [f for f in (probe.get("frames") or []) if isinstance(f, dict)]
    status = int(probe.get("status") or 0)
    host = (urlparse(url).hostname or "").lower() if url else ""
    # "Sparse" = a page that is essentially just the interstitial: little text, few links.
    sparse = text_len < 1200 and int(probe.get("links") or 0) < 15

    # Cloudflare's /cdn-cgi/challenge-platform/ script (markers.cf_cp) is injected into ordinary,
    # successfully served 200 pages whenever Bot Fight Mode / JS Detections is on, and it stays in
    # the DOM — a sparse landing page, SPA shell or login page carries it with no challenge at all.
    # So it isn't a challenge signal on its own; trust it only alongside a block/deny status. A real
    # "Just a moment..." challenge is caught by its title and the #challenge-running id (markers.cf).
    cf = bool(markers.get("cf")) or (bool(markers.get("cf_cp")) and status in (403, 503))

    def wall(kind: str, detail: str = "") -> Wall:
        return Wall(kind, url, detail)

    if "ERR_BLOCKED_BY_CLIENT" in text or (url.startswith("chrome-error://") and "BLOCKED_BY_CLIENT" in title):
        return wall("blocked_by_client")

    # Cloudflare: the block page ("Attention Required!", error 1020) won't clear by waiting;
    # the JS challenge ("Just a moment...") usually does.
    if re.match(r"^\s*attention required", title, re.I):
        return wall("cloudflare_block", title)
    if (_CF_TITLES.match(title) and (sparse or cf)) or (cf and sparse) \
            or (sparse and _CF_TEXT.search(text) and (cf or _CF_TITLES.match(title))):
        return wall("cloudflare", title)
    if sparse and _BROWSER_CHECK.search(text + " " + title):
        return wall("browser_check", title)

    if any(_DATADOME_SRC.search(f.get("src", "")) for f in frames) or (markers.get("dd") and sparse and status in (403, 429)):
        return wall("datadome")
    if markers.get("px") or (sparse and _PX_TEXT.search(text + " " + title)):
        return wall("perimeterx")
    if (re.match(r"^\s*access denied\s*$", title, re.I) and _AKAMAI_TEXT.search(text)) \
            or ("errors.edgesuite.net" in text and sparse):
        return wall("akamai")
    if (re.match(r"^(www\.)?google\.[a-z.]+$", host) and urlparse(url).path.startswith("/sorry")) \
            or (sparse and _GOOGLE_SORRY.search(text)):
        return wall("google_sorry")
    # An embedded CAPTCHA on a contact form is NOT a wall — only one that IS the page: a
    # visible challenge frame on a sparse page. (Invisible reCAPTCHA v3 badges are skipped by
    # the visibility check, and they sit on content-rich pages anyway.)
    if sparse and (any(f.get("visible") and _CAPTCHA_SRC.search(f.get("src", "")) for f in frames)
                   or _CAPTCHA_TEXT.search(text + " " + title) or _CAPTCHA_URL.search(urlparse(url).path + "?")):
        return wall("captcha")

    if status == 429:
        return wall("http_429")
    if status == 403:
        return wall("http_403")

    # Sign-in walls. Opening a login page on purpose is the agent's choice, not a wall.
    if requested_url and is_login_url(requested_url):
        return None
    redirected = _different_page(requested_url, url)
    if url and is_login_url(url) and redirected:
        return wall("signin", "redirected to a sign-in page")
    # A password field dominating a short page. A sign-UP / register form has one too, so
    # the title must say sign-in (or, after a redirect, the page's opening text must).
    pw = int(probe.get("password_fields") or 0)
    other = int(probe.get("other_fields") or 0)
    if pw >= 1 and other <= 2 and text_len < 1500 and not _SIGNUP.search(title):
        if _LOGIN_TITLE.search(title) or (redirected and requested_url and _LOGIN_TITLE.search(text[:400])):
            return wall("signin", "the page is a sign-in form")
    return None


def _different_page(requested: str, landed: str) -> bool:
    """True when the browser ended up somewhere other than the requested page (a redirect).
    With no requested URL (unknown), a login URL alone counts."""
    if not requested:
        return True
    a, b = urlparse(_with_scheme(requested)), urlparse(_with_scheme(landed))
    return (a.hostname, a.path.rstrip("/")) != (b.hostname, b.path.rstrip("/"))


def check(probe_output: str, requested_url: str = "") -> Wall | None:
    """:func:`parse_probe` + :func:`classify` in one call — the reuse entry point."""
    return classify(parse_probe(probe_output), requested_url)


def classify_failure(error_text: str, url: str = "") -> Wall | None:
    """A FAILED navigation that is really a block: Chrome's ``net::ERR_BLOCKED_BY_CLIENT``
    (an extension / content blocker refused the request). Other failures stay as they are."""
    if "ERR_BLOCKED_BY_CLIENT" in (error_text or ""):
        return Wall("blocked_by_client", url)
    return None


def message(wall: Wall) -> str:
    """The tool result for a wall: ``Error: blocked by …`` (greppable, and it keeps the
    ``Error:`` failure contract every caller already checks), the URL, and the handoff."""
    where = f" at {wall.url[:300]}" if wall.url else ""
    if wall.kind == "blocked_by_client":
        return (f"Error: blocked by {wall.label}{where}. An extension or content blocker in this "
                "browser profile refused the request — it is not the site. Tell the user; don't retry "
                "the same URL.")
    if wall.kind == "signin":
        return (f"Error: blocked by {wall.label}{where} ({wall.detail}). Do not type credentials. "
                "Ask the user to sign in in the console Browser panel, then continue.")
    if wall.kind in ("http_403", "http_429", "cloudflare_block", "akamai"):
        return (f"Error: blocked by {wall.label}{where} — the site refused this browser. Don't retry in "
                "a loop. Ask the user to open it in the console Browser panel (and clear any check "
                "there), then continue.")
    return (f"Error: blocked by {wall.label}{where} — a human check is showing instead of the page. "
            "Do not try to solve it. Ask the user to clear it in the console Browser panel, then continue.")
