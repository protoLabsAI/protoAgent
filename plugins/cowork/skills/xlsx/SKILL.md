---
name: xlsx
description: Produce, edit, or clean spreadsheets (.xlsx/.csv/.tsv) as real files — reconciliations, trackers, budgets, data cleanups, format conversions. Use whenever the deliverable is a spreadsheet or an existing one needs work, including messy tabular data that needs restructuring. Not for Word/PDF/deck outputs.
---

# Spreadsheets with openpyxl

Write the workbook with `execute_code` using `openpyxl`, save into the fenced
project folder (or configured `output_dir`), report the path. Plain `.csv`
/`.tsv` work uses the stdlib `csv` module — don't drag a workbook library
into a text-file job.

## Creating

```python
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

wb = Workbook()
ws = wb.active
ws.title = "Summary"
ws.append(["Item", "Owner", "Amount"])
for cell in ws[1]:
    cell.font = Font(bold=True)
for row in rows:
    ws.append(row)
ws.freeze_panes = "A2"
for i, _ in enumerate(ws[1], 1):
    ws.column_dimensions[get_column_letter(i)].width = 18
wb.save(path)
```

## Formulas — the one big trap

openpyxl writes formula *strings* (`ws["C10"] = "=SUM(C2:C9)"`) but **never
computes them**, and reading a formula cell back gives the formula, not a
value. So:

- If the operator will open the file in Excel: write formulas — they compute
  on open, and edits stay live.
- If a downstream step (or your verification) needs the *values*: compute in
  Python and write both, or write values only. Never report a total you
  didn't compute yourself.

## Cleaning messy data

Read with `load_workbook(src, read_only=True)`, normalize in Python (strip
junk rows, promote the real header, coerce types), write a fresh workbook.
Keep the original untouched; name the output `<name>-clean.xlsx`.

## Verify before reporting

Re-open the saved file and spot-check: row count, a couple of cell values, a
computed total. State the numbers you verified in the summary.

## Save it as a versioned artifact

Once the file is written and verified, register it in the Artifact panel with
`save_file_artifact(path, title=…)` when that tool is available (the artifact plugin, protoAgent
v0.107.0+). It stores the bytes, shows a Download card with a readable preview (a sheet table), and keeps
an edit history — pass the same `artifact_id` to save a later revision as v2/v3. Report the saved
path *and* that it's in the panel. If the tool isn't available, just report the path.
