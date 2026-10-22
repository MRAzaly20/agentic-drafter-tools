#!/usr/bin/env python3
"""
scan.py - Pengindeks drawing DXF (SLD / sistem proteksi gardu) untuk coding agent.

Membaca banyak file DXF (atau DWG bila ada converter), mengenali STYLE drawing
(profil di profiles.json), lalu menulis indeks kecil yang mudah dibaca agent:

  index/manifest.json        daftar file, profil terdeteksi, jumlah sheet/perangkat
  index/files/<nama>.json    detail per file (sheet, perangkat, rasio, placeholder, ...)
  index/devices.csv          semua perangkat (designation, jenis, model, deskripsi, posisi)
  index/functional_units.csv kode panel/bay (=E0X, +WR1, ...) + keterangan
  index/ratios.csv           rasio CT/VT (+ penanda placeholder XXXX)
  index/placeholders.csv     isian yang belum terisi (XXXX, XXkA, ...)
  index/sheets.csv           sheet: nomor gambar, revisi, jenis, judul
  index/texts.csv            semua teks (untuk grep)           [kecuali --no-texts]
  index/nets.json            konektivitas garis (eksperimental) [hanya --nets]
  index/toc.md, summary.md   ringkasan untuk dibaca agent

Contoh:
  python scan.py drawings/ -o index -j 4
  python scan.py drawings/ -o index --profile iec_ecad_export
  python scan.py --discover file_baru.dxf          # bantu membuat profil style baru
  python scan.py --list-profiles

Hanya butuh: pip install ezdxf
"""
import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

try:
    import ezdxf
    from ezdxf import bbox as ezbbox
    from ezdxf import recover
except ImportError:  # pragma: no cover
    sys.exit("ezdxf belum terpasang. Jalankan: pip install ezdxf")

SCAN_VERSION = 2
GUID_RX = re.compile(r"[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
HERE = Path(__file__).resolve().parent

GENERIC_FALLBACK = {
    "name": "generic", "description": "fallback", "signatures": [],
    "designation_regex": r"^-[A-Z]{1,4}\d{0,4}[A-Z]{0,3}$",
    "functional_regex": r"^[=+][A-Z0-9]{2,8}(?:[+=][A-Z0-9]{1,8})*$",
    "device_models": [], "model_link_kinds": ["protection_or_fuse", "relay", "device_unit", "meter"],
    "protection_functions": r"^(21|25|27|32|46|47|50|51|50N|51N|50/51|59|67|67N|79|81|86|87[A-Z]{0,2})$",
    "bay_keywords": "BAY|FEEDER|TRANSFORMER|PANEL", "drawing_no_regex": r"[A-Z0-9]{2,}[-_][A-Z0-9_-]{4,}",
    "frame_block_regex": r"^(.*FRAME.*|.*TITLE.*|.*BORDER.*|SCH.*)$", "title_attr_keys": {},
    "device_tag_attrs": ["TAG", "TAG1"], "device_desc_attrs": ["DESC", "DESC1"], "device_model_attrs": ["MODEL", "TYPE"],
}

# --------------------------------------------------------------------------- util


def load_profiles(path=None):
    p = Path(path) if path else HERE / "profiles.json"
    if not p.exists():
        print(f"[peringatan] {p} tidak ada; memakai profil generic saja", file=sys.stderr)
        return {"profiles": [GENERIC_FALLBACK], "default_labels": {}, "sheet_types": [],
                "kind_rules_by_description": [], "kind_rules_by_designation": []}
    return json.loads(p.read_text(encoding="utf-8"))


def clean_text(s):
    if s is None:
        return ""
    s = (s.replace("%%c", "Ø").replace("%%C", "Ø").replace("%%d", "°").replace("%%D", "°")
         .replace("%%p", "±").replace("%%P", "±").replace("%%u", "").replace("%%U", "")
         .replace("%%o", "").replace("%%O", ""))
    return s


def one_line(s):
    return re.sub(r"\s+", " ", s).strip()


def mtext_plain(e):
    try:
        return clean_text(e.plain_text())
    except Exception:
        return clean_text(re.sub(r"\\[A-Za-z][^;]*;|[{}]", "", e.text or ""))


def rnd(v, n=1):
    return round(float(v), n)


class Rect:
    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0, self.x1, self.y1 = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def contains(self, x, y):
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def expand(self, mx, my=None, bottom=None):
        my = mx if my is None else my
        return Rect(self.x0 - mx, self.y0 - (bottom if bottom is not None else my), self.x1 + mx, self.y1 + my)

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def h(self):
        return self.y1 - self.y0

    def as_list(self):
        return [rnd(self.x0), rnd(self.y0), rnd(self.x1), rnd(self.y1)]


# ------------------------------------------------------------------ pembacaan file


def find_dwg_converter():
    for name in ("ODAFileConverter", "ODAFileConverter.exe"):
        if shutil.which(name):
            return ("oda", shutil.which(name))
    if shutil.which("dwg2dxf"):
        return ("libredwg", shutil.which("dwg2dxf"))
    env = os.environ.get("ODA_CONVERTER")
    if env and Path(env).exists():
        return ("oda", env)
    return None


def dwg_to_dxf(path, tmpdir):
    conv = find_dwg_converter()
    if not conv:
        raise RuntimeError("DWG ditemukan tetapi tidak ada converter (ODAFileConverter / dwg2dxf). "
                           "Konversi dulu ke DXF, atau pasang ODA File Converter.")
    kind, exe = conv
    src_dir = Path(tmpdir) / "in"
    out_dir = Path(tmpdir) / "out"
    src_dir.mkdir()
    out_dir.mkdir()
    shutil.copy(path, src_dir / Path(path).name)
    if kind == "oda":
        subprocess.run([exe, str(src_dir), str(out_dir), "ACAD2018", "DXF", "0", "1", Path(path).name],
                       check=True, capture_output=True, timeout=600)
    else:
        subprocess.run([exe, "-o", str(out_dir / (Path(path).stem + ".dxf")), str(src_dir / Path(path).name)],
                       check=True, capture_output=True, timeout=600)
    outs = list(out_dir.glob("*.dxf"))
    if not outs:
        raise RuntimeError("konversi DWG gagal (tidak ada keluaran)")
    return outs[0]


def read_doc(path):
    """Mengembalikan (doc, catatan). Memakai mode recover bila file rusak ringan."""
    notes = []
    try:
        return ezdxf.readfile(path), notes
    except (ezdxf.DXFStructureError, UnicodeDecodeError) as exc:
        notes.append(f"readfile gagal ({exc}); memakai recover")
        doc, auditor = recover.readfile(path)
        if auditor.has_errors:
            notes.append(f"recover: {len(auditor.errors)} error diperbaiki")
        return doc, notes


# --------------------------------------------------------------- pengumpulan teks


def text_blocks_set(doc):
    """Nama block yang (langsung/bersarang) memuat TEXT/MTEXT/ATTRIB."""
    has = set()
    for b in doc.blocks:
        if any(x.dxftype() in ("TEXT", "MTEXT") for x in b):
            has.add(b.name)
    changed = True
    while changed:
        changed = False
        for b in doc.blocks:
            if b.name in has:
                continue
            if any(x.dxftype() == "INSERT" and x.dxf.name in has for x in b):
                has.add(b.name)
                changed = True
    return has


def _text_record(e, src, extra=None):
    t = e.dxftype()
    try:
        if t == "TEXT":
            raw = clean_text(e.dxf.text)
            h = e.dxf.height
            if e.dxf.hasattr("align_point") and (e.dxf.get("halign", 0) or e.dxf.get("valign", 0)):
                p = e.dxf.align_point
            else:
                p = e.dxf.insert
        elif t == "MTEXT":
            raw = mtext_plain(e)
            h = e.dxf.get("char_height", 0)
            p = e.dxf.insert
        elif t in ("ATTRIB", "ATTDEF"):
            raw = clean_text(e.dxf.text)
            h = e.dxf.get("height", 0)
            p = e.dxf.insert
        else:
            return None
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    if GUID_RX.fullmatch(raw.strip()):
        return None
    rec = {"text": raw.strip(), "x": float(p.x), "y": float(p.y), "h": float(h or 0),
           "layer": e.dxf.get("layer", "0"), "src": src, "type": t}
    if extra:
        rec.update(extra)
    return rec


def collect_texts(layout, tb_names, depth=0, out=None, max_depth=3):
    """Semua teks dalam layout (termasuk di dalam block yang memuat teks)."""
    out = [] if out is None else out
    for e in layout:
        t = e.dxftype()
        if t in ("TEXT", "MTEXT"):
            r = _text_record(e, "text")
            if r:
                out.append(r)
        elif t == "ATTDEF" and depth == 0:
            r = _text_record(e, "attdef", {"tag": e.dxf.get("tag", "")})
            if r:
                out.append(r)
        elif t == "INSERT":
            bname = e.dxf.name
            for a in getattr(e, "attribs", []):
                r = _text_record(a, "attrib", {"tag": a.dxf.get("tag", ""), "block": bname})
                if r:
                    out.append(r)
            if depth < max_depth and bname in tb_names:
                try:
                    for v in e.virtual_entities():
                        vt = v.dxftype()
                        if vt in ("TEXT", "MTEXT"):
                            r = _text_record(v, "blocktext", {"block": bname})
                            if r:
                                out.append(r)
                        elif vt == "INSERT":
                            sub = collect_texts([v], tb_names, depth + 1, [], max_depth)
                            for r in sub:
                                r.setdefault("block", bname)
                            out.extend(sub)
                except Exception:
                    pass
    return out


def lines_of(rec):
    return [l.strip() for l in rec["text"].replace("\\P", "\n").split("\n") if l.strip()]


# ------------------------------------------------------------- deteksi style (profil)


def file_signature_sets(doc, msp, texts):
    layers = {}
    for e in msp:
        lname = e.dxf.get("layer", "0")
        layers[lname] = layers.get(lname, 0) + 1
    for l in doc.layers:
        layers.setdefault(l.dxf.name, 0)
    blocks = set(b.name for b in doc.blocks if not b.name.startswith("*"))
    blocks |= {e.dxf.name for e in msp if e.dxftype() == "INSERT"}
    attrs = {r.get("tag", "").upper() for r in texts if r.get("tag")}
    for b in doc.blocks:
        for x in b:
            if x.dxftype() == "ATTDEF":
                attrs.add(x.dxf.get("tag", "").upper())
    for x in msp.query("ATTDEF"):
        attrs.add(x.dxf.get("tag", "").upper())
    textlines = []
    for r in texts:
        textlines.extend(lines_of(r))
    return layers, blocks, attrs, textlines


def score_profiles(profiles, layers, blocks, attrs, textlines):
    scores = {}
    for p in profiles:
        sigs = p.get("signatures", [])
        total = sum(s.get("weight", 1) for s in sigs) or 1
        got = 0.0
        for s in sigs:
            rx = re.compile(s["regex"], re.I if s["kind"] in ("layer", "block") else 0)
            if s["kind"] == "layer":
                n = sum(1 for k in layers if rx.search(k))
            elif s["kind"] == "block":
                n = sum(1 for k in blocks if rx.search(k))
            elif s["kind"] == "attr":
                n = sum(1 for k in attrs if rx.search(k))
            else:
                n = sum(1 for l in textlines if rx.search(l))
            cap = s.get("cap", 3)
            got += s.get("weight", 1) * min(n, cap) / cap
        scores[p["name"]] = round(got / total, 3)
    return scores


def pick_profile(profiles, scores, forced, min_score=0.25):
    by = {p["name"]: p for p in profiles}
    if forced and forced != "auto":
        if forced not in by:
            sys.exit(f"Profil '{forced}' tidak ada. Pilihan: {', '.join(by)}")
        return by[forced], scores.get(forced, 0.0), "dipaksa"
    cand = {k: v for k, v in scores.items() if k != "generic"}
    if cand:
        best = max(cand, key=cand.get)
        if cand[best] >= min_score:
            return by[best], cand[best], "otomatis"
    return by.get("generic", GENERIC_FALLBACK), scores.get("generic", 0.0), "fallback (tidak ada profil yang cocok)"


# ------------------------------------------------------------------- deteksi sheet


def vp_model_rect(vp):
    w, h = vp.dxf.width, vp.dxf.height
    vh = vp.dxf.view_height
    if not (w > 0 and h > 0 and vh > 0):
        return None
    vw = vh * w / h
    c = vp.dxf.view_center_point
    return Rect(c.x - vw / 2, c.y - vh / 2, c.x + vw / 2, c.y + vh / 2)


def vp_paper_rect(vp):
    c = vp.dxf.center
    return Rect(c.x - vp.dxf.width / 2, c.y - vp.dxf.height / 2, c.x + vp.dxf.width / 2, c.y + vp.dxf.height / 2)


def frame_rect_for(layout, frame_rx, vp_rect):
    """Cari INSERT frame/title-block yang melingkupi pusat viewport -> Rect frame."""
    cx, cy = (vp_rect.x0 + vp_rect.x1) / 2, (vp_rect.y0 + vp_rect.y1) / 2
    best = None
    for e in layout.query("INSERT"):
        if not frame_rx.search(e.dxf.name):
            continue
        try:
            b = ezbbox.extents([e])
        except Exception:
            continue
        if not b.has_data:
            continue
        r = Rect(b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)
        if r.contains(cx, cy) and (best is None or r.w * r.h < best.w * best.h):
            best = r
    return best


def model_frame_sheets(msp, texts, th):
    """Cadangan untuk drawing tanpa viewport: cari persegi besar berproporsi kertas di modelspace."""
    rects = []
    for e in msp.query("LWPOLYLINE"):
        try:
            pts = e.get_points("xy")
        except Exception:
            continue
        if not (e.closed or (len(pts) == 5 and pts[0] == pts[-1])) or len(pts) not in (4, 5):
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if min(w, h) <= 0 or w < 40 * th or h < 25 * th:
            continue
        ratio = max(w, h) / min(w, h)
        if 1.25 <= ratio <= 1.65:
            rects.append(Rect(min(xs), min(ys), max(xs), max(ys)))
    keep = []
    for r in rects:
        if any(o is not r and o.x0 <= r.x0 and o.y0 <= r.y0 and o.x1 >= r.x1 and o.y1 >= r.y1 and
               (o.w * o.h) > (r.w * r.h) * 1.001 for o in rects):
            continue
        n = sum(1 for t in texts if r.contains(t["x"], t["y"]))
        if n >= 10:
            keep.append(r)
    keep.sort(key=lambda r: (-round(r.y1 / (th * 50)), r.x0))
    return keep


def detect_sheets(doc, msp, model_texts, tb_names, prof, th):
    sheets = []
    frame_rx = re.compile(prof.get("frame_block_regex", GENERIC_FALLBACK["frame_block_regex"]), re.I)
    for lo in doc.layouts:
        if lo.name == "Model":
            continue
        vps = [v for v in lo.query("VIEWPORT")]
        usable = []
        for v in vps:
            if v.dxf.id == 1 and len(vps) > 1:
                continue
            mr = vp_model_rect(v)
            if mr is None:
                continue
            vd = v.dxf.get("view_direction_vector", None)
            if vd is not None and (abs(vd.x) > 1e-6 or abs(vd.y) > 1e-6):
                continue
            usable.append((v, mr))
        if not usable:
            continue
        ptexts = collect_texts(lo, tb_names)
        for v, mr in usable:
            pr = vp_paper_rect(v)
            fr = frame_rect_for(lo, frame_rx, pr)
            if fr is None:
                fr = pr.expand(max(5, pr.w * 0.03), max(5, pr.h * 0.03), max(5, pr.h * 0.12))
            sheets.append({"source": "viewport", "layout": lo.name, "vp_id": int(v.dxf.id),
                           "paper_rect": fr, "model_rect": mr.expand(mr.w * 0.04, mr.h * 0.04),
                           "title_texts": [t for t in ptexts if fr.contains(t["x"], t["y"])],
                           "frame_insert_attribs": _frame_attribs(lo, frame_rx, fr)})
    if sheets:
        sheets.sort(key=lambda s: (s["layout"], s["paper_rect"].x0, -s["paper_rect"].y0))
        return sheets
    frames = model_frame_sheets(msp, model_texts, th)
    if frames:
        for r in frames:
            sheets.append({"source": "model_frame", "layout": "Model", "vp_id": None, "paper_rect": r,
                           "model_rect": r, "title_texts": [t for t in model_texts if r.contains(t["x"], t["y"])],
                           "frame_insert_attribs": {}})
        return sheets
    xs = [t["x"] for t in model_texts] or [0]
    ys = [t["y"] for t in model_texts] or [0]
    r = Rect(min(xs) - th, min(ys) - th, max(xs) + th * 20, max(ys) + th * 3)
    return [{"source": "whole_model", "layout": "Model", "vp_id": None, "paper_rect": r, "model_rect": r,
             "title_texts": list(model_texts), "frame_insert_attribs": {}}]


def _frame_attribs(layout, frame_rx, rect):
    d = {}
    for e in layout.query("INSERT"):
        if frame_rx.search(e.dxf.name) and getattr(e, "attribs", None):
            p = e.dxf.insert
            if rect.expand(rect.w * 0.05).contains(p.x, p.y):
                for a in e.attribs:
                    if a.dxf.text:
                        d[a.dxf.tag.upper()] = clean_text(a.dxf.text).strip()
    return d


# --------------------------------------------------------- ekstraksi isi per sheet

CT_RX = re.compile(r"(?P<prim>[\dX]{1,5}(?:\s*[-–]\s*[\dX]{1,5})*)\s*[/:]\s*(?P<sec>[\dX](?:\.\d)?)\s*A\b", re.I)
CT_ID_RX = re.compile(r"^\s*(?P<id>(?:CT|TA)[-_ ]?\d{1,2}[A-Z]?)\s*[;:,]", re.I)
CLASS_RX = re.compile(r"CL(?:ASS)?\s*[:.]?\s*(?P<cl>[A-Z0-9.]+)", re.I)
VA_RX = re.compile(r"(?P<va>[\dX.]+\s*VA)", re.I)
VT_RX = re.compile(r"\d+(?:\.\d+)?\s*kV\s*(?:/\s*[√V]?\s*3)?\s*/\s*", re.I)
KV_RX = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*kV\b", re.I)
PLACEHOLDER_RX = re.compile(r"(?<![A-Za-z])(?:\d*X{2,}[\w./-]*|X\.X\w*)", re.I)


def is_placeholder_token(tok):
    return bool(re.search(r"X{2,}|X\.X", tok, re.I)) and not re.search(r"[a-wyz]{3,}", tok.lower())


def extract_ratios(texts):
    out = []
    for t in texts:
        for ln in lines_of(t):
            m = CT_RX.search(ln)
            if m and (re.match(r"^\s*(CT|TA)", ln, re.I) or "CT" in ln.upper()[:12] or CLASS_RX.search(ln)):
                prim, sec = re.sub(r"\s+", "", m.group("prim")), m.group("sec")
                cid = CT_ID_RX.match(ln)
                cl = CLASS_RX.search(ln)
                va = VA_RX.search(ln)
                out.append({"kind": "CT", "id": cid.group("id").strip() if cid else "",
                            "primary": prim, "secondary": sec + "A", "class": cl.group("cl") if cl else "",
                            "burden": va.group("va") if va else "", "placeholder": bool(re.search("X", prim + sec, re.I)),
                            "text": one_line(ln), "x": rnd(t["x"]), "y": rnd(t["y"])})
            elif VT_RX.search(ln) and re.search(r"[√]|/V3|LVT|\bVT\b|\bPT\b", ln, re.I):
                out.append({"kind": "VT", "id": "", "primary": "", "secondary": "", "class": "",
                            "burden": "", "placeholder": bool(PLACEHOLDER_RX.search(ln)),
                            "text": one_line(ln), "x": rnd(t["x"]), "y": rnd(t["y"])})
    return out


def extract_placeholders(texts):
    out = []
    for t in texts:
        if t["src"] == "attdef":
            continue
        for ln in lines_of(t):
            toks = [m.group(0) for m in PLACEHOLDER_RX.finditer(ln)]
            toks = [k for k in toks if is_placeholder_token(k)]
            if toks:
                out.append({"text": one_line(ln), "tokens": toks, "x": rnd(t["x"]), "y": rnd(t["y"])})
    return out


def classify_kind(desig, desc, rules):
    d = (desc or "").upper()
    for kind, rx in rules.get("kind_rules_by_description", []):
        if d and re.search(rx, d):
            return kind
    core = re.sub(r"^(?:[=+][A-Z0-9]+[-.]?)+", "", desig or "")
    for kind, rx in rules.get("kind_rules_by_designation", []):
        if re.search(rx, core or desig or "", re.I):
            return kind
    return "unknown"


def extract_devices(texts, insert_devices, prof, rules, th):
    desig_rx = re.compile(prof["designation_regex"])
    model_rxs = [re.compile(p, re.I) for p in prof.get("device_models", [])]
    func_rx = re.compile(prof.get("protection_functions", GENERIC_FALLBACK["protection_functions"]))
    devs, models, funcs = [], [], []
    for t in texts:
        if t["src"] in ("attdef", "attrib"):
            continue
        ls = lines_of(t)
        if not ls:
            continue
        first = ls[0]
        m = re.match(r"^(\S+)\s*(.*)$", first)
        tok, rest = (m.group(1), m.group(2)) if m else (first, "")
        if desig_rx.match(tok):
            desc = " ".join(([rest] if rest else []) + ls[1:])
            devs.append({"designation": tok, "description": one_line(desc), "model": "", "source": t["src"],
                         "x": t["x"], "y": t["y"], "h": t["h"], "link": "", "link_distance": None})
            continue
        if func_rx.match(first) and len(ls) == 1:
            funcs.append({"function": first, "x": t["x"], "y": t["y"]})
            continue
        for ln in ls:
            for rx in model_rxs:
                mm = rx.search(ln)
                if mm:
                    models.append({"model": one_line(mm.group(0)).upper(), "x": t["x"], "y": t["y"], "h": t["h"]})
                    break
    devs.extend(insert_devices)
    # deskripsi dari baris teks tepat di bawah designation (label + deskripsi ditulis terpisah)
    free = [t for t in texts if t["src"] not in ("attdef", "attrib")]
    for d in devs:
        if d["description"] or d["source"].startswith("attrib") or not d["h"]:
            continue
        h, cx, cy, chain = d["h"], d["x"], d["y"], []
        for _ in range(3):
            nxt = None
            for t in free:
                tl = lines_of(t)
                if not tl or desig_rx.match(tl[0].split()[0]):
                    continue
                if func_rx.match(tl[0]) or re.match(prof.get("functional_regex", "^$"), tl[0]):
                    continue
                if any(rx.search(tl[0]) for rx in model_rxs):
                    continue
                dy, dx = cy - t["y"], abs(t["x"] - d["x"])
                if 0 < dy <= 1.9 * h and dx <= 3 * h and (nxt is None or dy < nxt[0]):
                    nxt = (dy, t)
            if not nxt:
                break
            chain.append(one_line(nxt[1]["text"]))
            cy = nxt[1]["y"]
        d["description"] = " ".join(chain)
    for d in devs:
        d["kind"] = classify_kind(d["designation"], d["description"], rules)
    # hubungkan model <-> designation terdekat (perangkat berjenis relay/proteksi/dst)
    linkable = [d for d in devs if d["kind"] in prof.get("model_link_kinds", []) and not d["model"]]
    for mo in models:
        best, bd = None, None
        for d in linkable:
            dx, dy = mo["x"] - d["x"], mo["y"] - d["y"]
            dist = math.hypot(dx, dy)
            if dist > 45 * max(th, 1):
                continue
            same_row = abs(dy) <= 0.8 * max(d["h"], th, 1) and dx > 0  # tabel legenda: model di kanan, baris sama
            pen = 0.25 if same_row else (1.0 if dy <= th else 3.0)  # model biasanya di bawah label designation
            sc = dist * pen
            if bd is None or sc < bd:
                best, bd = d, sc
                bdist = dist
        if best is not None and not best["model"]:
            best["model"], best["link"], best["link_distance"] = mo["model"], "proximity", rnd(bdist)
    # fungsi proteksi -> perangkat proteksi terdekat
    prot = [d for d in devs if d["kind"] in ("protection_or_fuse", "device_unit")]
    for f in funcs:
        near, nd = None, None
        for d in prot:
            dist = math.hypot(f["x"] - d["x"], f["y"] - d["y"])
            if nd is None or dist < nd:
                near, nd = d, dist
        f["near_device"] = near["designation"] if near and nd < 60 * max(th, 1) else ""
    return devs, funcs, models


def extract_insert_devices(layout, rect, prof):
    tag_keys = [k.upper() for k in prof.get("device_tag_attrs", [])]
    desc_keys = [k.upper() for k in prof.get("device_desc_attrs", [])]
    model_keys = [k.upper() for k in prof.get("device_model_attrs", [])]
    out = []
    for e in layout.query("INSERT"):
        atts = getattr(e, "attribs", None)
        if not atts:
            continue
        p = e.dxf.insert
        if not rect.contains(p.x, p.y):
            continue
        a = {x.dxf.tag.upper(): clean_text(x.dxf.text).strip() for x in atts if x.dxf.text and x.dxf.text.strip()}
        tag = next((a[k] for k in tag_keys if a.get(k)), "")
        if not tag:
            continue
        desc = " ".join(a[k] for k in desc_keys if a.get(k))
        model = next((a[k] for k in model_keys if a.get(k)), "")
        out.append({"designation": tag, "description": one_line(desc), "model": model, "source": "attrib:" + e.dxf.name,
                    "x": float(p.x), "y": float(p.y), "h": 0.0, "link": "attrib" if model else "", "link_distance": None})
    return out


def extract_functional_units(texts, prof, th):
    rx = re.compile(prof.get("functional_regex", GENERIC_FALLBACK["functional_regex"]))
    out = []
    for t in texts:
        if t["src"] == "attdef":
            continue
        ls = lines_of(t)
        if len(ls) == 1 and rx.match(ls[0]):
            cap, cd = "", None
            h = t["h"] or th
            for direction in (1, -1):  # 1 = di bawah kode, -1 = di atas kode
                for o in texts:
                    if o is t or o["src"] == "attdef" or rx.match(o["text"].strip()):
                        continue
                    dy = (t["y"] - o["y"]) * direction
                    dx = abs(o["x"] - t["x"])
                    if 0 < dy <= 6 * h and dx <= 15 * h:
                        sc = dy + dx * 0.2
                        if cd is None or sc < cd:
                            cap, cd = one_line(o["text"]), sc
                if cap:
                    break
            out.append({"code": ls[0], "caption": cap, "x": rnd(t["x"]), "y": rnd(t["y"])})
    seen, uniq = set(), []
    for u in out:
        k = (u["code"], round(u["x"] / 10), round(u["y"] / 10))
        if k not in seen:
            seen.add(k)
            uniq.append(u)
    return uniq


FIELD_VALID = {
    "drawing_no": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{3,40}$"),
    "sheet": re.compile(r"^(?!0+$)\d{1,4}(\s*/\s*\d+)?$"),
    "revision": re.compile(r"^[A-Za-z0-9]{1,3}$"),
    "date": re.compile(r"^\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}$"),
    "scale": re.compile(r"^(1\s*:\s*\d+|N\.?T\.?S\.?|\d+\s*:\s*1)$", re.I),
    "drawn": re.compile(r"^[A-Za-z][A-Za-z .]{1,24}$"),
    "checked": re.compile(r"^[A-Za-z][A-Za-z .]{1,24}$"),
    "approved": re.compile(r"^[A-Za-z][A-Za-z .]{1,24}$"),
}
LABEL_WORDS = re.compile(r"\b(by|date|revision|sheet|scale|format|drawn|checked|appr|approved|customer|project|drawing no|dwg no|end user|next)\b", re.I)


def label_value_fields(title_texts, rect, labels):
    """Cari nilai di kanan/bawah label (Revision, Sheet, Date, ...). Nilai ATTDEF dipakai bila tak ada yang lain."""
    out, source = {}, {}
    cands = [t for t in title_texts]
    for key, rx in labels.items():
        lrx = re.compile(rx, re.I)
        valid = FIELD_VALID.get(key)
        found = None
        for t in cands:
            if t["src"] == "attdef" or len(t["text"]) > 30 or not lrx.match(t["text"].strip().rstrip(":").strip()):
                continue
            h = t["h"] or 3
            best = {"real": None, "attdef": None}
            for o in cands:
                if o is t:
                    continue
                txt = one_line(o["text"])
                if LABEL_WORDS.search(txt) or lrx.match(txt):
                    continue
                if valid is not None and not valid.match(txt):
                    continue
                dx, dy = o["x"] - t["x"], o["y"] - t["y"]
                right = abs(dy) <= 1.6 * h and 0 < dx <= 25 * h
                below = -5.5 * h <= dy < 0 and abs(dx) <= 4 * h
                if right or below:
                    d = math.hypot(dx, dy) * (1.0 if right else 1.3)
                    slot = "attdef" if o["src"] == "attdef" else "real"
                    if best[slot] is None or d < best[slot][0]:
                        best[slot] = (d, txt)
            pick = best["real"] or best["attdef"]
            if pick:
                found = (pick[1][:60], "attdef" if not best["real"] else "teks")
                break
        if found:
            out[key], source[key] = found
    return out, source


def build_sheet_info(sh, prof, cfg, model_texts, rules, th, idx, stem, opts):
    labels = {**cfg.get("default_labels", {}), **prof.get("labels", {})}
    title_texts = sh["title_texts"]
    mt = [t for t in model_texts if sh["model_rect"].contains(t["x"], t["y"])]
    fields = {}
    ak = {k: [x.upper() for x in v] for k, v in prof.get("title_attr_keys", {}).items()}
    for fld, keys in ak.items():
        for k in keys:
            if sh["frame_insert_attribs"].get(k):
                fields[fld] = sh["frame_insert_attribs"][k]
                break
    dn_rx = re.compile(prof.get("drawing_no_regex", GENERIC_FALLBACK["drawing_no_regex"]))
    lv, lv_src = label_value_fields(title_texts, sh["paper_rect"], labels)
    if "drawing_no" not in fields and "drawing_no" not in lv:
        pr0 = sh["paper_rect"]
        for t in sorted(title_texts, key=lambda t: t["y"]):
            if t["src"] == "attdef" or t["y"] > pr0.y0 + pr0.h * 0.35:
                continue
            m = dn_rx.search(t["text"])
            if m:
                fields["drawing_no"] = m.group(0)
                break
    fields_source = {k: "atribut frame" for k in fields}
    for k, v in lv.items():
        if k not in fields:
            fields[k] = v
            fields_source[k] = ("nilai bawaan ATTDEF (mungkin bukan nilai aktual)" if lv_src[k] == "attdef" else "label di title block")
    if "sheet" not in fields:
        pr = sh["paper_rect"]
        for t in title_texts:
            if t["src"] in ("text", "attrib") and re.fullmatch(r"0?[1-9]\d{0,2}", t["text"].strip()) and t["x"] > pr.x0 + pr.w * 0.6 and t["y"] < pr.y0 + pr.h * 0.2:
                fields["sheet"] = t["text"].strip()
                break
    insert_devs = []
    host = None
    # perangkat ber-atribut dicari di modelspace (di dalam rect model)
    devices, funcs, models = extract_devices(mt, [], prof, rules, th)
    ratios = extract_ratios(mt)
    holders = extract_placeholders(mt)
    units = extract_functional_units(mt, prof, th)
    bay_rx = re.compile(prof.get("bay_keywords", GENERIC_FALLBACK["bay_keywords"]), re.I)
    bays = []
    for t in mt:
        if t["src"] == "attdef":
            continue
        for ln in lines_of(t):
            if bay_rx.search(ln) and len(ln) <= 90 and ln not in bays and not re.search(r"^(NOTE|\d\.)", ln, re.I):
                bays.append(ln)
    volts = sorted({float(m.group(1)) for t in mt for m in KV_RX.finditer(t["text"])})
    title_lines, seen_tl = [], set()
    for t in sorted(title_texts, key=lambda t: (-t["y"], t["x"])):
        tl = one_line(t["text"])
        if t["src"] == "attdef" or len(tl) <= 3 or tl in seen_tl or LABEL_WORDS.fullmatch(tl.rstrip(": ")):
            continue
        seen_tl.add(tl)
        title_lines.append(tl)
    title_lines = title_lines[:40]
    # jenis sheet: (1) isi gambar, (2) heuristik perangkat, (3) teks title block
    stype = "unknown"
    content = " | ".join(one_line(t["text"]) for t in mt if t["src"] != "attdef").upper()
    types = cfg.get("sheet_types", [])
    for typ, rx in types:
        if typ in ("sld_protection", "general_overview"):
            continue
        if re.search(rx, content):
            stype = typ
            break
    if stype == "unknown" and (len(devices) + len(insert_devs)) >= 8 and (funcs or units):
        stype = "sld_protection"
    if stype == "unknown":
        pool = " | ".join(title_lines).upper()
        for typ, rx in types:
            if re.search(rx, pool):
                stype = typ
                break
    info = {
        "sheet_id": f"{stem}#{idx}", "index": idx, "layout": sh["layout"], "source": sh["source"],
        "viewport_id": sh["vp_id"], "paper_rect": sh["paper_rect"].as_list(),
        "model_rect": sh["model_rect"].as_list(), "sheet_type": stype,
        "drawing_no": fields.get("drawing_no", ""), "sheet_no": fields.get("sheet", ""),
        "revision": fields.get("revision", ""), "date": fields.get("date", ""),
        "scale": fields.get("scale", ""), "fields": fields, "fields_source": fields_source, "title_lines": title_lines,
        "bay_names": bays[:12], "voltage_levels_kv": volts,
        "functional_units": units,
        "devices": [{k: (rnd(v) if k in ("x", "y") else v) for k, v in d.items() if k != "h"} for d in devices],
        "protection_functions": [{"function": f["function"], "near_device": f["near_device"],
                                  "x": rnd(f["x"]), "y": rnd(f["y"])} for f in funcs],
        "ratios": ratios, "placeholders": holders, "text_count": len(mt),
        "device_models": sorted({m["model"] for m in models}),
    }
    dn = fields.get("drawing_no", "")
    if dn:
        info["drawing_no_matches_filename"] = bool(re.sub(r"\W", "", dn).upper() in re.sub(r"\W", "", stem).upper())
    if opts.get("texts", True):
        info["_texts"] = [{"layer": t["layer"], "src": t["src"], "x": rnd(t["x"]), "y": rnd(t["y"]),
                           "text": one_line(t["text"])} for t in mt if t["src"] != "attdef"]
    return info, mt


# ------------------------------------------------------------ konektivitas (eksperimental)


def build_nets(doc_layout, rect, texts, th, tol=1.5, bucket=60):
    segs = []
    for e in doc_layout:
        t = e.dxftype()
        if t == "LINE":
            a, b = e.dxf.start, e.dxf.end
            segs.append(((a.x, a.y), (b.x, b.y)))
        elif t == "LWPOLYLINE" and not e.closed:
            try:
                pts = e.get_points("xy")
            except Exception:
                continue
            for i in range(len(pts) - 1):
                segs.append((pts[i], pts[i + 1]))
    segs = [s for s in segs if rect.contains(*s[0]) or rect.contains(*s[1])]
    segs = [s for s in segs if math.dist(*s) > tol]
    n = len(segs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    grid = defaultdict(list)
    for i, (a, b) in enumerate(segs):
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        for gx in range(int(x0 // bucket), int(x1 // bucket) + 1):
            for gy in range(int(y0 // bucket), int(y1 // bucket) + 1):
                if len(grid[(gx, gy)]) < 400:
                    grid[(gx, gy)].append(i)

    def pt_seg(p, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        t = 0 if L2 == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
        return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))

    for i, (a, b) in enumerate(segs):
        for p in (a, b):
            for gx in (int(p[0] // bucket) - 0,):
                for gy in (int(p[1] // bucket) - 0,):
                    for j in grid.get((gx, gy), []):
                        if j != i and pt_seg(p, segs[j][0], segs[j][1]) <= tol:
                            union(i, j)
    comps = defaultdict(list)
    for i in range(n):
        comps[find(i)].append(i)
    ends = Counter()
    for a, b in segs:
        for p in (a, b):
            ends[(round(p[0] / tol), round(p[1] / tol))] += 1
    nets = []
    for cid, idxs in comps.items():
        if len(idxs) < 2:
            continue
        xs = [c for i in idxs for c in (segs[i][0][0], segs[i][1][0])]
        ys = [c for i in idxs for c in (segs[i][0][1], segs[i][1][1])]
        free = []
        for i in idxs:
            for p in segs[i]:
                if ends[(round(p[0] / tol), round(p[1] / tol))] == 1:
                    free.append(p)
        labels = []
        for p in free[:40]:
            for t in texts:
                if t["src"] == "attdef":
                    continue
                if math.hypot(t["x"] - p[0], t["y"] - p[1]) <= 3.5 * max(th, 1):
                    lab = one_line(t["text"])[:40]
                    if lab not in labels:
                        labels.append(lab)
        nets.append({"segments": len(idxs), "bbox": [rnd(min(xs)), rnd(min(ys)), rnd(max(xs)), rnd(max(ys))],
                     "free_ends": len(free), "labels_near_free_ends": labels[:8]})
    nets.sort(key=lambda x: -x["segments"])
    return nets[:600]


# ------------------------------------------------------------------ pemindaian file


def scan_file(args):
    path, root, out_dir, opts = args
    t0 = time.time()
    path = Path(path)
    rel = str(path.relative_to(root)) if root and path.is_relative_to(root) else path.name
    stem = re.sub(r"[^\w.-]+", "_", Path(rel).with_suffix("").as_posix())
    res = {"file": rel, "stem": stem, "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns,
           "scan_version": SCAN_VERSION, "errors": [], "notes": []}
    tmp = None
    try:
        cfg = load_profiles(opts.get("profiles"))
        src = path
        if path.suffix.lower() == ".dwg":
            tmp = tempfile.mkdtemp()
            src = dwg_to_dxf(path, tmp)
            res["notes"].append("DWG dikonversi ke DXF sementara")
        doc, notes = read_doc(str(src))
        res["notes"] += notes
        msp = doc.modelspace()
        tb = text_blocks_set(doc)
        model_texts = collect_texts(msp, tb)
        layers, blocks, attrs, textlines = file_signature_sets(doc, msp, model_texts)
        scores = score_profiles(cfg["profiles"], layers, blocks, attrs, textlines)
        prof, pscore, how = pick_profile(cfg["profiles"], scores, opts.get("profile"))
        heights = [t["h"] for t in model_texts if t["h"] > 0]
        th = statistics.median(heights) if heights else 2.5
        ec = Counter(e.dxftype() for e in msp)
        sheets_raw = detect_sheets(doc, msp, model_texts, tb, prof, th)
        sheets, all_texts_rows, nets_out = [], [], []
        insert_devs_all = extract_insert_devices(msp, Rect(-1e12, -1e12, 1e12, 1e12), prof)
        for i, sh in enumerate(sheets_raw, 1):
            info, mt = build_sheet_info(sh, prof, cfg, model_texts, cfg, th, i, stem, opts)
            # perangkat ber-atribut yang berada di rect model sheet
            extra_all = [d for d in insert_devs_all if sh["model_rect"].contains(d["x"], d["y"])]
            drx = re.compile(prof["designation_regex"])
            extra = [d for d in extra_all if drx.match(d["designation"])]
            syms = [d for d in extra_all if not drx.match(d["designation"])]
            info["block_symbols"] = [{"label": d["designation"], "block": d["source"].split(":", 1)[-1],
                                      "description": d["description"]} for d in syms[:60]]
            if extra:
                for d in extra:
                    d["kind"] = classify_kind(d["designation"], d["description"], cfg)
                    d["x"], d["y"] = rnd(d["x"]), rnd(d["y"])
                    d.pop("h", None)
                info["devices"].extend(extra)
            info["device_count"] = len(info["devices"])
            info["unique_designations"] = len({d["designation"] for d in info["devices"]})
            if opts.get("nets"):
                nets_out.append({"sheet_id": info["sheet_id"],
                                 "nets": build_nets(msp, sh["model_rect"], mt, th)})
            sheets.append(info)
        res.update({
            "dxf": {"version": doc.dxfversion, "units": int(doc.units),
                    "extmin": [rnd(v) for v in doc.header.get("$EXTMIN", (0, 0, 0))[:2]],
                    "extmax": [rnd(v) for v in doc.header.get("$EXTMAX", (0, 0, 0))[:2]]},
            "profile": {"name": prof["name"], "score": pscore, "selection": how, "scores": scores},
            "entities": dict(ec.most_common()), "median_text_height": rnd(th, 2),
            "layers": sorted(({"name": k, "entities": v} for k, v in layers.items()), key=lambda x: -x["entities"])[:40],
            "blocks_top": Counter(e.dxf.name for e in msp if e.dxftype() == "INSERT").most_common(25),
            "layouts": doc.layouts.names_in_taborder() if hasattr(doc.layouts, "names_in_taborder") else doc.layouts.names(),
            "sheets": sheets,
        })
        res["totals"] = {"sheets": len(sheets), "devices": sum(s["device_count"] for s in sheets),
                         "placeholders": sum(len(s["placeholders"]) for s in sheets),
                         "ratios": sum(len(s["ratios"]) for s in sheets)}
        if nets_out:
            res["_nets"] = nets_out
    except Exception as exc:  # satu file gagal tidak boleh menghentikan batch
        res["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    res["seconds"] = round(time.time() - t0, 1)
    fdir = Path(out_dir) / "files"
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / f"{stem}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return {k: res.get(k) for k in ("file", "stem", "size", "mtime_ns", "errors", "seconds")}


# ------------------------------------------------------------------------ agregasi


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def aggregate(out_dir, opts):
    out_dir = Path(out_dir)
    manifest, dev, units, rat, ph, sh_rows, txt, nets = [], [], [], [], [], [], [], []
    for jf in sorted((out_dir / "files").glob("*.json")):
        r = json.loads(jf.read_text(encoding="utf-8"))
        tot = r.get("totals", {})
        manifest.append({"file": r["file"], "json": f"files/{jf.name}", "size": r["size"], "mtime_ns": r["mtime_ns"],
                         "profile": r.get("profile", {}).get("name"), "profile_score": r.get("profile", {}).get("score"),
                         "profile_selection": r.get("profile", {}).get("selection"),
                         "sheets": tot.get("sheets"), "devices": tot.get("devices"),
                         "placeholders": tot.get("placeholders"), "errors": r.get("errors"), "notes": r.get("notes"),
                         "seconds": r.get("seconds"), "scan_version": r.get("scan_version")})
        prof = r.get("profile", {}).get("name", "")
        for s in r.get("sheets", []):
            sh_rows.append([r["file"], s["sheet_id"], s["layout"], s["source"], s["drawing_no"], s["sheet_no"],
                            s["revision"], s["sheet_type"], " | ".join(s["title_lines"][:4]), s["device_count"],
                            len(s["functional_units"]), len(s["placeholders"])])
            for d in s["devices"]:
                dev.append([r["file"], prof, s["sheet_id"], s["sheet_no"], s["layout"], d["designation"], d.get("kind", ""),
                            d.get("model", ""), d.get("description", ""), d.get("x"), d.get("y"), d.get("source", ""),
                            d.get("link", "")])
            for u in s["functional_units"]:
                units.append([r["file"], s["sheet_id"], s["sheet_no"], u["code"], u["caption"], u["x"], u["y"]])
            for q in s["ratios"]:
                rat.append([r["file"], s["sheet_id"], s["sheet_no"], q["kind"], q["id"], q["primary"], q["secondary"],
                            q["class"], q["burden"], int(q["placeholder"]), q["text"]])
            for p in s["placeholders"]:
                ph.append([r["file"], s["sheet_id"], s["sheet_no"], p["text"], " ".join(p["tokens"]), p["x"], p["y"]])
            for t in s.get("_texts", []):
                txt.append([r["file"], s["sheet_id"], t["layer"], t["src"], t["x"], t["y"], t["text"]])
        for n in r.get("_nets", []):
            nets.append({"file": r["file"], **n})
    write_csv(out_dir / "sheets.csv", ["file", "sheet_id", "layout", "source", "drawing_no", "sheet_no", "revision",
                                       "sheet_type", "title", "devices", "functional_units", "placeholders"], sh_rows)
    write_csv(out_dir / "devices.csv", ["file", "profile", "sheet_id", "sheet_no", "layout", "designation", "kind", "model",
                                        "description", "x", "y", "source", "model_link"], dev)
    write_csv(out_dir / "functional_units.csv", ["file", "sheet_id", "sheet_no", "code", "caption", "x", "y"], units)
    write_csv(out_dir / "ratios.csv", ["file", "sheet_id", "sheet_no", "kind", "id", "primary", "secondary", "class",
                                       "burden", "placeholder", "text"], rat)
    write_csv(out_dir / "placeholders.csv", ["file", "sheet_id", "sheet_no", "text", "tokens", "x", "y"], ph)
    if opts.get("texts", True):
        write_csv(out_dir / "texts.csv", ["file", "sheet_id", "layer", "src", "x", "y", "text"], txt)
    elif (out_dir / "texts.csv").exists():
        (out_dir / "texts.csv").unlink()
    if nets:
        (out_dir / "nets.json").write_text(json.dumps(nets, ensure_ascii=False), encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps(
        {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "scan_version": SCAN_VERSION, "files": manifest},
        ensure_ascii=False, indent=1), encoding="utf-8")
    # toc.md + summary.md
    lines = ["# Daftar sheet (otomatis dari scan.py)", ""]
    cur = None
    for row in sh_rows:
        if row[0] != cur:
            cur = row[0]
            lines += ["", f"## {cur}"]
        lines.append(f"- sheet {row[5] or '?'} rev {row[6] or '-'} [{row[7]}] {row[4]} — {row[8][:110]}")
    (out_dir / "toc.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pc = Counter((m["profile"] or "ERROR") for m in manifest)
    s = ["# Ringkasan indeks", "", f"File: {len(manifest)} | Sheet: {len(sh_rows)} | Perangkat: {len(dev)} | "
         f"Placeholder belum terisi: {len(ph)}", "", "Profil style terdeteksi: " +
         ", ".join(f"{k}={v}" for k, v in pc.most_common()), "", "| file | profil | skor | sheet | perangkat | placeholder | error |",
         "|---|---|---|---|---|---|---|"]
    for m in manifest:
        s.append(f"| {m['file']} | {m['profile']} | {m['profile_score']} | {m['sheets']} | {m['devices']} | "
                 f"{m['placeholders']} | {'; '.join(m['errors'] or [])[:60]} |")
    low = [m for m in manifest if m["profile_selection"] and m["profile_selection"].startswith("fallback")]
    if low:
        s += ["", "File dengan style tidak dikenali (pakai generic) — jalankan `--discover` pada file ini lalu buat profil:"]
        s += [f"- {m['file']}" for m in low]
    (out_dir / "summary.md").write_text("\n".join(s) + "\n", encoding="utf-8")
    return manifest


# ------------------------------------------------------------------------ discover


def discover(paths, out_dir):
    """Bantu membuat profil baru: tampilkan layer/block/pola teks paling khas."""
    layers, blocks, pats, attrs = Counter(), Counter(), Counter(), Counter()
    for p in paths:
        doc, _ = read_doc(str(p))
        msp = doc.modelspace()
        tb = text_blocks_set(doc)
        texts = collect_texts(msp, tb)
        for e in msp:
            layers[e.dxf.get("layer", "0")] += 1
            if e.dxftype() == "INSERT":
                blocks[e.dxf.name] += 1
        for t in texts:
            if t.get("tag"):
                attrs[t["tag"].upper()] += 1
            for ln in lines_of(t):
                pats[re.sub(r"\d+", "#", ln)[:40]] += 1
    skip = {"0", "Defpoints"}
    top_layers = [k for k, _ in layers.most_common(12) if k not in skip][:6]
    top_blocks = [k for k, _ in blocks.most_common(15) if not k.startswith("*")][:6]
    top_pats = [k for k, _ in pats.most_common(12) if len(k) >= 3][:6]

    def to_rx(p):
        return "^" + re.escape(p).replace("\\#", r"\d+") + "$"

    sugg = {"name": "style_baru", "description": "ISI: jelaskan style ini",
            "signatures": (
                [{"kind": "layer", "regex": "^(" + "|".join(re.escape(x) for x in top_layers) + ")$", "weight": 3, "cap": 2}] +
                ([{"kind": "block", "regex": "^(" + "|".join(re.escape(x) for x in top_blocks) + ")$", "weight": 3, "cap": 3}] if top_blocks else []) +
                ([{"kind": "attr", "regex": "^(" + "|".join(re.escape(x) for x, _ in attrs.most_common(6)) + ")$", "weight": 3, "cap": 2}] if attrs else []) +
                [{"kind": "text", "regex": to_rx(x), "weight": 2, "cap": 4} for x in top_pats[:3]]),
            "designation_regex": "ISI: regex tag perangkat, mis. ^-[A-Z]{1,3}\\d+$",
            "functional_regex": "^[=+][A-Z0-9]{2,8}$", "device_models": [], "model_link_kinds": ["protection_or_fuse", "relay"],
            "protection_functions": GENERIC_FALLBACK["protection_functions"], "bay_keywords": "BAY|FEEDER|PANEL",
            "drawing_no_regex": "ISI", "frame_block_regex": "^(.*FRAME.*|.*TITLE.*)$", "title_attr_keys": {},
            "device_tag_attrs": ["TAG"], "device_desc_attrs": ["DESC"], "device_model_attrs": ["MODEL"]}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "suggested_profile.json").write_text(json.dumps(sugg, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Layer teratas  :", layers.most_common(10))
    print("Block teratas  :", blocks.most_common(10))
    print("Tag atribut    :", attrs.most_common(8))
    print("Pola teks      :", pats.most_common(15))
    print(f"\nKerangka profil ditulis ke {out/'suggested_profile.json'}")
    print("Salin ke profiles.json (array 'profiles'), isi bagian 'ISI', lalu uji dengan --profile style_baru")


# ---------------------------------------------------------------------------- main


def gather_inputs(inputs):
    files, roots = [], {}
    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            for f in sorted(list(p.rglob("*.dxf")) + list(p.rglob("*.DXF")) + list(p.rglob("*.dwg")) + list(p.rglob("*.DWG"))):
                files.append(f)
                roots[f] = p
        elif p.exists():
            files.append(p)
            roots[p] = p.parent
        else:
            print(f"[peringatan] tidak ditemukan: {inp}", file=sys.stderr)
    seen, uniq = set(), []
    for f in files:
        if f.resolve() not in seen:
            seen.add(f.resolve())
            uniq.append(f)
    return uniq, roots


def main():
    ap = argparse.ArgumentParser(description="Indeks drawing DXF/DWG multi-style untuk coding agent")
    ap.add_argument("inputs", nargs="*", help="file atau folder DXF/DWG")
    ap.add_argument("-o", "--out", default="index", help="folder keluaran (default: index)")
    ap.add_argument("--profile", default="auto", help="nama profil, atau 'auto' (default)")
    ap.add_argument("--profiles", help="path profiles.json (default: di samping scan.py)")
    ap.add_argument("-j", "--jobs", type=int, default=1, help="jumlah proses paralel")
    ap.add_argument("--force", action="store_true", help="pindai ulang walau file tidak berubah")
    ap.add_argument("--nets", action="store_true", help="ekstrak konektivitas garis (eksperimental)")
    ap.add_argument("--no-texts", action="store_true", help="jangan tulis texts.csv")
    ap.add_argument("--discover", nargs="+", metavar="FILE", help="analisis file untuk membuat profil style baru")
    ap.add_argument("--list-profiles", action="store_true")
    a = ap.parse_args()

    cfg = load_profiles(a.profiles)
    if a.list_profiles:
        for p in cfg["profiles"]:
            print(f"- {p['name']}: {p.get('description', '')}")
        return
    if a.discover:
        discover(a.discover, a.out)
        return
    if not a.inputs:
        ap.error("beri file/folder input (atau --discover / --list-profiles)")
    files, roots = gather_inputs(a.inputs)
    if not files:
        sys.exit("Tidak ada file .dxf/.dwg ditemukan.")
    out_dir = Path(a.out)
    (out_dir / "files").mkdir(parents=True, exist_ok=True)
    opts = {"profile": a.profile, "profiles": a.profiles, "nets": a.nets, "texts": not a.no_texts}
    old = {}
    mf = out_dir / "manifest.json"
    if mf.exists() and not a.force:
        try:
            for m in json.loads(mf.read_text(encoding="utf-8"))["files"]:
                old[m["file"]] = m
        except Exception:
            pass
    todo = []
    for f in files:
        rel = str(f.relative_to(roots[f])) if f.is_relative_to(roots[f]) else f.name
        m = old.get(rel)
        stem = re.sub(r"[^\w.-]+", "_", Path(rel).with_suffix("").as_posix())
        if (m and not a.force and m.get("mtime_ns") == f.stat().st_mtime_ns and m.get("size") == f.stat().st_size
                and m.get("scan_version") == SCAN_VERSION and (out_dir / "files" / f"{stem}.json").exists()
                and not a.nets and a.profile == "auto" and not m.get("errors")):
            continue
        todo.append((str(f), str(roots[f]), str(out_dir), opts))
    print(f"{len(files)} file ditemukan, {len(todo)} perlu dipindai (sisanya tidak berubah).")
    t0 = time.time()
    if a.jobs > 1 and len(todo) > 1:
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            for i, r in enumerate(ex.map(scan_file, todo), 1):
                print(f"[{i}/{len(todo)}] {r['file']}  {r['seconds']}s" + (f"  ERROR {r['errors']}" if r["errors"] else ""))
    else:
        for i, t in enumerate(todo, 1):
            r = scan_file(t)
            print(f"[{i}/{len(todo)}] {r['file']}  {r['seconds']}s" + (f"  ERROR {r['errors']}" if r["errors"] else ""))
    manifest = aggregate(out_dir, opts)
    print(f"Selesai dalam {time.time() - t0:.0f}s. Indeks di: {out_dir}/  (mulai dari summary.md)")
    bad = [m for m in manifest if m["errors"]]
    if bad:
        print(f"{len(bad)} file error - lihat manifest.json / summary.md")


if __name__ == "__main__":
    main()
