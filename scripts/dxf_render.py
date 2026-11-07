#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dxf_render.py — 把 DXF 的局部区域或指定图块渲染成 PNG，用于视觉核对。

    python dxf_render.py 家具库.dxf --region 100000,140000,-12000,2000 --out after.png
    python dxf_render.py 家具库.dxf --blocks BF1141,CY0276,CU0102 --out cards.png
    python dxf_render.py 家具库.dxf --region a --region b --out compare.png   # 并排对比

依赖：ezdxf、pillow
"""
from __future__ import annotations

import argparse
import math
import os
import sys

try:
    import ezdxf
    from ezdxf import path as ezpath
    from ezdxf.math import Vec3
except ImportError:
    sys.exit("缺少依赖：pip install ezdxf")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("缺少依赖：pip install pillow")

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def pick_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def polys_of(container, matrix=None, tol=1.5):
    out = []
    for e in container:
        try:
            p = ezpath.make_path(e)
        except Exception:
            continue
        for sp in p.sub_paths():
            pts = [v for v in sp.flattening(tol)]
            if len(pts) < 2:
                continue
            if matrix is not None:
                pts = [matrix.transform(v) for v in pts]
            out.append([(v.x, v.y) for v in pts])
    return out


def collect_region(doc, x0, x1, y0, y1):
    msp = doc.modelspace()
    polys, texts = [], []
    for ins in msp.query("INSERT"):
        p = ins.dxf.insert
        if x0 - 100 <= p.x <= x1 + 100 and y0 - 100 <= p.y <= y1 + 100:
            blk = doc.blocks.get(ins.dxf.name)
            if blk is not None:
                polys += polys_of(blk, ins.matrix44())
    for t in msp.query("TEXT"):
        ap = t.dxf.align_point if t.dxf.hasattr("align_point") else t.dxf.insert
        if x0 - 200 <= ap.x <= x1 + 200 and y0 - 200 <= ap.y <= y1 + 200:
            texts.append((ap.x, ap.y, t.dxf.text, t.dxf.height, t.dxf.layer))
    return polys, texts


def render_region(doc, box, width):
    x0, x1, y0, y1 = box
    polys, texts = collect_region(doc, x0, x1, y0, y1)
    s = (width - 40) / max(x1 - x0, 1)
    h = int((y1 - y0) * s) + 40
    img = Image.new("RGB", (width, max(h, 120)), "white")
    dr = ImageDraw.Draw(img)
    for poly in polys:
        pts = [(20 + (x - x0) * s, img.height - 20 - (y - y0) * s) for x, y in poly]
        dr.line(pts, fill=(0, 0, 0), width=2, joint="curve")
    for x, y, txt, th, layer in texts:
        size = max(7, int(th * s * 2.2))     # 放大 2.2 倍，便于肉眼核对位置
        f = pick_font(size)
        col = (200, 0, 0) if layer in ("NUMBER", "NEW_NUMBER") else (0, 90, 200)
        px, py = 20 + (x - x0) * s, img.height - 20 - (y - y0) * s
        w = dr.textlength(txt, font=f)
        dr.text((px - w / 2, py - size / 2), txt, font=f, fill=col)
    return img


def render_blocks(doc, names, cols, px):
    CW, CH = px, int(px * 0.85)
    rows = math.ceil(len(names) / cols)
    img = Image.new("RGB", (cols * CW + 40, rows * CH + 90), "white")
    dr = ImageDraw.Draw(img)
    dr.text((20, 16), "block cards: " + ", ".join(names), font=pick_font(22), fill=(0, 0, 0))
    for i, name in enumerate(names):
        blk = doc.blocks.get(name)
        cx = 20 + (i % cols) * CW
        cy = 60 + (i // cols) * CH
        if blk is None:
            dr.rectangle([cx, cy, cx + CW - 20, cy + CH - 30], outline=(220, 0, 0))
            dr.text((cx + 10, cy + 10), name + " (missing)", font=pick_font(16), fill=(220, 0, 0))
            continue
        polys = polys_of(blk)
        xs = [p[0] for poly in polys for p in poly]
        ys = [p[1] for poly in polys for p in poly]
        if not xs:
            continue
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        s = min((CW - 80) / max(w, 1), (CH - 130) / max(h, 1))
        mx, my = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        ox, oy = cx + CW / 2 - 10, cy + 100 + (CH - 130) / 2
        dr.rectangle([cx + 2, cy + 2, cx + CW - 20, cy + CH - 20], outline=(232, 232, 232))
        for poly in polys:
            pts = [(ox + (x - mx) * s, oy - (y - my) * s) for x, y in poly]
            dr.line(pts, fill=(0, 0, 0), width=2, joint="curve")
        f = pick_font(16)
        dr.text((cx + 10, cy + 6), name, font=pick_font(18), fill=(180, 0, 0))
        dr.text((cx + 10, cy + 30), f"{w:.0f} x {h:.0f} mm   {len(list(blk))} entities", font=f, fill=(90, 90, 90))
    return img


def main(argv=None):
    ap = argparse.ArgumentParser(description="DXF → PNG 视觉核对")
    ap.add_argument("dxf")
    ap.add_argument("--region", action="append", default=[], help="x0,x1,y0,y1（可多次，自动并排）")
    ap.add_argument("--blocks", help="逗号分隔的块名")
    ap.add_argument("--out", required=True)
    ap.add_argument("--px", type=int, default=1800, help="宽度像素")
    ap.add_argument("--cols", type=int, default=4, help="块卡片列数")
    ap.add_argument("--encoding", default="utf-8")
    a = ap.parse_args(argv)

    doc = ezdxf.readfile(a.dxf, encoding=a.encoding)
    if a.blocks:
        names = [n.strip() for n in a.blocks.split(",") if n.strip()]
        img = render_blocks(doc, names, a.cols, max(a.px // max(a.cols, 1), 320))
    else:
        boxes = [[float(v) for v in r.split(",")] for r in a.region]
        if not boxes:
            sys.exit("需要 --region 或 --blocks")
        imgs = [render_region(doc, b, a.px) for b in boxes]
        if len(imgs) == 1:
            img = imgs[0]
        else:
            W = sum(i.width for i in imgs) + 20 * (len(imgs) - 1)
            H = max(i.height for i in imgs)
            img = Image.new("RGB", (W, H), "white")
            x = 0
            for i in imgs:
                img.paste(i, (x, 0))
                x += i.width + 20
    img.save(a.out)
    print(f"已写出 {a.out}  {img.size[0]}x{img.size[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
