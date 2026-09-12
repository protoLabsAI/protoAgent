---
name: pptx
description: Build or edit slide decks (.pptx) as real files — pitch decks, briefings, status readouts — or extract content from existing ones. Use whenever slides or presentations are the input or the deliverable. Not for documents, spreadsheets, or PDFs.
tools: [execute_code, save_file_artifact]
---

# Slide decks with python-pptx

Write the deck with `execute_code` using `python-pptx`, save into the fenced
project folder (or configured `output_dir`), report the path.

## Work outline-first

Draft the deck as a text outline (title + bullets per slide), confirm it with
the operator if scope is at all unclear, *then* render. Rendering is the
cheap part; the outline is where the deck is won.

```python
from pptx import Presentation
from pptx.util import Inches, Pt

prs = Presentation()  # or Presentation(template_path) to inherit branding
title_slide = prs.slides.add_slide(prs.slide_layouts[0])
title_slide.shapes.title.text = "Q3 Ops Review"
title_slide.placeholders[1].text = "Prepared by …"

for heading, bullets in outline:
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = heading
    body = slide.placeholders[1].text_frame
    body.text = bullets[0]
    for b in bullets[1:]:
        body.add_paragraph().text = b
prs.save(path)
```

- **Start from the operator's template** (.pptx with their branding) whenever
  one exists — decks that ignore brand layouts get redone by hand.
- Speaker notes: `slide.notes_slide.notes_text_frame.text = "…"` — offer
  them; presenters love not writing them.
- Images/charts: `slide.shapes.add_picture(...)`; for data charts, render
  with a plotting library to PNG first, then place.
- One idea per slide; if a bullet list passes ~6 items, split the slide.

## Reading / editing existing decks

Iterate `prs.slides` → `slide.shapes` → `shape.text_frame` for extraction
(e.g. turning a deck into a summary). Edits: modify in place, save to a new
file unless in-place was requested.

## Verify before reporting

Re-open and count slides; read back the titles. Report "12 slides, saved to
<path>" — verified, not assumed.

## Save it as a versioned artifact

Once the file is written and verified, register it in the Artifact panel with
`save_file_artifact(path, title=…)` when that tool is available (the artifact plugin, protoAgent
v0.107.0+). It stores the bytes, shows a Download card with a readable preview (a slide outline), and keeps
an edit history — pass the same `artifact_id` to save a later revision as v2/v3. Report the saved
path *and* that it's in the panel. If the tool isn't available, just report the path.
