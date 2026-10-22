#!/usr/bin/env python3
"""verify.py - bandingkan dua DXF: apa yang ditambah / dihapus / berubah (semua layout).

  python tools/verify.py SEBELUM.dxf SESUDAH.dxf [--region X0 X1 Y0 Y1] [--show 15]

Keluaran: jumlah entitas per jenis, daftar yang ditambah/dihapus, dan (dengan --region)
apakah ada perubahan di LUAR area yang dimaksud. Garis dianggap sama walau arahnya terbalik.
Exit code 1 bila ada perubahan di luar --region (cocok untuk skrip batch).
"""
import argparse, sys
from collections import Counter
import ezdxf


def r(v): return round(float(v), 1)


def sig(e, lname):
    t, L = e.dxftype(), e.dxf.layer
    try:
        if t == "LINE":
            a, b = e.dxf.start, e.dxf.end
            p = tuple(sorted([(r(a.x), r(a.y)), (r(b.x), r(b.y))])); return (lname, t, L, p), p[0]
        if t == "CIRCLE":
            c = e.dxf.center; return (lname, t, L, r(c.x), r(c.y), r(e.dxf.radius)), (c.x, c.y)
        if t == "ARC":
            c = e.dxf.center; return (lname, t, L, r(c.x), r(c.y), r(e.dxf.radius), round(e.dxf.start_angle), round(e.dxf.end_angle)), (c.x, c.y)
        if t in ("TEXT", "MTEXT"):
            p = e.dxf.insert; s = e.dxf.text if t == "TEXT" else e.text
            return (lname, t, L, r(p.x), r(p.y), s), (p.x, p.y)
        if t == "INSERT":
            p = e.dxf.insert
            return (lname, t, L, e.dxf.name, r(p.x), r(p.y), r(e.dxf.xscale), round(e.dxf.rotation)), (p.x, p.y)
        if t == "LWPOLYLINE":
            pts = tuple((r(x), r(y)) for x, y in e.get_points("xy")); return (lname, t, L, pts, e.closed), pts[0]
    except Exception:
        pass
    return (lname, t, L), None


def load(path):
    doc = ezdxf.readfile(path)
    c, pt = Counter(), {}
    for lo in doc.layouts:
        for e in lo:
            s, p = sig(e, lo.name)
            c[s] += 1; pt[s] = p
    return c, pt


def fmt(s): return " ".join(str(x) for x in s)[:150]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before"); ap.add_argument("after")
    ap.add_argument("--region", nargs=4, type=float, metavar=("X0", "X1", "Y0", "Y1"))
    ap.add_argument("--show", type=int, default=15)
    a = ap.parse_args()
    cb, pb = load(a.before); ca, pa = load(a.after)
    added, removed = ca - cb, cb - ca
    tb = Counter(k[1] for k, n in cb.items() for _ in range(n)); ta = Counter(k[1] for k, n in ca.items() for _ in range(n))
    print(f"entitas: {sum(cb.values())} -> {sum(ca.values())}  (ditambah {sum(added.values())}, dihapus/berubah {sum(removed.values())})")
    for t in sorted(set(tb) | set(ta)):
        if tb[t] != ta[t]: print(f"  {t}: {tb[t]} -> {ta[t]}")
    for title, d, pts in (("DITAMBAH", added, pa), ("DIHAPUS/BERUBAH", removed, pb)):
        if d:
            print(f"\n{title}:")
            for k, n in list(d.items())[:a.show]: print(f"  {n}x {fmt(k)}")
            if len(d) > a.show: print(f"  ... +{len(d) - a.show} lagi")
    bad = 0
    if a.region:
        x0, x1, y0, y1 = a.region
        for d, pts in ((added, pa), (removed, pb)):
            for k in d:
                p = pts.get(k)
                if p is None or not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1): bad += d[k]
        print(f"\nPerubahan di LUAR region: {bad}" + ("  <-- PERIKSA" if bad else "  (aman)"))
    if not added and not removed: print("\nTidak ada perbedaan.")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
