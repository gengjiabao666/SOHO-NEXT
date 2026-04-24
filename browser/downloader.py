#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser/downloader.py
页面导航与文件下载模块 —— 整个采集系统的核心文件

本模块负责在 Echotik 网站上模拟真实用户的完整操作流程，
通过 Playwright 自动化浏览器完成数据导出和文件下载。

操作流程（完整模拟真实用户点击顺序）：

商品榜日榜：
    侧边栏「选品」→「热销榜」→ 等页面加载 →
    展开导出下拉 → 选「200条」→ 点「导出」

商品榜周/月榜：
    侧边栏「选品」→「热销榜」→ 等页面加载 →
    点「周榜」/「月榜」Tab → 等数据刷新 →
    展开导出下拉 → 选「200条」→ 点「导出」

小店榜：
    侧边栏「小店」→「最佳跨境卖家」→ 等页面加载 →
    （周/月榜需点Tab）→ 展开导出下拉 → 选「200条」→ 点「导出」
"""

# ============================================================
# 标准库导入
# ============================================================
import os
import sys
from dataclasses import dataclass       # 用于定义数据类（DownloadResult）
from datetime import datetime            # 用于生成截图时间戳
from pathlib import Path                 # 跨平台路径处理
from typing import List, Optional        # 类型注解

# ============================================================
# 第三方库导入
# ============================================================
import yaml                              # 解析 tasks.yaml 配置文件
from dotenv import load_dotenv           # 从 .env 文件加载环境变量
from playwright.async_api import Page    # Playwright 异步页面对象

# ============================================================
# 项目内部模块导入
# ============================================================
from browser.anomaly import check_page_anomaly    # 页面异常检测（验证码、封禁等）
from browser.session import BrowserSession         # 浏览器会话管理
from utils.freshness import is_fresh               # 数据新鲜度检测（MD5去重）
from utils.logger import log_node                  # 结构化日志记录
from utils.notifier import _notify                 # 通知推送（企业微信等）
from utils.quota import record_export, check_quota_warning  # 导出配额管理
from utils.retry import async_retry                # 异步重试装饰器
from utils.account_tracker import mark_account_expired      # 账号过期标记
from utils.events import write_event, STAGE_SIDEBAR_PARENT, STAGE_SIDEBAR_CHILD, STAGE_DATA_WAIT, STAGE_ANOMALY_CHECK, STAGE_CATEGORY_SELECT, STAGE_TAB_CLICK, STAGE_DROPDOWN_HOVER, STAGE_COUNT_SELECT, STAGE_EXPORT_TRIGGER, STAGE_DOWNLOAD_CAPTURE, STAGE_FRESHNESS_CHECK


class SubscriptionExpiredError(Exception):
    """
    账号订阅到期异常

    当检测到 Echotik 账号订阅过期弹窗时抛出此异常，
    上层调用者可以捕获此异常来切换账号或终止任务。
    """
    def __init__(self, account: str):
        self.account = account  # 记录过期的账号标识
        super().__init__(f"账号 {account} 订阅已到期")

# 从 .env 文件加载环境变量（如 REPO_ROOT、TASKS_YAML 等）
load_dotenv()

# ============================================================
# 全局路径常量配置
# ============================================================
# 仓库根目录，默认为 /mnt/g/SOHO_repo（Windows 挂载盘路径）
REPO_ROOT    = os.getenv("REPO_ROOT", "/mnt/g/SOHO_repo")
# 导出文件的最终存储目录（按模块/日期归档）
BASE_EXPORTS = Path(REPO_ROOT) / "03_data_sources" / "echotik" / "exports"
# 下载文件的临时存放目录（下载后先存这里，再由归档模块移走）
TMP_DIR      = Path(REPO_ROOT) / "03_data_sources" / "echotik" / "inbox" / "_tmp"
# 任务配置文件路径（定义了所有模块的导航选择器和导出参数）
TASKS_YAML   = os.getenv("TASKS_YAML", "config/tasks.yaml")
# 截图和日志文件存放目录
LOG_DIR      = Path("logs")


@dataclass
class DownloadResult:
    """
    单次下载任务的结果数据类

    每次下载操作完成后返回此对象，包含下载状态和相关信息，
    供上层调用者判断是否需要重试、归档或跳过。
    """
    module:   str                        # 模块名称（如 "product_daily"、"shop_weekly"）
    win:      str                        # 时间窗口（如 "daily"、"weekly"、"monthly"）
    status:   str                        # 下载状态："success"=成功 / "stale"=数据未更新 / "failed"=失败
    tmp_path: Optional[Path] = None      # 下载文件的临时路径（失败时为 None）
    reason:   str = ""                   # 失败原因描述（成功时为空）
    category: str = ""                   # 品类筛选条件（空字符串表示全品类）


def _load_tasks() -> list:
    """
    从 YAML 配置文件加载任务模块列表

    读取 config/tasks.yaml 中的 modules 字段，
    每个模块定义了导航选择器、时间Tab映射、导出条数等参数。

    Returns:
        list: 模块配置字典列表
    """
    with open(TASKS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)["modules"]


async def _screenshot(page: Page, label: str):
    """
    截图归档到 logs/ 目录

    在关键操作节点自动截图，用于事后排查问题。
    文件名格式：page_{label}_{HHMMSS}.png

    Args:
        page:  Playwright 页面对象
        label: 截图标签（如 "product_daily_loaded"），用于区分不同截图
    """
    try:
        # 确保日志目录存在
        LOG_DIR.mkdir(exist_ok=True)
        # 生成时间戳，精确到秒，避免文件名冲突
        ts   = datetime.now().strftime("%H%M%S")
        path = LOG_DIR / f"page_{label}_{ts}.png"
        # 截取当前视口（非全页面），减少截图体积
        await page.screenshot(path=str(path), full_page=False)
        log_node("截图已归档", level="INFO", path=str(path))
    except Exception as e:
        # 截图失败不影响主流程，仅记录警告
        log_node("截图失败", level="WARN", error=str(e)[:60])


async def _dismiss_runtime_popup(page: Page, module_name: str = "", stage: str = "", account: str = "") -> bool:
    """
    运行期弹窗/遮罩清理。

    登录后的 dashboard、榜单页、导出页都可能弹出新的引导层、公告层、
    会员提示层或全屏遮罩。它们会遮挡侧边栏点击，因此在关键点击前需要
    主动清理一次。
    """
    popup_selectors = [
        "button:has-text('Start Now')",
        "button:has-text('start now')",
        "button:has-text('立即开始')",
        "button:has-text('Got it')",
        "button:has-text('Close')",
        "button:has-text('close')",
        "button:has-text('Continue')",
        "button:has-text('继续')",
        "button:has-text('知道了')",
        "button:has-text('确定')",
        "button:has-text('我知道了')",
        "[aria-label='Close']",
        "[class*='close']",
        "[class*='Close']",
        "[class*='modal'] button:last-child",
        "[class*='dialog'] button:last-child",
        "[class*='popup'] button",
    ]

    popup_found = False
    for sel in popup_selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() <= 0:
                continue
            if await loc.first.is_visible():
                await loc.first.click(timeout=3_000)
                popup_found = True
                log_node("运行期弹窗已关闭", level="INFO",
                         module=module_name, stage=stage, selector=sel)
                await page.wait_for_timeout(1200)
                await _check_subscription_expired(page, account)
                break
        except Exception as e:
            log_node("运行期弹窗关闭失败", level="DEBUG",
                     module=module_name, stage=stage,
                     selector=sel[:50], error=str(e)[:60])
            continue

    return popup_found


async def _check_subscription_expired(page: Page, account: str = ""):
    """
    检测 Echotik 账号订阅到期弹窗

    弹窗特征：包含 "Current free version" 或 "Upgrade for more privileges" 文本。
    只要账号过期，页面上任何操作都会弹出此提示。

    检测流程：
        1. 读取页面 body 文本（2秒超时，避免阻塞）
        2. 检查是否包含过期关键词
        3. 如果检测到过期：截图存证 → 标记账号过期 → 抛出异常

    Args:
        page:    Playwright 页面对象
        account: 当前使用的账号标识，用于标记过期

    Raises:
        SubscriptionExpiredError: 检测到账号过期时抛出
    """
    try:
        # 尝试读取页面文本，设置短超时避免长时间阻塞
        body_text = await page.inner_text("body", timeout=2_000)
    except Exception:
        # 读取失败（页面未加载完等情况），直接返回不影响主流程
        return

    # 检查页面文本中是否包含订阅过期的关键词
    if "Current free version" in body_text or "Upgrade for more privileges" in body_text:
        # 截图留证，方便事后确认
        await _screenshot(page, "subscription_expired")
        log_node("Echotik账号到期，请检查账号池", level="ERROR")

        # 标记账号过期并从号池移除，防止后续任务继续使用该账号
        if account:
            mark_account_expired(account)

        # 抛出异常让上层处理（切换账号或终止任务）
        raise SubscriptionExpiredError(account)


async def _debug_screenshot_sequence(page: Page, label: str,
                                      interval: int = 2, count: int = 6):
    """
    调试截图序列：每 interval 秒截一张图，共 count 张

    用于观察操作后页面的变化过程（如点击导出后页面状态变化），
    帮助开发者确认页面稳定时间，调试完成后可缩减截图频率。

    Args:
        page:     Playwright 页面对象
        label:    截图标签前缀
        interval: 截图间隔（秒），默认2秒
        count:    截图总数，默认6张（即观察12秒）
    """
    for i in range(1, count + 1):
        # 等待指定间隔时间
        await page.wait_for_timeout(interval * 1000)
        # 截图并在标签中标注经过的秒数（如 label_2s, label_4s, label_6s）
        await _screenshot(page, f"{label}_{i * interval}s")


async def _click_by_text(page: Page, text: str, timeout: int = 8_000,
                         desc: str = "", account: str = "") -> bool:
    """
    点击页面上包含指定文字的元素（基础版）

    使用多种 Playwright 选择器依次尝试定位并点击目标元素。
    选择器优先级：精确文本 → 按钮 → 链接 → span → li

    Args:
        page:    Playwright 页面对象
        text:    要匹配的元素文本内容
        timeout: 等待元素可见的超时时间（毫秒）
        desc:    操作描述（用于日志，为空时使用 text）
        account: 当前账号标识（用于订阅过期检测）

    Returns:
        bool: True=点击成功，False=所有选择器都未匹配到可点击元素
    """
    # 按优先级排列的选择器列表：从精确匹配到模糊匹配
    for sel in [
        f"text={text}",                      # Playwright 内置文本选择器
        f":text-is('{text}')",                # 精确文本匹配（伪选择器）
        f"button:has-text('{text}')",         # 按钮元素中包含文本
        f"a:has-text('{text}')",              # 链接元素中包含文本
        f"span:has-text('{text}')",           # span 元素中包含文本
        f"li:has-text('{text}')",             # 列表项中包含文本
    ]:
        try:
            # 取第一个匹配的元素
            loc = page.locator(sel).first
            # 等待元素可见
            await loc.wait_for(state="visible", timeout=timeout)
            # 执行点击
            await loc.click(timeout=timeout)
            log_node(f"点击成功: {desc or text}", level="INFO",
                     text=text, selector=sel)
            # 点击后等待1秒，让页面响应
            await page.wait_for_timeout(1_000)
            # 每次点击后检查是否触发了订阅过期弹窗
            await _check_subscription_expired(page, account)
            return True
        except Exception:
            # 当前选择器失败，继续尝试下一个
            continue
    # 所有选择器都失败，记录警告
    log_node(f"点击失败: {desc or text}", level="WARN", text=text)
    return False


async def _click_by_text_enhanced(page: Page, text: str, timeout: int = 10_000,
                                   desc: str = "", account: str = "") -> bool:
    """
    增强版文字点击，使用更多策略和调试信息

    相比基础版 _click_by_text，增加了以下能力：
    - 更多选择器策略（精确匹配 → 容器匹配 → 侧边栏特定匹配）
    - 自动滚动到元素可见区域
    - 元素可见性预检查
    - 失败时输出详细调试信息（页面文本是否包含目标文字）

    Args:
        page:    Playwright 页面对象
        text:    要匹配的元素文本内容
        timeout: 等待元素可见的超时时间（毫秒）
        desc:    操作描述（用于日志）
        account: 当前账号标识

    Returns:
        bool: True=点击成功，False=所有策略都失败
    """
    log_node(f"尝试点击: {text}", level="INFO", desc=desc)

    # 策略1: 精确匹配 —— 最可靠，优先尝试
    selectors = [
        f":text-is('{text}')",    # CSS 伪选择器精确匹配
        f"text={text}",           # Playwright 文本选择器
    ]

    # 策略2: 包含匹配 + 常见 HTML 容器元素
    # 遍历常见的容器标签，生成 has-text 选择器
    for container in ["div", "span", "a", "button", "li", "p"]:
        selectors.append(f"{container}:has-text('{text}')")

    # 策略3: 侧边栏特定选择器 —— 针对 Echotik 网站的侧边栏导航结构
    # 在 sidebar/menu/nav/aside 容器内查找目标文本
    for sidebar_prefix in ["[class*='sidebar']", "[class*='menu']", "nav", "aside"]:
        selectors.append(f"{sidebar_prefix} :text-is('{text}')")
        selectors.append(f"{sidebar_prefix} :has-text('{text}')")

    # 依次尝试所有选择器
    for i, sel in enumerate(selectors):
        try:
            loc = page.locator(sel).first
            # 先检查元素是否存在（count > 0）
            count = await loc.count()
            if count == 0:
                continue  # 元素不存在，跳过

            # 检查元素是否可见（可能存在但被隐藏）
            is_visible = await loc.is_visible()
            if not is_visible:
                log_node(f"元素存在但不可见", level="WARN",
                        selector=sel, attempt=f"{i+1}/{len(selectors)}")
                continue

            # 尝试滚动到元素可见区域（元素可能在视口外）
            try:
                await loc.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                pass  # 滚动失败不影响后续点击尝试

            # 等待元素可点击状态
            await loc.wait_for(state="visible", timeout=timeout)

            # 执行点击（force=False 表示不强制点击，遵循元素的可交互状态）
            await loc.click(timeout=timeout, force=False)
            log_node(f"点击成功: {desc or text}", level="INFO",
                     text=text, selector=sel, attempt=f"{i+1}/{len(selectors)}")
            # 点击后等待页面响应
            await page.wait_for_timeout(1_000)
            # 检查是否触发了订阅过期弹窗
            await _check_subscription_expired(page, account)
            return True

        except Exception as e:
            # 只记录前几次尝试的详细错误，避免日志过多
            if i < 5:
                log_node(f"尝试 {i+1} 失败", level="DEBUG",
                        selector=sel[:60], error=str(e)[:80])
            continue

    # ── 所有策略都失败，输出调试信息帮助排查 ──
    log_node(f"点击失败（尝试了{len(selectors)}种选择器）: {desc or text}",
             level="WARN", text=text)

    # 尝试获取页面上所有可见文本，判断目标文字是否存在于页面中
    try:
        body_text = await page.inner_text("body", timeout=3_000)
        if text in body_text:
            # 文字存在但无法点击 —— 可能被其他元素遮挡或在 iframe 中
            log_node(f"文字存在于页面中，但无法定位元素", level="WARN",
                    text=text, hint="可能被遮挡或在iframe中")
        else:
            # 文字不存在 —— 页面可能未完全加载或文字内容已变更
            log_node(f"文字不存在于页面中", level="WARN",
                    text=text, hint="页面可能未完全加载或文字已变更")
    except Exception:
        pass

    return False


# ============================================================
# 错误弹窗关键词列表
# 当导出操作触发的 popup 页面包含以下关键词时，判定为错误/限额提示
# ============================================================
_ERROR_POPUP_KEYWORDS = [
    "something went wrong",              # 通用错误提示
    "feature is limited",                # 功能受限提示
    "Current free version",              # 免费版限制提示
    "Upgrade for more privileges",       # 升级提示（账号过期）
]


def _is_error_popup(text: str) -> bool:
    """
    检测 popup 页面文本是否为错误/限额提示

    遍历预定义的错误关键词列表，不区分大小写地匹配。
    用于在导出操作后判断弹出的页面是否为错误页而非下载页。

    Args:
        text: popup 页面的 body 文本内容

    Returns:
        bool: True=检测到错误关键词，False=正常页面
    """
    if not text:
        return False
    # 逐个关键词匹配，不区分大小写
    for kw in _ERROR_POPUP_KEYWORDS:
        if kw.lower() in text.lower():
            log_node("检测到导出限额/错误提示", level="ERROR", keyword=kw,
                     text=text[:120])
            return True
    return False


async def _wait_for_data(page: Page, module_name: str, win: str):
    """
    等待页面数据加载完成，截图归档

    通过检测表格行或列表项元素是否出现来判断数据是否加载完成。
    依次尝试多种数据容器选择器，任一匹配即视为加载完成。
    如果所有选择器都未匹配，则等待5秒后继续（兜底策略）。

    Args:
        page:        Playwright 页面对象
        module_name: 模块名称（用于日志）
        win:         时间窗口（用于截图标签）
    """
    # 等待表格行出现作为加载完成的信号，最多等60秒
    log_node("等待页面数据加载...", level="INFO", module=module_name)
    # 依次尝试多种数据容器选择器（不同页面的 DOM 结构可能不同）
    for data_sel in ["table tbody tr", "[class*='rank-item']",
                     "[class*='list-item']", "[class*='table-row']"]:
        try:
            await page.locator(data_sel).first.wait_for(
                state="visible", timeout=60_000)
            log_node("页面数据已加载", level="INFO",
                     module=module_name, signal=data_sel)
            break  # 任一选择器匹配成功即退出循环
        except Exception:
            continue  # 当前选择器超时，尝试下一个
    else:
        # for-else: 所有选择器都未匹配到数据元素
        log_node("未检测到数据加载信号，等待5秒后继续",
                 level="WARN", module=module_name)
        await page.wait_for_timeout(5_000)

    # 数据加载后截图，记录页面状态
    await _screenshot(page, f"{module_name}_{win}_loaded")


async def _select_category(page: Page, category: str, module_name: str, win: str, account: str = "") -> bool:
    """
    选择商品品类筛选器

    在数据页面上选择指定的商品品类，并等待表格数据刷新。
    通过轮询检测表格第一行产品名是否变化来判断数据是否已刷新。

    流程：
        1. 记录当前表格第一行产品名（作为数据变化的基准）
        2. 找到 Product Category 筛选器
        3. 点击 More 展开全部品类选项
        4. 点击目标品类
        5. 轮询检测表格第一行是否变化（超时60秒）

    Args:
        page:        Playwright 页面对象
        category:    目标品类名称（如 "Electronics"、"Beauty"）
        module_name: 模块名称（用于日志）
        win:         时间窗口（用于日志）
        account:     当前账号标识

    Returns:
        bool: True=品类选择成功且数据已刷新，False=超时失败
    """
    import time as _time

    # ── 第1步：记录当前表格第一行产品名，作为数据变化检测的基准 ──
    old_first_product = ""
    try:
        first_row = page.locator("table tbody tr").first
        # 尝试获取产品名（通常在第2列，即 td 的第1个索引）
        old_first_product = await first_row.locator("td").nth(1).inner_text(timeout=5_000)
        old_first_product = old_first_product.strip()[:50]  # 取前50字符，避免过长
        log_node(f"记录当前首行产品: {old_first_product[:30]}...", level="INFO", module=module_name)
    except Exception:
        # 获取失败（可能表格为空或结构不同），后续将跳过变化检测
        log_node("无法获取当前首行产品，将跳过变化检测", level="WARN", module=module_name)

    # ── 第2步：等待 Product Category 筛选器出现 ──
    try:
        await page.wait_for_selector("text=Product Category", state="visible", timeout=10_000)
    except Exception:
        log_node("未找到 Product Category 筛选器", level="WARN",
                 module=module_name, category=category)
        return False

    # 等待筛选器完全渲染
    await page.wait_for_timeout(2_000)

    # ── 第3步：点击 More 按钮展开全部品类选项 ──
    more_clicked = False
    # 多种选择器尝试定位 More 按钮（不同页面结构可能不同）
    more_selectors = [
        ":has-text('Product Category') >> text=More",  # 在品类筛选器区域内找 More
        "text=/More\\s*[∨▼]/",                         # 带展开箭头的 More 文本
        "button:has-text('More')",                      # More 按钮
    ]
    for sel in more_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2_000):
                await loc.click(timeout=5_000)
                log_node(f"More 按钮已点击", level="INFO", module=module_name, selector=sel)
                more_clicked = True
                await page.wait_for_timeout(2_000)  # 等待品类列表展开动画
                break
        except Exception as e:
            log_node(f"More 选择器尝试失败: {sel}", level="DEBUG", module=module_name, error=str(e)[:50])
            continue

    if not more_clicked:
        # More 按钮未找到，可能品类列表已经全部展示，继续尝试直接选择
        log_node("More 按钮未找到，尝试直接选择品类", level="WARN", module=module_name)

    # 检查是否触发了订阅过期弹窗
    await _check_subscription_expired(page, account)

    # ── 第4步：点击目标品类 ──
    category_clicked = False
    # 多种选择器尝试定位品类元素
    category_selectors = [
        f"button:has-text('{category}')",   # 品类按钮
        f"text='{category}'",               # 精确文本匹配
        f"span:has-text('{category}')",     # span 容器
        f"div:has-text('{category}')",      # div 容器
    ]
    for sel in category_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=2_000):
                # 滚动到元素可见区域（品类列表可能很长）
                await loc.scroll_into_view_if_needed(timeout=3_000)
                await loc.click(timeout=5_000)
                log_node(f"品类已点击: {category}", level="INFO", module=module_name, selector=sel)
                category_clicked = True
                break
        except Exception as e:
            log_node(f"品类选择器尝试失败: {sel}", level="DEBUG", module=module_name, error=str(e)[:50])
            continue

    if not category_clicked:
        # 所有选择器都未匹配，截图帮助调试
        await _screenshot(page, f"{module_name}_category_select_failed")
        log_node(f"品类选择失败，所有选择器都未匹配: {category}", level="WARN", module=module_name)
        return False

    # ── 第5步：轮询检测表格数据变化（超时60秒） ──
    if not old_first_product:
        # 无法检测变化（第1步获取基准失败），使用固定等待兜底
        await page.wait_for_timeout(5_000)
        log_node(f"品类数据等待完成（无变化检测）: {category}", level="INFO", module=module_name)
        return True

    start_time = _time.time()
    timeout_seconds = 60       # 最长等待60秒
    check_interval = 2         # 每2秒检测一次

    log_node(f"开始轮询检测表格数据变化，超时{timeout_seconds}秒", level="INFO", module=module_name)

    while _time.time() - start_time < timeout_seconds:
        # 每隔 check_interval 秒检测一次
        await page.wait_for_timeout(check_interval * 1000)
        try:
            first_row = page.locator("table tbody tr").first
            new_first_product = await first_row.locator("td").nth(1).inner_text(timeout=5_000)
            new_first_product = new_first_product.strip()[:50]

            # 比较新旧首行产品名，不同则说明数据已刷新
            if new_first_product != old_first_product:
                elapsed = _time.time() - start_time
                log_node(f"检测到表格数据变化，耗时{elapsed:.1f}秒", level="INFO", module=module_name)
                log_node(f"新首行产品: {new_first_product[:30]}...", level="INFO", module=module_name)
                # 额外等待2秒确保数据完全稳定（避免数据还在加载中）
                await page.wait_for_timeout(2_000)
                log_node(f"品类数据已刷新: {category}", level="INFO", module=module_name)
                return True
        except Exception as e:
            log_node(f"检测表格时出错: {e}", level="WARN", module=module_name)

    # 超时：60秒内数据未变化，视为品类筛选失败
    log_node(f"品类数据刷新超时（{timeout_seconds}秒），跳过本项下载: {category}", level="ERROR", module=module_name)
    return False


async def download_all(
    wins: list[str],
    captured: str,
    session: BrowserSession,
    page: Page,
    module_filter: str = "",
    account: str = "",
) -> List[DownloadResult]:
    """
    旧版下载接口：按时间窗口列表下载全品类数据

    遍历 tasks.yaml 中定义的所有模块，对每个模块的每个时间窗口执行下载。
    不支持品类筛选，所有下载都是全品类。

    Args:
        wins:          时间窗口列表（如 ["daily", "weekly"]）
        captured:      采集日期字符串（如 "2026-03-19"）
        session:       浏览器会话对象
        page:          Playwright 页面对象
        module_filter: 模块名过滤器（为空则下载所有模块）
        account:       当前使用的账号标识

    Returns:
        List[DownloadResult]: 所有下载任务的结果列表
    """
    # 从配置文件加载所有模块定义
    modules = _load_tasks()
    results = []
    # 确保临时目录存在（parents=True 会递归创建父目录）
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    for module in modules:
        # 如果指定了模块过滤器，跳过不匹配的模块
        if module_filter and module["name"] != module_filter:
            continue
        for win in wins:
            # 跳过模块不支持的时间窗口
            if win not in module.get("wins", []):
                continue
            # 执行单个下载任务（全品类，category 为空）
            result = await _download_one(page, module, win, captured, category="", account=account)
            results.append(result)

    return results


async def download_all_v2(
    tasks: list[dict],
    captured: str,
    session: BrowserSession,
    page: Page,
    account: str = "",
) -> List[DownloadResult]:
    """
    新版下载接口：按详细任务列表下载（支持品类筛选）

    相比旧版 download_all，支持更精细的任务控制：
    - 每个任务可以指定具体的品类筛选条件
    - 任务列表由调度器根据配额和优先级动态生成

    Args:
        tasks:    任务列表，每个任务为字典，包含：
                  - module: 模块名称（如 "product_daily"）
                  - win:    时间窗口（如 "daily"）
                  - category: 品类名称（可选，为空表示全品类）
        captured: 采集日期字符串
        session:  浏览器会话对象
        page:     Playwright 页面对象
        account:  当前使用的账号标识

    Returns:
        List[DownloadResult]: 所有下载任务的结果列表
    """
    # 将模块列表转为字典，方便按名称快速查找模块配置
    modules_config = {m["name"]: m for m in _load_tasks()}
    results = []
    # 确保临时目录存在
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    for task in tasks:
        module_name = task["module"]
        win = task["win"]
        category = task.get("category", "")  # 品类为可选字段，默认全品类

        # 校验模块名是否存在于配置中
        if module_name not in modules_config:
            log_node(f"未知模块: {module_name}", level="WARN")
            continue

        module = modules_config[module_name]
        # 校验模块是否支持该时间窗口
        if win not in module.get("wins", []):
            log_node(f"模块 {module_name} 不支持 {win}", level="WARN")
            continue

        # 执行单个下载任务
        result = await _download_one(page, module, win, captured, category=category, account=account)
        results.append(result)

    return results


@async_retry(max_attempts=3, base_delay=30.0)
async def _download_one(
    page: Page,
    module: dict,
    win: str,
    captured: str,
    category: str = "",
    account: str = "",
) -> DownloadResult:
    """
    单个模块的完整下载流程（核心函数）

    这是整个下载系统最核心的函数，完整模拟用户在 Echotik 网站上的操作：
    侧边栏导航 → 等待数据加载 → 异常检测 → 品类筛选 → 时间Tab切换 →
    导出操作 → 文件下载 → 新鲜度检测

    装饰器 @async_retry 提供自动重试能力：最多3次，间隔30秒。

    Args:
        page:     Playwright 页面对象（已登录状态）
        module:   模块配置字典（来自 tasks.yaml），包含导航选择器等参数
        win:      时间窗口（如 "daily"、"weekly"、"monthly"）
        captured: 采集日期字符串（如 "2026-03-19"）
        category: 品类筛选条件（为空表示全品类）
        account:  当前使用的账号标识

    Returns:
        DownloadResult: 下载结果（包含状态、文件路径、失败原因等）
    """
    # ── 从模块配置中提取各项参数 ──
    module_name         = module["name"]                       # 模块名称（如 "product"）
    nav_parent_selector = module["nav_parent_selector"]        # 一级菜单展开箭头的 CSS 选择器
    submenu_id          = module["submenu_id"]                 # 子菜单容器的 HTML ID
    nav_child           = module["nav_child"]                  # 二级菜单链接文字（如 "热销榜"）
    time_tab_map        = module.get("time_tab_map", {})       # 时间窗口到Tab文字的映射
    time_tab            = time_tab_map.get(win, "")            # 当前窗口对应的Tab文字（日榜为空，默认选中）
    export_count        = module.get("export_count", "200 Records")  # 导出条数选项文本
    ds                  = module.get("ds", "p")                # 数据源标识（用于新鲜度检测）
    has_category_filter = module.get("has_category_filter", False)   # 是否支持品类筛选

    # 构建任务标签（用于日志和截图文件命名）
    task_label = f"{module_name}_{win}"
    if category:
        task_label += f"_{category}"  # 有品类时追加品类名

    log_node("开始下载", level="INFO",
             module=module_name, win=win, category=category or "全品类",
             export_count=export_count)

    # ══════════════════════════════════════════
    # 步骤1：侧边栏导航（基于 Playwright 录制的精确选择器）
    # 模拟用户点击侧边栏菜单，导航到目标数据页面
    # 流程：关闭弹窗 → 展开一级菜单 → 点击二级菜单
    # ══════════════════════════════════════════
    log_node(f"侧边栏导航: {nav_child}",
             level="INFO", module=module_name)

    # 截图记录导航前的页面状态（用于对比和调试）
    await _screenshot(page, f"{module_name}_{win}_before_nav")

    # 点击前先清理运行期弹窗/遮罩，避免遮挡侧边栏
    await _dismiss_runtime_popup(page, module_name=module_name,
                                 stage="before_sidebar_nav", account=account)

    # ── 点击一级菜单展开箭头（使用录制的精确选择器） ──
    # 先检查子菜单是否已展开（重试场景下可能已展开，再点会收起）
    submenu_visible = False
    try:
        submenu_visible = await page.locator(f"#{submenu_id}").is_visible()
    except Exception:
        pass

    if submenu_visible:
        # 子菜单已展开，跳过一级菜单点击（避免重复点击导致收起）
        log_node("子菜单已展开，跳过一级菜单点击", level="INFO",
                 module=module_name, submenu_id=submenu_id)
    else:
        # 子菜单未展开，需要点击一级菜单箭头来展开
        log_node("点击一级菜单展开箭头", level="INFO", module=module_name,
                 selector=nav_parent_selector)
        try:
            loc = page.locator(nav_parent_selector)
            # 先滚动到元素可见区域（侧边栏可能很长）
            await loc.scroll_into_view_if_needed(timeout=5_000)
            await loc.click(timeout=8_000)
            log_node("一级菜单已展开", level="INFO", module=module_name)
            await page.wait_for_timeout(1_000)
            await _check_subscription_expired(page, account)
        except Exception as e:
            log_node("一级菜单首次点击失败，尝试关闭弹窗后重试", level="WARN",
                     module=module_name, error=str(e)[:80])
            await _dismiss_runtime_popup(page, module_name=module_name,
                                         stage="sidebar_parent_retry", account=account)
            try:
                loc = page.locator(nav_parent_selector)
                await loc.scroll_into_view_if_needed(timeout=5_000)
                await loc.click(timeout=8_000, force=True)
                log_node("一级菜单重试后已展开", level="INFO", module=module_name)
                await page.wait_for_timeout(1_000)
                await _check_subscription_expired(page, account)
            except Exception as e2:
                # 一级菜单展开失败，无法继续导航，直接返回失败
                await _screenshot(page, f"{module_name}_{win}_nav_fail")
                write_event(STAGE_SIDEBAR_PARENT, "FAILED", context={"module": module_name, "win": win, "category": category, "account": account}, detail=str(e2)[:120], screenshot=f"logs/{module_name}_{win}_nav_fail*.png")
                return DownloadResult(module=module_name, win=win, category=category,
                                      status="failed",
                                      reason=f"一级菜单展开失败: {str(e2)[:60]}")

        await page.wait_for_timeout(1_000)  # 等子菜单展开动画完成

    # ── 点击二级菜单链接（在 submenu 容器内精确匹配） ──
    log_node(f"点击二级菜单: {nav_child}", level="INFO", module=module_name,
             submenu_id=submenu_id)
    try:
        # 在指定的子菜单容器内，通过 role=link 和文字精确定位二级菜单
        loc = page.locator(f"#{submenu_id}").get_by_role("link", name=nav_child)
        await loc.wait_for(state="visible", timeout=5_000)
        await loc.click(timeout=8_000)
        log_node("二级菜单已点击", level="INFO", module=module_name,
                 nav_child=nav_child)
        await page.wait_for_timeout(1_000)
        await _check_subscription_expired(page, account)
    except Exception as e:
        log_node("二级菜单首次点击失败，尝试关闭弹窗后重试", level="WARN",
                 module=module_name, nav_child=nav_child, error=str(e)[:80])
        await _dismiss_runtime_popup(page, module_name=module_name,
                                     stage="sidebar_child_retry", account=account)
        try:
            loc = page.locator(f"#{submenu_id}").get_by_role("link", name=nav_child)
            await loc.wait_for(state="visible", timeout=5_000)
            await loc.click(timeout=8_000, force=True)
            log_node("二级菜单重试后已点击", level="INFO", module=module_name,
                     nav_child=nav_child)
            await page.wait_for_timeout(1_000)
            await _check_subscription_expired(page, account)
        except Exception as e2:
            # 二级菜单点击失败，无法到达目标页面，返回失败
            await _screenshot(page, f"{module_name}_{win}_nav_fail")
            write_event(STAGE_SIDEBAR_CHILD, "FAILED", context={"module": module_name, "win": win, "category": category, "account": account}, detail=str(e2)[:120], screenshot=f"logs/{module_name}_{win}_nav_fail*.png")
            return DownloadResult(module=module_name, win=win, category=category,
                                  status="failed",
                                  reason=f"二级菜单点击失败: {str(e2)[:60]}")

    # 导航完成后等待5秒，让页面开始加载数据
    await page.wait_for_timeout(5_000)

    # ══════════════════════════════════════════
    # 步骤2：等待页面数据加载完成
    # 检测表格行等数据元素出现，确认页面已完全加载
    # ══════════════════════════════════════════
    await _wait_for_data(page, module_name, win)

    # ══════════════════════════════════════════
    # 步骤3：页面异常检测
    # 检查是否出现验证码、IP封禁、服务器错误等异常
    # ══════════════════════════════════════════
    status, reason = await check_page_anomaly(
        page, context_desc=f"{module_name}_{win}导航后"
    )
    # 验证码或封禁：无法继续，直接返回失败
    if status in ("captcha", "blocked"):
        await _screenshot(page, f"{module_name}_{win}_anomaly")
        return DownloadResult(module=module_name, win=win, category=category,
                              status="failed", reason=reason)
    # 页面错误：标记失败，等待重试装饰器自动重试
    if status == "error":
        log_node("页面错误，标记failed等待重试", level="WARN",
                 module=module_name, reason=reason)
        await _screenshot(page, f"{module_name}_{win}_error")
        return DownloadResult(module=module_name, win=win, category=category,
                              status="failed", reason=reason)

    log_node("页面状态正常", level="INFO", module=module_name)
    write_event(STAGE_ANOMALY_CHECK, "SUCCESS", context={"module": module_name, "win": win, "category": category})

    # ══════════════════════════════════════════
    # 步骤3.5：品类筛选（如果指定了品类且模块支持品类筛选）
    # 在导出前先筛选品类，确保导出的是指定品类的数据
    # ══════════════════════════════════════════
    if category and has_category_filter:
        log_node(f"选择品类: {category}", level="INFO", module=module_name)
        category_ok = await _select_category(page, category, module_name, win)
        if not category_ok:
            # 品类筛选失败（超时或未找到品类），跳过本项下载
            log_node(f"品类筛选失败，跳过本项下载: {category}", level="ERROR", module=module_name)
            write_event(STAGE_CATEGORY_SELECT, "FAILED", context={"module": module_name, "win": win, "category": category}, detail="品类筛选超时")
            return DownloadResult(module=module_name, win=win, category=category, status="failed", tmp_path=None, reason="品类筛选超时")
        # 品类筛选成功，截图记录
        await _screenshot(page, f"{task_label}_category_selected")
        write_event(STAGE_CATEGORY_SELECT, "SUCCESS", context={"module": module_name, "win": win, "category": category})

    # ══════════════════════════════════════════
    # 步骤4：点击时间 Tab（日榜为默认选中，无需点击）
    # 周榜/月榜需要点击对应的 Tab 切换数据
    # ══════════════════════════════════════════
    if time_tab:
        # 非日榜：需要点击对应的时间Tab（如 "Weekly"、"Monthly"）
        log_node(f"点击时间Tab: {time_tab}", level="INFO",
                 module=module_name, win=win)
        ok = await _click_by_text(page, time_tab,
                                  desc=f"时间Tab: {time_tab}")
        if ok:
            # Tab切换成功，等待3秒让数据刷新
            await page.wait_for_timeout(3_000)
            await _screenshot(page, f"{module_name}_{win}_tab_switched")
            write_event(STAGE_TAB_CLICK, "SUCCESS", context={"module": module_name, "win": win, "tab": time_tab})
        else:
            # Tab点击失败，截图记录但继续执行（可能仍在日榜数据上）
            log_node("Tab点击失败，截图后继续",
                     level="WARN", tab=time_tab, win=win)
            await _screenshot(page, f"{module_name}_{win}_tab_fail")
            write_event(STAGE_TAB_CLICK, "FAILED", context={"module": module_name, "win": win, "tab": time_tab}, detail="Tab点击失败")
    else:
        # 日榜：默认选中，无需点击Tab
        log_node(f"日榜为默认选中，无需点Tab", level="INFO",
                 module=module_name)

    # ══════════════════════════════════════════
    # 步骤5：导出操作（悬停下拉 → 选条数 → 等 popup → 兜底点 Export）
    # 这是最复杂的步骤，涉及多种交互方式和兜底策略
    # ══════════════════════════════════════════

    # ── 注册 download / popup 事件监听（全程生效） ──
    # 这些监听器会在后续操作触发下载或弹窗时自动收集事件
    download_obj = None       # 最终捕获到的下载对象
    download_from = None      # 下载来源标识（用于日志）
    popup_page = None         # 导出触发的弹出页面
    popup_url = None          # 弹出页面的 URL
    main_downloads = []       # 主页面触发的下载事件列表
    popup_list = []           # 弹出页面列表
    # 监听主页面的 download 事件（文件下载时触发）
    page.on("download", lambda d: main_downloads.append(d))
    # 监听主页面的 popup 事件（新窗口/标签页打开时触发）
    page.on("popup", lambda p: popup_list.append(p))

    # ── 5-1 悬停导出下拉箭头，触发条数菜单弹出 ──
    # Echotik 的导出按钮旁有一个下拉箭头，悬停后会弹出条数选择菜单
    log_node("导出1：悬停下拉箭头", level="INFO", module=module_name)
    try:
        # 获取页面上第3个按钮（索引2），即导出下拉箭头
        dropdown_btn = page.get_by_role("button").nth(2)
        # 悬停触发下拉菜单
        await dropdown_btn.hover(timeout=8_000)
        log_node("下拉菜单已弹出", level="INFO")
        write_event(STAGE_DROPDOWN_HOVER, "SUCCESS", context={"module": module_name, "win": win})
        await page.wait_for_timeout(1_000)
        await _check_subscription_expired(page, account)
    except Exception as e:
        # 悬停失败时尝试点击作为兜底
        log_node("下拉箭头悬停失败，尝试点击",
                 level="WARN", error=str(e)[:60])
        try:
            await dropdown_btn.click(timeout=5_000)
        except Exception:
            pass  # 点击也失败则继续，后续会有兜底策略

    # 等待下拉菜单展开动画完成
    await page.wait_for_timeout(800)
    await _screenshot(page, f"{module_name}_{win}_dropdown_hover")

    # ── 5-2 选择导出条数（选完可能直接触发导出 popup） ──
    # 点击条数选项（如 "200 Records"），某些情况下会直接触发下载弹窗
    log_node(f"导出2：选择条数 {export_count}", level="INFO",
             module=module_name)
    try:
        await page.get_by_text(export_count).click(timeout=5_000)
        log_node(f"条数已选择: {export_count}", level="INFO")
        write_event(STAGE_COUNT_SELECT, "SUCCESS", context={"module": module_name, "win": win, "count": export_count})
        await page.wait_for_timeout(1_000)
        await _check_subscription_expired(page, account)
    except Exception as e:
        # 条数选择失败，截图记录但继续（后续兜底策略会处理）
        log_node(f"条数选择失败: {export_count}",
                 level="WARN", error=str(e)[:60])
        write_event(STAGE_COUNT_SELECT, "FAILED", context={"module": module_name, "win": win, "count": export_count}, detail=str(e)[:120])
        await _screenshot(page, f"{module_name}_{win}_count_fail")

    # 等待5秒，观察选条数是否已经触发了 popup（有些页面选条数即触发导出）
    await page.wait_for_timeout(5_000)

    await _screenshot(page, f"{module_name}_{win}_after_count")

    # ── 判断选条数后是否已触发 popup ──
    if popup_list:
        # 情况A：选条数后直接触发了 popup（某些页面的行为）
        popup_page = popup_list[0]
        popup_url = popup_page.url
        log_node("选条数后直接触发了 popup", level="INFO",
                 url=popup_url[:120])
        # 读取 popup 内容，检测是否为错误/限额页面（而非正常下载页）
        popup_body = ""
        try:
            await popup_page.wait_for_load_state("domcontentloaded", timeout=5_000)
            await _screenshot(popup_page, f"{module_name}_{win}_popup_content")
            popup_body = await popup_page.inner_text("body", timeout=3_000)
            log_node("popup 页面文本", level="INFO",
                     text=popup_body[:200], url=popup_url[:200])
        except Exception as e:
            log_node("popup 截图/读取失败", level="WARN", error=str(e)[:80])
        # 检测是否为限额/错误提示页面
        if _is_error_popup(popup_body):
            # 是错误页面，关闭 popup 并返回失败
            try:
                await popup_page.close()
            except Exception:
                pass
            write_event(STAGE_EXPORT_TRIGGER, "FAILED", context={"module": module_name, "win": win, "category": category}, detail=f"导出受限: {popup_body[:100]}")
            return DownloadResult(module=module_name, win=win, category=category,
                                  status="failed",
                                  reason=f"导出受限: {popup_body[:100]}")
    else:
        # 情况B（5-3 兜底）：选条数没触发 popup，需要手动点击 Export 按钮
        log_node("导出3：选条数未触发 popup，点击 Export 按钮",
                 level="INFO", module=module_name)
        try:
            # 使用 expect_popup 上下文管理器等待 popup 弹出
            async with page.expect_popup(timeout=30_000) as popup_info:
                # 点击 Export 按钮触发导出
                await page.get_by_role("button", name="Export").click(
                    timeout=8_000)
                log_node("Export 按钮已点击", level="INFO")
                write_event(STAGE_EXPORT_TRIGGER, "SUCCESS", context={"module": module_name, "win": win, "category": category})
                await page.wait_for_timeout(1_000)
                await _check_subscription_expired(page, account)
            # 获取弹出的 popup 页面对象
            popup_page = await popup_info.value
            popup_url = popup_page.url
            log_node("popup 已打开", level="INFO", url=popup_url[:120])
            # 读取 popup 内容，检测是否为错误/限额页面
            popup_body = ""
            try:
                await popup_page.wait_for_load_state("domcontentloaded", timeout=5_000)
                await _screenshot(popup_page, f"{module_name}_{win}_export_popup_content")
                popup_body = await popup_page.inner_text("body", timeout=3_000)
                log_node("Export popup 页面文本", level="INFO",
                         text=popup_body[:200], url=popup_url[:200])
            except Exception as e:
                log_node("Export popup 截图失败", level="WARN", error=str(e)[:80])
            # 检测是否为限额/错误提示
            if _is_error_popup(popup_body):
                try:
                    await popup_page.close()
                except Exception:
                    pass
                write_event(STAGE_EXPORT_TRIGGER, "FAILED", context={"module": module_name, "win": win, "category": category}, detail=f"导出受限: {popup_body[:100]}")
                return DownloadResult(module=module_name, win=win, category=category,
                                      status="failed",
                                      reason=f"导出受限: {popup_body[:100]}")
        except Exception as e:
            # Export 按钮点击后 popup 未弹出，导出失败
            await _screenshot(page, f"{module_name}_{win}_export_fail")
            write_event(STAGE_EXPORT_TRIGGER, "FAILED", context={"module": module_name, "win": win, "category": category}, detail=f"popup 未弹出: {str(e)[:80]}", screenshot=f"logs/{module_name}_{win}_export_fail*.png")
            raise RuntimeError(f"导出失败，popup 未弹出: {str(e)[:80]}")

    # ── 在 popup 页面上也注册 download 事件监听 ──
    # popup 页面可能会触发文件下载（而非主页面）
    popup_downloads = []
    popup_page.on("download", lambda d: popup_downloads.append(d))

    # ── 等待 download 事件触发（最多 16s，每 2s 检查一次） ──
    # 下载可能来自主页面或 popup 页面，两边都要检查
    for _ in range(8):
        await page.wait_for_timeout(2_000)
        # 优先检查主页面的下载事件
        if main_downloads:
            download_obj = main_downloads[0]
            download_from = "main_download"
            break
        # 再检查 popup 页面的下载事件
        if popup_downloads:
            download_obj = popup_downloads[0]
            download_from = "popup_download"
            break

    # ── 关闭 popup 页面（无论下载是否成功） ──
    if popup_page:
        try:
            await popup_page.close()
        except Exception:
            pass  # 关闭失败不影响主流程

    # ══════════════════════════════════════════
    # 步骤6：保存下载文件到临时目录
    # 支持两种下载方式：
    #   方式A：通过 Playwright download 事件保存（优先）
    #   方式B：通过 API 直接请求 popup URL 下载（兜底）
    # ══════════════════════════════════════════
    tmp_path = None       # 下载文件的临时保存路径
    orig_name = None      # 原始文件名

    if download_obj:
        # ── 方式A：通过 download 事件保存文件 ──
        # Playwright 已经捕获到了下载事件，直接保存文件
        log_node(f"下载事件来源: {download_from}", level="INFO",
                 module=module_name,
                 filename=download_obj.suggested_filename)
        # 使用浏览器建议的文件名，兜底用模块名+窗口名
        orig_name = download_obj.suggested_filename or f"{module_name}_{win}.xlsx"
        tmp_path  = TMP_DIR / orig_name
        # 将下载的文件保存到临时目录
        await download_obj.save_as(str(tmp_path))
        write_event(STAGE_DOWNLOAD_CAPTURE, "SUCCESS", context={"module": module_name, "win": win, "category": category, "from": download_from, "file": orig_name})
    else:
        # ── 方式B：download 事件未触发，用 popup URL 直接发起 HTTP 请求下载 ──
        # 某些情况下 popup 页面不会触发 download 事件，但其 URL 就是下载链接
        log_node("download 事件未触发，改用 API 直接请求",
                 level="WARN", module=module_name, url=popup_url[:120])
        # 使用 Playwright 的 API 请求上下文（自动携带 cookies）
        api = page.context.request
        resp = await api.get(popup_url)
        # 检查 HTTP 响应状态
        if resp.status != 200:
            raise RuntimeError(
                f"API 直接请求失败: HTTP {resp.status}, url={popup_url[:80]}")
        body = await resp.body()
        # 校验返回内容是否为 HTML 错误页（正常的 xlsx 文件以 PK 魔数 0x504B0304 开头）
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type or (body[:4] != b"PK\x03\x04" and b"<html" in body[:500].lower()):
            # 返回的是 HTML 页面而非 xlsx 文件，说明下载失败
            body_text = body[:500].decode("utf-8", errors="replace")
            log_node("API 返回 HTML 错误页而非 xlsx", level="ERROR",
                     module=module_name, content_type=content_type,
                     body=body_text[:200])
            write_event(STAGE_DOWNLOAD_CAPTURE, "FAILED", context={"module": module_name, "win": win, "category": category, "from": "api_request"}, detail=f"API返回HTML错误页: {body_text[:100]}")
            return DownloadResult(module=module_name, win=win, category=category,
                                  status="failed",
                                  reason=f"API返回HTML错误页: {body_text[:100]}")
        # 从 Content-Disposition 响应头提取原始文件名，兜底用模块名
        cd = resp.headers.get("content-disposition", "")
        if "filename=" in cd:
            # 解析 filename= 后面的值，去除引号
            orig_name = cd.split("filename=")[-1].strip().strip('"').strip("'")
        else:
            orig_name = f"{module_name}_{win}.xlsx"
        # 将响应体写入临时文件
        tmp_path = TMP_DIR / orig_name
        tmp_path.write_bytes(body)
        download_from = "api_request"
        log_node(f"API 直接请求成功", level="INFO",
                 module=module_name, filename=orig_name)
        write_event(STAGE_DOWNLOAD_CAPTURE, "SUCCESS", context={"module": module_name, "win": win, "category": category, "from": download_from, "file": orig_name})

    # 记录下载文件大小（KB）
    size_kb = tmp_path.stat().st_size / 1024
    log_node("下载完成", level="INFO",
             module=module_name, win=win,
             file=orig_name, size=f"{size_kb:.1f}KB")

    # ── 记录导出配额（从 "200 Records" 提取数字 200） ──
    # 用于跟踪每日导出量，接近限额时发出预警
    try:
        count_num = int(export_count.split()[0])  # "200 Records" → 200
        record_export(task=f"{module_name}_{win}", count=count_num)
        check_quota_warning()  # 检查是否接近每日配额上限
    except Exception:
        pass  # 解析失败不影响主流程

    # ══════════════════════════════════════════
    # 步骤7：新鲜度检测
    # 通过 MD5 比对判断下载的数据是否与历史文件相同
    # 如果相同则标记为 "stale"（数据未更新），避免重复归档
    # ══════════════════════════════════════════
    fresh = is_fresh(tmp_path, BASE_EXPORTS, captured, win, ds)
    if not fresh:
        # 数据未更新：MD5 与历史文件相同，标记为 stale
        log_node("数据未更新（MD5与历史文件相同）", level="WARN",
                 module=module_name, win=win)
        write_event(STAGE_FRESHNESS_CHECK, "SKIPPED", context={"module": module_name, "win": win, "category": category}, detail="MD5与历史文件相同")
        return DownloadResult(module=module_name, win=win, category=category,
                              status="stale", tmp_path=tmp_path)

    # 数据已更新，文件准备就绪，等待归档模块处理
    log_node("数据已更新，文件准备就绪", level="INFO",
             module=module_name, win=win)
    write_event(STAGE_FRESHNESS_CHECK, "SUCCESS", context={"module": module_name, "win": win, "category": category, "file": orig_name})
    return DownloadResult(module=module_name, win=win, category=category,
                          status="success", tmp_path=tmp_path)