"""The ``artifact-ref`` chat component (#3617): a POINTER to one artifact version.

Every create/revise tool appends one to its reply (after the model-facing text), and the
server lifts it into a component-v1 frame; the console renders it as a chip that opens the
Artifact panel on exactly that artifact + version (``protoArtifact:select`` into the shell).

The props are pointer data only — id, lifetime version number, a clipped title, the kind —
never the artifact's code: they persist in chat history (and a chip in an incognito chat
must carry nothing a non-incognito surface would keep), while the content stays in the
artifact store it already lives in.

``version`` is the LIFETIME number (``_store._version_key``), not the list position the
model-facing text reports: at the ``max_versions`` cap every commit trims the front and
shifts positions, so only the lifetime number still names the same version later. The two
agree until the cap is hit.
"""

from __future__ import annotations

from . import _pdfview, _store

ARTIFACT_REF = "artifact-ref"

# Whether the tools append the chip tail. ``register()`` turns it off on a host without the
# plugin component seam, which couldn't lift the tail out of the tool card.
EMIT = True

_KINDS = frozenset({"html", "svg", "mermaid", "react", "markdown", "vega-lite", "file"})
_ID_MAX = 64
TITLE_MAX = 200
# `inline`/`height` are the inline-placement hints (ADR 0118 D2): a chip rendered as the
# artifact's own embedded frame, clamped to [HEIGHT_MIN, HEIGHT_MAX] px. Both are optional —
# an older payload (and every default panel call) simply has neither.
_PROPS = {"artifact_id", "version", "versions_total", "title", "kind", "inline", "height", "page"}
HEIGHT_MIN = 80
HEIGHT_MAX = 1200


def _pos_int(v: object) -> bool:
    # bool is an int subclass — `True` is not a version number.
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def validate_artifact_ref(props: dict) -> str | None:
    """Why ``props`` is not a valid ``artifact-ref``, or ``None`` when it is. Registered with
    the host (``registry.register_component``), which drops any payload this rejects."""
    if not isinstance(props, dict):
        return "props must be an object"
    extra = set(props) - _PROPS
    if extra:
        return f"unexpected artifact-ref props: {', '.join(sorted(extra))}"
    art_id = props.get("artifact_id")
    if not isinstance(art_id, str) or not art_id or len(art_id) > _ID_MAX:
        return f"artifact-ref artifact_id must be a non-empty string of at most {_ID_MAX} chars"
    if not _pos_int(props.get("version")):
        return "artifact-ref version must be a positive integer"
    total = props.get("versions_total")
    if total is not None and (not _pos_int(total) or total < props["version"]):
        return "artifact-ref versions_total must be an integer >= version"
    title = props.get("title", "")
    if not isinstance(title, str) or len(title) > TITLE_MAX:
        return f"artifact-ref title must be a string of at most {TITLE_MAX} chars"
    if props.get("kind") not in _KINDS:
        return f"artifact-ref kind must be one of: {', '.join(sorted(_KINDS))}"
    # The inline-placement hints are optional and advisory: a malformed one is DROPPED, never a
    # reason to reject the whole pointer (the chip still opens the panel). `inline` must be a bool;
    # `height` must be a plain int, clamped into the frame's [HEIGHT_MIN, HEIGHT_MAX] px range.
    if "inline" in props and not isinstance(props["inline"], bool):
        props.pop("inline")
    if "height" in props:
        h = props["height"]
        if isinstance(h, int) and not isinstance(h, bool):
            props["height"] = max(HEIGHT_MIN, min(HEIGHT_MAX, h))
        else:
            props.pop("height")
    if "page" in props and not _pos_int(props["page"]):
        props.pop("page")
    return None


def page_hint(
    file_meta: dict | None, kind: str, page: int, *, input_only: bool = False
) -> tuple[int | None, str | None, str]:
    """Return the accepted page, refusal, and reply note for a PDF page hint.

    Saves use ``input_only`` before preparing the file: absent metadata means its
    type is not known yet. That pass checks arguments only, never accepts a hint;
    the final pass must use the stored preview verdict.
    """
    if page == 0:
        return None, None, ""
    if page < 0:
        refusal = "page must be a positive PDF page number (1-based), or 0 for no page hint."
    elif input_only and file_meta is None and kind == "file":
        return None, None, ""
    elif kind != "file" or not _pdfview.is_pdf(
        (file_meta or {}).get("filename", ""), (file_meta or {}).get("mime", "")
    ):
        refusal = "page is only supported for a PDF. Omit page or use 0 for other files."
    elif input_only:
        return None, None, ""
    else:
        pdf = (file_meta or {}).get("pdf") or {}
        if pdf.get("render") is not True:
            reason = pdf.get("reason") or "no approved PDF preview verdict"
            refusal = f"The PDF shows as text ({reason}), so it can't open at a page."
        else:
            pages = pdf.get("pages")
            if _pos_int(pages) and page > pages:
                return pages, None, f" — the PDF has {pages} pages — opens at page {pages}"
            return page, None, f" — opens at page {page}"
    return None, refusal, f" — {refusal}"


def ref_props(
    art: dict,
    *,
    inline: bool = False,
    height: int | None = None,
    page: int | None = None,
    version_pos: int | None = None,
) -> dict:
    """The ``artifact-ref`` props for a version of ``art`` — its LATEST by default (the one a
    tool just wrote), or the 1-based list position ``version_pos`` (what cite_artifact points at
    an ALREADY-saved version, #4761).

    ``inline`` adds the inline-placement flag (ADR 0118 D2); ``height`` adds a clamped px hint.
    ``page`` adds a 1-based PDF page hint. ``version_pos`` defaults to ``None`` → the latest
    version, so a plain panel/save call's props are byte-for-byte as before."""
    # ``versions_total`` is the LATEST version's lifetime number (``_store._version_key``), which
    # is the version total. The prop ``version`` is a version's own lifetime number — NOT its list
    # position, which the max_versions trim shifts (see the module docstring): for the latest that
    # IS the total; for an earlier position it's offset by how many versions have been trimmed off
    # the front (``total - len(versions)``), the same arithmetic ``_store._locate_version`` inverts.
    total = _store._version_key(art)[0]
    n = total if version_pos is None else version_pos + total - len(art.get("versions") or [])
    title = " ".join(str(art.get("title") or "").split())  # one line — it's a chip label
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1] + "…"
    props = {
        "artifact_id": str(art["id"]),
        "version": int(n),
        "versions_total": int(total),
        "title": title,
        "kind": str(art.get("kind") or ""),
    }
    if inline:
        props["inline"] = True
    if height is not None:
        props["height"] = max(HEIGHT_MIN, min(HEIGHT_MAX, int(height)))
    if _pos_int(page):
        props["page"] = page
    return props


def ref_tail(
    art: dict,
    *,
    inline: bool = False,
    height: int | None = None,
    page: int | None = None,
    version_pos: int | None = None,
) -> str:
    """The component tail a tool appends to its reply: ``"\\n" + <sentinel payload>``, or
    ``""`` when the props wouldn't validate (never let a pointer break the reply).

    ``inline``/``height`` carry the inline-placement hints (ADR 0118 D2) into the props;
    ``version_pos`` points the chip at a non-latest version (cite_artifact, #4761)."""
    if not EMIT:
        return ""
    props = ref_props(art, inline=inline, height=height, page=page, version_pos=version_pos)
    if validate_artifact_ref(props) is not None:
        return ""
    from graph.sdk import encode_component

    return "\n" + encode_component(ARTIFACT_REF, props)
