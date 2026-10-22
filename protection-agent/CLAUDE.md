# Agent Guide — Analyzing & Editing Substation Protection DXF Drawings

This document defines the **standard working method** for agents handling DXF drawings (SLDs, wiring, terminal blocks, panel layouts).
The core idea: a DXF is **data that you read and modify through scripts**, not text to be read raw, and every result is
**checked by rendering an image** before it is handed over.

## 0. Principles (apply to every task)
1. **Never read raw DXF.** Use the index (`index/`) and the tools in `tools/`. Only summaries may enter the context.
2. **Look before you edit.** Render the relevant area to PNG and study it. Coordinates alone are not enough to understand an SLD.
3. **Measure, don't guess.** Positions, spacing and free space must come from data (`region_dump.py`), not estimates.
4. **Edit through scripts with `assert`.** The script must stop if a reference point is not what you expected.
5. **Always write a new file.** Never overwrite files in `drawings/`. Results go to `out/`.
6. **Verify twice:** data comparison (`verify.py`) and visual render.
7. **Report honestly:** state assumptions and what was left unchanged. Protection-engineering correctness remains the engineer's responsibility.

## 1. Project structure
```
drawings/    original DXF files (treat as read-only)
out/         edit results (copies)
index/       automatic index produced by scan.py (do not edit by hand)
tools/       scan.py, profiles.json, render.py, region_dump.py, dxf_edit.py, verify.py
examples/    complete example edit scripts (ct3_to_79.py)
specs/       per-bay parameters (CT ratio, ratings, relay type) — source of truth for technical data
```

## 2. Available tools
| Tool | Purpose | Example |
|---|---|---|
| `scan.py` | Index all DXFs: sheets, devices, ratios, placeholders, texts | `python tools/scan.py drawings/ -o index -j 4` |
| `render.py` | Render a modelspace area to PNG (+ coordinate grid) | `python tools/render.py F.dxf o.png X0 X1 Y0 Y1 --grid 100` |
| `region_dump.py` | List entities/texts inside an area (find reference points) | `python tools/region_dump.py F.dxf X0 X1 Y0 Y1 --text` |
| `dxf_edit.py` | Edit helper: `one()` verifies anchors, `wire()`, `copy()`, `save()` | see `examples/ct3_to_79.py` |
| `verify.py` | Compare two DXFs: what was added/removed, check changes outside an area | `python tools/verify.py A.dxf B.dxf --region X0 X1 Y0 Y1` |

Note: the tools' console messages are in Indonesian (e.g. `Perubahan di LUAR region` = "Changes OUTSIDE region").

## 3. Standard workflow (follow in order)

### Step 0 — Understand the request
- Identify the **source object, target object, and bay/sheet** (e.g. "from CT-3 to function 79 on -F87L, ITP-1").
- If two interpretations would produce different drawings, ask **one** question. Otherwise proceed and state the assumption.
- Check whether the object has **copies** (e.g. bay ITP-2 is a copy of ITP-1 with identical text). Limit edits to the requested bay.

### Step 1 — Inspect (cheap, no images)
- Read `index/summary.md` and `index/toc.md`; search `devices.csv` / `texts.csv` (grep) to find the sheet and rough coordinates.
- If the file is not indexed yet: `python tools/scan.py <file> -o index`.
- **Do not** print the whole file. Take only counts, short lists, and the coordinates you need.

### Step 2 — Look
- `python tools/render.py FILE view.png X0 X1 Y0 Y1 --grid 100` on the working area (about 1,300–5,000 units wide; smaller area = more legible).
- Observe: layout, cable direction, nearby symbols, free space. Re-render zoomed in where things are crowded.
- Images are expensive in context — use them deliberately (view at the start, zoom into crowded parts, view at the end).
- If an image looks unchanged after re-rendering, use a **new output filename** (the viewer may cache the old image).

### Step 3 — Reference points (measure)
- `python tools/region_dump.py FILE X0 X1 Y0 Y1 [--types LINE,CIRCLE] [--text]` for the objects involved:
  circle centers, line endpoints, text position/height, box boundaries (dashed polylines), layers.
- Record: source and target coordinates, the **spacing between terminal rows** (pitch), **free space**, and **text around the route**.
- The correct connection point is the **edge of the symbol** (e.g. top/bottom of a circle = center ± radius), not its center.

### Step 4 — Plan the route / placement (on paper first)
Write a short plan before any code: the ordered points (x,y), the layer of each segment, the objects to copy, the new labels.
Rules:
- **Orthogonal only** (horizontal/vertical), following the wiring pattern already used on the sheet.
- **Use the existing pitch.** Place a new terminal row in a slot spaced like the other rows.
  Check that the slot is actually empty (here the middle slot was occupied by circle 59 → the route was bent around it).
- **Clearance:** keep the route at least about one text height from other text, and never overlapping or running tight and parallel to other lines.
  Measure: exactly midway between two objects is better than hugging one of them.
- **Crossings** of wires have no dot. **Junction dots (black)** only at T-branches. Never add a dot at a crossing.
- A route entering a device box must pass through a **terminal symbol**, like the other wires on that box.
- **Copy the style, don't redraw:** copy the terminal symbol, label and `**` mark from the nearest row using `copy()`.
- Do not move or modify existing objects unless asked. Report conflicts (e.g. a "Spare for future" note).
- A new name (e.g. a terminal designation) not supplied by the user is an **assumption** → state it in the report.

### Step 5 — Edit through a script
Use `dxf_edit.Edit` (see `examples/ct3_to_79.py`):
1. `ed.one("CIRCLE", x, y, tol=0.2, layer=...)` for **every reference point** → the script stops unless exactly one object matches.
2. Selectors (`select`) for objects to copy must be restricted by **both X and Y** — a copied bay has the same text at the same y.
3. `ed.copy(...)` for symbols/labels (replace text via `text={...}`), `ed.wire([...])` for cables; layers follow the drawing.
4. `ed.save()` to `out/<name>_<summary>.dxf`. One script = one repeatable change. Keep the script in `out/` or `examples/`.

### Step 6 — Verify (mandatory)
1. **Data:** `python tools/verify.py drawings/a.dxf out/a_rev.dxf --region X0 X1 Y0 Y1`
   → added/removed counts match the plan; **"Perubahan di LUAR region: 0"** (changes outside region = 0). If not 0, find the cause before continuing.
2. **Visual:** render the changed area (zoomed) and check: wires connect to symbol edges, nothing collides with text, labels are readable,
   crossings have no dots, other bays are unchanged.
3. **Index:** `python tools/scan.py out/ -o index` and confirm devices/placeholders changed as expected.
4. If something is wrong: fix the **script** and re-run from the original file (don't patch the previous result).

### Step 7 — Final report (fixed format)
```
Done           : <1–2 sentences>
File           : out/<name>.dxf
Changes        : <entity count, new objects, layers> (from verify.py)
Assumptions    : <names/placements chosen without instruction>
Not changed    : <e.g. bay ITP-2, NOTE 1>
To be checked  : <technical points that are the engineer's decision>
```
A few lines per section at most. Do not paste long logs.

## 4. Project drawing conventions (E2010-96572-BD-03-SLD)
- One DXF holds many sheets. A3 sheets live in **paperspace** (`Layout2`): frame + title block + a **VIEWPORT** pointing at a modelspace area.
  **A new bay added in modelspace does NOT appear in the layout** until it gets its own sheet + viewport (copy the sheet frame, offset paper +468.4, add a viewport).
- `=E0X` bay, `+S1` marshalling kiosk, `+WR1` control & protection panel. IEC designations: `-Q50`, `-F87L`, `-K861`, `-X87LA`.
- Devices: C264 (-A30 BCU), MiCOM P546 (-F87L), P142 (-F50), PM5560 (-P50), BJ-8RP (-K861/862/863), VDF-10 (trip supervision).
- Relay terminal rows: pitch **97.7** units; designation label + `**` mark (supplied by TME) above the terminal; terminal = arcs + center line.
- Cables: LINE on layer `0`; stubs next to terminals on layer `CONNECTION`. `SAO` circles = marshalling points.
- `XXXX`, `XXkA`, `X.X` = unfilled placeholders (`index/placeholders.csv`).
- Some title-block values in this file are only default ATTDEFs from the template (not actual values) → see `fields_source` in the index.
- The file contains OLE2FRAME objects; saving through DXF may drop them → ask the user to check in AutoCAD.
- Header extents may be empty after saving with ezdxf → set `$EXTMIN/$EXTMAX` if needed so Zoom Extents works.

## 5. Pitfalls that already happened (do not repeat)
| Problem | Cause | Prevention |
|---|---|---|
| New bay "missing" in AutoCAD | Only in modelspace, no viewport in Layout2 | Add a sheet + viewport; tell the user how to ZOOM to the coordinates |
| Label copied into another bay too | Selector used only y; the copied bay has the same text | Restrict `select` by both X and Y |
| Plain word ("LINE") read as a designation | Regex too loose for the attribute style | Require a digit in the designation; test on other styles |
| Wrong drawing number | A relay model text matched the regex | Search by the "Drawing No" label and the bottom of the sheet first |
| Script failed to import | File named `inspect.py` collided with a Python standard module | Never name scripts after standard modules |
| Render looked unchanged | Viewer cached the old image | Use a new output filename |

## 6. Speed & context budget
- One script performs many operations at once; don't edit entities one by one through conversation.
- Batch over many files: `scan.py -j 4`, summarize results (only failed/changed files).
- Do **one sample file first**; if correct, repeat on the others.
- Use images sparingly: one before, one zoomed after.

## 7. Request template for users (for accurate results)
Good:
> In `drawings/X.dxf`, Line Bay **ITP-1**: connect **CT-3** to function **79** on **-F87L**. Do not change ITP-2. Save to `out/`.

Unclear (the agent must ask or state an assumption):
> Add a CT connection to the relay.

Include when available: bay/sheet, source & target objects, connection type (cable/terminal), desired names, parts that must not change.

## 8. Checklist before declaring done
- [ ] Reference points verified with `assert` (not guessed)
- [ ] No original file overwritten
- [ ] `verify.py`: counts match the plan, changes outside region = 0
- [ ] Zoomed render checked: connected to symbol edges, no text collisions, same style
- [ ] Index updated (`scan.py`)
- [ ] Report states assumptions, what was not changed, and what the engineer must check
