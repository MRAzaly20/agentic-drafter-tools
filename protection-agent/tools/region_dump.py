#!/usr/bin/env python3
"""region_dump.py - daftar entitas dalam suatu area (untuk mencari titik acuan yang tepat).

  python tools/region_dump.py FILE X0 X1 Y0 Y1 [--types LINE,CIRCLE,TEXT] [--text] [--max 400]

--text  hanya teks (TEXT/MTEXT) dengan posisi + tinggi. Koordinat dibulatkan 0.1.
Keluaran ringkas, satu entitas per baris: JENIS layer geometri.
"""
import argparse, re
import ezdxf


def r(v): return round(float(v), 1)


def row(e):
    t, L = e.dxftype(), e.dxf.layer
    if t == "LINE":
        a, b = e.dxf.start, e.dxf.end
        return (a.x, a.y), f"LINE {L} ({r(a.x)},{r(a.y)})->({r(b.x)},{r(b.y)})"
    if t == "CIRCLE":
        c = e.dxf.center
        return (c.x, c.y), f"CIRCLE {L} c=({r(c.x)},{r(c.y)}) r={r(e.dxf.radius)}"
    if t == "ARC":
        c = e.dxf.center
        return (c.x, c.y), f"ARC {L} c=({r(c.x)},{r(c.y)}) r={r(e.dxf.radius)} {round(e.dxf.start_angle)}-{round(e.dxf.end_angle)}"
    if t == "LWPOLYLINE":
        p = e.get_points("xy")
        return p[0], f"LWPOLYLINE {L} closed={e.closed} ltype={e.dxf.linetype} " + " ".join(f"({r(x)},{r(y)})" for x, y in p[:8]) + (" ..." if len(p) > 8 else "")
    if t == "INSERT":
        p = e.dxf.insert
        return (p.x, p.y), f"INSERT {L} {e.dxf.name} at=({r(p.x)},{r(p.y)}) sc={r(e.dxf.xscale)} rot={round(e.dxf.rotation)}"
    if t in ("TEXT", "MTEXT"):
        p = e.dxf.insert
        s = re.sub(r"\s+", " ", e.plain_text() if t == "MTEXT" else e.dxf.text).strip()
        h = e.dxf.char_height if t == "MTEXT" else e.dxf.height
        return (p.x, p.y), f"{t} {L} at=({r(p.x)},{r(p.y)}) h={r(h)} '{s[:60]}'"
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    for k in ("x0", "x1", "y0", "y1"): ap.add_argument(k, type=float)
    ap.add_argument("--types", default="")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--max", type=int, default=400)
    a = ap.parse_args()
    types = {t.strip().upper() for t in a.types.split(",") if t.strip()}
    if a.text: types = {"TEXT", "MTEXT"}
    doc = ezdxf.readfile(a.src)
    n = 0
    for e in doc.modelspace():
        if types and e.dxftype() not in types: continue
        pt, line = row(e)
        if pt is None: continue
        if a.x0 <= pt[0] <= a.x1 and a.y0 <= pt[1] <= a.y1:
            n += 1
            if n <= a.max: print(line)
    print(f"-- {n} entitas" + (f" (ditampilkan {a.max})" if n > a.max else ""))


if __name__ == "__main__":
    main()
