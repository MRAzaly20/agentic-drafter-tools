#!/usr/bin/env python3
"""render.py - render area modelspace DXF ke PNG (untuk dilihat agent / engineer).

  python tools/render.py FILE OUT.png X0 X1 Y0 Y1 [--grid 200] [--dpi 110] [--width 16]

X0 X1 Y0 Y1 = batas area dalam satuan drawing. --grid menampilkan garis bantu + angka koordinat
(berguna untuk membaca titik acuan). Hanya modelspace; sheet paperspace diperiksa lewat scan.py.
"""
import argparse
import ezdxf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("x0", type=float); ap.add_argument("x1", type=float)
    ap.add_argument("y0", type=float); ap.add_argument("y1", type=float)
    ap.add_argument("--grid", type=float, default=0, help="jarak garis bantu (satuan drawing), 0 = mati")
    ap.add_argument("--dpi", type=int, default=110)
    ap.add_argument("--width", type=float, default=16, help="lebar gambar (inci)")
    a = ap.parse_args()
    x0, x1, y0, y1 = a.x0, a.x1, a.y0, a.y1
    if x1 <= x0 or y1 <= y0:
        raise SystemExit("Batas area tidak valid: pastikan X0<X1 dan Y0<Y1")
    doc = ezdxf.readfile(a.src)
    h = max(2.0, a.width * (y1 - y0) / (x1 - x0))
    fig = plt.figure(figsize=(a.width, h), dpi=a.dpi)
    ax = fig.add_axes([0.04, 0.04, 0.95, 0.95] if a.grid else [0, 0, 1, 1])
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
    Frontend(RenderContext(doc), MatplotlibBackend(ax), config=cfg).draw_layout(doc.modelspace(), finalize=False)
    ax.set_xlim(x0, x1); ax.set_ylim(y0, y1)
    if a.grid:
        import numpy as np
        ax.set_axis_on()  # backend ezdxf mematikan sumbu secara default
        ax.xaxis.set_visible(True); ax.yaxis.set_visible(True)
        for sp in ax.spines.values(): sp.set_visible(True)
        ax.set_xticks(np.arange((x0 // a.grid) * a.grid, x1 + a.grid, a.grid))
        ax.set_yticks(np.arange((y0 // a.grid) * a.grid, y1 + a.grid, a.grid))
        ax.grid(True, color="#d0d0ff", linewidth=0.4)
        ax.tick_params(labelsize=7, colors="black", labelbottom=True, labelleft=True, bottom=True, left=True)
        ax.set_xlim(x0, x1); ax.set_ylim(y0, y1)
    else:
        ax.axis("off")
    fig.savefig(a.out, facecolor="white")
    print("ok ->", a.out)


if __name__ == "__main__":
    main()
