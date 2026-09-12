---
name: docx
description: Produce or modify Word documents (.docx) as real files — reports, memos, letters, proposals, or any deliverable the operator will open in Word. Use when the requested output is a Word file, or when an existing .docx needs content extracted, edited, or restructured. Not for spreadsheets, decks, or PDFs — those have their own skills.
tools: [execute_code, save_file_artifact]
---

# Word documents with python-docx

Deliverables are files: write the document with `execute_code` using
`python-docx`, save it into the operator's fenced project folder (if there
are several, ask once which and remember it), and report the saved path. If the library
is missing, say so — it's declared in this plugin's `requires_pip` and the
operator installs it once from the console.

## Creating

```python
from docx import Document

doc = Document()
doc.add_heading("Quarterly Ops Report", level=0)
doc.add_paragraph("Summary paragraph…")
doc.add_heading("Findings", level=1)
for point in findings:
    doc.add_paragraph(point, style="List Bullet")
table = doc.add_table(rows=1, cols=3)
table.style = "Light Grid Accent 1"
doc.save(path)
```

- Build structure from headings, not bold paragraphs — headings drive the
  navigation pane and any generated table of contents.
- For letters/memos, set base styles once (`doc.styles["Normal"].font`)
  rather than styling every run.
- Images: `doc.add_picture(path, width=Inches(5))`.

## Editing an existing file

Open with `Document(src)`, modify, and **save to a new file** unless the
operator explicitly asked for in-place edits — never destroy their original.
Find-and-replace must iterate `paragraph.runs` (replacing at the paragraph
level drops character formatting).

## Verify before reporting

Re-open the saved file and read back a couple of elements (heading count,
table dimensions). A file that saves without error can still be structurally
wrong; check what you claim.

## Honest limits

python-docx cannot compute a live table of contents (insert a TOC field or
list headings manually), and tracked changes/comments are read-mostly. Say so
rather than approximating silently.

## Save it as a versioned artifact

Once the file is written and verified, register it in the Artifact panel with
`save_file_artifact(path, title=…)` when that tool is available (the artifact plugin, protoAgent
v0.107.0+). It stores the bytes, shows a Download card with a readable preview (the document text), and keeps
an edit history — pass the same `artifact_id` to save a later revision as v2/v3. Report the saved
path *and* that it's in the panel. If the tool isn't available, just report the path.
