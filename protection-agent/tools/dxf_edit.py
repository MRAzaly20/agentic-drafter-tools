"""dxf_edit.py - helper edit DXF yang aman: titik acuan diverifikasi, selalu simpan ke file baru.

Contoh (lihat examples/ct3_to_79.py):

    from dxf_edit import Edit
    ed = Edit("drawings/a.dxf", "out/a_rev.dxf")          # OUT tidak boleh sama dengan SRC
    ct3 = ed.one("CIRCLE", 33014.7, -2577.5, layer="SAO") # gagal jika tidak tepat 1 objek
    ed.wire([(x, y), (x, y2), (x2, y2)])                   # polyline orthogonal -> LINE per segmen
    ed.copy(ed.select(lambda e: ...), dy=97.7, text={"-X87LB": "-X87LC"})
    ed.save()                                              # cetak selisih jumlah entitas
"""
from pathlib import Path
import ezdxf


def pos(e):
    t = e.dxftype()
    if t in ("CIRCLE", "ARC"): return e.dxf.center.x, e.dxf.center.y
    if t in ("TEXT", "MTEXT", "INSERT"): return e.dxf.insert.x, e.dxf.insert.y
    if t == "LINE": return e.dxf.start.x, e.dxf.start.y
    if t == "LWPOLYLINE":
        p = e.get_points("xy"); return p[0]
    return None


def text_of(e):
    return (e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text).strip()


class Edit:
    def __init__(self, src, out):
        if Path(src).resolve() == Path(out).resolve():
            raise SystemExit("OUT tidak boleh sama dengan SRC (jangan menimpa file asli)")
        self.doc = ezdxf.readfile(src)
        self.msp = self.doc.modelspace()
        self.out, self.n0 = out, len(self.msp)

    # ---- mencari / memverifikasi titik acuan
    def select(self, pred):
        return [e for e in self.msp if pred(e)]

    def find(self, dxftype, x, y, tol=0.5, layer=None, text=None):
        res = []
        for e in self.msp:
            if e.dxftype() != dxftype: continue
            p = pos(e)
            if p is None or abs(p[0] - x) > tol or abs(p[1] - y) > tol: continue
            if layer and e.dxf.layer != layer: continue
            if text is not None and text_of(e) != text: continue
            res.append(e)
        return res

    def one(self, dxftype, x, y, **kw):
        res = self.find(dxftype, x, y, **kw)
        assert len(res) == 1, f"Titik acuan {dxftype} ({x},{y}) {kw}: ditemukan {len(res)} objek, seharusnya 1"
        return res[0]

    # ---- menambah geometri
    def line(self, a, b, layer="0"):
        return self.msp.add_line(a, b, dxfattribs={"layer": layer})

    def wire(self, pts, layer="0"):
        for a, b in zip(pts, pts[1:]):
            self.line(a, b, layer)

    def copy(self, entities, dx=0.0, dy=0.0, text=None):
        """Salin entitas (gaya/layer ikut), geser (dx,dy). text={'lama':'baru'} mengganti isi TEXT/MTEXT."""
        out = []
        for e in entities:
            c = e.copy(); self.msp.add_entity(c); c.translate(dx, dy, 0)
            if text and c.dxftype() in ("TEXT", "MTEXT"):
                old = text_of(c)
                if old in text:
                    if c.dxftype() == "TEXT": c.dxf.text = text[old]
                    else: c.text = text[old]
            out.append(c)
        return out

    def save(self):
        self.doc.saveas(self.out)
        print(f"disimpan: {self.out} | entitas {self.n0} -> {len(self.msp)} (+{len(self.msp) - self.n0})")
