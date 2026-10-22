"""Contoh: koneksi CT-3 -> terminal baru (-X87LC) -> fungsi 79 (-F87L) pada Line Bay ITP-1.
Jalankan dari folder proyek:  python examples/ct3_to_79.py drawings/a.dxf out/a_ct3-79.dxf
Urutan: verifikasi titik acuan -> salin simbol terminal -> tarik kabel -> simpan."""
import sys
sys.path.insert(0, "tools")
from dxf_edit import Edit, text_of

src, out = sys.argv[1], sys.argv[2]
ed = Edit(src, out)

# 1. TITIK ACUAN (assert: berhenti bila geometri tidak seperti yang diperkirakan)
ct3 = ed.one("CIRCLE", 33014.7, -2577.5, tol=0.2, layer="SAO")      # ujung garis CT-3
c79 = ed.one("CIRCLE", 34138.8, -1844.4, tol=0.2)                   # lingkaran fungsi 79
ct3_top = (ct3.dxf.center.x, ct3.dxf.center.y + ct3.dxf.radius)
p79_bot = (c79.dxf.center.x, c79.dxf.center.y - c79.dxf.radius)

# 2. PERENCANAAN (angka hasil pengukuran, bukan tebakan)
Y_OLD, Y_NEW = -2039.9, -1942.2      # baris -X87LB -> slot kosong berjarak sama (97.7)
DY = Y_NEW - Y_OLD
XV, YH = 33490.0, -2068.0            # belokan di dalam kotak P546 (di antara garis -X87LB dan teks MICOM P546)

# 3. SALIN SIMBOL TERMINAL (gaya ikut) + label baru
term = ed.select(lambda e: e.dxf.layer == "0" and (
    (e.dxftype() == "ARC" and 33360 < e.dxf.center.x < 33400 and -2046 < e.dxf.center.y < -2034) or
    (e.dxftype() == "LINE" and abs(e.dxf.start.y - Y_OLD) < .5 and abs(e.dxf.end.y - Y_OLD) < .5
     and 33360 < e.dxf.start.x < 33400 and 33360 < e.dxf.end.x < 33400)))
ed.copy(term, dy=DY)
# PENTING: batasi juga X — bay ITP-2 adalah salinan ITP-1 dan punya teks yang sama di y yang sama
lab = ed.select(lambda e: (e.dxftype() == "TEXT" and text_of(e) == "-X87LB"
                           and abs(e.dxf.insert.x - 33338.3) < 1 and abs(e.dxf.insert.y + 2025.2) < 1)
                or (e.dxftype() == "MTEXT" and text_of(e) == "**"
                    and abs(e.dxf.insert.x - 33382.3) < 1 and abs(e.dxf.insert.y + 2025.3) < 1))
ed.copy(lab, dy=DY, text={"-X87LB": "-X87LC"})

# 4. KABEL
x = ct3_top[0]
ed.wire([ct3_top, (x, Y_NEW), (33308.0, Y_NEW)])
ed.line((33308.0, Y_NEW), (33356.8, Y_NEW), "CONNECTION")
ed.line((33405.7, Y_NEW), (33454.6, Y_NEW), "CONNECTION")
ed.wire([(33454.6, Y_NEW), (XV, Y_NEW), (XV, YH), (p79_bot[0], YH), p79_bot])

ed.save()
