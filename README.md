# 🚀 SOHO-NEXT

```text
  ____  ___  _   _  ___          _   _ _______  _______
 / ___|/ _ \| | | |/ _ \        | \ | | ____\ \/ /_   _|
 \___ \ | | | |_| | | | | _____ |  \| |  _|  \  /  | |
  ___) | |_| |  _  | |_| |_____|| |\  | |___ /  \  | |
 |____/ \___/|_| |_|\___/       |_| \_|_____/_/\_\ |_|
```

**Niche Exploration and eXtraction Tool** > 个人专属的外贸 SOHO 智能选品与市场情报数据引擎。

---

自动登录 Echotik 平台、下载商品榜/小店榜数据、触发清洗流水线的无人值守采集系统。

- **运行环境**：WSL (Ubuntu) + Conda + Python 3.11
- **浏览器自动化**：Playwright + Chromium
- **当前版本**：v2.1

---

## 目录结构

```
echotik_collector/
├── .env.example          # 环境变量模板（复制为 .env 后填写真实值）
├── .gitignore
├── requirements.txt      # Python 依赖
├── main.py               # 主入口（命令行参数 + 异步流程调度）
├── file_router.py        # 文件路由（inbox/_tmp → inbox/d|w|m）
├── pipeline_runner.py    # 触发清洗脚本（使用 PYTHON_BIN 完整路径）
│
├── browser/
│   ├── session.py        # 登录 + Cookie 复用 + 失败截图调试
│   ├── downloader.py     # 页面导航 + 导出文件下载 + 新鲜度检测
│   └── anomaly.py        # 页面异常检测（规则 / AI 多提供商）
│
├── scheduler/
│   └── trigger.py        # 日/周/月调度 + 即时重试
│
├── utils/
│   ├── logger.py         # emoji 日志系统（终端 + 文件双输出）
│   ├── retry.py          # 异步指数退避重试装饰器
│   ├── freshness.py      # MD5 新鲜度对比（避免重复处理旧数据）
│   ├── notifier.py       # 企微 / 飞书报警通知
│   └── cleanup_screenshots.py  # 截图定期清理（周转移 + 季度删除）
│
├── config/
│   └── tasks.yaml        # 采集任务配置（模块、选择器、粒度）
│
├── tools/                # 调试与测试工具
│   ├── debug_page.py     # 页面内容调试（自动登录 + DOM 输出）
│   ├── test_navigation.py # 侧边栏导航测试
│   └── test.sh           # 交互式快速测试菜单
│
├── recordings/           # Playwright 录制脚本（选择器参考）
│   ├── recorded_daily.py
│   ├── recorded_weekly.py
│   ├── recorded_monthly.py
│   └── recorded_download.py
│
├── docs/                 # 项目文档
│   ├── echotik_flowchart.md        # Mermaid 运行流程图
│   ├── CHANGELOG.md                # 版本变更记录
│   ├── OPTIMIZATION_REPORT.md      # 优化报告
│   └── Echotik采集器_操作指南_v2.1.docx
│
└── logs/                 # 运行日志（自动创建，按天轮转）
    ├── echotik_YYYY-MM-DD.log
    ├── _trash/           # 截图垃圾桶（周日转入，季度清空）
    └── *.png             # 运行截图（导航/登录/异常等）
```

---

## 模块说明

### main.py — 主入口

程序的统一入口，负责解析命令行参数、初始化日志、决定粒度、调用下载流程。

**命令行参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| `--captured` | 指定采集日期 | `--captured 2026-03-01` |
| `--wins` | 强制指定粒度，逗号分隔 | `--wins d,w` |
| `--dry-run` | 演练模式，不实际下载 | `--dry-run` |

---

### browser/session.py — 登录模块

管理登录态，支持 Cookie 复用和账号密码兜底登录。

**工作流程：**
1. 检查 `config/cookies_*.json` 是否存在
2. 有 Cookie → 尝试无感登录 → 验证 `LOGIN_SUCCESS_SELECTOR` 是否出现
3. Cookie 失效 → 用账号密码重新登录（5步详细日志）
4. 登录失败 → 自动保存截图到 `logs/debug_login_*.png`

**调试技巧：**
- 查看 `logs/debug_login_*.png` 可以直观看到登录卡在哪一步
- 启动时日志会打印 `password_len` 和 `password_hint`，可验证 `.env` 密码读取是否正确

---

### browser/downloader.py — 下载模块

负责导航到目标页面、切换时间 Tab、触发导出下载、检测文件新鲜度。

**下载流程（单个模块单个粒度）：**
1. 侧边栏导航（Arco Design 精确选择器）
2. 调用 `anomaly.py` 检测页面状态
3. 点击时间范围 Tab（日榜跳过，周/月榜点 Tab）
4. 悬停导出下拉箭头 → 选择 200 Records（可能直接触发导出）
5. 若未触发则点击 Export 按钮 → 等待 popup
6. 优先通过 download 事件保存，兜底用 popup URL 直接 API 请求
7. MD5 新鲜度对比（相同则标记 `stale`，等待重试）

---

### browser/anomaly.py — 页面异常检测

在每次下载前检测页面状态，返回 `normal / captcha / blocked / error`。

**提供商选择（.env 中配置 `ANOMALY_PROVIDER`）：**

| 提供商 | 说明 | 需要配置 |
|--------|------|----------|
| `rule`（默认）| 纯关键词规则，无需 API | 无 |
| `qwen` | 阿里云千问视觉，推荐 | `QWEN_API_KEY` + `QWEN_BASE_URL`（可选） |
| `claude` | Anthropic Claude | `ANTHROPIC_API_KEY` + `pip install anthropic` |
| `openai` | GPT-4o / GPT-4o-mini | `OPENAI_API_KEY` + `OPENAI_BASE_URL`（可选） |
| `gemini` | Google Gemini | `GEMINI_API_KEY` |
| `kimi` | Moonshot Kimi | `KIMI_API_KEY` |

每个提供商支持 `{NAME}_BASE_URL` 自定义 API 地址（用于第三方转发服务）。
AI 调用失败时自动降级为 `rule` 模式，不影响主流程。

---

### scheduler/trigger.py — 调度模块

根据日期决定本次执行哪些粒度，并管理整体重试逻辑。

**粒度调度规则：**
- `d`（日榜）：每天执行
- `w`（周榜）：每周一执行
- `m`（月榜）：每月 1 日执行

**重试机制：**
- 第 1 次尝试失败（stale 或 failed）→ 立即重试第 2 次
- 第 2 次仍失败 → 发送企微/飞书报警通知，退出等待下次 crontab

---

### pipeline_runner.py — 清洗触发模块

下载完成后，用子进程调用 `echotik_pipeline.py` 执行数据清洗。

**关键设计：** 使用 `.env` 中 `PYTHON_BIN` 指定的完整路径（conda env 内的 python），而不是系统 `python3`，确保 pipeline 能找到 pandas、openpyxl 等依赖。

---

### file_router.py — 文件路由模块

将 `inbox/_tmp/` 中下载成功的文件移动到对应粒度子目录：
- `inbox/d/` — 日榜
- `inbox/w/` — 周榜
- `inbox/m/` — 月榜

`stale` 和 `failed` 状态的文件留在 `_tmp/` 供人工排查。

---

### utils/logger.py — 日志系统

结构化 emoji 日志，终端和文件双输出，按天自动轮转，保留 30 天。

**日志级别图标：**
`🚀 START` · `✅ INFO` · `⚠️ WARN` · `❌ ERROR` · `⏳ WAIT` · `🎉 DONE`

---

### utils/retry.py — 重试装饰器

异步函数指数退避重试。默认配置：最多 3 次，首次等待 30 秒，后续倍增（30s → 60s → 120s）。

---

### utils/freshness.py — 新鲜度检测

对比新下载文件与历史 `raw/` 目录中同类文件的 MD5。相同则标记 `stale`，等待 Echotik 平台更新数据后重试。

---

### utils/notifier.py — 报警通知

支持企业微信和飞书机器人 Webhook，触发场景：
- 最终采集失败（所有重试耗尽）
- Pipeline 执行失败
- 验证码 / 账号风控

---

### config/tasks.yaml — 任务配置

定义采集模块（商品榜、小店榜）的 Arco Design 侧边栏选择器、支持的粒度、时间 Tab 文字、导出条数。

---

### recordings/ — Playwright 录制脚本

通过 `playwright codegen` 录制的真实操作脚本，作为选择器和交互流程的参考依据。每次页面改版后重新录制，确保选择器与实际页面一致。

---

### tools/ — 调试与测试工具

| 工具 | 用途 |
|------|------|
| `debug_page.py` | 自动登录后输出页面 DOM，排查导航失败 |
| `test_navigation.py` | 测试侧边栏点击，显示浏览器窗口便于观察 |
| `test.sh` | 交互式菜单，快速选择调试/测试/采集 |

---

### utils/cleanup_screenshots.py — 截图清理

- 每周日：`logs/*.png` → `logs/_trash/`
- 每季度首日（1/1、4/1、7/1、10/1）：清空 `_trash/`
- 支持 `--move` / `--purge` 手动执行

---

## 安装与部署

### 第一步：创建 Conda 环境

```bash
conda create -n echotik_exporter python=3.11 -y
conda activate echotik_exporter
pip install -r requirements.txt
playwright install chromium
```

### 第二步：记录 Python 解释器路径

```bash
conda activate echotik_exporter
which python
# 输出示例：/home/gjb/miniconda3/envs/echotik_exporter/bin/python
# 将此路径填入 .env 的 PYTHON_BIN
```

### 第三步：配置 .env

```bash
cp .env.example .env
# 用编辑器打开 .env，填写以下必填项：
# ECHOTIK_ACCOUNTS、ECHOTIK_PASSWORDS、REPO_ROOT、PIPELINE_SCRIPT、PYTHON_BIN
```

**常见配置问题：**
> 密码中如有特殊字符（`@` `#` `$` `!` 等），请用单引号包裹整行值：
> `ECHOTIK_PASSWORDS='my$pecial@pass'`
> 不要在值两端加引号（除非密码含特殊字符）：✅ `ECHOTIK_PASSWORDS=mypassword`

### 第四步：录制浏览器选择器

```bash
# 设置代理（替换为你的代理端口）
export https_proxy=http://127.0.0.1:7890

# 启动录制工具
playwright codegen https://www.echotik.live/login
```

在弹出的浏览器中正常登录，然后将 Inspector 里生成的选择器填入 `browser/session.py`。

### 第五步：测试运行

```bash
# 演练模式（不实际下载）
python main.py --dry-run

# 真实测试（指定日期，只跑日榜）
python main.py --captured 2026-03-03 --wins d
```

### 第六步：配置定时任务

```bash
crontab -e
```

添加以下内容：

```
# 主采集任务：每天 07:30
30 7 * * * cd /home/gjb/workspace/echotik_collector && /home/gjb/miniconda3/envs/echotik_exporter/bin/python main.py >> logs/cron.log 2>&1

# 截图清理：每天 00:05（脚本内部判断周日转移、季度首日清空）
5 0 * * * cd /home/gjb/workspace/echotik_collector && /home/gjb/miniconda3/envs/echotik_exporter/bin/python -m utils.cleanup_screenshots >> logs/cleanup.log 2>&1
```

> **WSL2 注意**：crontab 仅在 WSL 运行时生效。保持一个终端窗口常开，或在 `~/.bashrc` 中加入 `service cron status > /dev/null 2>&1 || sudo service cron start > /dev/null 2>&1` 自动启动 cron。

---

## 常用命令

```bash
# 激活环境
conda activate echotik

# 演练模式
python main.py --dry-run

# 运行今天的任务
python main.py

# 指定日期 + 粒度
python main.py --captured 2026-03-01 --wins d,w

# 查看实时日志
tail -f logs/echotik_$(date +%Y-%m-%d).log

# 查看 cron 日志
tail -100 logs/cron.log

# 查看登录调试截图（登录失败时自动生成）
ls logs/debug_login_*.png
```

---

## 调试工具

### 页面内容调试

当遇到导航失败时，使用此工具查看页面实际内容：

```bash
python tools/debug_page.py
```

功能：
- 自动登录并截图
- 打印页面所有可见文本
- 查找侧边栏相关元素
- 浏览器保持打开60秒供检查

### 导航功能测试

测试侧边栏点击功能：

```bash
python tools/test_navigation.py
```

功能：
- 测试多种选择器策略
- 显示浏览器窗口便于观察
- 点击后截图验证
- 浏览器保持打开30秒

### 快速测试脚本

交互式测试菜单：

```bash
./tools/test.sh
```

提供选项：
1. 运行页面调试工具
2. 运行导航测试
3. 运行 dry-run 测试
4. 运行真实采集（仅日榜）

### 手动打开 Playwright 浏览器

用与脚本完全相同的 Chromium + 代理环境手动操作，排查导出/下载问题：

```bash
playwright open --browser chromium --proxy-server http://172.22.176.1:10808 https://www.echotik.live/login
```

---

## 故障排查

### 登录失败

1. 查看 `logs/debug_login_*.png` 截图，直观确认页面状态
2. 查看日志中的 `password_len` 和 `password_hint`，确认密码读取正确
3. 检查 `.env` 中密码是否误加了引号（如 `"mypassword"` → 实际读取会包含引号）
4. 尝试临时设置 `headless` 模式查看浏览器实际行为

### 数据未更新（stale）

Echotik 平台数据尚未刷新，脚本会立即重试一次，仍失败则发送通知，等待下次 crontab。

### ModuleNotFoundError

未激活 conda 环境，或 crontab 中 `PYTHON_BIN` 路径配置错误。

### 07:30 未自动执行

WSL 已关闭，crontab 不在运行。保持一个 WSL 终端窗口常开即可。

---

## 数据流向

```
Echotik 网站
    ↓  Playwright 浏览器自动导出
inbox/_tmp/（临时暂存）
    ↓  file_router.py 按粒度分类
inbox/d/  inbox/w/  inbox/m/
    ↓  pipeline_runner.py 触发清洗
exports/captured=YYYY-MM-DD/
    ├── raw/        标准命名原始 xlsx
    ├── clean/      products_clean_v0.csv / shops_clean_v0.csv
    └── candidates/ top50 候选列表
```
