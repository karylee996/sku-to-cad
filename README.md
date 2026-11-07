# sku-to-cad

把 **SKU 产品图 + ERP 表格** 变成风格统一的家具平面图块；也能把一张已有家具库 DXF 里的
**橙色占位方块** 批量校对成正式图块（尺寸核对、品牌补全、几何重绘、图层/标签规范化）。

> 实测：228 个橙色占位块，4 类几何模板，一次跑完；脚本机时 < 10 s，人工只需看冲突清单。

## 它解决的问题

ERP 导出的家具库常常长这样：从没画过的 SKU 只有 `长×宽` 的橙色方框 + 「待校对 1480 × 1040 mm」的备注，
而旁边已画好的家具是另一套规范（真几何、`FURNITURE` 层、SKU/品牌两行字）。
这个工具把前者批量改造成后者，并把「核对不上」的东西显式列出来，而不是静默糊过去。

**尺寸来源三级兜底**：表格 D 列 → SKU 图上的尺寸引线（Windows 自带 OCR，无需 Tesseract）→ 保留原值等人工。
实测 7 个「表格里查不到」的 SKU，OCR 自动判定对 6 个，剩 1 个（套装椅）打 ⚠ 进复核清单。

## 安装

```bash
pip install ezdxf openpyxl pillow
pip install winsdk          # 可选：只用 Windows 自带的 OCR 读图上的尺寸
git clone <this repo> && cd sku-to-cad
```

## 快速开始

```bash
# 1) 体检：这张图有多少待校对块、有哪些不合规
python scripts/sku_to_cad.py audit 家具库.dxf

# 2) 预演：只出清单，不动文件
python scripts/sku_to_cad.py fix 家具库.dxf --table 家具表格.xlsx --sku-dir SKU/ --report fix.csv

# 2b) 表格缺规格时，用 OCR 从 SKU 图读尺寸（Windows 自带引擎）
python scripts/sku_to_cad.py fix 家具库.dxf --table 家具表格.xlsx --sku-dir SKU/ \
        --ocr auto --ocr-cache ocr_cache.json --report fix.csv

# 2c) 只想看 OCR 读到了什么
python scripts/ocr_dims.py --sku-dir SKU/ --skus TZ2614,TZ3818 --cur "TZ2614=1000x1000"

# 3) 写回（会先自检，不通过不写）
python scripts/sku_to_cad.py fix 家具库.dxf --table 家具表格.xlsx --apply

# 4) 视觉核对：校对区和参照区同比例并排
python scripts/dxf_render.py 家具库.dxf --region 100000,140000,-12000,2000 --out after.png
python scripts/dxf_render.py 家具库.dxf --region 0,30000,-12000,2000 --out ref.png
```

## 先用示例数据跑一遍（不用你的图纸）

```bash
python examples/make_demo.py          # 生成 demo.dxf + demo_table.xlsx（9 个待校对块）
python scripts/sku_to_cad.py audit demo.dxf
python scripts/sku_to_cad.py fix demo.dxf --table demo_table.xlsx --apply
python scripts/dxf_render.py demo.dxf --region 9000,24000,-12000,0 --out demo.png
```

示例里故意埋了这些坑：只有 4 条线的占位块、品牌写着「名称待核对」、ERP 默认 1000×1000 方块
（真实尺寸 1700×800 只在表格里）。跑完 `audit` 应显示「图块规范: 全部通过」。

## 目录

```
SKILL.md                 给 AI agent 用的完整作业指导（规则/流程/坑/验收）
rules/house_style.json   规则外置：图层、字高、阈值、品类码、配方、OCR 参数
scripts/sku_to_cad.py    audit / fix / index 三个命令
scripts/ocr_dims.py      从 SKU 图读尺寸引线（Windows.Media.Ocr，双引擎）
scripts/dxf_render.py    DXF → PNG，做视觉核对
docs/LEARNED.md          规则怎么从真实图纸反推出来的（含实测数字与 OCR 准确率）
```

## 设计取向

- **不猜数据**：表格没有、图上没有的尺寸，保留原值并列进「待人工」，绝不编。
- **不改无关内容**：只动待校对块及其标签；共享块被多处引用时会先检查再动。
- **一次读写**：预演阶段全部在内存里，写回只发生一次。
- **可复跑**：处理过的块不再是占位块，重复运行不会二次加工。

## 适配你自己的图

改 `rules/house_style.json` 即可，不用改代码：

| 键 | 说明 |
|---|---|
| `layers` | 插入层、待校对层、编号/品牌/备注层名 |
| `text` | 字高、上下偏移、颜色、字体样式 |
| `thresholds.chair_limit_mm` | 椅子共模板的口径（本项目 650） |
| `category_codes` | 品类码 → 几何家族 |
| `brand_placeholders` | 哪些文字算「占位品牌」 |
| `ocr` | 双引擎开关、默认方块值、容差 |

## License

MIT
