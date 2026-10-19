"""Render editable docs diagrams: python docs/dev/media/sources/render_diagrams.py."""

from html import escape
import json
from pathlib import Path
import textwrap

ROOT = Path(__file__).resolve().parents[4]
SPECS = Path(__file__).with_name("diagrams.json")


def text(x, y, value, size=21, color="#182437", width=29, weight=400):
    lines = textwrap.wrap(value, width=width)
    return '<text x="%s" y="%s" fill="%s" font-size="%s" font-weight="%s">%s</text>' % (
        x,
        y,
        color,
        size,
        weight,
        "".join(f'<tspan x="{x}" dy="{0 if i == 0 else 29}">{escape(line)}</tspan>' for i, line in enumerate(lines)),
    )


def render(key, spec):
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="680" viewBox="0 0 1200 680" role="img" aria-labelledby="title desc"><title id="title">{escape(spec["title"])}</title><desc id="desc">{escape(spec["subtitle"] + " " + spec["note"])}</desc>',
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8" fill="none" stroke="#5865cc" stroke-width="1.5"/></marker></defs>',
        '<rect width="1200" height="680" rx="20" fill="#f5f7fb"/><g font-family="Arial, Helvetica, sans-serif">',
        text(48, 57, spec["title"], 32, width=67, weight=700),
        text(48, 103, spec["subtitle"], 20, "#4b5870", width=105),
    ]

    def box(x, y, width, label, color="#ffffff"):
        parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="86" rx="12" fill="{color}" stroke="#cdd4e3"/>')
        parts.append(text(x + 18, y + 35, label, width=max(18, width // 12)))

    def arrow(path, dashed=False):
        dash = ' stroke-dasharray="7 5"' if dashed else ""
        parts.append(f'<path d="{path}" fill="none" stroke="#5865cc" stroke-width="2" marker-end="url(#arrow)"{dash}/>')

    if key == "a2a-task-lifecycle":
        box(48, 180, 240, "SUBMITTED")
        box(378, 180, 240, "WORKING")
        arrow("M288 223 H370")
        for y, label in [(145, "COMPLETED"), (275, "FAILED"), (405, "CANCELED")]:
            box(860, y, 280, label)
            arrow(f"M618 223 H740 V{y + 43} H852")
        box(378, 420, 280, "INPUT_REQUIRED", "#edf0ff")
        arrow("M498 266 V412")
        parts.append(text(518, 335, "needs input", 18, "#4b5870"))
        arrow("M378 463 H320 V223 H370", True)
        parts.append(text(48, 385, "Follow-up message in the same context resumes work", 18, "#4b5870", width=25))
    elif key == "box-instance-paths":
        parts.append('<rect x="48" y="145" width="1100" height="405" rx="16" fill="#edf0ff" stroke="#cdd4e3"/>')
        parts.append(text(70, 180, "/srv/agents/  ·  box root", 23, weight=700))
        box(80, 207, 1036, "host-config.yaml  ·  defaults inherited by both instances")
        box(80, 343, 490, "alice/config/ + workspace/ + private stores")
        box(626, 343, 490, "bob/config/ + workspace/ + private stores")
        arrow("M325 293 V335")
        arrow("M871 293 V335")
        parts.append(
            text(80, 488, "commons/  ·  explicitly shared material; private stores remain separate", 21, width=90)
        )
    else:
        for row, cells in enumerate(spec["rows"]):
            y = 160 + row * 132
            for col, cell in enumerate(cells):
                x = 48 + col * 375
                parts.append(f'<rect x="{x}" y="{y}" width="350" height="104" rx="12" fill="white" stroke="#cdd4e3"/>')
                parts.append(text(x + 18, y + 37, cell, width=28, weight=600 if col == 0 else 400))
                if col < 2:
                    parts.append(
                        f'<path d="M{x + 353} {y + 52} H{x + 369}" stroke="#5865cc" stroke-width="2" marker-end="url(#arrow)"/>'
                    )
    parts += [text(48, 594, spec["note"], 19, "#4b5870", width=105), "</g></svg>"]
    svg = "\n".join(parts) + "\n"
    source = SPECS.parent / f"{key}.svg"
    source.write_text(svg)
    target = ROOT / "docs/public/media" / key / "v1.svg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(svg)


if __name__ == "__main__":
    for diagram_id, diagram in json.loads(SPECS.read_text()).items():
        render(diagram_id, diagram)
