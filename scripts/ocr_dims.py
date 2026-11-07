#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocr_dims.py — 从 SKU 产品图里读尺寸引线，补全表格缺失的规格。

依赖 Windows 自带的 OCR 引擎（Windows 10/11，无需装 Tesseract）：
    pip install winsdk

单独用：
    python scripts/ocr_dims.py --sku-dir SKU/ --skus TZ2614,TZ3818 --json out.json
    python scripts/ocr_dims.py --sku-dir SKU/ --all --cache ocr_cache.json

被 sku_to_cad.py fix --ocr auto 调用。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

# 数值 + 单位（英文/中文），以及直径符号
RE_DIM = re.compile(
    r"(?:[⌀ΦØφ]|直径\s*)?(\d+(?:\.\d+)?)\s*(mm|cm|m|毫米|厘米|米)\b", re.IGNORECASE)
RE_DIA = re.compile(r"(?:[⌀ΦØφ]|直径)\s*(\d+(?:\.\d+)?)\s*(mm|cm|m|毫米|厘米|米)?", re.IGNORECASE)

# 行内出现这些词，说明这个数值是"高"，不是平面尺寸
HEIGHT_WORDS = ("高", "height", "H高")
# 出现这些词说明是平面尺寸
FOOTPRINT_WORDS = ("长", "宽", "深", "length", "width", "depth", "总长", "总深")

UNIT_MM = {"mm": 1.0, "毫米": 1.0, "cm": 10.0, "厘米": 10.0, "m": 1000.0, "米": 1000.0}
SANITY = (80.0, 8000.0)          # 家具平面尺寸的合理范围


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #
def _engine(lang=None):
    """尺寸是拉丁数字，默认优先英文引擎；中文引擎对 '1 ． 7 m' 这类更差。"""
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.globalization import Language
    order = ([lang] if lang else []) + ["en-US", "zh-Hans-CN", "zh-CN"]
    for tag in order:
        try:
            eng = OcrEngine.try_create_from_language(Language(tag))
        except Exception:
            eng = None
        if eng is not None:
            return eng
    return OcrEngine.try_create_from_user_profile_languages()


async def _ocr_file(engine, path):
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import StorageFile, FileAccessMode
    f = await StorageFile.get_file_from_path_async(os.path.abspath(path))
    stream = await f.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    res = await engine.recognize_async(bmp)
    out = []
    for line in res.lines:
        words = list(line.words)
        if not words:
            continue
        x0 = min(w.bounding_rect.x for w in words)
        y0 = min(w.bounding_rect.y for w in words)
        x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
        y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
        out.append({"text": line.text, "w": x1 - x0, "h": y1 - y0, "x": x0, "y": y0})
    return out


def ocr_image(path, dual=True):
    """返回 OCR 行 [{text,w,h,x,y}]。dual=True 时跑英文+中文两个引擎再合并：
    数字（1.7m）英文引擎更准，中文关键词（圆/高/长）只有中文引擎给得出。"""
    try:
        from winsdk.windows.media.ocr import OcrEngine  # noqa: F401
    except ImportError:
        raise SystemExit("需要 Windows OCR：pip install winsdk（Windows 10/11 自带引擎）")
    engines = []
    for lg in (["en-US", "zh-Hans-CN"] if dual else ["en-US"]):
        eng = _engine(lg)
        if eng is not None and all(eng.recognizer_language.language_tag != e.recognizer_language.language_tag
                                   for e in engines):
            engines.append(eng)
    if not engines:
        raise SystemExit("系统没有可用的 OCR 语言包（设置 → 时间和语言 → 语言 → 添加英语/中文）")
    lines, seen = [], set()
    for eng in engines:
        for ln in asyncio.run(_ocr_file(eng, path)):
            key = (ln["text"].strip(), round(ln["h"] / max(ln["w"], 1), 2))
            if ln["text"].strip() and key not in seen:
                seen.add(key)
                lines.append(ln)
    return lines


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
FULLWIDTH = str.maketrans({
    "．": ".", "。": ".", "，": ",", "：": ":", "；": ";", "（": "(", "）": ")",
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "Ｍ": "M", "Ｃ": "C", "ｍ": "m", "ｃ": "c",
})


def normalize_text(s: str) -> str:
    """中文 OCR 会在字符之间插空格，还常用全角符号/中点当小数点：
       '1 ． 7 m' → '1.7m'   '1 · 8 m' → '1.8m'   '1 7 4 c m' → '174cm'
    """
    s = s.translate(FULLWIDTH)
    s = re.sub(r"\s+", " ", s).strip()
    # 中点/间隔号当小数点：1 · 8 → 1.8
    s = re.sub(r"(?<=\d)\s*[·•・]\s*(?=\d)", ".", s)
    # 逗号/句点两侧的空格去掉：0 ， 76 → 0,76
    s = re.sub(r"(?<=\d)\s*([.,])\s*(?=\d)", r"\1", s)
    # 逗号当小数点：0,76 → 0.76（后面不超过两位数字）
    s = re.sub(r"(?<=\d),(?=\d{1,2}(?![0-9]))", ".", s)
    # 反复消数字之间的空格：1 7 4 → 174
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"(?<=\d)\s+(?=\d)", "", s)
    # 数字与单位之间的空格：174 c m → 174c m
    s = re.sub(r"(?<=[\d.])\s+(?=[A-Za-z]{1,2}\b)", "", s)
    # 单位字母之间的空格：c m → cm
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"(?<=[A-Za-z])\s+(?=[A-Za-z]{1,2}\b)", "", s)
    return s


def parse_lines(lines):
    """把 OCR 行解析成 [{mm, raw, is_height, is_dia}]"""
    out = []
    for item in lines:
        if isinstance(item, str):
            item = {"text": item, "w": 1, "h": 0}
        ln = normalize_text(item["text"])
        low = ln.lower()
        vertical = item.get("h", 0) > item.get("w", 0) * 1.25   # 竖排标注 = 高度
        is_h = vertical or any(w in ln or w in low for w in HEIGHT_WORDS)
        is_fp = any(w in ln or w in low for w in FOOTPRINT_WORDS)
        height = is_h and not is_fp

        for m in RE_DIA.finditer(ln):
            v = float(m.group(1)) * UNIT_MM.get((m.group(2) or "mm").lower(), 1.0)
            if SANITY[0] <= v <= SANITY[1]:
                out.append({"mm": round(v, 1), "raw": m.group(0), "is_height": False, "is_dia": True})
        for m in RE_DIM.finditer(ln):
            v = float(m.group(1)) * UNIT_MM.get(m.group(2).lower(), 1.0)
            if SANITY[0] <= v <= SANITY[1]:
                out.append({"mm": round(v, 1), "raw": m.group(0), "is_height": height, "is_dia": False})
    # 同一数值可能被两个引擎各读到一次（一眼横向、一眼竖向）：
    # 只要有一次被判定为"高"，就按高处理（保守），避免把高度当成平面尺寸
    merged = {}
    for d in out:
        k = d["mm"]
        if k not in merged:
            merged[k] = dict(d)
        else:
            merged[k]["is_height"] = merged[k]["is_height"] or d["is_height"]
            merged[k]["is_dia"] = merged[k]["is_dia"] or d["is_dia"]
    return sorted(merged.values(), key=lambda d: -d["mm"])


def decide(parsed, cur_w, cur_h, tol=0.15, default_square=(800, 900, 1000, 1200), text=""):
    """给 OCR 结果 + 现有图块尺寸 → (w, h, status, note)"""
    if not parsed:
        return None, None, "ocr-empty", "图上看不到尺寸"

    dias = [d["mm"] for d in parsed if d["is_dia"]]
    if dias:
        d = max(dias)
        return d, d, "ocr-dia", f"直径 {d:.0f}mm"

    vals = sorted({d["mm"] for d in parsed if not d["is_height"]}, reverse=True)
    allv = sorted({d["mm"] for d in parsed}, reverse=True)
    if len(vals) < 2:
        vals = allv
    if len(vals) < 2:
        return None, None, "ocr-one-value", f"只读到一个尺寸 {vals}"

    # 圆形家具（图上写"圆几/圆形茶几/圆桌"）→ 取最大值当直径
    if "圆" in text and max(vals) <= 2000:
        d = max(vals)
        return d, d, "ocr-round", f"圆形，取最大 {d:.0f}mm"

    def near(a, b):
        return abs(a - b) <= max(tol * max(a, b), 15)

    # 方形占位块（ERP 默认值）不能靠"命中"证实：同一个数值会同时满足长和宽
    square_placeholder = abs(cur_w - cur_h) < 1

    # 1) 现有尺寸已能被 OCR 证实
    if (not square_placeholder) and any(near(cur_w, v) for v in vals) and any(near(cur_h, v) for v in vals):
        return cur_w, cur_h, "ocr-confirm", "与现有尺寸一致"
    if (not square_placeholder) and any(near(cur_w, v) for v in vals) and not any(near(cur_h, v) for v in vals):
        cand = [v for v in vals if not near(cur_w, v)]
        if cand:
            best = min(cand, key=lambda v: abs(v - cur_h))
            return cur_w, best, "ocr-partial", f"宽 {cur_w:.0f} 已确认，深取 {best:.0f}（建议值，需人工确认）"
    if (not square_placeholder) and any(near(cur_h, v) for v in vals) and not any(near(cur_w, v) for v in vals):
        cand = [v for v in vals if not near(cur_h, v)]
        if cand:
            best = min(cand, key=lambda v: abs(v - cur_w))
            return best, cur_h, "ocr-partial", f"深 {cur_h:.0f} 已确认，长取 {best:.0f}（建议值，需人工确认）"

    # 2) 占位块是"默认方块"（ERP 没尺寸）→ 取最大的两个
    is_default = (abs(cur_w - cur_h) < 1) and int(round(cur_w)) in default_square
    if is_default and len(vals) >= 2:
        a, b = vals[0], vals[1]
        weak = (b / a) > 0.88     # 两个值很接近 → 可能把高度当成平面尺寸，标出来让人复核
        return a, b, "ocr-default-square", (
            f"默认方块，按图上最大两个 {a:.0f}×{b:.0f}" + ("（两者接近，建议复核）" if weak else ""))

    # 3) 图上只有两个平面尺寸 → 直接用
    if len(vals) == 2:
        return vals[0], vals[1], "ocr-two-values", f"{vals[0]:.0f}×{vals[1]:.0f}"

    return None, None, "ocr-ambiguous", "读到多个尺寸，无法判定平面两维：" + ", ".join(f"{v:.0f}" for v in vals[:6])


# 只允许这些判定自动改写尺寸；ocr-partial / ocr-ambiguous / ocr-one-value 只报告，交人工
AUTO_APPLY = {"ocr-dia", "ocr-round", "ocr-two-values", "ocr-default-square"}


# --------------------------------------------------------------------------- #
def find_photo(sku_dir, code):
    for fn in os.listdir(sku_dir):
        stem, ext = os.path.splitext(fn)
        if ext.lower() in (".jpg", ".jpeg", ".png", ".webp") and stem.upper() == code.upper():
            return os.path.join(sku_dir, fn)
    # 退一步：带变体后缀的图（CY1963Z1.png）
    for fn in os.listdir(sku_dir):
        stem, ext = os.path.splitext(fn)
        if ext.lower() in (".jpg", ".jpeg", ".png", ".webp") and stem.upper().startswith(code.upper()):
            return os.path.join(sku_dir, fn)
    return None


def read_dims(sku_dir, code, cache=None):
    """返回 (parsed, lines, photo_path, text)"""
    if cache is not None and code in cache:
        c = cache[code]
        return c.get("parsed", []), c.get("lines", []), c.get("photo"), c.get("text", "")
    path = find_photo(sku_dir, code)
    if not path:
        return [], [], None, ""
    lines = ocr_image(path)
    parsed = parse_lines(lines)
    text = " ".join((l["text"] if isinstance(l, dict) else l) for l in lines)
    if cache is not None:
        cache[code] = {"parsed": parsed, "lines": lines, "photo": os.path.basename(path), "text": text}
    return parsed, lines, path, text


def main(argv=None):
    ap = argparse.ArgumentParser(description="从 SKU 产品图读尺寸")
    ap.add_argument("--sku-dir", required=True)
    ap.add_argument("--skus", help="逗号分隔")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--cur", help="现有尺寸 w x h，如 1000x1000（逗号分隔多个 sku=w x h）")
    ap.add_argument("--cache")
    ap.add_argument("--json")
    a = ap.parse_args(argv)

    cache = {}
    if a.cache and os.path.exists(a.cache):
        cache = json.load(open(a.cache, encoding="utf-8"))

    codes = []
    if a.skus:
        codes = [c.strip().upper() for c in a.skus.split(",") if c.strip()]
    elif a.all:
        codes = sorted({os.path.splitext(f)[0].upper() for f in os.listdir(a.sku_dir)
                        if os.path.splitext(f)[1].lower() in (".jpg", ".jpeg", ".png", ".webp")})
    else:
        sys.exit("需要 --skus 或 --all")

    rows = []
    for code in codes:
        parsed, lines, path, text = read_dims(a.sku_dir, code, cache)
        cur = None
        if a.cur:
            for part in a.cur.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip().upper() == code:
                        cur = v.strip().lower().replace("*", "x").split("x")
        print(f"\n{code}  ({os.path.basename(path) if path else '无图'})")
        if lines:
            print("   OCR:", " | ".join((l["text"] if isinstance(l, dict) else l) for l in lines[:8]))
        if parsed:
            print("   尺寸:", ", ".join(f"{d['raw']}=" + f"{d['mm']:.0f}mm"
                                       + ("(高)" if d["is_height"] else "")
                                       + ("(直径)" if d["is_dia"] else "") for d in parsed))
        if cur and len(cur) == 2:
            w, h, status, note = decide(parsed, float(cur[0]), float(cur[1]), text=text)
            print(f"   判定: {status}  {note}" + (f"  → {w:.0f}×{h:.0f}" if w else ""))
        rows.append({"sku": code, "lines": lines, "dims": parsed,
                     "photo": os.path.basename(path) if path else None})

    if a.cache:
        json.dump(cache, open(a.cache, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n缓存 → {a.cache}")
    if a.json:
        json.dump(rows, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"结果 → {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
