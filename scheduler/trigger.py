#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scheduler/trigger.py
调度与重试逻辑

功能：
    1. 根据日期判断本次需要下载哪些粒度（d/w/m）
    2. 最多尝试 2 次（立即重试，不等待）
    3. 全部失败：发送报警通知，结束进程（重试交给 crontab）
"""

# ============================================================
# 标准库导入
# ============================================================
import os                          # 用于读取环境变量（账号、代理等配置）
from datetime import date          # 用于获取当前日期，判断今天需要执行哪些任务
from typing import Callable        # 类型注解，用于标注回调函数参数的类型

# ============================================================
# 第三方库导入
# ============================================================
# Playwright 异步 API，用于启动和控制无头浏览器
from playwright.async_api import async_playwright

# ============================================================
# 项目内部模块导入
# ============================================================
# BrowserSession：封装了浏览器会话管理（登录、Cookie 保持等）
from browser.session import BrowserSession
# SubscriptionExpiredError：当 echotik 账号订阅过期时抛出的自定义异常
from browser.downloader import SubscriptionExpiredError
# log_node：结构化日志输出工具，支持 level 和任意关键字参数
from utils.logger import log_node
# notify_final_failure：所有重试失败后发送报警通知（如企业微信/邮件）
# notify_success：任务全部成功后发送成功通知
from utils.notifier import notify_final_failure, notify_success


def _get_win_host_ip() -> str:
    """
    动态获取 Windows 宿主机 IP（移植自 bashrc 的 wsl_win_host_ip）。

    背景：在 WSL2 环境中，Windows 宿主机的 IP 地址每次重启都会变化，
    因此不能硬编码，必须在运行时动态获取。获取到的 IP 主要用于构建
    代理地址，让 WSL2 内的浏览器通过 Windows 上的代理软件（如 v2rayN）上网。

    方法1：从 /proc/net/route 读取默认网关（eth0 的 gateway）
    方法2：兜底从 /etc/resolv.conf 读取 nameserver

    返回：
        str: Windows 宿主机 IP 地址，获取失败返回空字符串 ""
    """
    import struct  # 导入 struct 模块（此处实际未使用，可能是历史遗留）

    # ── 方法1：从 Linux 内核路由表 /proc/net/route 中解析默认网关 IP ──
    # 路由表格式：Iface Destination Gateway Flags RefCnt Use Metric Mask ...
    # 默认路由的 Destination 为 00000000，Gateway 字段包含网关的十六进制 IP
    try:
        # 第一次遍历：查找 eth0 接口上 gateway 列为 00000000 的行
        # （注意：这里的逻辑实际上检查的是 parts[2] == "00000000"，
        #  即 gateway 为 0 的行，这通常不是我们要找的默认网关行，
        #  但代码仍然尝试解析它）
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3 and parts[0] == "eth0" and parts[2] == "00000000":
                    # gateway 字段是小端序（little-endian）的十六进制字符串
                    # 例如 "0101A8C0" 表示 192.168.1.1
                    gw_hex = parts[2]
                    # 重新读取 gateway 列（iface=parts[0], dest=parts[1], gateway=parts[2]）
                    # 默认路由的特征是 dest=00000000
                    gw_hex = parts[2]
                    # 将十六进制网关地址转换为点分十进制 IP
                    # 小端序：从右往左每两位取一组，转为十进制
                    # 索引 (6,4,2,0) 对应从高字节到低字节的顺序
                    ip = ".".join(str(int(gw_hex[i:i+2], 16))
                                  for i in (6, 4, 2, 0))
                    # 排除全零地址（无效网关）
                    if ip != "0.0.0.0":
                        return ip

        # 第二次遍历：查找目标地址为 00000000（默认路由）且网关不为 00000000 的行
        # 这是更准确的默认网关查找逻辑
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.strip().split()
                if (len(parts) >= 3
                        and parts[1] == "00000000"       # dest 为 0 = 默认路由
                        and parts[2] != "00000000"):     # gateway 不为 0 = 有效网关
                    gw_hex = parts[2]
                    # 同样将小端序十六进制转为点分十进制 IP
                    ip = ".".join(str(int(gw_hex[i:i+2], 16))
                                  for i in (6, 4, 2, 0))
                    return ip
    except Exception:
        # 读取路由表失败（文件不存在、权限不足等），静默跳过，尝试方法2
        pass

    # ── 方法2：从 /etc/resolv.conf 中读取 DNS 服务器地址作为兜底 ──
    # 在 WSL2 中，resolv.conf 的 nameserver 通常指向 Windows 宿主机 IP
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                parts = line.strip().split()
                # 找到 "nameserver x.x.x.x" 行，取 IP 地址部分
                if parts and parts[0] == "nameserver":
                    return parts[1]
    except Exception:
        # 读取 resolv.conf 也失败，放弃获取
        pass

    # 两种方法都失败，返回空字符串表示无法获取
    return ""


def _proxy_tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    """
    检测指定的 host:port 是否可达（TCP 连接探测）。

    用于在自动探测代理时，先验证代理端口是否真的在监听，
    避免浏览器启动后因代理不可用而超时卡死。

    参数：
        host:    目标主机 IP 地址
        port:    目标端口号
        timeout: 连接超时时间（秒），默认 2 秒

    返回：
        True = 端口可达（代理服务正在运行），False = 不可达
    """
    import socket  # 延迟导入 socket 模块，仅在需要时加载
    try:
        # 尝试建立 TCP 连接，成功则说明端口可达
        # with 语句确保连接在检测完成后自动关闭
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        # 连接失败（拒绝连接、超时、网络不可达等），返回 False
        return False


def _get_proxy_settings() -> dict | None:
    """
    动态构建代理配置，传给 Playwright 浏览器。

    本函数按优先级依次尝试三种方式获取代理配置：
      1. 从环境变量中读取完整的代理地址（PLAYWRIGHT_PROXY / https_proxy 等）
      2. 自动探测：动态获取 WSL2 中 Windows 宿主机 IP + .env 中的 PROXY_PORT
         拼接成完整代理地址（适配 WSL2 每次重启 IP 变化的场景）
      3. 都没有 → 返回 None，浏览器将直连不走代理

    .env 配置示例（二选一）：
      # 方式A：完整地址（优先）
      PLAYWRIGHT_PROXY=http://172.26.96.1:10808

      # 方式B：只填端口，IP 自动获取（推荐，适应 WSL2 动态 IP）
      PROXY_PORT=10808

    返回：
        dict: {"server": "http://IP:PORT"} 格式的代理配置，供 Playwright 使用
        None: 无代理配置，浏览器直连
    """
    # ── 优先级1：从环境变量中读取完整的代理 URL ──
    # 按优先级依次检查多个常见的代理环境变量名
    # PLAYWRIGHT_PROXY 是本项目自定义的，其余是通用的代理环境变量
    proxy_url = (
        os.getenv("PLAYWRIGHT_PROXY", "").strip()   # 项目专用代理变量（最高优先级）
        or os.getenv("https_proxy",    "").strip()   # 通用 HTTPS 代理（小写）
        or os.getenv("HTTPS_PROXY",    "").strip()   # 通用 HTTPS 代理（大写）
        or os.getenv("http_proxy",     "").strip()   # 通用 HTTP 代理（小写）
        or os.getenv("HTTP_PROXY",     "").strip()   # 通用 HTTP 代理（大写）
    )
    # 如果找到了任何一个非空的代理 URL，直接使用
    if proxy_url:
        log_node("浏览器使用指定代理", level="INFO", proxy=proxy_url)
        return {"server": proxy_url}

    # ── 优先级2：自动探测 Windows 宿主机 IP + PROXY_PORT 拼接代理地址 ──
    # 从环境变量读取代理端口号（仅端口，IP 自动获取）
    proxy_port_str = os.getenv("PROXY_PORT", "").strip()
    if proxy_port_str:
        # 校验端口号格式：必须是纯数字
        try:
            proxy_port = int(proxy_port_str)
        except ValueError:
            log_node("PROXY_PORT 格式错误，应为纯数字",
                     level="WARN", value=proxy_port_str)
            return None

        # 动态获取 Windows 宿主机 IP（通过路由表或 DNS 配置）
        host_ip = _get_win_host_ip()
        if not host_ip:
            # 无法获取宿主机 IP，自动探测失败
            log_node("无法获取 Windows 宿主机 IP，代理自动探测失败",
                     level="WARN",
                     hint="请在 .env 中直接设置 PLAYWRIGHT_PROXY=http://<IP>:<PORT>")
            return None

        # 在使用前先验证代理端口是否可达（TCP 握手探测）
        if not _proxy_tcp_ok(host_ip, proxy_port):
            # 端口不可达，可能是代理软件未启动或未开启局域网访问
            log_node("代理端口不可达，请确认 v2rayN 正在运行且已开启允许局域网",
                     level="WARN",
                     host=host_ip, port=proxy_port)
            return None

        # 拼接完整的代理 URL
        proxy_url = f"http://{host_ip}:{proxy_port}"
        log_node("浏览器使用自动探测代理", level="INFO",
                 windows_ip=host_ip, port=proxy_port, proxy=proxy_url)
        return {"server": proxy_url}

    # ── 优先级3：无任何代理配置，浏览器将直连 ──
    log_node("未检测到代理配置，浏览器直连",
             level="INFO",
             hint="如需代理请在 .env 中设置 PROXY_PORT=10808 或 PLAYWRIGHT_PROXY=http://IP:PORT")
    return None


def get_tasks_for_today(captured: str = None) -> list[str]:
    """
    根据日期决定本次执行哪些粒度（旧版接口，保持向后兼容）。

    这是 V1 版本的任务调度函数，仅返回粒度标识列表（如 ["d", "w"]），
    不包含具体模块和品类信息。新代码应使用 get_detailed_tasks_for_today()。

    调度规则：
        d（日榜）- 每天都执行
        w（周榜）- 每周一执行商品榜，每周二执行小店榜
        m（月榜）- 每月1号执行商品榜，每月2号执行小店榜

    参数：
        captured: 指定日期字符串（ISO 格式，如 "2026-03-19"），
                  为 None 时使用当天日期。用于手动补采历史数据。

    返回：
        list[str]: 粒度标识列表，如 ["d"] 或 ["d", "w"] 或 ["d", "m"]
    """
    # 解析日期：如果传入了 captured 参数则使用指定日期，否则使用今天
    d = date.fromisoformat(captured) if captured else date.today()

    # 日榜每天都要采集，所以 "d" 始终在列表中
    wins = ["d"]

    # weekday() 返回 0=周一, 1=周二, ..., 6=周日
    # 周一或周二需要额外采集周榜数据
    if d.weekday() in (0, 1):   # 周一或周二
        wins.append("w")

    # 每月1号或2号需要额外采集月榜数据
    if d.day in (1, 2):         # 1号或2号
        wins.append("m")

    # 记录本次调度决策到日志
    log_node("本日任务调度", level="INFO", date=str(d), wins=wins)
    return wins


def get_detailed_tasks_for_today(captured: str = None) -> list[dict]:
    """
    根据日期和 tasks.yaml 配置文件决定本次执行哪些具体任务（V2 版本）。

    相比 get_tasks_for_today()，本函数返回更详细的任务信息，
    包含具体的模块名、粒度和品类，支持按品类筛选采集。

    参数：
        captured: 指定日期字符串（ISO 格式，如 "2026-03-19"），
                  为 None 时使用当天日期。用于手动补采历史数据。

    返回：
        list[dict]: 任务列表，每个任务是一个字典，包含：
            - module:   模块名（如 "product_rank", "shop_rank"）
            - win:      粒度标识（"d"=日榜, "w"=周榜, "m"=月榜）
            - category: 品类名称（空字符串表示全品类）
    """
    import yaml            # 用于解析 YAML 配置文件
    from pathlib import Path  # 用于构建跨平台的文件路径

    # 解析日期：如果传入了 captured 参数则使用指定日期，否则使用今天
    d = date.fromisoformat(captured) if captured else date.today()
    weekday = d.weekday()  # 获取星期几：0=周一, 1=周二, ..., 6=周日
    day_of_month = d.day   # 获取当月第几天（1~31）

    # ── 加载 tasks.yaml 配置文件 ──
    # 配置文件位于项目根目录下的 config/tasks.yaml
    config_path = Path(__file__).parent.parent / "config" / "tasks.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 从配置中提取已启用的品类名称列表
    # 只保留 enabled=True（默认为 True）的品类
    categories = [c["name"] for c in config.get("categories", []) if c.get("enabled", True)]

    # 将模块列表转为字典，方便按名称快速查找模块配置
    # 键=模块名, 值=模块完整配置（包含 wins, has_category_filter 等字段）
    modules = {m["name"]: m for m in config.get("modules", [])}

    # 获取调度配置（包含 daily, weekly, monthly 三个子节点）
    schedule = config.get("schedule", {})

    # 用于收集本次需要执行的所有任务
    tasks = []

    # ── 日榜任务：每天都要采集 ──
    # 从 schedule.daily 中读取每天需要执行的模块列表
    daily_modules = schedule.get("daily", [])
    for module_name in daily_modules:
        # 跳过配置中不存在的模块（防止配置错误）
        if module_name not in modules:
            continue
        module = modules[module_name]
        # 检查该模块是否支持日榜粒度（wins 列表中包含 "d"）
        if "d" not in module.get("wins", []):
            continue
        # 为每个品类生成一个独立的任务
        for category in categories:
            # 如果品类为空字符串（全品类）或该模块支持品类筛选，则添加任务
            if category == "" or module.get("has_category_filter", False):
                tasks.append({
                    "module": module_name,
                    "win": "d",
                    "category": category,
                })

    # ── 周榜任务：根据今天是星期几决定采集哪些模块 ──
    weekly_schedule = schedule.get("weekly", {})
    # 将 Python 的 weekday() 数字映射为英文星期名（与 YAML 配置中的键对应）
    weekday_map = {0: "monday", 1: "tuesday", 2: "wednesday", 3: "thursday",
                   4: "friday", 5: "saturday", 6: "sunday"}
    weekday_key = weekday_map.get(weekday, "")  # 获取今天对应的英文星期名
    # 从周调度配置中获取今天需要执行的模块列表
    weekly_modules = weekly_schedule.get(weekday_key, [])
    for module_name in weekly_modules:
        if module_name not in modules:
            continue
        module = modules[module_name]
        # 检查该模块是否支持周榜粒度
        if "w" not in module.get("wins", []):
            continue
        for category in categories:
            if category == "" or module.get("has_category_filter", False):
                tasks.append({
                    "module": module_name,
                    "win": "w",
                    "category": category,
                })

    # ── 月榜任务：根据今天是几号决定采集哪些模块 ──
    monthly_schedule = schedule.get("monthly", {})
    # 构建日期键名，如 "day_1", "day_2"，与 YAML 配置中的键对应
    day_key = f"day_{day_of_month}"
    # 从月调度配置中获取今天需要执行的模块列表
    monthly_modules = monthly_schedule.get(day_key, [])
    for module_name in monthly_modules:
        if module_name not in modules:
            continue
        module = modules[module_name]
        # 检查该模块是否支持月榜粒度
        if "m" not in module.get("wins", []):
            continue
        for category in categories:
            if category == "" or module.get("has_category_filter", False):
                tasks.append({
                    "module": module_name,
                    "win": "m",
                    "category": category,
                })

    # ── 输出调度结果日志 ──
    log_node("本日详细任务调度", level="INFO", date=str(d), task_count=len(tasks))
    # 逐条打印每个任务的详细信息，方便排查
    for t in tasks:
        cat_label = t["category"] if t["category"] else "全品类"
        log_node(f"  - {t['module']}_{t['win']} ({cat_label})", level="INFO")

    return tasks


async def run_with_retry(
    wins: list[str],
    captured: str,
    download_fn: Callable,
    route_fn: Callable,
    pipeline_fn: Callable,
    max_attempts: int = 2,
    module_filter: str = "",
) -> bool:
    """
    下载流程 V1（多账号轮换 × 每账号 max_attempts 次重试）。

    这是旧版的重试入口，基于粒度列表（wins）调度。新代码应使用 run_with_retry_v2()。

    核心设计思路：
        外层循环遍历所有可用账号，内层循环对每个账号最多尝试 max_attempts 次。
        任一账号的某次尝试中所有任务都成功 → 立即结束并通知成功。
        某个账号的所有尝试都失败 → 自动切换到下一个账号继续。
        所有账号都用完仍有失败 → 发送失败报警通知。

    参数：
        wins:          粒度列表，如 ["d", "w"]，表示需要下载日榜和周榜
        captured:      数据日期字符串（ISO 格式），如 "2026-03-19"
        download_fn:   下载回调函数，负责实际的数据采集工作
        route_fn:      路由回调函数，将成功下载的文件分发到目标目录
        pipeline_fn:   后处理管道回调函数，在全部成功后执行数据清洗/入库等
        max_attempts:  每个账号的最大尝试次数，默认 2 次
        module_filter: 模块过滤器，仅采集指定模块（空字符串表示不过滤）

    返回：
        True = 全部任务成功完成，False = 最终失败（所有账号都已耗尽）
    """
    # ── 账号状态检查 ──
    # 延迟导入账号追踪工具（避免循环依赖）
    from utils.account_tracker import check_account_change, log_account_status, check_account_expiry
    # 检测账号配置是否发生变更（与上次运行对比）
    check_account_change()
    # 输出当前账号的使用统计信息（已用天数、剩余天数等）
    log_account_status()
    # 检查账号是否即将到期，如果快到期会输出警告
    check_account_expiry()

    # 获取代理配置（可能为 None，表示直连）
    proxy = _get_proxy_settings()

    # ── 从环境变量读取账号和密码列表 ──
    # 账号和密码以逗号分隔存储在环境变量中，如 "a]@x.com,b@y.com"
    accounts_str  = os.getenv("ECHOTIK_ACCOUNTS", "")
    passwords_str = os.getenv("ECHOTIK_PASSWORDS", "")
    # 解析账号列表：按逗号分割，去除空白和引号
    accounts  = [a.strip().strip('"').strip("'")
                 for a in accounts_str.split(",") if a.strip()]
    # 解析密码列表：与账号列表一一对应
    passwords = [p.strip().strip('"').strip("'")
                 for p in passwords_str.split(",") if p.strip()]
    # 如果没有配置任何账号，直接报错返回
    if not accounts:
        log_node("未配置账号", level="ERROR")
        return False

    # 可用账号总数
    total_accounts = len(accounts)

    async def _launch_browser(pw):
        """
        启动 Chromium 无头浏览器并创建新的浏览器上下文。

        每次重试都会重新启动浏览器，确保干净的浏览器状态，
        避免上一次失败的残留状态影响本次尝试。

        参数：
            pw: Playwright 实例

        返回：
            tuple: (browser, context, page, session)
                - browser: 浏览器实例，用于最终关闭
                - context: 浏览器上下文，隔离的会话环境
                - page:    页面对象，用于页面操作
                - session: BrowserSession 封装，管理登录态
        """
        # 启动 Chromium 浏览器（无头模式，可选代理）
        browser = await pw.chromium.launch(
            headless=True,
            proxy=proxy,
        )
        # 创建新的浏览器上下文（相当于一个独立的浏览器窗口）
        # 设置视口大小为 1440x900，模拟桌面浏览器
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            # 设置 User-Agent 伪装为 Chrome 浏览器，避免被网站识别为爬虫
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        # 在上下文中打开一个新页面
        page    = await context.new_page()
        # 创建 BrowserSession 实例，封装登录和会话管理逻辑
        session = BrowserSession(context)
        return browser, context, page, session

    # ── 主流程：启动 Playwright 并开始多账号轮换重试 ──
    async with async_playwright() as pw:
        # 跨账号累积已成功路由的结果列表
        # 即使某个账号只完成了部分任务，成功的部分也会被保留
        all_routed = []

        # ── 外层循环：遍历所有可用账号 ──
        for acct_idx in range(total_accounts):
            # 获取当前账号和对应密码
            acct = accounts[acct_idx]
            pwd  = passwords[acct_idx] if acct_idx < len(passwords) else ""
            # 对账号做脱敏处理，用于日志输出（如 "abc***@gmail.com"）
            acct_masked = acct[:3] + "***@" + acct.split("@")[-1] if "@" in acct else acct[:3] + "***"

            log_node(f"切换到账号 {acct_idx + 1}/{total_accounts}",
                     level="START", account=acct_masked)

            # 复制一份待处理的粒度列表，避免修改原始列表
            # 重试时会缩小这个列表，只重试失败的粒度
            pending_wins = wins[:]

            # ── 内层循环：同一账号的多次重试 ──
            for attempt in range(1, max_attempts + 1):

                log_node(f"账号 {acct_idx + 1} 第 {attempt}/{max_attempts} 次尝试：启动浏览器",
                         level="START", account=acct_masked,
                         tasks=pending_wins, captured=captured)

                # 每次尝试都重新启动浏览器（干净状态）
                browser, context, page, session = await _launch_browser(pw)

                # 指定使用当前账号登录（覆盖 session 中的默认账号）
                session.set_single_account(acct, pwd)

                # ── 步骤1：登录 echotik 网站 ──
                try:
                    await session.ensure_login(page)
                except RuntimeError as e:
                    # 登录失败（验证码错误、账号被封等），放弃当前账号，换下一个
                    log_node("登录失败，尝试下一个账号",
                             level="WARN", account=acct_masked, error=str(e)[:80])
                    await browser.close()
                    break  # 跳出 attempt 循环，进入下一个账号

                # ── 步骤2：执行数据下载 ──
                try:
                    # 调用下载回调函数，执行实际的数据采集
                    results = await download_fn(
                        wins=pending_wins,
                        captured=captured,
                        session=session,
                        page=page,
                        module_filter=module_filter,
                        account=acct,
                    )
                except SubscriptionExpiredError as e:
                    # 账号订阅已过期，无法继续使用，换下一个账号
                    log_node("账号订阅到期，自动切换下一个账号",
                             level="WARN", account=acct_masked)
                    await browser.close()
                    break  # 跳出 attempt 循环，进入下一个账号

                # ── 步骤3：关闭浏览器，统计本次结果 ──
                await browser.close()
                log_node("浏览器已关闭", level="INFO",
                         account=acct_masked, attempt=attempt)

                # 按状态分类下载结果
                # success: 下载成功的任务
                # stale:   数据过期/未更新的任务（网站数据还没刷新）
                # failed:  下载失败的任务（网络错误、页面异常等）
                success_list = [r for r in results if r.status == "success"]
                stale_list   = [r for r in results if r.status == "stale"]
                failed_list  = [r for r in results if r.status == "failed"]

                log_node(f"账号 {acct_idx + 1} 第 {attempt} 次尝试结果",
                         level="INFO",
                         account=acct_masked,
                         success=len(success_list),
                         stale=len(stale_list),
                         failed=len(failed_list))

                # 将成功的结果立即路由（分发到目标目录）
                if success_list:
                    route_fn(success_list)
                    # 累积到跨账号的成功列表中
                    all_routed.extend(success_list)

                # ── 判断：全部成功 → 执行后处理并通知 ──
                if not stale_list and not failed_list:
                    # 所有任务都成功了，执行后处理管道（数据清洗、入库等）
                    if pipeline_fn: pipeline_fn(captured)
                    # 发送成功通知
                    notify_success(
                        captured=captured,
                        success=[f"{r.module}/{r.win}" for r in all_routed],
                        attempt=attempt,
                    )
                    return True

                # ── 判断：还有重试机会（同一账号内） ──
                if attempt < max_attempts:
                    log_node(
                        f"部分任务未完成，同账号第 {attempt + 1} 次重试",
                        level="WARN",
                        account=acct_masked,
                        stale=[f"{r.module}/{r.win}" for r in stale_list],
                        failed=[f"{r.module}/{r.win}" for r in failed_list],
                    )
                    # 缩小待重试范围：只重试失败和过期的粒度，去重
                    pending_wins = list(set(
                        [r.win for r in stale_list] + [r.win for r in failed_list]
                    ))
                    continue  # 继续下一次 attempt

                # ── 判断：当前账号重试次数耗尽，换下一个账号 ──
                log_node(f"账号 {acct_masked} 重试 {max_attempts} 次仍失败，尝试下一个账号",
                         level="WARN",
                         stale=[f"{r.module}/{r.win}" for r in stale_list],
                         failed=[f"{r.module}/{r.win}" for r in failed_list])
                break  # 跳出 attempt 循环，进入下一个账号

        # ── 所有账号都已用完，仍有任务未完成 → 发送失败报警 ──
        log_node("所有账号均已尝试，发送失败通知", level="ERROR")
        notify_final_failure(
            captured=captured,
            stale=[],
            failed=[f"全部 {total_accounts} 个账号均失败"],
        )

        return False


async def run_with_retry_v2(
    tasks: list[dict],
    captured: str,
    download_fn: Callable,
    route_fn: Callable,
    pipeline_fn: Callable,
    max_attempts: int = 2,
) -> bool:
    """
    下载流程 V2（支持详细任务列表，包含品类筛选）。

    相比 V1 版本 run_with_retry()，V2 的主要改进：
      - 接收详细的任务列表（包含 module/win/category），而非简单的粒度列表
      - 重试时精确到具体任务粒度（模块+粒度+品类），而非整个粒度
      - 成功通知中包含品类信息，更易于排查

    整体流程与 V1 相同：多账号轮换 × 每账号多次重试。

    参数：
        tasks:        任务列表，每个任务是 dict，包含 {module, win, category}
        captured:     数据日期字符串（ISO 格式），如 "2026-03-19"
        download_fn:  下载回调函数，负责实际的数据采集工作
        route_fn:     路由回调函数，将成功下载的文件分发到目标目录
        pipeline_fn:  后处理管道回调函数，在全部成功后执行数据清洗/入库等
        max_attempts: 每个账号的最大尝试次数，默认 2 次

    返回：
        True = 全部任务成功完成，False = 最终失败（所有账号都已耗尽）
    """
    # ── 账号状态检查 ──
    # 延迟导入账号追踪工具（避免循环依赖）
    from utils.account_tracker import check_account_change, log_account_status, check_account_expiry
    # 检测账号配置是否发生变更（与上次运行对比）
    check_account_change()
    # 输出当前账号的使用统计信息（已用天数、剩余天数等）
    log_account_status()
    # 检查账号是否即将到期，如果快到期会输出警告
    check_account_expiry()

    # 获取代理配置（可能为 None，表示直连）
    proxy = _get_proxy_settings()

    # ── 从环境变量读取账号和密码列表 ──
    # 账号和密码以逗号分隔存储在环境变量中，如 "a@x.com,b@y.com"
    accounts_str  = os.getenv("ECHOTIK_ACCOUNTS", "")
    passwords_str = os.getenv("ECHOTIK_PASSWORDS", "")
    # 解析账号列表：按逗号分割，去除空白和引号
    accounts  = [a.strip().strip('"').strip("'")
                 for a in accounts_str.split(",") if a.strip()]
    # 解析密码列表：与账号列表一一对应
    passwords = [p.strip().strip('"').strip("'")
                 for p in passwords_str.split(",") if p.strip()]
    # 如果没有配置任何账号，直接报错返回
    if not accounts:
        log_node("未配置账号", level="ERROR")
        return False

    # 可用账号总数
    total_accounts = len(accounts)

    async def _launch_browser(pw):
        """
        启动 Chromium 无头浏览器并创建新的浏览器上下文。

        每次重试都会重新启动浏览器，确保干净的浏览器状态，
        避免上一次失败的残留状态影响本次尝试。

        参数：
            pw: Playwright 实例

        返回：
            tuple: (browser, context, page, session)
                - browser: 浏览器实例，用于最终关闭
                - context: 浏览器上下文，隔离的会话环境
                - page:    页面对象，用于页面操作
                - session: BrowserSession 封装，管理登录态
        """
        # 启动 Chromium 浏览器（无头模式，可选代理）
        browser = await pw.chromium.launch(
            headless=True,
            proxy=proxy,
        )
        # 创建新的浏览器上下文（相当于一个独立的浏览器窗口）
        # 设置视口大小为 1440x900，模拟桌面浏览器
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            # 设置 User-Agent 伪装为 Chrome 浏览器，避免被网站识别为爬虫
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        # 在上下文中打开一个新页面
        page    = await context.new_page()
        # 创建 BrowserSession 实例，封装登录和会话管理逻辑
        session = BrowserSession(context)
        return browser, context, page, session

    # ── 主流程：启动 Playwright 并开始多账号轮换重试 ──
    async with async_playwright() as pw:
        # 跨账号累积已成功路由的结果列表
        # 即使某个账号只完成了部分任务，成功的部分也会被保留
        all_routed = []

        # ── 外层循环：遍历所有可用账号 ──
        for acct_idx in range(total_accounts):
            # 获取当前账号和对应密码
            acct = accounts[acct_idx]
            pwd  = passwords[acct_idx] if acct_idx < len(passwords) else ""
            # 对账号做脱敏处理，用于日志输出（如 "abc***@gmail.com"）
            acct_masked = acct[:3] + "***@" + acct.split("@")[-1] if "@" in acct else acct[:3] + "***"

            log_node(f"切换到账号 {acct_idx + 1}/{total_accounts}",
                     level="START", account=acct_masked)

            # 复制一份待处理的任务列表，避免修改原始列表
            # 重试时会缩小这个列表，只包含失败的任务
            pending_tasks = tasks[:]

            # ── 内层循环：同一账号的多次重试 ──
            for attempt in range(1, max_attempts + 1):

                log_node(f"账号 {acct_idx + 1} 第 {attempt}/{max_attempts} 次尝试：启动浏览器",
                         level="START", account=acct_masked,
                         task_count=len(pending_tasks), captured=captured)

                # 每次尝试都重新启动浏览器（干净状态）
                browser, context, page, session = await _launch_browser(pw)

                # 指定使用当前账号登录（覆盖 session 中的默认账号）
                session.set_single_account(acct, pwd)

                # ── 步骤1：登录 echotik 网站 ──
                try:
                    await session.ensure_login(page)
                except RuntimeError as e:
                    # 登录失败（验证码错误、账号被封等），放弃当前账号，换下一个
                    log_node("登录失败，尝试下一个账号",
                             level="WARN", account=acct_masked, error=str(e)[:80])
                    await browser.close()
                    break  # 跳出 attempt 循环，进入下一个账号

                # ── 步骤2：执行数据下载 ──
                try:
                    # 调用下载回调函数，传入详细任务列表（V2 使用 tasks 参数而非 wins）
                    results = await download_fn(
                        tasks=pending_tasks,
                        captured=captured,
                        session=session,
                        page=page,
                        account=acct,
                    )
                except SubscriptionExpiredError as e:
                    # 账号订阅已过期，无法继续使用，换下一个账号
                    log_node("账号订阅到期，自动切换下一个账号",
                             level="WARN", account=acct_masked)
                    await browser.close()
                    break  # 跳出 attempt 循环，进入下一个账号

                # ── 步骤3：关闭浏览器，统计本次结果 ──
                await browser.close()
                log_node("浏览器已关闭", level="INFO",
                         account=acct_masked, attempt=attempt)

                # 按状态分类下载结果
                # success: 下载成功的任务
                # stale:   数据过期/未更新的任务（网站数据还没刷新）
                # failed:  下载失败的任务（网络错误、页面异常等）
                success_list = [r for r in results if r.status == "success"]
                stale_list   = [r for r in results if r.status == "stale"]
                failed_list  = [r for r in results if r.status == "failed"]

                log_node(f"账号 {acct_idx + 1} 第 {attempt} 次尝试结果",
                         level="INFO",
                         account=acct_masked,
                         success=len(success_list),
                         stale=len(stale_list),
                         failed=len(failed_list))

                # 将成功的结果立即路由（分发到目标目录）
                if success_list:
                    route_fn(success_list)
                    # 累积到跨账号的成功列表中
                    all_routed.extend(success_list)

                # ── 判断：全部成功 → 执行后处理并通知 ──
                if not stale_list and not failed_list:
                    # 所有任务都成功了，执行后处理管道（数据清洗、入库等）
                    if pipeline_fn: pipeline_fn(captured)
                    # 构建成功任务的描述信息（包含品类），用于通知消息
                    success_desc = []
                    for r in all_routed:
                        # 如果有品类信息则附加到描述中，如 "product_rank/d/美妆"
                        cat_label = f"/{r.category}" if r.category else ""
                        success_desc.append(f"{r.module}/{r.win}{cat_label}")
                    # 发送成功通知
                    notify_success(
                        captured=captured,
                        success=success_desc,
                        attempt=attempt,
                    )
                    return True

                # ── 判断：还有重试机会（同一账号内） ──
                if attempt < max_attempts:
                    # 从失败和过期的结果中重新构建待重试任务列表
                    # V2 的优势：精确到 module+win+category 粒度重试
                    failed_tasks = []
                    for r in stale_list + failed_list:
                        failed_tasks.append({
                            "module": r.module,
                            "win": r.win,
                            "category": r.category,
                        })
                    log_node(
                        f"部分任务未完成，同账号第 {attempt + 1} 次重试",
                        level="WARN",
                        account=acct_masked,
                        failed_count=len(failed_tasks),
                    )
                    # 用失败任务列表替换待处理列表，下次只重试这些
                    pending_tasks = failed_tasks
                    continue  # 继续下一次 attempt

                # ── 判断：当前账号重试次数耗尽，换下一个账号 ──
                log_node(f"账号 {acct_masked} 重试 {max_attempts} 次仍失败，尝试下一个账号",
                         level="WARN",
                         stale=len(stale_list),
                         failed=len(failed_list))
                break  # 跳出 attempt 循环，进入下一个账号

        # ── 所有账号都已用完，仍有任务未完成 → 发送失败报警 ──
        log_node("所有账号均已尝试，发送失败通知", level="ERROR")
        notify_final_failure(
            captured=captured,
            stale=[],
            failed=[f"全部 {total_accounts} 个账号均失败"],
        )

        return False