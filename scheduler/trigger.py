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

import os
from datetime import date
from typing import Callable

from playwright.async_api import async_playwright

from browser.session import BrowserSession
from utils.logger import log_node
from utils.notifier import notify_final_failure, notify_success


def _get_win_host_ip() -> str:
    """
    动态获取 Windows 宿主机 IP（移植自 bashrc 的 wsl_win_host_ip）

    方法1：从 /proc/net/route 读取默认网关（eth0 的 gateway）
    方法2：兜底从 /etc/resolv.conf 读取 nameserver
    WSL2 每次重启 IP 都会变，必须动态获取。
    """
    import struct
    # 方法1：/proc/net/route
    try:
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 3 and parts[0] == "eth0" and parts[2] == "00000000":
                    # gateway 是小端序的十六进制，转换为 IP
                    gw_hex = parts[2]
                    # 实际 gateway 在第3列（index 2），但默认路由的gateway在第3列
                    # 重新读：iface=parts[0], dest=parts[1], gateway=parts[2]
                    # 默认路由是 dest=00000000
                    gw_hex = parts[2]
                    ip = ".".join(str(int(gw_hex[i:i+2], 16))
                                  for i in (6, 4, 2, 0))
                    if ip != "0.0.0.0":
                        return ip
        # 再找一次：dest=00000000 的行，gateway 不为 00000000
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.strip().split()
                if (len(parts) >= 3
                        and parts[1] == "00000000"
                        and parts[2] != "00000000"):
                    gw_hex = parts[2]
                    ip = ".".join(str(int(gw_hex[i:i+2], 16))
                                  for i in (6, 4, 2, 0))
                    return ip
    except Exception:
        pass

    # 方法2：/etc/resolv.conf nameserver
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                parts = line.strip().split()
                if parts and parts[0] == "nameserver":
                    return parts[1]
    except Exception:
        pass

    return ""


def _proxy_tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    """检测 host:port 是否可达（移植自 _proxy_tcp_ok）"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _get_proxy_settings() -> dict | None:
    """
    动态构建代理配置，传给 Playwright 浏览器。

    优先级：
      1. .env / 环境变量中的 PLAYWRIGHT_PROXY（明确指定，直接使用）
      2. 自动探测：动态获取 Windows 宿主机 IP + .env 中的 PROXY_PORT
         （移植自 bashrc 的 proxy_on 逻辑，适配 WSL2 动态 IP）
      3. 都没有 → 返回 None（浏览器直连）

    .env 配置示例（二选一）：
      # 方式A：完整地址（优先）
      PLAYWRIGHT_PROXY=http://172.26.96.1:10808

      # 方式B：只填端口，IP 自动获取（推荐，适应 WSL2 动态 IP）
      PROXY_PORT=10808
    """
    # 优先级1：完整代理地址（环境变量或 .env）
    proxy_url = (
        os.getenv("PLAYWRIGHT_PROXY", "").strip()
        or os.getenv("https_proxy",    "").strip()
        or os.getenv("HTTPS_PROXY",    "").strip()
        or os.getenv("http_proxy",     "").strip()
        or os.getenv("HTTP_PROXY",     "").strip()
    )
    if proxy_url:
        log_node("浏览器使用指定代理", level="INFO", proxy=proxy_url)
        return {"server": proxy_url}

    # 优先级2：自动探测 Windows 宿主机 IP + PROXY_PORT
    proxy_port_str = os.getenv("PROXY_PORT", "").strip()
    if proxy_port_str:
        try:
            proxy_port = int(proxy_port_str)
        except ValueError:
            log_node("PROXY_PORT 格式错误，应为纯数字",
                     level="WARN", value=proxy_port_str)
            return None

        host_ip = _get_win_host_ip()
        if not host_ip:
            log_node("无法获取 Windows 宿主机 IP，代理自动探测失败",
                     level="WARN",
                     hint="请在 .env 中直接设置 PLAYWRIGHT_PROXY=http://<IP>:<PORT>")
            return None

        if not _proxy_tcp_ok(host_ip, proxy_port):
            log_node("代理端口不可达，请确认 v2rayN 正在运行且已开启允许局域网",
                     level="WARN",
                     host=host_ip, port=proxy_port)
            return None

        proxy_url = f"http://{host_ip}:{proxy_port}"
        log_node("浏览器使用自动探测代理", level="INFO",
                 windows_ip=host_ip, port=proxy_port, proxy=proxy_url)
        return {"server": proxy_url}

    # 优先级3：无代理配置
    log_node("未检测到代理配置，浏览器直连",
             level="INFO",
             hint="如需代理请在 .env 中设置 PROXY_PORT=10808 或 PLAYWRIGHT_PROXY=http://IP:PORT")
    return None


def get_tasks_for_today(captured: str = None) -> list[str]:
    """
    根据日期决定本次执行哪些粒度（旧接口，保持兼容）

    规则：
        d（日榜）- 每天执行
        w（周榜）- 每周一/周二执行（周一商品榜，周二小店榜）
        m（月榜）- 每月1/2日执行（1号商品榜，2号小店榜）

    返回：
        粒度列表，如 ["d"] 或 ["d", "w"] 或 ["d", "m"]
    """
    d = date.fromisoformat(captured) if captured else date.today()
    wins = ["d"]
    if d.weekday() in (0, 1):   # 周一或周二
        wins.append("w")
    if d.day in (1, 2):         # 1号或2号
        wins.append("m")
    log_node("本日任务调度", level="INFO", date=str(d), wins=wins)
    return wins


def get_detailed_tasks_for_today(captured: str = None) -> list[dict]:
    """
    根据日期和 tasks.yaml 配置决定本次执行哪些任务

    返回：
        任务列表，每个任务包含 {module, win, category}
    """
    import yaml
    from pathlib import Path

    d = date.fromisoformat(captured) if captured else date.today()
    weekday = d.weekday()  # 0=周一, 1=周二, ...
    day_of_month = d.day

    # 加载配置
    config_path = Path(__file__).parent.parent / "config" / "tasks.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    categories = [c["name"] for c in config.get("categories", []) if c.get("enabled", True)]
    modules = {m["name"]: m for m in config.get("modules", [])}
    schedule = config.get("schedule", {})

    tasks = []

    # 日榜：每天采集
    daily_modules = schedule.get("daily", [])
    for module_name in daily_modules:
        if module_name not in modules:
            continue
        module = modules[module_name]
        if "d" not in module.get("wins", []):
            continue
        for category in categories:
            # 全品类或该模块支持品类筛选
            if category == "" or module.get("has_category_filter", False):
                tasks.append({
                    "module": module_name,
                    "win": "d",
                    "category": category,
                })

    # 周榜：根据星期几决定采集哪些模块
    weekly_schedule = schedule.get("weekly", {})
    weekday_map = {0: "monday", 1: "tuesday", 2: "wednesday", 3: "thursday",
                   4: "friday", 5: "saturday", 6: "sunday"}
    weekday_key = weekday_map.get(weekday, "")
    weekly_modules = weekly_schedule.get(weekday_key, [])
    for module_name in weekly_modules:
        if module_name not in modules:
            continue
        module = modules[module_name]
        if "w" not in module.get("wins", []):
            continue
        for category in categories:
            if category == "" or module.get("has_category_filter", False):
                tasks.append({
                    "module": module_name,
                    "win": "w",
                    "category": category,
                })

    # 月榜：根据日期决定采集哪些模块
    monthly_schedule = schedule.get("monthly", {})
    day_key = f"day_{day_of_month}"
    monthly_modules = monthly_schedule.get(day_key, [])
    for module_name in monthly_modules:
        if module_name not in modules:
            continue
        module = modules[module_name]
        if "m" not in module.get("wins", []):
            continue
        for category in categories:
            if category == "" or module.get("has_category_filter", False):
                tasks.append({
                    "module": module_name,
                    "win": "m",
                    "category": category,
                })

    log_node("本日详细任务调度", level="INFO", date=str(d), task_count=len(tasks))
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
    下载流程（多账号轮换 × 每账号 max_attempts 次重试）

    流程：
        账号A → 尝试1 → 尝试2 → 失败 → 换账号B → 尝试1 → 尝试2 → ...
        任一账号全部成功 → route → pipeline → 通知成功 → 结束
        所有账号都用完 → 通知失败 → 结束

    返回：
        True = 全部成功，False = 最终失败
    """
    # 检测账号变更并输出使用状态
    from utils.account_tracker import check_account_change, log_account_status, check_account_expiry
    check_account_change()
    log_account_status()
    check_account_expiry()

    proxy = _get_proxy_settings()

    # 读取账号列表
    accounts_str  = os.getenv("ECHOTIK_ACCOUNTS", "")
    passwords_str = os.getenv("ECHOTIK_PASSWORDS", "")
    accounts  = [a.strip().strip('"').strip("'")
                 for a in accounts_str.split(",") if a.strip()]
    passwords = [p.strip().strip('"').strip("'")
                 for p in passwords_str.split(",") if p.strip()]
    if not accounts:
        log_node("未配置账号", level="ERROR")
        return False

    total_accounts = len(accounts)

    async def _launch_browser(pw):
        """启动浏览器，返回 (browser, context, page, session)"""
        browser = await pw.chromium.launch(
            headless=True,
            proxy=proxy,
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page    = await context.new_page()
        session = BrowserSession(context)
        return browser, context, page, session

    async with async_playwright() as pw:
        all_routed = []  # 跨账号累积已成功路由的结果

        for acct_idx in range(total_accounts):
            acct = accounts[acct_idx]
            pwd  = passwords[acct_idx] if acct_idx < len(passwords) else ""
            acct_masked = acct[:3] + "***@" + acct.split("@")[-1] if "@" in acct else acct[:3] + "***"

            log_node(f"切换到账号 {acct_idx + 1}/{total_accounts}",
                     level="START", account=acct_masked)

            pending_wins = wins[:]

            for attempt in range(1, max_attempts + 1):

                log_node(f"账号 {acct_idx + 1} 第 {attempt}/{max_attempts} 次尝试：启动浏览器",
                         level="START", account=acct_masked,
                         tasks=pending_wins, captured=captured)

                browser, context, page, session = await _launch_browser(pw)

                # 指定使用当前账号登录
                session.set_single_account(acct, pwd)

                try:
                    await session.ensure_login(page)
                except RuntimeError as e:
                    log_node("登录失败，尝试下一个账号",
                             level="WARN", account=acct_masked, error=str(e)[:80])
                    await browser.close()
                    break  # 跳出 attempt 循环，换下一个账号

                results = await download_fn(
                    wins=pending_wins,
                    captured=captured,
                    session=session,
                    page=page,
                    module_filter=module_filter,
                )

                await browser.close()
                log_node("浏览器已关闭", level="INFO",
                         account=acct_masked, attempt=attempt)

                success_list = [r for r in results if r.status == "success"]
                stale_list   = [r for r in results if r.status == "stale"]
                failed_list  = [r for r in results if r.status == "failed"]

                log_node(f"账号 {acct_idx + 1} 第 {attempt} 次尝试结果",
                         level="INFO",
                         account=acct_masked,
                         success=len(success_list),
                         stale=len(stale_list),
                         failed=len(failed_list))

                if success_list:
                    route_fn(success_list)
                    all_routed.extend(success_list)

                # ── 全部成功 ──
                if not stale_list and not failed_list:
                    pipeline_fn(captured)
                    notify_success(
                        captured=captured,
                        success=[f"{r.module}/{r.win}" for r in all_routed],
                        attempt=attempt,
                    )
                    return True

                # ── 还有重试机会（同一账号）──
                if attempt < max_attempts:
                    log_node(
                        f"部分任务未完成，同账号第 {attempt + 1} 次重试",
                        level="WARN",
                        account=acct_masked,
                        stale=[f"{r.module}/{r.win}" for r in stale_list],
                        failed=[f"{r.module}/{r.win}" for r in failed_list],
                    )
                    pending_wins = list(set(
                        [r.win for r in stale_list] + [r.win for r in failed_list]
                    ))
                    continue

                # ── 当前账号重试耗尽，换下一个账号 ──
                log_node(f"账号 {acct_masked} 重试 {max_attempts} 次仍失败，尝试下一个账号",
                         level="WARN",
                         stale=[f"{r.module}/{r.win}" for r in stale_list],
                         failed=[f"{r.module}/{r.win}" for r in failed_list])
                break  # 跳出 attempt 循环

        # ── 所有账号都用完：通知失败 ──
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
    下载流程 V2（支持详细任务列表，包含品类筛选）

    参数：
        tasks: 任务列表，每个任务包含 {module, win, category}

    流程：
        账号A → 尝试1 → 尝试2 → 失败 → 换账号B → 尝试1 → 尝试2 → ...
        任一账号全部成功 → route → pipeline → 通知成功 → 结束
        所有账号都用完 → 通知失败 → 结束

    返回：
        True = 全部成功，False = 最终失败
    """
    # 检测账号变更并输出使用状态
    from utils.account_tracker import check_account_change, log_account_status, check_account_expiry
    check_account_change()
    log_account_status()
    check_account_expiry()

    proxy = _get_proxy_settings()

    # 读取账号列表
    accounts_str  = os.getenv("ECHOTIK_ACCOUNTS", "")
    passwords_str = os.getenv("ECHOTIK_PASSWORDS", "")
    accounts  = [a.strip().strip('"').strip("'")
                 for a in accounts_str.split(",") if a.strip()]
    passwords = [p.strip().strip('"').strip("'")
                 for p in passwords_str.split(",") if p.strip()]
    if not accounts:
        log_node("未配置账号", level="ERROR")
        return False

    total_accounts = len(accounts)

    async def _launch_browser(pw):
        """启动浏览器，返回 (browser, context, page, session)"""
        browser = await pw.chromium.launch(
            headless=True,
            proxy=proxy,
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page    = await context.new_page()
        session = BrowserSession(context)
        return browser, context, page, session

    async with async_playwright() as pw:
        all_routed = []  # 跨账号累积已成功路由的结果

        for acct_idx in range(total_accounts):
            acct = accounts[acct_idx]
            pwd  = passwords[acct_idx] if acct_idx < len(passwords) else ""
            acct_masked = acct[:3] + "***@" + acct.split("@")[-1] if "@" in acct else acct[:3] + "***"

            log_node(f"切换到账号 {acct_idx + 1}/{total_accounts}",
                     level="START", account=acct_masked)

            pending_tasks = tasks[:]

            for attempt in range(1, max_attempts + 1):

                log_node(f"账号 {acct_idx + 1} 第 {attempt}/{max_attempts} 次尝试：启动浏览器",
                         level="START", account=acct_masked,
                         task_count=len(pending_tasks), captured=captured)

                browser, context, page, session = await _launch_browser(pw)

                # 指定使用当前账号登录
                session.set_single_account(acct, pwd)

                try:
                    await session.ensure_login(page)
                except RuntimeError as e:
                    log_node("登录失败，尝试下一个账号",
                             level="WARN", account=acct_masked, error=str(e)[:80])
                    await browser.close()
                    break  # 跳出 attempt 循环，换下一个账号

                results = await download_fn(
                    tasks=pending_tasks,
                    captured=captured,
                    session=session,
                    page=page,
                )

                await browser.close()
                log_node("浏览器已关闭", level="INFO",
                         account=acct_masked, attempt=attempt)

                success_list = [r for r in results if r.status == "success"]
                stale_list   = [r for r in results if r.status == "stale"]
                failed_list  = [r for r in results if r.status == "failed"]

                log_node(f"账号 {acct_idx + 1} 第 {attempt} 次尝试结果",
                         level="INFO",
                         account=acct_masked,
                         success=len(success_list),
                         stale=len(stale_list),
                         failed=len(failed_list))

                if success_list:
                    route_fn(success_list)
                    all_routed.extend(success_list)

                # ── 全部成功 ──
                if not stale_list and not failed_list:
                    pipeline_fn(captured)
                    # 构建成功任务描述
                    success_desc = []
                    for r in all_routed:
                        cat_label = f"/{r.category}" if r.category else ""
                        success_desc.append(f"{r.module}/{r.win}{cat_label}")
                    notify_success(
                        captured=captured,
                        success=success_desc,
                        attempt=attempt,
                    )
                    return True

                # ── 还有重试机会（同一账号）──
                if attempt < max_attempts:
                    # 重新构建待重试任务列表
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
                    pending_tasks = failed_tasks
                    continue

                # ── 当前账号重试耗尽，换下一个账号 ──
                log_node(f"账号 {acct_masked} 重试 {max_attempts} 次仍失败，尝试下一个账号",
                         level="WARN",
                         stale=len(stale_list),
                         failed=len(failed_list))
                break  # 跳出 attempt 循环

        # ── 所有账号都用完：通知失败 ──
        log_node("所有账号均已尝试，发送失败通知", level="ERROR")
        notify_final_failure(
            captured=captured,
            stale=[],
            failed=[f"全部 {total_accounts} 个账号均失败"],
        )

        return False