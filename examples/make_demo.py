#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_demo.py — 生成一套可自测的示例数据（demo.dxf + demo_table.xlsx）

    python examples/make_demo.py
    python scripts/sku_to_cad.py audit demo.dxf
    python scripts/sku_to_cad.py fix demo.dxf --table demo_table.xlsx --report demo_report.csv
    python scripts/sku_to_cad.py fix demo.dxf --table demo_table.xlsx --apply
    python scripts/dxf_render.py demo.dxf --region 8000,24000,-6000,0 --out demo.png

demodata 故意埋了几种典型情况：
    · 已经有真实几何的合格块（作为椅模板来源）
    · 只有 4 条线的橙色占位块
    · 品牌写着「名称待核对」
    · ERP 默认 1000×1000 方块（真实尺寸只在表格里）
    · 一个圆形茶几、一个沙发、一张床、若干柜子
"""
import math
import os
import sys

import ezdxf
from ezdxf.enums import TextEntityAlignment

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DXF = os.path.join(HERE, "..", "demo.dxf")
OUT_XLSX = os.path.join(HERE, "..", "demo_table.xlsx")

# SKU, 名称, 规格(长*宽*高cm), 品牌, 占位块尺寸(mm)
ITEMS = [
    ("CY1001", "示例 温莎餐椅", "46*41*83cm", "SAMPLE", (460, 410)),
    ("CY1002", "示例 餐椅 宽版", "52*54*80cm", "SAMPLE", (520, 540)),
    ("CJ2001", "示例 圆茶几", "100*100*35cm", "SAMPLE", (1000, 1000)),
    ("CZ2002", "示例 实木餐桌", "180*85*75cm", "SAMPLE", (1800, 850)),
    ("DG3001", "示例 边柜", "157*45*81cm", "SAMPLE", (1570, 450)),
    ("SG3002", "示例 书架", "120*40*195cm", "SAMPLE", (400, 1200)),
    ("BF4001", "示例 三人沙发", "238*110*78cm", "SAMPLE", (2380, 1100)),
    ("CU5001", "示例 双人床", "180*200*90cm", "SAMPLE", (1800, 2000)),
    ("TZ6001", "示例 书桌", "170*80*76cm", "SAMPLE", (1000, 1000)),   # 默认方块，真实 1700×800
]


def chair_template_entities():
    """一把 536×581 的示意椅子（模拟真实库里的 19 实体描摹轮廓）"""
    hw, hd = 268.0, 290.0
    ents = [("rect", [(-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)])]
    # 靠背弧（用折线近似）
    back = [(-hw + 20 + i * (2 * hw - 40) / 12, hd - 40 * math.sin(math.pi * i / 12)) for i in range(13)]
    ents.append(("poly", back))
    # 左右扶手
    for sx in (-1, 1):
        ents.append(("poly", [(sx * hw, -hd * 0.5), (sx * (hw - 30), -hd * 0.2),
                              (sx * (hw - 30), hd * 0.3), (sx * hw, hd * 0.55)]))
    # 座面
    ents.append(("poly", [(-hw * 0.8, -hd * 0.6), (hw * 0.8, -hd * 0.6),
                          (hw * 0.8, hd * 0.2), (-hw * 0.8, hd * 0.2)]))
    return ents


def build():
    doc = ezdxf.new("R2018", setup=False)
    doc.header["$INSUNITS"] = 4
    for name, color in [("FURNITURE", 7), ("NUMBER", 2), ("BRAND", 7),
                        ("ERP_PENDING", 30), ("ERP_REVIEW", 6), ("NEW_NUMBER", 2)]:
        doc.layers.add(name, color=color)
    doc.styles.add("FURNITURE_TEXT", font="simhei.ttf")
    msp = doc.modelspace()

    def add_labels(ins_pt, sku, brand):
        t = msp.add_text(sku, height=100, dxfattribs={"layer": "NUMBER", "color": 2, "style": "FURNITURE_TEXT"})
        t.set_placement((ins_pt[0], ins_pt[1] + 70), align=TextEntityAlignment.MIDDLE_CENTER)
        t = msp.add_text(brand, height=100, dxfattribs={"layer": "BRAND", "color": 7, "style": "FURNITURE_TEXT"})
        t.set_placement((ins_pt[0], ins_pt[1] - 70), align=TextEntityAlignment.MIDDLE_CENTER)

    # ---- 已合格的椅模板块（供 fix 自动识别）----
    blk = doc.blocks.new("CY0999")
    for kind, pts in chair_template_entities():
        blk.add_lwpolyline(pts, close=True, dxfattribs={"layer": "0", "color": 0, "linetype": "ByBlock"})
    msp.add_blockref("CY0999", (2000, -1500), dxfattribs={"layer": "FURNITURE", "color": 7})
    add_labels((2000, -1500), "CY0999", "SAMPLE")

    # ---- 橙色占位块 ----
    for i, (sku, name, spec, brand, (w, h)) in enumerate(ITEMS):
        col, row = i % 3, i // 3
        ins_pt = (12000 + col * 4500, -1200 - row * 3800)
        b = doc.blocks.new(sku)
        hw, hd = w / 2.0, h / 2.0
        b.add_lwpolyline([(-hw, -hd), (hw, -hd)], dxfattribs={"layer": "0"})
        b.add_lwpolyline([(hw, -hd), (hw, hd)], dxfattribs={"layer": "0"})
        b.add_lwpolyline([(hw, hd), (-hw, hd)], dxfattribs={"layer": "0"})
        b.add_lwpolyline([(-hw, hd), (-hw, -hd)], dxfattribs={"layer": "0"})
        # 占位块要"4 条 LINE"才算占位，这里用真正的 LINE
        for e in list(b):
            b.delete_entity(e)
        b.add_line((-hw, -hd), (hw, -hd), dxfattribs={"layer": "0"})
        b.add_line((hw, -hd), (hw, hd), dxfattribs={"layer": "0"})
        b.add_line((hw, hd), (-hw, hd), dxfattribs={"layer": "0"})
        b.add_line((-hw, hd), (-hw, -hd), dxfattribs={"layer": "0"})

        msp.add_blockref(sku, ins_pt, dxfattribs={"layer": "ERP_PENDING", "color": 30})
        t = msp.add_text(sku, height=170, dxfattribs={"layer": "NEW_NUMBER", "color": 6, "style": "FURNITURE_TEXT"})
        t.set_placement((ins_pt[0], ins_pt[1] + 105), align=TextEntityAlignment.MIDDLE_CENTER)
        t = msp.add_text("名称待核对", height=145, dxfattribs={"layer": "BRAND", "color": 7, "style": "FURNITURE_TEXT"})
        t.set_placement((ins_pt[0], ins_pt[1] - 105), align=TextEntityAlignment.MIDDLE_CENTER)
        t = msp.add_text(f"待校对  {w} × {h} mm", height=145,
                         dxfattribs={"layer": "ERP_REVIEW", "color": 6, "style": "FURNITURE_TEXT"})
        t.set_placement((ins_pt[0], ins_pt[1] - 700), align=TextEntityAlignment.MIDDLE_CENTER)

    t = msp.add_text("ERP 待校对家具 · 待校对", height=330,
                     dxfattribs={"layer": "ERP_REVIEW", "color": 6, "style": "FURNITURE_TEXT"})
    t.set_placement((12000, 800), align=TextEntityAlignment.MIDDLE_CENTER)

    doc.saveas(OUT_DXF)
    print("已生成", os.path.normpath(OUT_DXF))

    # ---- 示例表格 ----
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["产品编号", "产品图片", "物料名称", "规格型号 / 品牌名称", "总可销"])
    for sku, name, spec, brand, _ in ITEMS:
        ws.append([sku + "Z1", None, name, f"{spec}\n{brand}", 1])
    wb.save(OUT_XLSX)
    print("已生成", os.path.normpath(OUT_XLSX))


if __name__ == "__main__":
    build()
