---
name: pdf
description: Read, extract from, combine, split, watermark, fill, or produce PDF files. Use whenever a .pdf is the input or the deliverable — merging reports, pulling text or tables out, rotating scans, filling forms, or generating a polished PDF from content.
---

# PDFs with pypdf (+ reportlab for generation)

Two different jobs, two libraries — both via `execute_code`:

## Manipulating existing PDFs — pypdf

```python
from pypdf import PdfReader, PdfWriter

# extract text
text = "\n".join(page.extract_text() for page in PdfReader(src).pages)

# merge
w = PdfWriter()
for part in [a, b, c]:
    w.append(part)
w.write(out)

# split / rotate / subset
w = PdfWriter()
for page in PdfReader(src).pages[2:5]:
    page.rotate(90)
    w.add_page(page)
w.write(out)
```

Also: form fill (`w.update_page_form_field_values`), encrypt/decrypt
(`w.encrypt(pwd)` / `PdfReader(src, password=pwd)`), metadata
(`reader.metadata`), attachments and watermarks (`page.merge_page(stamp)`).

**Scanned PDFs**: `extract_text()` returning almost nothing means image-only
pages. Say so plainly and offer what's available (OCR only if an OCR tool is
actually installed — check, don't assume).

## Producing new PDFs — reportlab

For a *generated* deliverable (report, one-pager, cover sheet):

```python
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

styles = getSampleStyleSheet()
doc = SimpleDocTemplate(path, pagesize=LETTER)
doc.build([Paragraph("Title", styles["Title"]), Spacer(1, 12)] + [Paragraph(p, styles["BodyText"]) for p in paragraphs])
```

If the content is really a Word document the operator also wants as PDF,
build the .docx (docx skill) and produce the PDF alongside it — don't force
page layout through reportlab when Word styling is the source of truth.

## Verify before reporting

Re-open the output: page count, and text extraction on a sample page for
generated PDFs. Report the verified page count with the saved path.

## Save it as a versioned artifact

Once the file is written and verified, register it in the Artifact panel with
`save_file_artifact(path, title=…)` when that tool is available (the artifact plugin, protoAgent
v0.107.0+). It stores the bytes, shows a Download card with a readable preview (the extracted text), and keeps
an edit history — pass the same `artifact_id` to save a later revision as v2/v3. Report the saved
path *and* that it's in the panel. If the tool isn't available, just report the path.
