#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sku_to_cad.py — 家具平面图块批量校对 / 生成工具

三种用法：
    audit  <dxf>                             体检：图层、图块、标签是否合规，列出待校对块
    fix    <dxf> --table <xlsx> [--sku-dir]   把占位矩形批量改造成统一风格图块（默认只预演）
    index  <dxf> [--out index.json]           导出图块模板索引（尺寸/实体数/指纹），供复用

设计原则（来自真实图纸反推的 house style，详见 SKILL.md）：
    块内几何  layer 0 / color ByBlock / linetype ByBlock，外包中心 = (0,0)
    INSERT   layer FURNITURE / color 7 / scale 1，旋转只出现在 INSERT 上
    标签     NUMBER(h100,色2) 在插入点 +(0,+70)；BRAND(h100,色7) 在 +(0,-70)，中/中对齐

依赖：ezdxf、openpyxl（fix 需要读表格）
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

try:
    import ezdxf
    from ezdxf import bbox
    from ezdxf.math import Matrix44, Vec3
    from ezdxf.enums import TextEntityAlignment
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖：pip install ezdxf")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "..", "rules", "house_style.json")


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #
FALLBACK_RULES = {
    "layers": {"insert": "FURNITURE", "pending": "ERP_PENDING", "number": "NUMBER",
               "brand": "BRAND", "legacy_number": "NEW_NUMBER", "review": "ERP_REVIEW"},
    "block": {"inner_layer": "0", "inner_color": 0, "inner_linetype": "ByBlock",
              "center_tolerance": 1.0},
    "insert": {"color": 7, "scale": 1.0},
    "text": {"style": "FURNITURE_TEXT", "number_height": 100, "brand_height": 100,
             "number_dy": 70, "brand_dy": -70, "color_number": 2, "color_brand": 7},
    "thresholds": {"size_tolerance_mm": 20, "chair_limit_mm": 650,
                   "text_search_radius": 1500, "placeholder_line_count": 4},
    "brand_placeholders": ["待核对", "待校对", "待定"],
    "category_codes": {
        "BF": "sofa", "PF": "sofa", "TZ": "sofa",
        "CU": "bed",
        "CY": "chair",
        "CJ": "table", "CZ": "table", "CG": "table",
        "DG": "cabinet", "SG": "cabinet", "QG": "screen", "SJ": "cabinet",
    },
    "recipes": {
        "rect":   {"desc": "闭合矩形", "entities": 1},
        "circle": {"desc": "圆", "entities": 1},
        "bed":    {"desc": "矩形+床尾带线+分缝+床头线", "entities": 4},
        "sofa":   {"desc": "圆角轮廓+靠背线+2扶手线+坐垫分格", "entities": "4~8"},
        "lounge": {"desc": "圆角轮廓+靠背线+2扶手线", "entities": 4},
        "chair":  {"desc": "复用统一椅模板", "entities": 19},
    },
}


def load_rules(path: str | None) -> dict:
    rules = json.loads(json.dumps(FALLBACK_RULES))  # deep copy
    path = path or DEFAULT_RULES
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(rules.get(k), dict):
                rules[k].update(v)
            else:
                rules[k] = v
    return rules


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def block_bbox(blk):
    """精确包围盒。必须 fast=False：fast=True 会把大半径圆弧按整圆取极值，
    真实样例里 536mm 的椅子会被算成 566mm。"""
    return bbox.extents(list(blk), fast=False)


def is_placeholder(ents, rules) -> bool:
    n = rules["thresholds"]["placeholder_line_count"]
    return len(ents) == n and all(e.dxftype() == "LINE" for e in ents)


def sku_base(sku: str) -> str:
    m = re.match(r"^([A-Z]{2}\d{4})(.*)$", sku.upper())
    return m.group(1) if m else sku.upper()


def parse_spec(spec: str | None):
    """'405*305*73cm\\nBN' -> [4050, 3050, 730] (mm)。单位混用：<10 视为米。"""
    if not spec:
        return None
    first = str(spec).split("\n")[0].strip()
    nums = re.findall(r"\d+(?:\.\d+)?", first)
    if len(nums) < 2:
        return None
    unit_cm = "cm" in first.lower() or "厘米" in first
    vals = [float(x) for x in nums]
    out = []
    for v in vals[:3]:
        if unit_cm:
            out.append(v * 10.0)
        else:
            out.append(v * 1000.0 if v <= 10 else v * 10.0)
    return out


def brand_from_spec(spec: str | None):
    if not spec or "\n" not in str(spec):
        return None
    return str(spec).split("\n", 1)[1].strip() or None


class TextGrid:
    """按 1000mm 网格索引文字，避免 O(inserts × texts)。"""

    def __init__(self, texts, cell=1000.0):
        self.cell = cell
        self.grid = defaultdict(list)
        for t in texts:
            p = self.point(t)
            self.grid[(int(p[0] // cell), int(p[1] // cell))].append((p[0], p[1], t))

    @staticmethod
    def point(t):
        ap = t.dxf.align_point if t.dxf.hasattr("align_point") else t.dxf.insert
        return (ap.x, ap.y)

    def nearest(self, pt, layers, radius):
        layers = set(layers)
        r = int(radius // self.cell) + 1
        cx, cy = int(pt[0] // self.cell), int(pt[1] // self.cell)
        best = {}
        for gx in range(cx - r, cx + r + 1):
            for gy in range(cy - r, cy + r + 1):
                for x, y, t in self.grid.get((gx, gy), ()):
                    if t.dxf.layer not in layers:
                        continue
                    d = math.hypot(x - pt[0], y - pt[1])
                    if d <= radius and (t.dxf.layer not in best or d < best[t.dxf.layer][0]):
                        best[t.dxf.layer] = (d, t)
        return {k: v[1] for k, v in best.items()}


# --------------------------------------------------------------------------- #
# 几何生成（全部以原点为中心）
# --------------------------------------------------------------------------- #
def _round_rect(w, h, r):
    b = math.tan(math.radians(45) / 2)
    hw, hd = w / 2.0, h / 2.0
    r = min(r, hw * 0.9, hd * 0.9)
    return [(-hw + r, -hd, 0.0), (hw - r, -hd, b), (hw, -hd + r, 0.0), (hw, hd - r, b),
            (hw - r, hd, 0.0), (-hw + r, hd, b), (-hw, hd - r, 0.0), (-hw, -hd + r, b)]


def build_geometry(cat, w, h, tpl_entities, tpl_portrait=None):
    """返回 [(kind, data)]；kind ∈ poly / polyb / line / circle"""
    if cat == "chair" and tpl_entities:
        # 复用统一椅模板；横向/纵向按占位块长宽比决定是否转 90°
        portrait = h > w
        out = []
        for e in tpl_entities:
            ne = e.copy()
            if portrait:
                ne.transform(Matrix44.z_rotate(math.radians(90)))
            out.append(("raw", ne))
        return out

    if cat == "circle":
        return [("circle", (0.0, 0.0, min(w, h) / 2.0))]

    if cat == "rect":
        hw, hd = w / 2.0, h / 2.0
        return [("poly", [(-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)])]

    if cat == "bed":
        portrait = h > w
        L, D = (h, w) if portrait else (w, h)
        ents = [("poly", [(-L/2, -D/2), (L/2, -D/2), (L/2, D/2), (-L/2, D/2)])]
        x0 = -L/2 + 500
        ents.append(("line", [(x0, -D/2), (x0, D/2)]))
        ents.append(("line", [(x0, 0), (-L/2, 0)]))
        if L > 1200:
            ents.append(("line", [(L/2 - 500, -D/2), (L/2 - 500, D/2)]))
        if portrait:
            ents = _rotate(ents, 90)
        return ents

    if cat == "sofa":
        portrait = h > w
        L, D = (h, w) if portrait else (w, h)
        back, arm = 230.0, 150.0
        ents = [("polyb", _round_rect(L, D, min(L, D) * 0.06))]
        ents.append(("line", [(-L/2 + arm * 0.4, D/2 - back), (L/2 - arm * 0.4, D/2 - back)]))
        ents.append(("line", [(-L/2 + arm, D/2), (-L/2 + arm, -D/2)]))
        ents.append(("line", [(L/2 - arm, D/2), (L/2 - arm, -D/2)]))
        inner = L - 2 * arm
        n = max(1, int(round(inner / 800.0)))
        for i in range(1, n):
            x = -L/2 + arm + inner * i / n
            ents.append(("line", [(x, D/2 - 20), (x, -D/2)]))
        if portrait:
            ents = _rotate(ents, 90)
        return ents

    if cat == "lounge":
        back, arm = 200.0, 120.0
        return [("polyb", _round_rect(w, h, min(w, h) * 0.05)),
                ("line", [(-w/2 + arm * 0.5, h/2 - back), (w/2 - arm * 0.5, h/2 - back)]),
                ("line", [(-w/2 + arm, h/2), (-w/2 + arm, -h/2)]),
                ("line", [(w/2 - arm, h/2), (w/2 - arm, -h/2)])]

    hw, hd = w / 2.0, h / 2.0
    return [("poly", [(-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)])]


def _rotate(ents, deg):
    m = Matrix44.z_rotate(math.radians(deg))
    out = []
    for kind, data in ents:
        if kind == "poly":
            out.append(("poly", [(m.transform(Vec3(x, y, 0)).x, m.transform(Vec3(x, y, 0)).y) for x, y in data]))
        elif kind == "polyb":
            out.append(("polyb", [(m.transform(Vec3(x, y, 0)).x, m.transform(Vec3(x, y, 0)).y, b) for x, y, b in data]))
        elif kind == "line":
            out.append(("line", [(m.transform(Vec3(x, y, 0)).x, m.transform(Vec3(x, y, 0)).y) for x, y in data]))
        elif kind == "circle":
            out.append(("circle", data))
    return out


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #
def classify(block_name, product_name, w, h, rules):
    """名称关键词优先（比品类码具体），再回落到品类码。

    真实项目里 TZ 既有沙发也有书桌、CU 既有床也有床头柜、CY 里有单人沙发，
    只靠前缀会判错；产品名里的「柜/床/沙发/椅/桌」才是可靠信号。
    """
    pref = block_name[:2].upper()
    nm = (product_name or "").strip()
    limit = rules["thresholds"]["chair_limit_mm"]
    round_like = ("圆" in nm) and abs(w - h) <= 30

    def chair_like():
        if round_like and ("凳" in nm or max(w, h) <= 450):
            return "circle"
        if "凳" in nm and max(w, h) <= 500:
            return "rect"
        if max(w, h) <= limit:
            return "chair"
        if min(w, h) <= 450:
            return "rect"          # 长凳 / 条形
        return "lounge"

    # 1) 产品名关键词
    if any(k in nm for k in ("柜", "架", "屏风", "隔断", "镜")):
        return "circle" if round_like else "rect"
    if "床" in nm:
        return "bed" if min(w, h) >= 1000 else "rect"
    if "沙发" in nm:
        return "circle" if round_like else "sofa"
    if "椅" in nm or "凳" in nm:
        return chair_like()
    if any(k in nm for k in ("桌", "几", "台")):
        return "circle" if round_like else "rect"

    # 2) 回落到品类码
    fam = rules["category_codes"].get(pref, "rect")
    if fam == "chair":
        return chair_like()
    if fam == "bed":
        if "柜" in nm or min(w, h) < 1000:
            return "circle" if round_like else "rect"
        return "bed"
    if fam == "sofa":
        return "circle" if round_like else "sofa"
    if fam == "table":
        return "circle" if round_like else "rect"
    return "circle" if round_like else "rect"


def find_chair_template(doc, approved_layer, limit, rules):
    """从本项目已有合格块里挑"最常用于 ≤650 椅子"的那一个作为模板。"""
    sig = Counter()
    owner = {}
    for ins in doc.modelspace().query("INSERT"):
        if ins.dxf.layer != approved_layer or ins.dxf.name[:2].upper() != "CY":
            continue
        blk = doc.blocks.get(ins.dxf.name)
        if blk is None or not len(blk):
            continue
        bb = block_bbox(blk)
        if max(bb.size.x, bb.size.y) > limit:
            continue
        key = (round(bb.size.x), round(bb.size.y), len(list(blk)))
        sig[key] += 1
        owner.setdefault(key, ins.dxf.name)
    if not sig:
        return None, None
    key, _ = sig.most_common(1)[0]
    return owner[key], key


# --------------------------------------------------------------------------- #
# 表格
# --------------------------------------------------------------------------- #
def read_table(path, sheet=None):
    """返回 [{'sku','name','spec','brand'}]；兼容列顺序变化（按表头识别）。"""
    try:
        import openpyxl
    except ImportError:
        sys.exit("缺少依赖：pip install openpyxl")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = ws.iter_rows(min_row=1, max_row=1, values_only=True)
    header = [str(c).strip() if c else "" for c in next(rows)]

    def find(*keys):
        for i, h in enumerate(header):
            if any(k in h for k in keys):
                return i
        return None

    i_sku = find("产品编号", "SKU", "编号")
    i_name = find("物料名称", "名称", "品名")
    i_spec = find("规格", "品牌名称")
    out = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if i_sku is None or i_sku >= len(r) or not r[i_sku]:
            continue
        sku = str(r[i_sku]).strip()
        spec = r[i_spec] if i_spec is not None and i_spec < len(r) else None
        out.append({
            "sku": sku,
            "name": r[i_name] if i_name is not None and i_name < len(r) else None,
            "spec": spec,
            "brand": brand_from_spec(spec),
            "size": parse_spec(spec),
        })
    wb.close()
    return out


def index_table(rows):
    idx = defaultdict(list)
    for r in rows:
        idx[sku_base(r["sku"])].append(r)
    return idx


def index_photos(path):
    idx = defaultdict(list)
    if not path or not os.path.isdir(path):
        return idx
    for fn in os.listdir(path):
        stem, ext = os.path.splitext(fn)
        if ext.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            idx[sku_base(stem)].append(fn)
    return idx


# --------------------------------------------------------------------------- #
# 命令：audit
# --------------------------------------------------------------------------- #
def cmd_audit(args):
    rules = load_rules(args.rules)
    doc = ezdxf.readfile(args.dxf, encoding=args.encoding)
    msp = doc.modelspace()
    L = rules["layers"]

    inserts = list(msp.query("INSERT"))
    by_layer = Counter(i.dxf.layer for i in inserts)
    pending = [i for i in inserts if i.dxf.layer == L["pending"]]
    print(f"文件: {args.dxf}")
    print(f"图块 {len(list(doc.blocks))} / 插入 {len(inserts)} {dict(by_layer)}")
    print(f"待校对（{L['pending']}）: {len(pending)}")

    tol = rules["block"]["center_tolerance"]
    bad = Counter()
    offenders = defaultdict(list)
    seen = set()
    for ins in inserts:
        if ins.dxf.name in seen:
            continue
        seen.add(ins.dxf.name)
        blk = doc.blocks.get(ins.dxf.name)
        if blk is None or not len(blk):
            bad["空块"] += 1
            continue
        ents = list(blk)
        if any(e.dxftype() in ("TEXT", "MTEXT", "DIMENSION", "HATCH") for e in ents):
            bad["块内文字/标注/填充"] += 1
            offenders["块内文字/标注/填充"].append(ins.dxf.name)
        if any(e.dxf.layer != rules["block"]["inner_layer"] for e in ents):
            bad["块内非 0 层"] += 1
            offenders["块内非 0 层"].append(ins.dxf.name)
        if any(e.dxf.color != rules["block"]["inner_color"] for e in ents):
            bad["块内颜色未 ByBlock"] += 1
            offenders["块内颜色未 ByBlock"].append(ins.dxf.name)
        bb = block_bbox(blk)
        cx, cy = (bb.extmin.x + bb.extmax.x) / 2, (bb.extmin.y + bb.extmax.y) / 2
        if abs(cx) > tol or abs(cy) > tol:
            bad["未居中"] += 1
            offenders["未居中"].append(f"{ins.dxf.name}({cx:.0f},{cy:.0f})")
        if ins.dxf.layer == L["insert"] and ins.dxf.color not in (256, rules["insert"]["color"]):
            bad["INSERT 颜色异常"] += 1

    grid = TextGrid(msp.query("TEXT"))
    miss = []
    for ins in inserts:
        if ins.dxf.layer not in (L["insert"],):
            continue
        pt = (ins.dxf.insert.x, ins.dxf.insert.y)
        got = grid.nearest(pt, (L["number"], L["legacy_number"], L["brand"]),
                           rules["thresholds"]["text_search_radius"])
        if L["number"] not in got and L["legacy_number"] not in got:
            miss.append(ins.dxf.name + " 缺编号")
        if L["brand"] not in got:
            miss.append(ins.dxf.name + " 缺品牌")

    print("\n体检结果:")
    if not bad:
        print("  图块规范: 全部通过")
    else:
        for k, v in bad.most_common():
            sample = ", ".join(offenders[k][:5])
            print(f"  {k}: {v}  例: {sample}")
    print(f"  标签缺失: {len(miss)}" + (f"  例: {', '.join(miss[:6])}" if miss else ""))
    if pending:
        print(f"\n待校对块清单（前 20）: {', '.join(i.dxf.name for i in pending[:20])}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"pending": [i.dxf.name for i in pending],
                       "issues": {k: v for k, v in bad.items()},
                       "missing_labels": miss}, f, ensure_ascii=False, indent=1)
        print(f"\n已写出 {args.json}")
    return 0


# --------------------------------------------------------------------------- #
# 命令：index
# --------------------------------------------------------------------------- #
def cmd_index(args):
    rules = load_rules(args.rules)
    doc = ezdxf.readfile(args.dxf, encoding=args.encoding)
    L = rules["layers"]
    out = []
    for ins in doc.modelspace().query("INSERT"):
        if ins.dxf.layer != L["insert"]:
            continue
        blk = doc.blocks.get(ins.dxf.name)
        if blk is None:
            continue
        bb = block_bbox(blk)
        out.append({
            "block": ins.dxf.name,
            "w": round(bb.size.x, 1), "h": round(bb.size.y, 1),
            "entities": len(list(blk)),
            "types": dict(Counter(e.dxftype() for e in blk)),
        })
    data = {"source": os.path.basename(args.dxf), "blocks": out}
    path = args.out or "block_index.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"已导出 {len(out)} 个合格图块 → {path}")
    return 0


# --------------------------------------------------------------------------- #
# 命令：fix
# --------------------------------------------------------------------------- #
def cmd_fix(args):
    rules = load_rules(args.rules)
    L, T = rules["layers"], rules["text"]
    doc = ezdxf.readfile(args.dxf, encoding=args.encoding)
    msp = doc.modelspace()

    table_idx = {}
    photos = defaultdict(list)
    if args.table:
        table_idx = index_table(read_table(args.table, args.sheet))
    if args.sku_dir:
        photos = index_photos(args.sku_dir)
    if not args.table:
        print("提示：未提供 --table，将只做格式规范化，不改尺寸/品牌")

    # OCR 兜底（表格没规格时从 SKU 图读尺寸）
    ocr_mode = getattr(args, "ocr", "auto") or "off"
    sku_dir = args.sku_dir
    ocr_dims = None
    ocr_cache = {}
    if ocr_mode != "off" and sku_dir:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import ocr_dims  # noqa: F811
            cache_path = getattr(args, "ocr_cache", None)
            if cache_path and os.path.exists(cache_path):
                ocr_cache = json.load(open(cache_path, encoding="utf-8"))
            print(f"OCR 兜底: 开启（{ocr_mode}），缓存 {len(ocr_cache)} 条")
        except Exception as e:
            print(f"OCR 不可用（{e}），跳过")
            ocr_mode, ocr_dims = "off", None

    pending = [i for i in msp.query("INSERT") if i.dxf.layer == L["pending"]]
    if args.max_items:
        pending = pending[:args.max_items]
    if not pending:
        print("没有待校对块，退出。")
        return 0

    tpl_name, tpl_key = None, None
    if args.chair_template:
        tpl_name = args.chair_template
    else:
        tpl_name, tpl_key = find_chair_template(doc, L["insert"],
                                                rules["thresholds"]["chair_limit_mm"], rules)
    tpl_entities = list(doc.blocks.get(tpl_name)) if tpl_name else []
    if tpl_name:
        print(f"椅模板: {tpl_name}  {tpl_key}  {len(tpl_entities)} 实体")

    texts = list(msp.query("TEXT"))
    grid = TextGrid(texts)
    rows = []
    stats = Counter()
    issues = []

    for ins in pending:
        name = ins.dxf.name
        rec = (table_idx.get(sku_base(name)) or [{}])[0]
        blk = doc.blocks.get(name)
        if blk is None:
            issues.append((name, "块不存在"))
            continue
        ents = list(blk)
        bb = block_bbox(blk)
        w, h = bb.size.x, bb.size.y
        cx, cy = (bb.extmin.x + bb.extmax.x) / 2, (bb.extmin.y + bb.extmax.y) / 2
        placeholder = is_placeholder(ents, rules)

        # ---- 已经套用共用椅模板的块：不再拿表格尺寸去"挑错" ----
        on_template = bool(tpl_key) and (
            (abs(w - tpl_key[0]) <= 2 and abs(h - tpl_key[1]) <= 2) or
            (abs(w - tpl_key[1]) <= 2 and abs(h - tpl_key[0]) <= 2))

        # ---- 尺寸：表格 > SKU 图 OCR > 现有包围盒 ----
        tw, th = w, h
        tol = rules["thresholds"]["size_tolerance_mm"]
        spec = rec.get("size")
        status_size = "no-spec"
        size_src = "keep"
        ocr_status, ocr_note = "", ""
        if on_template:
            status_size, size_src = "ok-template", "template"
            tw, th = w, h
        elif spec and len(spec) >= 2:
            Lw, Ww = spec[0], spec[1]
            if abs(Lw - w) <= tol and abs(Ww - h) <= tol:
                status_size = "ok"
                size_src = "table"
            elif abs(Lw - h) <= tol and abs(Ww - w) <= tol:
                tw, th, status_size, size_src = Lw, Ww, "ok-rot", "table"
            elif placeholder and max(Lw, Ww) >= 300:
                tw, th, status_size, size_src = Lw, Ww, "fixed", "table"
                issues.append((name, f"尺寸 {round(w)}×{round(h)} → {round(Lw)}×{round(Ww)}（表格）"))
            else:
                status_size = "mismatch-keep"
                size_src = "keep"
                issues.append((name, f"尺寸不一致，保留原值 {round(w)}×{round(h)}（表格 {round(Lw)}×{round(Ww)}）"))

        # ---- OCR 兜底：表格没有规格、或占位块是 ERP 默认方块时，从 SKU 图读尺寸 ----
        need_ocr = (ocr_mode == "all") or (ocr_mode == "auto" and size_src == "keep" and placeholder)
        if need_ocr and sku_dir and ocr_dims is not None:
            ow = oh = None
            try:
                parsed, _lines, _photo, _text = ocr_dims.read_dims(sku_dir, name, ocr_cache)
                if parsed:
                    ow, oh, ocr_status, ocr_note = ocr_dims.decide(parsed, w, h, text=_text)
                else:
                    ocr_status, ocr_note = "ocr-empty", "图上没读到尺寸"
            except SystemExit as e:
                ocr_status, ocr_note = "ocr-unavailable", str(e)
                ocr_mode = "off"
            if ocr_status in ocr_dims.AUTO_APPLY and ow:
                tw, th, size_src = ow, oh, "ocr"
                stats["OCR补尺寸"] += 1
                issues.append((name, f"尺寸 {round(w)}×{round(h)} → {round(ow)}×{round(oh)}（图 {ocr_status}）"
                                     + ("  ⚠" + ocr_note if "复核" in ocr_note else "")))
            elif ow:
                issues.append((name, f"OCR 建议 {round(ow)}×{round(oh)} 未自动采用（{ocr_note}）"))
            elif ocr_status and ocr_status not in ("ocr-empty", "ocr-unavailable"):
                stats[ocr_status] += 1

        # ---- 几何 ----
        if placeholder:
            cat = classify(name, rec.get("name"), tw, th, rules)
            for e in list(blk):
                blk.delete_entity(e)
            for kind, data in build_geometry(cat, tw, th, tpl_entities):
                if kind == "poly":
                    blk.add_lwpolyline(data, close=True, dxfattribs={
                        "layer": L["block"] if "block" in L else "0", "color": 0, "linetype": "ByBlock"})
                elif kind == "polyb":
                    blk.add_lwpolyline([(x, y, 0, 0, b) for x, y, b in data], format="xyseb",
                                       close=True, dxfattribs={"layer": "0", "color": 0, "linetype": "ByBlock"})
                elif kind == "line":
                    blk.add_line(data[0], data[1], dxfattribs={"layer": "0", "color": 0, "linetype": "ByBlock"})
                elif kind == "circle":
                    blk.add_circle((data[0], data[1]), data[2],
                                   dxfattribs={"layer": "0", "color": 0, "linetype": "ByBlock"})
                elif kind == "raw":
                    ne = data
                    ne.dxf.layer = "0"; ne.dxf.color = 0; ne.dxf.linetype = "ByBlock"
                    blk.add_entity(ne)
            if abs(cx) > 1 or abs(cy) > 1:
                ins.dxf.insert = (ins.dxf.insert.x + cx, ins.dxf.insert.y + cy)
        else:
            cat = "keep"
            for e in blk:
                if abs(cx) > 0.5 or abs(cy) > 0.5:
                    e.transform(Matrix44.translate(-cx, -cy, 0))
                e.dxf.layer = "0"; e.dxf.color = 0; e.dxf.linetype = "ByBlock"
            if abs(cx) > 1 or abs(cy) > 1:
                ins.dxf.insert = (ins.dxf.insert.x + cx, ins.dxf.insert.y + cy)

        ins.dxf.layer, ins.dxf.color = L["insert"], rules["insert"]["color"]
        stats[cat] += 1

        # ---- 标签 ----
        pt = (ins.dxf.insert.x, ins.dxf.insert.y)
        hit = grid.nearest(pt, (L["number"], L["legacy_number"], L["brand"]),
                           rules["thresholds"]["text_search_radius"])
        num = hit.get(L["number"]) or hit.get(L["legacy_number"])
        br = hit.get(L["brand"])
        if num is not None:
            num.dxf.layer, num.dxf.color = L["number"], T["color_number"]
            num.dxf.height = T["number_height"]
            num.dxf.style = T["style"]
            num.set_placement((pt[0], pt[1] + T["number_dy"]), align=TextEntityAlignment.MIDDLE_CENTER)
        else:
            t = msp.add_text(name, height=T["number_height"],
                             dxfattribs={"layer": L["number"], "color": T["color_number"], "style": T["style"]})
            t.set_placement((pt[0], pt[1] + T["number_dy"]), align=TextEntityAlignment.MIDDLE_CENTER)
            stats["新建编号"] += 1

        brand_ok = bool(br) and not is_brand_placeholder(br.dxf.text, rules)
        if br is not None and brand_ok:
            pass
        elif rec.get("brand"):
            if br is None:
                br = msp.add_text(rec["brand"], height=T["brand_height"],
                                  dxfattribs={"layer": L["brand"], "color": T["color_brand"], "style": T["style"]})
                stats["新建品牌"] += 1
            else:
                br.dxf.text = rec["brand"]
                stats["改写品牌"] += 1
        elif br is not None:
            msp.delete_entity(br)
            texts.remove(br)
            br = None
            stats["删除占位品牌"] += 1

        if br is not None:
            br.dxf.layer, br.dxf.color = L["brand"], T["color_brand"]
            br.dxf.height = T["brand_height"]
            br.dxf.style = T["style"]
            br.set_placement((pt[0], pt[1] + T["brand_dy"]), align=TextEntityAlignment.MIDDLE_CENTER)

        rows.append({
            "block": name, "category": cat,
            "old": f"{round(w)}x{round(h)}", "new": f"{round(tw)}x{round(th)}",
            "size_status": status_size, "size_src": size_src,
            "ocr_status": ocr_status, "ocr_note": ocr_note,
            "brand": (rec.get("brand") or (br.dxf.text if br is not None else "")),
            "photo": "Y" if photos.get(sku_base(name)) else "N",
            "table": "Y" if rec.get("sku") else "N",
        })

    # 清理待校对说明层
    review = [t for t in msp.query("TEXT") if t.dxf.layer == L["review"]]
    for t in review:
        msp.delete_entity(t)
    if review:
        stats["删除待校对说明"] += len(review)

    print("\n分类统计:", dict(stats))
    if ocr_cache and getattr(args, "ocr_cache", None):
        json.dump(ocr_cache, open(args.ocr_cache, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"OCR 缓存 → {args.ocr_cache}（{len(ocr_cache)} 条）")
    if issues:
        print("\n修正/异常:")
        for n, msg in issues[:40]:
            print(f"  {n}: {msg}")
        if len(issues) > 40:
            print(f"  ... 共 {len(issues)} 条")

    if args.report:
        with open(args.report, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["block"])
            wr.writeheader()
            wr.writerows(rows)
        print(f"\n清单已写出 {args.report}（{len(rows)} 行）")

    if not args.apply:
        print("\n[预演] 未修改文件。确认无误后加 --apply 写回。")
        return 0

    # ---- 写回前自检 ----
    bad = []
    left = [i for i in msp.query("INSERT") if i.dxf.layer == L["pending"]]
    if left:
        bad.append(f"仍有 {len(left)} 个 {L['pending']} 插入")
    tol = rules["block"]["center_tolerance"]
    for ins in pending:
        blk = doc.blocks.get(ins.dxf.name)
        if blk is None or not len(blk):
            bad.append(f"{ins.dxf.name} 空块")
            continue
        bb = block_bbox(blk)
        if abs((bb.extmin.x + bb.extmax.x) / 2) > tol or abs((bb.extmin.y + bb.extmax.y) / 2) > tol:
            bad.append(f"{ins.dxf.name} 未居中")
    if bad:
        print("\n自检未通过，未写入:", bad[:8])
        return 1
    doc.saveas(args.dxf, encoding=args.encoding)
    print(f"\n已写回 {args.dxf}")
    return 0


def is_brand_placeholder(txt, rules):
    if not txt:
        return True
    if any(p in txt for p in rules["brand_placeholders"]):
        return True
    if len(txt) > 24 or "/" in txt:
        return True
    return False


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="家具平面图块校对/生成工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="体检 DXF 是否合规")
    a.add_argument("dxf")
    a.add_argument("--rules")
    a.add_argument("--json")
    a.add_argument("--encoding", default="utf-8")
    a.set_defaults(func=cmd_audit)

    i = sub.add_parser("index", help="导出合格图块索引")
    i.add_argument("dxf")
    i.add_argument("--rules")
    i.add_argument("--out")
    i.add_argument("--encoding", default="utf-8")
    i.set_defaults(func=cmd_index)

    f = sub.add_parser("fix", help="批量校对占位块 → 统一风格（默认预演）")
    f.add_argument("dxf")
    f.add_argument("--table", help="家具表格 xlsx")
    f.add_argument("--sheet", help="工作表名")
    f.add_argument("--sku-dir", help="SKU 图片目录（统计有无图；--ocr 开启时用于读尺寸）")
    f.add_argument("--rules")
    f.add_argument("--ocr", choices=["off", "auto", "all"], default="auto",
                   help="表格没规格时用 OCR 从 SKU 图读尺寸：auto=只在缺规格/默认方块时读；all=全部读；off=关闭")
    f.add_argument("--ocr-cache", help="OCR 结果缓存 json（复跑省时间）")
    f.add_argument("--chair-template", help="指定椅模板块名（默认自动挑选）")
    f.add_argument("--report", help="导出逐块清单 csv")
    f.add_argument("--max-items", type=int, help="只处理前 N 个（试跑）")
    f.add_argument("--apply", action="store_true", help="真正写回 DXF")
    f.add_argument("--encoding", default="utf-8")
    f.set_defaults(func=cmd_fix)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
