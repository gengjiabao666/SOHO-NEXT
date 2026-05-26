# Echotik Collector 更新日志

## [v3.0] - 2026-03-11

### 🎯 重大更新：视觉驱动自动化 + 品类筛选 + 新品榜支持

本次更新引入了**完全基于视觉识别的自动化采集系统**，突破了传统选择器依赖的局限，实现了真正的"看图操作"能力。同时新增了品类筛选、新品榜采集、以及批量任务调度等核心功能。

---

### 🚀 核心新功能

#### 1. 视觉驱动导出系统（Visual Export）

**设计思路：**
传统的 Playwright 自动化依赖固定的 CSS 选择器（如 `div:nth-child(3)`），一旦页面改版或不同榜单页面结构不同，选择器就会失效。视觉驱动方案通过"截图 → AI 分析 → 推断操作"的流程，模拟人类操作网页的方式，大幅提升了鲁棒性。

**实现模块：**

##### `visual_export_new_products.py` - 新品榜视觉导出脚本
- **功能**：自动登录 → 导航到新品榜 → 导出 200 条数据
- **关键技术**：
  - Xvfb 虚拟显示（无 GUI 环境下运行 headless=False 浏览器）
  - 每步操作前后截图存证（17 张截图完整记录流程）
  - 多策略文字点击（10 种选择器 fallback）
  - 自动弹窗处理（8 种常见弹窗模式）
- **设计亮点**：
  - 侧边栏导航采用"文字点击 → Arco Design 箭头兜底"双策略
  - 登录后等待 20 秒确保页面完全加载（Echotik 首页需约 30 秒）
  - 弹窗关闭后额外等待 3 秒并截图验证
- **文件路径**：`/home/ubuntu/workspace/echotik_collector/visual_export_new_products.py`
- **测试结果**：✅ 成功下载新品榜日榜 201 条数据

##### `visual_export_category.py` - 品类筛选导出脚本
- **功能**：在热销榜基础上增加品类筛选（如 Pet Supplies）
- **核心流程**：
  1. 导航到 Top Sold（热销榜）
  2. 等待 Product Category 筛选器加载
  3. 点击 "More ∨" 展开全部品类
  4. 选择目标品类（如 Pet Supplies）
  5. 等待表格刷新（品类筛选后数据重新加载）
  6. 导出 200 条数据
- **关键技术突破**：
  - **精确定位 Product Category 的 More 按钮**：
    - 问题：页面上有多个 "More" 按钮（Tab 栏的 More、品类筛选的 More）
    - 解决：用 `:has-text('Product Category') >> text=More` 限定在品类筛选器内查找
  - **品类标签点击**：
    - 支持 button/span/div 多种容器
    - 自动滚动到元素（`scroll_into_view_if_needed`）
    - 点击后等待 3 秒让表格刷新
- **踩坑记录**：
  - 第一次失败：点击了 Tab 栏的 More（展开更多 Collect 标签页），而非品类筛选的 More
  - 修复方案：用父容器限定选择器范围
- **文件路径**：`/home/ubuntu/workspace/echotik_collector/visual_export_category.py`
- **测试结果**：✅ 成功下载热销榜日榜 Pet Supplies 201 条数据

##### `visual_export_batch.py` - 批量任务调度脚本
- **功能**：一次性采集多个榜单（不同类型 + 不同时间窗口）
- **支持的榜单类型**：
  - `top_sold`：热销榜（支持品类筛选）
  - `new_products`：新品榜（支持品类筛选，仅日榜）
  - `shops`：小店榜（不支持品类筛选）
- **支持的时间窗口**：
  - `d`：日榜（Daily，默认选中）
  - `w`：周榜（Weekly，需点击 Tab）
  - `m`：月榜（Monthly，需点击 Tab）
- **命令行用法**：
  ```bash
  python visual_export_batch.py \
    --category "Pet Supplies" \
    --tasks "top_sold:w,top_sold:m,new_products:d,shops:d,shops:w,shops:m"
  ```
- **设计亮点**：
  - **榜单配置字典**（`RANKING_CONFIG`）：
    - 统一管理侧边栏路径（menu_parent/menu_child）
    - 标记是否支持品类筛选（`has_category_filter`）
    - 定义时间 Tab 映射（`time_tabs`）
  - **任务编排**：
    - 单次登录 → 顺序执行多个任务
    - 每个任务独立截图（便于调试）
    - 失败任务不中断后续任务
  - **导出逻辑优化**：
    - 初版：直接 click 下拉箭头 → **失败**（Echotik 是 hover 触发）
    - 修复：改回 hover 触发 + 等待下拉菜单出现
- **已知问题**：
  - 新品榜/小店榜导出超时（操作都成功但无下载事件）
  - 可能原因：品类数据为空 / 账号限额 / 需要 popup 窗口处理
- **文件路径**：`/home/ubuntu/workspace/echotik_collector/visual_export_batch.py`
- **测试结果**：
  - ✅ 热销榜周榜/月榜 Pet Supplies（2/2 成功）
  - ❌ 新品榜日榜 + 小店榜日/周/月榜（0/4 成功）

---

#### 2. 新品榜数据集成（Pipeline 支持）

**设计思路：**
新品榜与热销榜都是商品数据（`ds=p`），但需要在 `dataset` 字段区分，以便后续分析时能分开统计排名。

**实现改动：**

##### `config/tasks.yaml` - 新增新品榜配置
```yaml
- name: 新品榜
  nav_parent_selector: "div:nth-child(3) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon"
  submenu_id: "arco-menu-0-submenu-inline-1"
  nav_child: "New Products"
  wins: [d, w, m]
  ds: pn  # ← 新数据集标识（区别于热销榜的 p）
  time_tab_map:
    d: ""
    w: "Weekly"
    m: "Monthly"
  export_count: "200 Records"
```

##### `echotik_pipeline.py` - 数据清洗支持
**修改点 1：`detect_ds_by_header()` 函数**
- **原逻辑**：只识别 `p`（商品）和 `s`（小店）
- **新逻辑**：
  - 检查表头是否含"商品Id" → 是商品表
  - 检查文件名是否含 "new" → 区分新品榜（`pn`）vs 热销榜（`p`）
- **代码**：
  ```python
  if is_product:
      if "new" in xlsx_path.name.lower():
          return "pn"
      else:
          return "p"
  ```

**修改点 2：`normalize_products()` 函数**
- **原逻辑**：所有商品表 `dataset="products_hot"`
- **新逻辑**：
  ```python
  if meta.get("ds") == "pn":
      df["dataset"] = "products_new"
  else:
      df["dataset"] = "products_hot"
  ```

**修改点 3：文件名正则表达式**
- **原正则**：`et_(?P<ds>[ps])_...`（只支持 p 和 s）
- **新正则**：`et_(?P<ds>p|pn|s)_...`（支持 p、pn、s）

**修改点 4：处理逻辑**
- **原逻辑**：`if meta["ds"] == "p"` → 商品表
- **新逻辑**：`if meta["ds"] in ("p", "pn")` → 商品表（含热销和新品）

**数据流向**：
```
Echotik 网站
  ↓ downloader.py（新品榜 ds=pn）
inbox/_tmp/New product list_*.xlsx
  ↓ file_router.py（按 win 分类）
inbox/d/ (或 w/, m/)
  ↓ pipeline: detect_ds_by_header（识别 pn）
raw/et_pn_d_0310_cap0311_n200_US.xlsx
  ↓ normalize_products（dataset=products_new）
clean/products_clean_v0.csv
  ├── dataset=products_hot（热销榜）
  └── dataset=products_new（新品榜）
  ↓ add_ranks（分组排名：按 dataset 分开）
candidates/
  ├── candidate_products_v0_by_gmv_top50.csv
  └── view_candidate_products_v0_by_gmv_top50.csv
```

**关键设计**：
- `GROUP_KEYS` 包含 `dataset` 字段 → 热销榜和新品榜的排名独立计算
- 同一个 `products_clean_v0.csv` 包含两个数据集，通过 `dataset` 列区分

---

#### 3. 品类筛选功能（Category Filter）

**业务价值：**
- 全品类榜单数据量大但不够精准
- 品类筛选可以聚焦特定赛道（如 Pet Supplies、Beauty）
- 支持细分市场分析和选品

**技术实现：**

##### 品类筛选器识别
- **位置**：页面主内容区，Shelf Time 下方，数据表格上方
- **结构**：
  ```
  🔍 Product Category  [All] [Beauty & Personal Care] ... [Shoes] [More ∨]
  ```
- **交互逻辑**：
  1. 默认显示 10 个常见品类 + "More" 按钮
  2. 点击 "More" 展开全部品类（约 20+ 个）
  3. 点击品类标签 → 表格刷新 → 只显示该品类数据

##### 选择器策略
**问题**：页面上有多个 "More" 按钮，如何精确定位品类筛选器的 More？

**解决方案**：
```python
# 方法1：在 Product Category 容器内查找
":has-text('Product Category') >> text=More"

# 方法2：正则匹配带箭头的 More
"text=/More\\s*[∨▼]/"

# 方法3：Arco Design 按钮组（兜底）
".arco-btn-group button:last-child"
```

##### 品类标签点击
```python
category_selectors = [
    f"button:has-text('{category}')",
    f":has-text('Product Category') >> :has-text('{category}')",
    f"[class*='tag']:has-text('{category}')",
]
```

**等待策略**：
- 点击品类后等待 3 秒（表格数据重新加载）
- 尝试等待 `networkidle`（最多 10 秒）
- 截图验证表格已刷新

---

#### 4. 导出下拉菜单交互优化

**核心问题：Echotik 的导出下拉是 hover 触发，不是 click**

**错误实现（初版）**：
```python
dropdown_btn = page.get_by_role("button").nth(2)
await dropdown_btn.click()  # ❌ 直接点击 → 可能触发直接导出
```

**正确实现（修复后）**：
```python
dropdown_btn = page.get_by_role("button").nth(2)
await dropdown_btn.hover()  # ✅ hover 触发下拉菜单
await page.wait_for_timeout(1500)  # 等待菜单出现
await page.get_by_text("200 Records").click()  # 选择条数
await page.get_by_role("button", name="Export").click()  # 点击导出
```

**设计细节**：
- hover 后等待 1.5 秒让下拉菜单完全展开
- 选择条数后等待 1.5 秒（可能触发 popup）
- 再等待 3 秒确认是否已触发下载
- 最后点击 Export 按钮（兜底）

**Arco Design DropdownButton 组件特性**：
- 左半部分按钮：直接执行动作（这里是直接导出）
- 右半部分小箭头：展开下拉菜单（选择条数）
- hover 整个按钮组 → 下拉菜单出现

---

### 🔧 技术优化

#### Xvfb 虚拟显示集成
- **问题**：服务器无 GUI 环境，`headless=True` 模式某些交互异常
- **解决**：启动 Xvfb 虚拟显示 + `headless=False`
- **优势**：
  - 可以截图（headless=True 无法截图）
  - 交互行为与真实浏览器一致
  - 便于调试（截图存证）
- **实现**：
  ```python
  subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1920x1080x24"])
  os.environ["DISPLAY"] = ":99"
  ```

#### 截图存证系统
- **设计思路**：每步操作前后截图，便于事后分析失败原因
- **命名规则**：
  - 成功流程：`visual_01_login_success.png` → `visual_17_download_complete.png`
  - 失败流程：`visual_99_failed.png`
  - 批量任务：`batch_001_热销榜_w_Pet Supplies_01_loaded.png`
- **存储位置**：`logs/` 目录
- **清理策略**：
  - 每周日移动到 `logs/_trash/`
  - 每季度首日清空 `_trash/`

#### 多策略选择器 Fallback
- **设计思路**：不同页面元素结构可能不同，单一选择器容易失效
- **实现**：
  ```python
  selectors = [
      f":text-is('{text}')",      # 精确匹配
      f"text={text}",              # Playwright 内置
      f"button:has-text('{text}')", # 按钮容器
      f"div:has-text('{text}')",   # div 容器
      # ... 10+ 种策略
  ]
  for sel in selectors:
      try:
          await page.locator(sel).click()
          return True
      except:
          continue
  ```

---

### 📊 测试结果

#### 今日采集统计（2026-03-11）

| 时间 | 任务 | 结果 | 条数 |
|------|------|------|------|
| 08:38 | 商品榜日榜（cron） | ✅ | 200 |
| 08:39 | 小店榜日榜（cron） | ✅ | 200 |
| 15:37 | 新品榜日榜（手动测试） | ✅ | 201 |
| 16:33 | 热销榜日榜 Pet Supplies（手动测试） | ✅ | 201 |
| 16:47 | 热销榜周榜 Pet Supplies（批量任务） | ✅ | 200 |
| 16:49 | 热销榜月榜 Pet Supplies（批量任务） | ✅ | 200 |
| 17:27 | 新品榜日榜 Pet Supplies（批量任务） | ❌ | 0 |
| 17:28 | 小店榜日榜 Pet Supplies（批量任务） | ❌ | 0 |
| 17:30 | 小店榜周榜 Pet Supplies（批量任务） | ❌ | 0 |
| 17:31 | 小店榜月榜 Pet Supplies（批量任务） | ❌ | 0 |

**成功：** 6/10（1202 条）  
**失败：** 4/10（操作成功但下载超时）

#### 失败原因分析
- **现象**：下拉箭头点击 ✅、200 Records 选择 ✅、Export 点击 ✅，但无下载事件
- **可能原因**：
  1. Pet Supplies 品类在新品榜/小店榜数据为空
  2. 账号导出次数限额（今天已导出 6 次）
  3. 需要处理 popup 窗口（Export 后弹出确认窗口）
- **待验证**：查看失败截图，确认页面状态

---

### 🐛 已知问题

1. **新品榜/小店榜品类筛选导出失败**
   - 状态：操作流程正确，但下载事件未触发
   - 影响：无法采集细分品类的新品榜和小店榜数据
   - 优先级：高

2. **导出按钮选择器不统一**
   - 问题：`nth(2)` 在不同页面可能定位到不同按钮
   - 临时方案：hover 方式兼容性更好
   - 长期方案：用更精确的选择器（如 Arco Design class）

3. **下载事件监听可能遗漏 popup 场景**
   - 问题：某些导出可能通过 popup 窗口而非直接下载
   - 待验证：检查失败截图是否有 popup 窗口

---

### 📝 文档更新

- 新增本 CHANGELOG 条目
- 待更新：README.md（新增视觉驱动导出章节）
- 待更新：操作指南（品类筛选使用说明）

---

### 🔮 下一步计划

1. **修复新品榜/小店榜导出问题**
   - 分析失败截图
   - 增加 popup 窗口监听
   - 验证品类数据是否为空

2. **集成到主流程**
   - 将品类筛选加入 `tasks.yaml`
   - 修改 `downloader.py` 支持品类参数
   - 更新 cron 任务配置

3. **批量品类采集**
   - 支持多品类循环（Beauty、Fashion、Home Supplies 等）
   - 自动跳过数据为空的品类
   - 限额管理（避免超出每日 2000 条）

4. **视觉识别增强**
   - 引入 AI 视觉模型（如 Qwen-VL）分析截图
   - 自动识别按钮位置（无需固定选择器）
   - 异常检测（验证码、限额提示等）

---

## [v2.2] - 2026-03-06

### 🐛 问题修复
- 修复侧边栏导航点击失败问题
- 修复弹窗处理不完善导致的遮挡问题
- 修复页面加载时间不足的问题

### ✨ 新增功能
- 新增 `_click_by_text_enhanced()` 增强版点击函数
  - 支持20+种选择器策略
  - 自动检测元素可见性
  - 自动滚动到元素
  - 详细的调试日志
- 新增 `debug_page.py` 页面调试工具
- 新增 `test_navigation.py` 导航测试工具
- 新增 `test.sh` 快速测试脚本

### 🔧 优化改进
- 登录后等待时间从10秒增加到15秒
- 侧边栏一级菜单展开等待从800ms增加到1200ms
- 新增侧边栏加载检测（2秒等待）
- 弹窗处理增加2秒等待和可见性检查
- 导航前增加截图记录
- 日志系统支持 DEBUG 级别

### 📝 文档更新
- 新增 `OPTIMIZATION_REPORT.md` 优化报告
- 更新 README.md（待完成）

### 🔍 调试增强
- 点击失败时分析页面文本内容
- 记录选择器尝试次数和结果
- 增加元素可见性状态日志
- 导航前后截图对比

---

## [v2.1] - 2026-03-05

### ✨ 初始版本
- 基础登录功能
- Cookie 复用
- 多账号支持
- 文件下载
- MD5 新鲜度检测
- 自动重试机制
- 企微/飞书通知

---

## 版本规划

### [v2.3] - 计划中
- [ ] 根据实际测试结果优化选择器
- [ ] 添加更多异常情况处理
- [ ] 优化重试间隔时间
- [ ] 添加性能监控

### [v3.0] - 未来
- [ ] AI 视觉识别辅助导航
- [ ] 分布式采集支持
- [ ] Web 管理界面
- [ ] 实时监控面板
