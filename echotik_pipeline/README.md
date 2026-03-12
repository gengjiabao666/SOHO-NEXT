# Echotik Pipeline

Echotik 数据清洗流水线，将采集器下载的原始 xlsx 文件处理为标准化的 CSV 数据。

## 数据流向

```
inbox/d|w|m/          原始下载文件（含品类子目录）
    ↓ 标准命名 + 移动
exports/captured=YYYY-MM-DD/
    ├── raw/          标准命名的原始 xlsx
    ├── clean/        清洗后的 CSV（products_clean_v0.csv / shops_clean_v0.csv）
    └── candidates/   Top50 候选列表（按 GMV / 销量排序）
```

## 文件命名规则

```
et_{ds}_{win}_{period}_cap{cap}_n{top}_{geo}[_{category}].xlsx

ds:       p (商品热销榜) / pn (新品榜) / s (小店榜)
win:      d (日榜) / w (周榜) / m (月榜)
period:   日榜=MMDD / 周榜=MMDD-MMDD / 月榜=YYYYMM
cap:      采集日期 MMDD
top:      导出条数（默认 200）
geo:      地区（默认 US）
category: 品类（可选，如 pet_supplies）

示例：
  et_p_d_0311_cap0312_n200_US.xlsx              # 商品日榜 全品类
  et_p_d_0311_cap0312_n200_US_pet_supplies.xlsx # 商品日榜 Pet Supplies
  et_s_w_0302-0308_cap0312_n200_US.xlsx         # 小店周榜
  et_p_m_202602_cap0307_n200_US.xlsx            # 商品月榜
```

## 使用方法

```bash
# 激活环境
conda activate echotik_clean

# 处理今天的数据
python echotik_pipeline.py

# 指定采集日期
python echotik_pipeline.py --captured 2026-03-12

# 指定数据根目录
python echotik_pipeline.py --root /mnt/g/SOHO_repo

# 演练模式（不实际移动文件）
python echotik_pipeline.py --dry-run

# 完整参数
python echotik_pipeline.py --captured 2026-03-12 --root /mnt/g/SOHO_repo --geo US --top 200
```

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--captured` | 今天 | 采集日期 YYYY-MM-DD |
| `--root` | /mnt/g/SOHO_repo | SOHO_repo 根目录 |
| `--geo` | US | 地区代码 |
| `--top` | 200 | 导出条数 |
| `--dry-run` | - | 演练模式，只打印不执行 |

## 处理流程

### 1. inbox → raw（标准命名）

- 递归扫描 `inbox/d|w|m/` 及其子目录（支持品类子目录）
- 通过表头自动识别数据类型（商品/新品/小店）
- 从路径提取品类信息（如 `inbox/d/pet_supplies/` → `_pet_supplies` 后缀）
- 按标准命名规则重命名并移动到 `raw/`

### 2. raw → clean（数据清洗）

- 定位表头行（自动跳过前几行的说明文字）
- 提取关键字段，统一列名
- 添加 rank 排名列
- 商品表和小店表分别输出为 `products_clean_v0.csv` / `shops_clean_v0.csv`

### 3. clean → candidates（候选筛选）

- 按 GMV 和销量分别排序
- 输出 Top50 候选列表
- 生成可视化视图（含关键指标）

## 输出文件

### raw/
原始 xlsx 文件，标准命名。

### clean/
| 文件 | 说明 |
|------|------|
| `products_clean_v0.csv` | 商品数据（热销榜 + 新品榜合并） |
| `shops_clean_v0.csv` | 小店数据 |

### candidates/
| 文件 | 说明 |
|------|------|
| `candidate_products_v0_by_gmv_top50.csv` | 商品 Top50（按 GMV） |
| `candidate_products_v0_by_units_top50.csv` | 商品 Top50（按销量） |
| `candidate_shops_v0_by_gmv_top50.csv` | 小店 Top50（按 GMV） |
| `candidate_shops_v0_by_units_top50.csv` | 小店 Top50（按销量） |
| `view_candidate_*.csv` | 可视化视图（精简字段） |

### manifest.txt
元数据文件，记录本次处理的参数：
```
source: echotik
captured: 2026-03-12
cap_mmdd: 0312
geo: US
top_n: 200
period_d: 0311
period_w: 0302-0308
period_m: 202602
```

## 依赖

```
pandas
openpyxl
```

## 与采集器的关系

本脚本由 `echotik_collector` 的 `pipeline_runner.py` 自动调用：

1. 采集器下载文件到 `inbox/_tmp/`
2. `file_router.py` 按粒度/品类路由到 `inbox/d|w|m/[category]/`
3. `pipeline_runner.py` 调用本脚本处理数据
4. 处理完成后通知飞书群

也可独立运行，手动处理 inbox 中的文件。

## 版本历史

### v1.1 - 2026-03-12
- 支持品类子目录递归扫描
- 标准命名支持品类后缀
- 正则匹配兼容品类字段

### v1.0 - 2026-02-28
- 初始版本
- inbox → raw → clean → candidates 全流程
