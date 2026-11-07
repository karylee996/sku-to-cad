---
name: sku-to-cad
description: Turn furniture SKU data (product photos + an ERP spreadsheet) into clean, consistent AutoCAD plan blocks, or batch-proofread an existing furniture library DXF that still contains orange placeholder rectangles. Encodes the house drawing style (1:1 mm, block geometry on layer 0 / ByBlock, bbox centred on the origin, INSERT on FURNITURE, SKU + brand labels at fixed offsets, one closed rectangle for simple case goods, a shared template for dining chairs ≤650 mm) and ships a CLI that audits, fixes and indexes DXF libraries without touching anything else.
whenToUse: The user has a furniture library DXF with orange "待校对" placeholder blocks, or a spreadsheet/SKU-photo set and wants plan blocks generated or standardised; or asks to check a furniture DXF against a drawing standard (layers, block style, label offsets, sizes).
---

# sku-to-cad — SKU 资料 ⇄ 家具平面图块

把「SKU 产品图 + ERP 表格」变成风格统一的 CAD 平面图块，或者反向把一张已有家具库 DXF 里的橙色占位块批量校对成正式图块。

**核心不是"画得像"，而是"格式统一、尺寸可核、批量可复跑"。**

---

## 1. House style（这套图的硬规则）

| 项 | 规则 | 为什么 |
|---|---|---|
| 单位 | **mm，1:1**（图块几何 = 真实尺寸） | 4000×3050 的沙发就是 4000×3050 |
| `$INSUNITS` | 常写成 1（英寸）**但与实际不符** | 不要按 header 换算，会差 25.4 倍 |
| 缩放 | `xscale = yscale = 1.0` 恒定 | 永远不缩图块 |
| 朝向 | 只出现在 `INSERT.rotation` | 块内不烘焙旋转 |
| 块内几何 | `layer 0` + `color 0(ByBlock)` + `linetype ByBlock` | 插入到 FURNITURE 层即继承颜色/线型 |
| 块原点 | **包围盒几何中心 = (0,0)** | 插入点 = 家具中心，摆放/统计才有意义 |
| 简单家具 | **1 条闭合矩形就够了** | 桌/几/柜/架/屏 类，内部线是噪声 |
| 圆桌/圆几 | 1 个 `CIRCLE` | 不要用矩形近似 |
| 床 | 矩形 + 床尾带线 + 分缝 + 床头线（3~4 实体） | 表达床垫/床头语义 |
| 沙发 | 圆角轮廓 + 靠背线 + 2 条扶手线 + 坐垫分格（4~8 实体） | 表达坐向 |
| 椅（CY，≤650） | **全部共用一个椅模板**（不缩放），但**每个 SKU 独立块名** | 平面图分辨不出 460 与 580 的椅子 |
| 椅（>650） | 圆角轮廓 + 靠背线 + 2 扶手线；长凳类直接矩形 | 超出口径，单独画 |
| 屏风 | 每扇 1 条细长矩形，沿折点串联 | 不要画合页 |
| 禁止 | 无 HATCH、无 DIMENSION、块内无 TEXT、不画腿/五金/材质 | 平面图只留轮廓 |

**标签（每条 INSERT 固定两条 TEXT）**

| 层 | 字高 | 颜色 | 位置 | 对齐 |
|---|---|---|---|---|
| `NUMBER` | 100 | 2（黄） | 插入点 `+(0, +70)` | `halign=1` 居中 / `valign=2` 中线 |
| `BRAND` | 100 | 7（白） | 插入点 `+(0, -70)` | 同上 |

字体样式 `FURNITURE_TEXT`（黑体 simhei）。两条文字**同 x 轴心**。

---

## 2. 命名与编码

- SKU = `品类码(2 字母) + 型号(4 位) + 变体后缀(可选)`，例 `BF0836Z2`
- 图块名 = SKU **去掉变体后缀** → `BF0836`（历史块 `QG0089Z3` 保留后缀，匹配要双向兼容）
- 品类码决定几何家族：

| 码 | 品类 | 码 | 品类 |
|---|---|---|---|
| BF / PF / TZ | 沙发 | CJ / CZ / CG | 桌 / 几 |
| CY | 椅 | DG / SG / SJ | 柜 / 架 |
| CU | 床 | QG | 屏风 |

---

## 3. 数据链路

```
表格 A=产品编号  C=物料名称  D=规格型号/品牌名
        │                        │
        │                        └─ "405*305*73cm\nBleu Nature"
        │                              ↓ ×10 → 4050×3050 mm，高丢弃
        ├─ SKU 去后缀 → 图块名 → 品类码 → 选配方
        │
        └─ B=产品图片（WPS DISPIMG 单元格图）→ xl/media/*.png
                                            ↓ 图上尺寸引线（OCR）
                                    值 <10 当米，≥10 当厘米
```

**尺寸优先级**：表格 D 列 > SKU 图引线（OCR）> 现有图块包围盒（无资料就保留，不臆造）。

### OCR 兜底（表格没有规格时）

`fix --ocr auto`（默认）只在「表格无规格」或「占位块是 ERP 默认方块（800/900/1000/1200 正方形）」时才读图，
避免无谓的 OCR 开销。引擎用 Windows 自带的 `Windows.Media.Ocr`（`pip install winsdk`，无需 Tesseract），
**英文 + 中文双引擎各跑一遍再合并**：数字（`1.7m`）英文更准，中文关键词（`圆`/`高`/`长`）只有中文引擎给得出。

判定分级——只有高置信度的会自动改写尺寸：

| 判定 | 含义 | 是否自动套用 |
|---|---|---|
| `ocr-dia` / `ocr-round` | 图上标了 ⌀，或文字含「圆」 | ✅ |
| `ocr-default-square` | 占位块是默认方块 → 取图上最大两个 | ✅（两值接近时打 ⚠ 提示复核） |
| `ocr-two-values` | 图上只有两个平面尺寸 | ✅ |
| `ocr-confirm` | 与现有尺寸一致 | 不改（仅确认） |
| `ocr-partial` | 一维对上、另一维只是「最接近的候选」 | ❌ 只给建议 |
| `ocr-ambiguous` | 多个尺寸无法判断 | ❌ 进人工队列 |

---

## 4. 工作流

### 阶段 0 · 先体检

```bash
python scripts/sku_to_cad.py audit 家具库.dxf --json audit.json
```

输出：图块数、插入数、待校对块清单、块内违规（层/色/文字/未居中）、标签缺失。

### 阶段 1 · 摸清"参照样式"

不要凭空决定样式，从**同一张图里已经合格的块**反推：

```bash
python scripts/sku_to_cad.py index 家具库.dxf --out blocks.json
```

看 `blocks.json`：合格块大多是什么实体数？简单家具是不是就一个矩形？椅子模板复用了多少次？
（真实项目里：256 个边柜中 208 个是单矩形，75 把椅子共用同一个 19 实体模板。）

### 阶段 2 · 预演，看清单

```bash
python scripts/sku_to_cad.py fix 家具库.dxf \
    --table 家具表格.xlsx --sku-dir SKU/ \
    --ocr auto --ocr-cache ocr_cache.json \
    --report fix_report.csv
```

默认**不写文件**。检查 `fix_report.csv`：`size_status`（ok / ok-rot / ok-template / fixed / mismatch-keep / no-spec）、
`size_src`（table / ocr / template / keep）、`ocr_status`、`ocr_note`、`table`(Y/N)、`photo`(Y/N)。

先用 `--max-items 20` 抽 20 个看判定是否符合预期，再全量跑。

### 阶段 3 · 写回

```bash
python scripts/sku_to_cad.py fix 家具库.dxf --table 家具表格.xlsx --apply
```

写回前自动自检（无残留 pending、每块居中、非空块），不过就不写。

### 阶段 4 · 验收（必做）

```bash
python scripts/sku_to_cad.py audit 家具库.dxf          # 应为 0 违规
python scripts/dxf_render.py 家具库.dxf --region 100000,140000,-12000,2000 --out after.png
python scripts/dxf_render.py 家具库.dxf --region 0,30000,-12000,2000 --out ref.png
```

**同一 mm 比例**渲染「校对区」与「已合格区」并排看：轮廓软硬、标签位置、是否有块相互压叠。
再抽查 6~8 个不同类型的块（发图给用户确认，比描述快）。

---

## 5. 坑（都踩过）

| 坑 | 症状 | 处理 |
|---|---|---|
| `bbox.extents(fast=True)` | 椅子 536mm 被算成 566mm | **必须 `fast=False`**：fast 会把大半径圆弧当整圆取极值 |
| 块几何用了 OCS 镜像 | 渲染出来左右翻转 | 用 `ezdxf.path.make_path()` 展开，别手算 center/angle |
| 复制块时 handle | 同一 handle 出现在多个块里 → 文件损坏 | 用 `add_entity(e.copy())`，ezdxf 会自动重编 handle |
| 共享块名 | 同一个块被"待校对"和"合格"两处引用，改一处动两处 | 改块前先查引用数；必要时补偿另一个 INSERT 的位置 |
| 品牌字段是占位 | 文字是「名称待核对」「新品待校对」，或整段商品名 | 按 `brand_placeholders` + 长度/斜杠判定，用表格第二行覆盖 |
| 默认方块 | 大量 SKU 都是 `1000×1000` | 是 ERP 缺尺寸的默认值；查 SKU 图引线，查不到就标记待补，别猜 |
| WPS 单元格图片 | `=DISPIMG("ID_xxx",1)` 读不到图 | 图在 `xl/media/`，`xl/cellimages.xml` + rels 里有 ID→文件映射 |
| 备注文字层 | 每条占位块下有一行「待校对 1480 × 1040 mm」和整层标题 | 校对完成后**整层清除**，否则图里留一堆"待校对" |
| OCR 全角/空格 | 「1 ． 7 m」「1 · 8 m」「1 7 4 c m」 | 归一化：全角→半角、中点/逗号当小数点、反复合并数字间空格 |
| OCR 高度误判 | 椅子 95cm 总高被当成平面 950 | 竖排文字框（h>w×1.25）或含「高」字的行标记为高度；两值接近时打 ⚠ |
| 椅模板被当成尺寸错误 | 表格 460×410，块却是 536×581 → 一堆假报警 | 块尺寸 ≈ 模板尺寸时判 `ok-template`，不与表格比 |
| 每次 ERP 重新导入会覆盖库 | 修好的块又变回橙色 | 工具是幂等的，导入后重跑一次即可；建议把命令挂到导入流程后面 |

---

## 6. 一体化改造清单（写回后应满足）

- [ ] `ERP_PENDING` 层 0 个 INSERT
- [ ] 图内 0 条「待校对 / 待核对」文字
- [ ] 228/228 块：`layer 0` + `color 0` + 居中
- [ ] 每条 INSERT 有 NUMBER + BRAND，dx=0 / dy=-70
- [ ] `doc.audit()` 0 error
- [ ] 抽样渲染与参照区观感一致（圆角/实体密度）
- [ ] 导出清单：已改 / 一致 / 待人工（通常人工率 1%~5%）

---

## 7. 什么时候必须找人

- SKU 在表格里查不到、也没有产品图 → 尺寸无从核对，**保留原值 + 列清单**，不要编
- 组合/套装 SKU（椅+脚蹬、茶几 L+M+S）→ 需要人决定平面按哪件画
- 曲面/异形家具（弧形沙发、异形桌）→ 包围盒 ≠ 标称长度，按现有图形保留
- 品类跨界的 SKU（`CY` 但其实是沙发、`CU` 但其实是床头柜）→ 名称关键词优先于品类码

---

## 8. 命令速查

```bash
# 体检
python scripts/sku_to_cad.py audit <dxf> [--json audit.json]

# 导出合格块索引（做模板池）
python scripts/sku_to_cad.py index <dxf> [--out blocks.json]

# 预演 / 写回
python scripts/sku_to_cad.py fix <dxf> --table <xlsx> [--sku-dir SKU/] [--ocr auto|all|off] \
        [--ocr-cache ocr.json] [--report r.csv] [--max-items 20]
python scripts/sku_to_cad.py fix <dxf> --table <xlsx> --apply

# 单独看 OCR 读到了什么（调试/建立信任）
python scripts/ocr_dims.py --sku-dir SKU/ --skus TZ2614,TZ3818 --cur "TZ2614=1000x1000"
python scripts/ocr_dims.py --sku-dir SKU/ --all --json ocr_dump.json --cache ocr_cache.json

# 渲染核对
python scripts/dxf_render.py <dxf> --region x0,x1,y0,y1 --out view.png [--px 2000]
python scripts/dxf_render.py <dxf> --blocks BF1141,CY0276,CU0102 --out cards.png
```

依赖：`pip install ezdxf openpyxl pillow`（OCR 额外需要 `pip install winsdk`，仅 Windows）
