## [v2.5] - 2026-03-12

### ✨ 新增功能
- **品类筛选集成到主流程**
  - `config/tasks.yaml` 新增 `categories` 配置（全品类 + Pet Supplies）
  - `download_all_v2()` 支持按品类筛选导出
  - `_select_category()` 自动点击品类筛选器
  - 文件路由支持品类子目录（如 `inbox/d/pet_supplies/`）
- **调度拆分（避免超限额）**
  - 周榜拆分：周一采集商品榜周榜，周二采集小店榜周榜
  - 月榜拆分：1号采集商品榜月榜，2号采集小店榜月榜
  - 最坏情况（1号是周一）：10任务 × 200条 = 2000条，刚好不超限
- **导出配额追踪**
  - `utils/quota.py` 记录每日导出条数
  - 超过1600条自动发送飞书告警
  - 数据存储在 `data/quota/YYYY-MM-DD.json`
- **账号生命周期追踪**
  - `utils/account_tracker.py` 检测账号变更、记录使用天数
  - 第4天自动发送到期预警（Echotik Pro 账号有效期4天）
  - 数据存储在 `data/account_tracker.json`

### 🔧 优化改进
- `trigger.py` 新增 `get_detailed_tasks_for_today()` 返回详细任务列表
- `trigger.py` 新增 `run_with_retry_v2()` 支持品类筛选任务
- `main.py` 自动选择 v2 接口（无 `--wins` 参数时）
- `file_router.py` 品类文件自动归档到子目录
- `echotik_pipeline.py` 支持递归扫描品类子目录
- `echotik_pipeline.py` 标准命名支持品类后缀（如 `et_p_d_0311_cap0312_n200_US_pet_supplies.xlsx`）

### 📝 配置变更
- `tasks.yaml` 新增 `categories` 和 `schedule` 配置块
- `tasks.yaml` 模块新增 `has_category_filter` 字段

---

# Echotik Collector 更新日志

## [v2.4] - 2026-03-11

### 🐛 问题修复
- **P0: 修复 `sys.exit(1)` 阻断多账号轮换**
  - `_check_subscription_expired()` 改为抛出 `SubscriptionExpiredError` 异常
  - `trigger.py` 捕获异常后自动切换下一个账号
  - 新增 `notify_subscription_expired()` 公开通知函数
- **P0: 修复 `.nth(2)` 脆弱选择器**
  - 下拉箭头定位改为多策略（Arco Design 精确选择器 → fallback nth(2)）
- **P1: 修复事件监听器累积**
  - 用命名函数替代 lambda
  - try/finally 块确保 `page.remove_listener()` 清理
- **P1: 修复 `_notify` 私有函数导入**
  - 改用 `notify_subscription_expired()` 公开函数

### 🔧 优化改进
- `@async_retry` 装饰器新增 `no_retry_on` 参数，指定不重试的异常类型
- `_download_one` 重试策略优化：`max_attempts=2, base_delay=10.0`
- 订阅到期异常不再触发无意义重试

### ✨ 新增功能
- **批量品类采集脚本** `visual_export_batch.py`（待测试）
  - 支持指定品类（如 Pet Supplies）批量导出多个榜单
  - 多账号轮换支持
  - 任务组合验证（拒绝无效的 ranking:win 组合）
  - 订阅到期检测
  - 改用 headless=True（移除 Xvfb 依赖）

---

## [v2.3] - 2026-03-10

### ✨ 新增功能
- **多账号轮换机制**
  - 账号A 失败 → 自动切换账号B → 继续重试
  - `session.set_single_account()` 支持 trigger 指定单账号登录
- **飞书文件推送**
  - Pipeline 完成后自动推送 raw/clean/candidates 文件到飞书群
  - `send_files_to_feishu()` 函数
- **账号使用天数统计**
  - 成功通知中显示账号已使用天数
  - `.env` 新增 `ECHOTIK_ACCOUNTS_SINCE` 配置

### 🔧 优化改进
- `trigger.py` 重构：外层账号循环 × 内层重试循环
- `notifier.py` 新增飞书应用 API 文件上传功能

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

### [v2.5] - 计划中
- [ ] `visual_export_batch.py` 测试通过后融合进主采集流程
- [ ] 品类筛选功能集成到 `config/tasks.yaml`
- [ ] 支持命令行指定品类参数

### [v3.0] - 未来
- [ ] AI 视觉识别辅助导航
- [ ] 分布式采集支持
- [ ] Web 管理界面
- [ ] 实时监控面板
