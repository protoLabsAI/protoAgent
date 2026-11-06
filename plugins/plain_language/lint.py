"""A deterministic plain-language checker.

Many plain-language rules are mechanical, and those are the ones a model drifts on.
This module flags them without a model call, so the agent (via the
``plain_language_check`` tool) and the eval (``eval/ab.py``) score the same way.

It is a heuristic linter, not a grammar parser: a finding is a place to look, not
proof of a fault. Severity is ``major`` (almost always worth fixing) or ``minor``
(fix unless there is a reason). Code spans, fenced code blocks and URLs are skipped,
so commands and identifiers are never "corrected".

Rules come from digital.gov's plain language guide
(https://digital.gov/guides/plain-language). Where the guide gives a number
(paragraphs of 150 words or less, never over 250; at most three heading levels;
two or three abbreviations) the check uses it. The sentence-length limits are this
plugin's own heuristic: the guide says "one idea per sentence" but sets no number.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# ── word lists ────────────────────────────────────────────────────────────────

# Complex word → plain alternative. From the guide's familiar-terms and style pages,
# plus the common wordy phrases its "short and simple" page targets.
SUBSTITUTIONS: dict[str, str] = {
    "utilize": "use",
    "utilizes": "uses",
    "utilized": "used",
    "utilizing": "using",
    "utilization": "use",
    "commence": "start",
    "commences": "starts",
    "commenced": "started",
    "facilitate": "help",
    "facilitates": "helps",
    "leverage": "use",
    "leverages": "uses",
    "leveraging": "using",
    "promulgate": "issue",
    "endeavor": "try",
    "ascertain": "find out",
    "subsequently": "later",
    "prior to": "before",
    "in order to": "to",
    "in order that": "so",
    "in the event of": "if",
    "in the event that": "if",
    "in the amount of": "for",
    "with regard to": "about",
    "with respect to": "about",
    "in regard to": "about",
    "is able to": "can",
    "are able to": "can",
    "on a daily basis": "daily",
    "on a weekly basis": "weekly",
    "on a monthly basis": "monthly",
    "at this point in time": "now",
    "at the present time": "now",
    "due to the fact that": "because",
    "owing to the fact that": "because",
    "in light of the fact that": "because",
    "despite the fact that": "although",
    "a number of": "several, many, or a few",
    "the majority of": "most",
    "in close proximity to": "near",
    "for the purpose of": "to, for",
    "has the ability to": "can",
    "have the ability to": "can",
    "it is important to note that": "(cut it)",
    "it should be noted that": "(cut it)",
    "please note that": "(cut it)",
    "addressee": "you",
}

# Modifiers that add length without meaning (the guide's short-and-simple page).
FILLER = (
    "very",
    "really",
    "actually",
    "basically",
    "essentially",
    "totally",
    "completely",
    "absolutely",
    "quite",
    "simply",
    "literally",
    "extremely",
    "incredibly",
)

# Pairs that say one thing twice (the guide's doublets).
DOUBLETS = {
    "due and payable": "due",
    "cease and desist": "stop",
    "null and void": "void",
    "each and every": "each, or every",
    "first and foremost": "first",
    "any and all": "any, or all",
    "full and complete": "complete",
    "true and accurate": "accurate",
    "terms and conditions": "terms",
}

NEGATIVES = {
    "no fewer than": "at least",
    "not less than": "at least",
    "no later than": "by",
    "unless and until": "until",
    "not unlike": "like",
}

# Abbreviations nearly every reader of this agent knows. They are never counted
# against the guide's "two or three abbreviations" limit.
COMMON_ABBREVIATIONS = frozenset(
    """
    A2A ADR AI AM API APIS AWS CEO CI CLI CPU CSS CSV DB DM DNS EU FAQ FBI GB GPU GUI
    HTML HTTP HTTPS ID IDS IDE IP JSON JWT KB LLM LLMS MB MCP OK OS PDF PM PR PRS QA QR
    RAM REST SDK SMS SQL SSH SSO TB TLS TODO TV UI UK UN URL URLS US USA USB UTC UX VPN
    XML YAML ZIP ASAP FYI ETA RSS SVG PNG JPG GIF CSV UUID OAUTH LAN WIFI CD PST PT ET
    """.split()
)

_HIDDEN_VERB_LEAD = (
    r"make|makes|made|making|take|takes|took|taken|taking|give|gives|gave|given|giving|"
    r"conduct|conducts|conducted|conducting|perform|performs|performed|performing|"
    r"provide|provides|provided|providing|achieve|achieves|achieved|reach|reaches|reached|"
    r"effect|effects|effected|carry out|carries out|carried out|undertake|undertakes|undertook"
)
_NOMINAL = r"[a-z]+(?:tion|sion|ment|ance|ence|sis|ure|al)"
_HIDDEN_VERB_RE = re.compile(rf"\b(?:{_HIDDEN_VERB_LEAD})\s+(?:a|an|the)\s+(?:\w+\s+)?({_NOMINAL})\b", re.I)
_THE_X_OF_RE = re.compile(r"\bthe\s+([a-z]+(?:tion|sion|ment|ance))\s+of\b", re.I)

_IRREGULAR_PARTICIPLES = (
    "made|done|built|sent|set|run|given|taken|found|shown|known|held|kept|paid|put|"
    "read|seen|told|written|chosen|drawn|driven|broken|spoken|stolen|thrown|hidden|"
    "begun|brought|bought|caught|taught|thought|left|lost|meant|met|sold|sought|spent|"
    "understood|won|withdrawn|forgotten|overridden|rewritten"
)
_PASSIVE_RE = re.compile(
    rf"\b(?:am|is|are|was|were|be|been|being|get|gets|got|gotten)\s+(?:\w+ly\s+)?"
    rf"((?:[a-z]+ed)|(?:{_IRREGULAR_PARTICIPLES}))\b(?!\s+(?:to|that)\b)",
    re.I,
)
# "-ed" words that are adjectives far more often than participles in a passive.
_PASSIVE_ADJECTIVES = frozenset(
    "based tired interested worried concerned excited bored pleased supposed used limited "
    "advanced detailed experienced related married dedicated complicated sophisticated "
    "skilled talented qualified balanced aged naked wicked sacred beloved crooked rugged "
    "need needed red bed shed hundred embed embedded enabled disabled "
    "missed stuck focused".split()
)

_ABBREV_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,5}s?\b")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")

# ── data ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str  # "major" | "minor"
    line: int
    excerpt: str
    suggestion: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    findings: list[Finding]
    words: int
    sentences: int
    paragraphs: int

    @property
    def major(self) -> int:
        return sum(1 for f in self.findings if f.severity == "major")

    @property
    def minor(self) -> int:
        return sum(1 for f in self.findings if f.severity == "minor")

    @property
    def per_100_words(self) -> float:
        """Weighted findings per 100 words (major counts 2, minor 1). Lower is plainer."""
        if not self.words:
            return 0.0
        return round((2 * self.major + self.minor) * 100 / self.words, 2)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.rule] = out.get(f.rule, 0) + 1
        return dict(sorted(out.items()))

    def as_dict(self) -> dict:
        return {
            "words": self.words,
            "sentences": self.sentences,
            "paragraphs": self.paragraphs,
            "major": self.major,
            "minor": self.minor,
            "per_100_words": self.per_100_words,
            "counts": self.counts(),
            "findings": [f.as_dict() for f in self.findings],
        }


# ── text preparation ─────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """``(line number, text)`` for every prose line: fenced code dropped, inline code
    and URLs blanked (so offsets stay put), link targets removed but link text kept."""
    out: list[tuple[int, str]] = []
    in_fence = False
    for i, raw in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), raw)
        line = _MD_LINK_RE.sub(lambda m: m.group(1), line)
        line = _URL_RE.sub(lambda m: " " * len(m.group(0)), line)
        out.append((i, line))
    return out


def _excerpt(line: str, start: int, end: int, pad: int = 25) -> str:
    a = max(0, start - pad)
    b = min(len(line), end + pad)
    snippet = " ".join(line[a:b].split())
    return ("…" if a > 0 else "") + snippet + ("…" if b < len(line) else "")


_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_RE = re.compile(r"^\s*\|")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(\[])")


def _blocks(lines: list[tuple[int, str]]) -> list[tuple[str, int, str]]:
    """Group prose lines into ``(kind, first line, text)`` blocks: heading, list item,
    table row, or paragraph (consecutive non-blank prose lines)."""
    blocks: list[tuple[str, int, str]] = []
    para: list[str] = []
    para_start = 0

    def flush() -> None:
        nonlocal para
        if para:
            blocks.append(("para", para_start, " ".join(s.strip() for s in para)))
            para = []

    for n, line in lines:
        if not line.strip():
            flush()
            continue
        if _HEADING_RE.match(line):
            flush()
            blocks.append(("heading", n, line))
        elif _LIST_RE.match(line):
            flush()
            blocks.append(("list", n, _LIST_RE.sub("", line, count=1)))
        elif _TABLE_RE.match(line):
            flush()
            blocks.append(("table", n, line))
        else:
            if not para:
                para_start = n
            para.append(line)
    flush()
    return blocks


def _sentences(text: str) -> list[str]:
    return [s for s in _SENT_SPLIT_RE.split(text.strip()) if s.strip()]


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


# ── checks ───────────────────────────────────────────────────────────────────


def _phrase_checks(n: int, line: str, findings: list[Finding]) -> None:
    low = line.lower()

    for phrase, plain in SUBSTITUTIONS.items():
        for m in re.finditer(rf"\b{re.escape(phrase)}\b", low):
            findings.append(
                Finding("complex-word", "minor", n, _excerpt(line, m.start(), m.end()), f'"{phrase}" → {plain}')
            )
    for phrase, plain in DOUBLETS.items():
        for m in re.finditer(rf"\b{re.escape(phrase)}\b", low):
            findings.append(Finding("doublet", "minor", n, _excerpt(line, m.start(), m.end()), f'"{phrase}" → {plain}'))
    for phrase, plain in NEGATIVES.items():
        for m in re.finditer(rf"\b{re.escape(phrase)}\b", low):
            findings.append(
                Finding("negative-phrasing", "minor", n, _excerpt(line, m.start(), m.end()), f'"{phrase}" → {plain}')
            )
    for word in FILLER:
        for m in re.finditer(rf"\b{word}\b", low):
            findings.append(
                Finding(
                    "filler", "minor", n, _excerpt(line, m.start(), m.end()), f'cut "{word}" or use a stronger word'
                )
            )
    for m in re.finditer(r"\bshall\b", low):
        findings.append(
            Finding(
                "shall",
                "major",
                n,
                _excerpt(line, m.start(), m.end()),
                'use "must" for a requirement, "will" for a fact',
            )
        )
    for m in re.finditer(r"\b(?:e\.g\.|i\.e\.|eg\.|ie\.)", low):
        findings.append(
            Finding(
                "latin-abbreviation", "minor", n, _excerpt(line, m.start(), m.end()), 'write "for example" or "that is"'
            )
        )
    for m in re.finditer(r"\band/or\b", low):
        findings.append(
            Finding("and-or", "major", n, _excerpt(line, m.start(), m.end()), 'pick "and" or "or" ("X, Y, or both")')
        )
    for m in _HIDDEN_VERB_RE.finditer(line):
        findings.append(
            Finding(
                "hidden-verb",
                "minor",
                n,
                _excerpt(line, m.start(), m.end()),
                f'use the verb inside "{m.group(1)}" ("make a decision" → "decide")',
            )
        )
    for m in _THE_X_OF_RE.finditer(line):
        findings.append(
            Finding(
                "hidden-verb",
                "minor",
                n,
                _excerpt(line, m.start(), m.end()),
                f'"the {m.group(1)} of" often reads better as a verb',
            )
        )
    for m in _PASSIVE_RE.finditer(line):
        if m.group(1).lower() in _PASSIVE_ADJECTIVES:
            continue
        findings.append(
            Finding(
                "passive-voice",
                "minor",
                n,
                _excerpt(line, m.start(), m.end()),
                "possible passive voice: say who does it",
            )
        )


_VAGUE_LINK_RE = re.compile(r"\[(click here|here|read more|more|this|link|this link|learn more)\]\(", re.I)


def _link_checks(raw_lines: list[str], findings: list[Finding]) -> None:
    in_fence = False
    for n, raw in enumerate(raw_lines, start=1):
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in _VAGUE_LINK_RE.finditer(raw):
            findings.append(
                Finding(
                    "vague-link",
                    "major",
                    n,
                    _excerpt(raw, m.start(), m.end()),
                    "link text should say where the link goes",
                )
            )
        for m in re.finditer(r"\bclick here\b", raw, re.I):
            if not _VAGUE_LINK_RE.search(raw[max(0, m.start() - 1) : m.end() + 2]):
                findings.append(
                    Finding("vague-link", "major", n, _excerpt(raw, m.start(), m.end()), 'don\'t write "click here"')
                )


def check(text: str) -> Report:
    """Lint ``text`` (plain text or Markdown) and return a :class:`Report`."""
    text = text or ""
    raw_lines = text.splitlines()
    lines = _prose_lines(text)
    blocks = _blocks(lines)
    findings: list[Finding] = []

    for n, line in lines:
        if _TABLE_RE.match(line):
            continue
        _phrase_checks(n, line, findings)
    _link_checks(raw_lines, findings)

    total_words = 0
    total_sentences = 0
    paragraphs = 0
    heading_levels: set[int] = set()
    prev_kind = ""
    for kind, n, body in blocks:
        if kind == "heading":
            hm = _HEADING_RE.match(body)
            level = len(hm.group(1)) if hm else 1
            heading_levels.add(level)
            title = hm.group(2) if hm else body
            if len(_words(title)) == 1 and title.strip().lower() not in {"summary", "overview"}:
                findings.append(Finding("one-word-heading", "minor", n, title.strip(), "say what the section is about"))
            if level >= 4:
                findings.append(
                    Finding("heading-depth", "major", n, title.strip(), "use three heading levels or fewer")
                )
            total_words += len(_words(title))
        elif kind == "list":
            if prev_kind in ("", "heading"):
                findings.append(
                    Finding(
                        "list-without-lead-in",
                        "minor",
                        n,
                        body.strip()[:60],
                        "introduce the list with a sentence",
                    )
                )
            total_words += len(_words(body))
            total_sentences += max(1, len(_sentences(body)))
        elif kind == "table":
            total_words += len(_words(body))
        else:
            paragraphs += 1
            words = len(_words(body))
            total_words += words
            if words > 250:
                findings.append(
                    Finding("long-paragraph", "major", n, body[:60] + "…", f"{words} words; split it (never over 250)")
                )
            elif words > 150:
                findings.append(
                    Finding("long-paragraph", "minor", n, body[:60] + "…", f"{words} words; aim for 150 or fewer")
                )
            for sentence in _sentences(body):
                total_sentences += 1
                sw = len(_words(sentence))
                if sw > 40:
                    findings.append(
                        Finding("long-sentence", "major", n, sentence[:60] + "…", f"{sw} words; one idea per sentence")
                    )
                elif sw > 30:
                    findings.append(
                        Finding("long-sentence", "minor", n, sentence[:60] + "…", f"{sw} words; consider splitting")
                    )
        # A list item continues a list, so its predecessor for lead-in purposes is the list.
        prev_kind = kind

    # Abbreviations: the guide's limit is two or three per document, defined at first use.
    seen: dict[str, int] = {}
    prose = "\n".join(line for _, line in lines)
    for n, line in lines:
        for m in _ABBREV_RE.finditer(line):
            tok = m.group(0)
            base = tok[:-1] if tok.endswith("s") and tok[:-1].isupper() else tok
            if base in COMMON_ABBREVIATIONS or len(base) < 2 or base.isdigit():
                continue
            seen.setdefault(base, n)
    undefined = [a for a in seen if not re.search(rf"\(\s*{re.escape(a)}s?\s*\)", prose)]
    for abbr in undefined:
        findings.append(
            Finding(
                "undefined-abbreviation",
                "minor",
                seen[abbr],
                abbr,
                "spell it out at first use, or drop it",
            )
        )
    if len(seen) > 3:
        findings.append(
            Finding(
                "too-many-abbreviations",
                "minor",
                min(seen.values()),
                ", ".join(sorted(seen)),
                f"{len(seen)} uncommon abbreviations; the guide suggests two or three",
            )
        )

    findings.sort(key=lambda f: (f.line, 0 if f.severity == "major" else 1, f.rule))
    return Report(findings=findings, words=total_words, sentences=total_sentences, paragraphs=paragraphs)


def format_report(report: Report, *, limit: int = 40) -> str:
    """A compact, human-readable report for the tool reply."""
    if not report.findings:
        return f"No plain-language findings ({report.words} words). Still read it as the reader would."
    head = (
        f"{len(report.findings)} findings in {report.words} words "
        f"({report.major} major, {report.minor} minor; {report.per_100_words} weighted per 100 words)."
    )
    counts = ", ".join(f"{k} {v}" for k, v in report.counts().items())
    rows = [f"- line {f.line} [{f.severity}] {f.rule}: {f.excerpt} → {f.suggestion}" for f in report.findings[:limit]]
    more = len(report.findings) - limit
    if more > 0:
        rows.append(f"- …and {more} more")
    return "\n".join(
        [
            head,
            f"By rule: {counts}.",
            "These are heuristics: passive voice and hidden-verb hits are places to look, not proof.",
            *rows,
        ]
    )
