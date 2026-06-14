"""Runtime knobs + presets — a bounded, reversible control surface an LLM strategist can
steer a deterministic plugin engine with.

The pattern the SpaceTraders two-loop fleet hand-rolled: a set of tunable engine knobs read
live at call time (so a change takes effect on the running engine immediately), typed
coercion + clamping on set, named *presets* (curated knob bundles = a whole doctrine in one
move), and a decision log of every change. This generalizes it: declare your knobs once, get
``get``/``set``/``reset``/presets + a change log, and (optionally) auto-generated agent tools.

Pure stdlib — no protoAgent host deps, so it unit-tests directly. The tool factory imports
langchain lazily, so importing this module never requires it.

    KNOBS = (Knobs()
             .define("min_margin", 30, lo=0, help="cr/unit floor for a trade route")
             .define("buy_buffer", 600_000, lo=0, help="reinvest a hauler above this")
             .define("sink_cutoff", "ABUNDANT", choices=["LIMITED", "HIGH", "ABUNDANT"]))
    KNOBS.preset("trade-max", {"buy_buffer": 300_000, "min_margin": 20},
                 blurb="pure arbitrage")

    KNOBS.get("min_margin")            # read live in the engine
    KNOBS.set("min_margin", "15")      # the strategist tunes (typed-coerced + clamped + logged)
    KNOBS.apply_preset("trade-max")    # a whole doctrine in one move
    registry.register_tools(make_knob_tools(KNOBS, prefix="fleet"))  # st_tune/st_knobs/st_preset
"""

from __future__ import annotations

from typing import Any

__all__ = ["Knob", "Knobs", "make_knob_tools"]

_TRUE = {"1", "true", "on", "yes", "y"}


class Knob:
    """One declared knob: its default, current value, type, optional bounds/choices, help."""

    __slots__ = ("name", "default", "value", "kind", "lo", "hi", "choices", "help")

    def __init__(self, name, default, *, kind, lo=None, hi=None, choices=None, help=""):
        self.name = name
        self.default = default
        self.value = default
        self.kind = kind
        self.lo = lo
        self.hi = hi
        self.choices = choices
        self.help = help

    def coerce(self, raw: Any) -> Any:
        """Coerce ``raw`` to this knob's type (accepts numbers-as-strings, bare strings),
        validate against ``choices``, and clamp to ``[lo, hi]``."""
        if self.choices is not None:
            s = str(raw).strip()
            for c in self.choices:
                if str(c).lower() == s.lower():
                    return c
            raise ValueError(f"{self.name} must be one of {list(self.choices)}")
        if self.kind is bool:
            return str(raw).strip().lower() in _TRUE
        if self.kind is int:
            v: Any = int(float(raw))
        elif self.kind is float:
            v = float(raw)
        else:
            return str(raw)
        if self.lo is not None:
            v = max(self.lo, v)
        if self.hi is not None:
            v = min(self.hi, v)
        return v


class Knobs:
    """A registry of runtime knobs + presets, with a change log. Holds the values itself
    (read ``get`` live in your engine) — no module globals."""

    def __init__(self, *, log_cap: int = 50):
        self._knobs: dict[str, Knob] = {}
        self._presets: dict[str, dict] = {}
        self._log: list[dict] = []
        self._log_cap = log_cap

    # ── declaration ────────────────────────────────────────────────────────────────────
    def define(self, name: str, default: Any, *, kind: type | None = None,
               lo: Any = None, hi: Any = None, choices: list | None = None,
               help: str = "") -> "Knobs":
        """Declare a knob (chainable). ``kind`` is inferred from ``default`` if omitted
        (bool/int/float/str). ``lo``/``hi`` clamp numbers; ``choices`` constrains values."""
        if name in self._knobs:
            raise ValueError(f"knob {name!r} already defined")
        k = kind or (bool if isinstance(default, bool) else
                     int if isinstance(default, int) else
                     float if isinstance(default, float) else str)
        self._knobs[name] = Knob(name, default, kind=k, lo=lo, hi=hi, choices=choices, help=help)
        return self

    def preset(self, name: str, overrides: dict, *, blurb: str = "") -> "Knobs":
        """Declare a named preset — a bundle of knob overrides applied as one doctrine
        (chainable). Every key must be a defined knob."""
        unknown = set(overrides) - set(self._knobs)
        if unknown:
            raise ValueError(f"preset {name!r} references unknown knobs: {sorted(unknown)}")
        self._presets[name] = {"overrides": dict(overrides), "blurb": blurb}
        return self

    # ── read ───────────────────────────────────────────────────────────────────────────
    def get(self, name: str) -> Any:
        return self._knobs[name].value

    def values(self) -> dict:
        """Current ``name -> value`` for all knobs (audit/telemetry)."""
        return {n: k.value for n, k in self._knobs.items()}

    def schema(self) -> list[dict]:
        """Per-knob ``{name, value, default, choices?, range?, help}`` (for a show tool/UI)."""
        out = []
        for k in self._knobs.values():
            row = {"name": k.name, "value": k.value, "default": k.default, "help": k.help}
            if k.choices is not None:
                row["choices"] = list(k.choices)
            if k.lo is not None or k.hi is not None:
                row["range"] = [k.lo, k.hi]
            out.append(row)
        return out

    def presets(self) -> dict:
        return {n: dict(p) for n, p in self._presets.items()}

    def changes(self) -> list[dict]:
        """The decision log — every set/preset change, newest last."""
        return list(self._log)

    # ── mutate ─────────────────────────────────────────────────────────────────────────
    def set(self, name: str, value: Any) -> str:
        """Set a knob (typed-coerced, validated, clamped) and log the change. Returns a
        human message; raises nothing the agent can't read — unknown/invalid come back as text."""
        key = (name or "").strip()
        if key not in self._knobs:
            return f"unknown knob {name!r}; knobs: {', '.join(self._knobs)}"
        k = self._knobs[key]
        try:
            new = k.coerce(value)
        except (TypeError, ValueError) as e:
            return f"bad value for {key}: {e}"
        old = k.value
        k.value = new
        if new != old:
            self._record("tune", f"{key}: {old} → {new}")
        return f"{key}: {old} → {new}"

    def reset(self) -> None:
        """Restore every knob to its declared default (no log entry per knob)."""
        for k in self._knobs.values():
            k.value = k.default

    def apply_preset(self, name: str) -> str:
        """Reset to defaults, then apply the preset's overrides (so switching presets is not
        cumulative). Logs one decision. Returns a message."""
        key = (name or "").strip()
        if key not in self._presets:
            return f"unknown preset {name!r}; presets: {', '.join(self._presets) or '—'}"
        self.reset()
        for n, v in self._presets[key]["overrides"].items():
            self._knobs[n].value = self._knobs[n].coerce(v)
        blurb = self._presets[key]["blurb"]
        self._record("preset", f"→ {key}" + (f" ({blurb})" if blurb else ""))
        return f"preset → {key}" + (f" ({blurb})" if blurb else "") + f" · knobs now {self.values()}"

    def _record(self, action: str, detail: str) -> None:
        self._log.append({"action": action, "detail": detail})
        del self._log[: -self._log_cap]


def make_knob_tools(knobs: Knobs, *, prefix: str,
                    show: bool = True, tune: bool = True, presets: bool = True) -> list:
    """Auto-generate the agent-facing control-surface tools for ``knobs`` — ``<prefix>_knobs``
    (show), ``<prefix>_tune`` (set one knob), ``<prefix>_preset`` (show/apply a preset). Returns
    a list of LangChain tools to ``registry.register_tools(...)``. langchain is imported lazily,
    so importing this module never requires it.
    """
    from langchain_core.tools import tool  # lazy — keeps the module host-free to import

    made: list = []
    knob_help = "; ".join(f"{r['name']} ({r['help']})" if r["help"] else r["name"]
                          for r in knobs.schema())

    if show:
        async def _show() -> str:
            lines = [f"{prefix} knobs:"]
            for r in knobs.schema():
                extra = (f" choices={r['choices']}" if "choices" in r
                         else f" range={r['range']}" if "range" in r else "")
                lines.append(f"  {r['name']} = {r['value']} (default {r['default']}){extra}"
                             + (f" — {r['help']}" if r["help"] else ""))
            if knobs.presets():
                lines.append("presets: " + ", ".join(knobs.presets()))
            return "\n".join(lines)
        _show.__name__ = f"{prefix}_knobs"
        _show.__doc__ = f"Show the current {prefix} engine knobs, their ranges, and presets."
        made.append(tool(_show))

    if tune:
        async def _tune(knob: str, value: str) -> str:
            return knobs.set(knob, value)
        _tune.__name__ = f"{prefix}_tune"
        _tune.__doc__ = (
            f"Tune ONE {prefix} engine knob at runtime (reversible; takes effect immediately). "
            f"Knobs: {knob_help}.\n\nArgs:\n    knob: the knob name.\n    value: the new value "
            f"(a number, or one of the knob's choices).")
        made.append(tool(_tune))

    if presets and knobs.presets():
        async def _preset(name: str = "") -> str:
            if not name:
                return f"{prefix} presets: " + " · ".join(
                    f"{n} ({p['blurb']})" if p["blurb"] else n for n, p in knobs.presets().items())
            return knobs.apply_preset(name)
        _preset.__name__ = f"{prefix}_preset"
        _preset.__doc__ = (
            f"Set or show the {prefix} engine PRESET — a named knob bundle (a whole doctrine in "
            f"one move) vs {prefix}_tune's single knob. Call with no name to list presets.\n\n"
            f"Args:\n    name: the preset to apply, or \"\" to list them.")
        made.append(tool(_preset))

    return made
