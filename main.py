#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py
Echotik 自动采集器主入口

使用方式：
    # 自动模式（默认）：按 tasks.yaml 调度全采集
    python main.py
    python main.py --captured 2026-03-01

    # 手动模式：精确指定采集项（格式：模块:粒度:品类）
    python main.py --tasks "新品榜:d:Pet Supplies"
    python main.py --tasks "新品榜:d:,新品榜:d:Pet Supplies"
    python main.py --tasks "商品榜:d:Pet Supplies,小店榜:d:Pet Supplies"

    # 跳过 pipeline/analyst（测试时使用）
    python main.py --tasks "新品榜:d:Pet Supplies" --no-pipeline

    # 演练模式（不实际下载）
    python main.py --dry-run
    python main.py --tasks "新品榜:d:Pet Supplies" --dry-run

定时任务（crontab）：
    # 直接使用 conda env 的完整 python 路径，无需 conda activate
    30 7 * * * /home/ubuntu/miniconda3/envs/echotik_exporter/bin/python /path/to/main.py >> logs/cron.log 2>&1
"""

# ============================================================
# 标准库导入
# ============================================================
import argparse          # 命令行参数解析模块
import asyncio           # 异步 IO 模块，用于运行 async main()
import os                # 操作系统接口，用于读写环境变量、文件路径等
import sys               # 系统相关功能，用于获取 Python 解释器路径和退出码
from datetime import date  # 日期类，用于获取今天的日期作为默认采集日期
from pathlib import Path   # 面向对象的文件路径操作（本文件中未直接使用，但保留以备扩展）

# ============================================================
# 第三方库导入
# ============================================================
import yaml              # YAML 解析库（本文件中未直接使用，由 scheduler 内部使用）
from dotenv import load_dotenv  # 从 .env 文件加载环境变量到 os.environ

# ============================================================
# 项目内部模块导入
# ============================================================
from browser.downloader import download_all_v2          # 浏览器自动化下载函数，负责实际的页面抓取和文件下载
from file_router import route_files                      # 文件路由器，将下载的原始文件分发到对应目录
from pipeline_runner import run_pipeline                 # 数据处理管道，对下载的数据进行清洗、转换、分析
from scheduler.trigger import get_detailed_tasks_for_today, run_with_retry_v2  # 调度器：获取今日任务列表、带重试的任务执行器
from utils.logger import log_node, setup_logger          # 日志工具：结构化日志输出、日志初始化
from utils.events import init_session, write_event, STAGE_SESSION_START, STAGE_SESSION_END  # 结构化事件日志

# 加载 .env 文件中的环境变量（如 PROXY_PORT、PLAYWRIGHT_PROXY 等配置项）
# 必须在使用 os.getenv() 读取这些变量之前调用
load_dotenv()


def proxy_on():
    """
    自动设置 HTTP/HTTPS 代理，等价于在终端手动执行 proxy_on 命令。

    本函数专为 WSL2 环境设计：自动探测 Windows 宿主机 IP，并将代理指向
    宿主机上运行的 v2rayN 代理软件。这样 Playwright 浏览器和其他网络请求
    都能通过代理访问外网。

    代理设置的优先级（从高到低）：
      1. 终端已 export https_proxy（用户手动执行过 proxy_on）→ 直接复用，不覆盖
      2. .env 中已配置 PLAYWRIGHT_PROXY（写死完整代理地址）→ 直接复用，不覆盖
      3. 自动探测模式：
         - 方法1：从 /proc/net/route 读取默认网关 IP（最准确）
         - 方法2：从 /etc/resolv.conf 读取 nameserver IP（兜底方案）
         - 检测代理端口是否可达
         - 设置全部代理环境变量
      4. 端口不可达 → 打印警告，提示用户确认 v2rayN 已运行并开启局域网访问
    """
    # 导入 socket 模块，用于 TCP 端口可达性检测
    import socket

    # ---- 优先级1：检查终端是否已经手动设置了代理环境变量 ----
    # 如果用户在启动脚本前已经手动执行了 proxy_on，则无需重复设置
    if os.getenv("https_proxy") or os.getenv("HTTPS_PROXY"):
        log_node("检测到已有代理环境变量，跳过自动设置", level="INFO",
                 proxy=os.getenv("https_proxy") or os.getenv("HTTPS_PROXY"))
        return

    # ---- 优先级2：检查 .env 文件中是否已写死了完整的代理地址 ----
    # 如果 .env 中配置了 PLAYWRIGHT_PROXY=http://x.x.x.x:port，则直接使用
    if os.getenv("PLAYWRIGHT_PROXY"):
        log_node("使用 .env 中的 PLAYWRIGHT_PROXY", level="INFO",
                 proxy=os.getenv("PLAYWRIGHT_PROXY"))
        return

    # ---- 读取代理端口号 ----
    # 从 .env 的 PROXY_PORT 读取，默认值 10808（v2rayN 的默认 SOCKS/HTTP 端口）
    port_str = os.getenv("PROXY_PORT", "10808").strip()
    try:
        port = int(port_str)  # 将端口字符串转为整数
    except ValueError:
        # 端口格式错误（如包含字母），记录警告并退出
        log_node("PROXY_PORT 格式错误，应为纯数字", level="WARN", value=port_str)
        return

    # ---- 优先级3：自动探测 Windows 宿主机 IP ----

    # 方法1：从 /proc/net/route 读取默认网关 IP（WSL2 中最准确的方式）
    # /proc/net/route 中，Destination 为 00000000 的行即为默认路由，
    # 其 Gateway 字段（十六进制小端序）就是 Windows 宿主机的 IP
    host_ip = ""
    try:
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.strip().split()
                # 查找默认路由行：Destination == 00000000 且 Gateway != 00000000
                if (len(parts) >= 3
                        and parts[1] == "00000000"
                        and parts[2] != "00000000"):
                    gw_hex = parts[2]  # 网关 IP 的十六进制表示（小端序）
                    # 将小端序十六进制转换为点分十进制 IP 地址
                    # 例如 "0101A8C0" -> 192.168.1.1（从右往左每两位一组转换）
                    host_ip = ".".join(
                        str(int(gw_hex[i:i+2], 16)) for i in (6, 4, 2, 0)
                    )
                    break  # 找到第一条默认路由即可
    except Exception:
        # 读取失败（文件不存在或权限不足），静默跳过，尝试方法2
        pass

    # 方法2：从 /etc/resolv.conf 读取 nameserver IP（兜底方案）
    # WSL2 中 /etc/resolv.conf 的 nameserver 通常指向 Windows 宿主机
    if not host_ip:
        try:
            with open("/etc/resolv.conf") as f:
                for line in f:
                    p = line.strip().split()
                    # 找到 "nameserver x.x.x.x" 行，取 IP 地址部分
                    if p and p[0] == "nameserver":
                        host_ip = p[1]
                        break  # 取第一个 nameserver 即可
        except Exception:
            # 读取失败，静默跳过
            pass

    # 如果两种方法都无法获取宿主机 IP，记录警告并退出
    if not host_ip:
        log_node("无法获取 Windows 宿主机 IP，跳过代理设置", level="WARN",
                 hint="请手动运行 proxy_on 后再启动脚本")
        return

    # ---- 检测代理端口是否可达 ----
    # 尝试建立 TCP 连接到 host_ip:port，超时 2 秒
    # 如果连接失败，说明 v2rayN 未运行或未开启局域网访问
    try:
        with socket.create_connection((host_ip, port), timeout=2):
            pass  # 连接成功即可，立即关闭
    except OSError:
        # 端口不可达，记录警告并退出（不设置代理，避免后续请求全部超时）
        log_node("代理端口不可达，请确认 v2rayN 正在运行并已开启「允许局域网」",
                 level="WARN", windows_ip=host_ip, port=port)
        return

    # ---- 设置全部代理环境变量 ----
    # 构造代理 URL，格式为 http://宿主机IP:端口
    proxy_url = f"http://{host_ip}:{port}"
    # 同时设置大写和小写版本的环境变量，确保所有库都能识别
    # HTTP_PROXY / http_proxy：HTTP 请求代理
    # HTTPS_PROXY / https_proxy：HTTPS 请求代理
    # ALL_PROXY / all_proxy：所有协议的代理
    # PLAYWRIGHT_PROXY：Playwright 浏览器专用的代理配置
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "PLAYWRIGHT_PROXY"):
        os.environ[key] = proxy_url

    # 设置不走代理的地址列表（本地地址不需要代理）
    no_proxy = "localhost,127.0.0.1,::1,.local"
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy

    # 记录代理设置成功的日志
    log_node("代理已自动设置（等价于 proxy_on）",
             level="INFO", windows_ip=host_ip, port=port, proxy=proxy_url)


def parse_tasks_arg(tasks_str: str) -> list[dict]:
    """
    解析 --tasks 命令行参数，将用户输入的任务字符串转换为结构化的任务列表。

    参数：
        tasks_str (str): 用户通过 --tasks 传入的任务字符串。
                         格式为 "模块:粒度:品类"，多个任务用英文逗号分隔。
                         品类部分可以为空，表示采集全品类。

    返回：
        list[dict]: 解析后的任务列表，每个任务是一个字典，包含：
                    - module (str): 模块名称，如 "新品榜"、"商品榜"、"小店榜"
                    - win (str): 时间粒度，d=日榜、w=周榜、m=月榜
                    - category (str): 品类名称，空字符串表示全品类

    示例：
        "新品榜:d:Pet Supplies"           -> [{"module": "新品榜", "win": "d", "category": "Pet Supplies"}]
        "新品榜:d:,新品榜:d:Pet Supplies" -> [{"module": "新品榜", "win": "d", "category": ""}, ...]
    """
    tasks = []  # 存放解析结果的列表

    # 按逗号分割多个任务项，逐一解析
    for item in tasks_str.split(","):
        item = item.strip()  # 去除首尾空白
        if not item:
            continue  # 跳过空项（如尾部多余的逗号导致的空字符串）

        # 按冒号分割为 [模块, 粒度, 品类] 三部分
        parts = item.split(":")

        # 至少需要 模块 和 粒度 两部分，否则格式错误
        if len(parts) < 2:
            log_node(f"任务格式错误，跳过: {item}", level="WARN",
                     hint="格式应为 模块:粒度:品类")
            continue

        module = parts[0].strip()    # 模块名称（如 "新品榜"）
        win = parts[1].strip()       # 时间粒度（d/w/m）
        # 品类部分可选，不提供时默认为空字符串（表示全品类）
        category = parts[2].strip() if len(parts) > 2 else ""

        # 校验粒度值是否合法，只允许 d（日）、w（周）、m（月）
        if win not in ("d", "w", "m"):
            log_node(f"粒度无效，跳过: {item}", level="WARN",
                     hint="粒度应为 d/w/m")
            continue

        # 校验通过，加入任务列表
        tasks.append({"module": module, "win": win, "category": category})

    return tasks


def parse_args():
    """
    解析命令行参数，定义本脚本支持的所有 CLI 选项。

    返回：
        argparse.Namespace: 解析后的参数对象，包含以下属性：
            - captured (str): 采集日期，格式 YYYY-MM-DD，默认为今天
            - tasks (str | None): 手动模式的任务字符串，None 表示使用自动模式
            - no_pipeline (bool): 是否跳过 pipeline 数据处理步骤
            - dry_run (bool): 是否为演练模式（只打印任务，不实际执行）
    """
    # 创建参数解析器，设置程序描述信息
    parser = argparse.ArgumentParser(description="Echotik 自动采集器")

    # --captured：指定采集日期，影响数据的存储路径和文件名
    parser.add_argument(
        "--captured",
        type=str,
        default=date.today().isoformat(),  # 默认值为今天的日期（ISO 格式：YYYY-MM-DD）
        help="采集日期 YYYY-MM-DD（默认今天）",
    )

    # --tasks：手动模式，精确指定要采集的任务项
    # 不传此参数时进入自动模式，由 tasks.yaml 配置文件决定今日任务
    parser.add_argument(
        "--tasks",
        type=str,
        default=None,  # 默认 None，表示自动模式
        help="手动模式：指定采集项，格式 模块:粒度:品类，多项逗号分隔（如 '新品榜:d:Pet Supplies'）",
    )

    # --no-pipeline：跳过数据处理管道，仅下载不处理
    # 适用于调试下载功能时，避免触发后续的数据清洗和分析流程
    parser.add_argument(
        "--no-pipeline",
        action="store_true",  # 布尔开关，传了就是 True，不传就是 False
        help="跳过 pipeline 和 analyst（测试时使用）",
    )

    # --dry-run：演练模式，只打印将要执行的任务列表，不实际下载
    # 适用于确认任务配置是否正确
    parser.add_argument(
        "--dry-run",
        action="store_true",  # 布尔开关
        help="演练模式：打印将要执行的任务，不实际下载",
    )

    # 解析并返回命令行参数
    return parser.parse_args()


async def main():
    """
    主函数（异步），整个采集流程的入口点。

    执行流程：
      1. 解析命令行参数
      2. 初始化日志系统
      3. 自动设置代理（WSL2 环境下连接 Windows 宿主机的 v2rayN）
      4. 确定本次采集的任务列表（手动模式 或 自动模式）
      5. 如果是演练模式，打印任务后退出
      6. 正式执行：调用 run_with_retry_v2 进行带重试的下载、路由、管道处理
      7. 根据执行结果输出成功/失败日志，失败时以非零退出码退出
    """
    # ---- 步骤1：解析命令行参数 ----
    args = parse_args()
    captured = args.captured        # 采集日期字符串，如 "2026-03-19"
    dry_run = args.dry_run          # 是否为演练模式
    no_pipeline = args.no_pipeline  # 是否跳过 pipeline

    # ---- 步骤2：初始化日志系统 ----
    # 根据采集日期创建日志文件，确保日志按日期归档
    setup_logger(captured)
    init_session(captured)

    # ---- 步骤3：自动设置代理 ----
    # 必须在浏览器启动前完成，否则 Playwright 无法通过代理访问外网
    proxy_on()

    # 记录启动日志，包含关键运行参数，便于事后排查
    log_node(
        "Echotik 自动采集器启动",
        level="START",
        captured=captured,
        dry_run=dry_run,
        python=sys.executable,  # 记录当前使用的 Python 解释器路径，便于确认 conda 环境
    )
    write_event(STAGE_SESSION_START, "SUCCESS", context={"captured": captured, "dry_run": dry_run})

    # ---- 步骤4：确定本次需要下载的任务列表 ----
    if args.tasks:
        # 手动模式：用户通过 --tasks 参数精确指定了采集项
        tasks = parse_tasks_arg(args.tasks)
        if not tasks:
            # 解析后没有有效任务（全部格式错误），记录错误并退出
            log_node("没有有效的任务", level="ERROR")
            return
        # 打印每个任务的详细信息，方便用户确认
        log_node("手动模式", level="INFO", tasks_count=len(tasks))
        for t in tasks:
            cat_label = t["category"] if t["category"] else "全品类"  # 空品类显示为"全品类"
            log_node("任务项", level="INFO", module=t["module"], win=t["win"], category=cat_label)
    else:
        # 自动模式：根据 tasks.yaml 配置和当前日期，自动计算今天应该执行哪些任务
        tasks = get_detailed_tasks_for_today(captured)
        if not tasks:
            # 今天没有需要执行的任务（可能是非采集日），正常退出
            log_node("今天没有需要执行的任务", level="INFO")
            return
        log_node("自动模式", level="INFO", tasks_count=len(tasks))

    # ---- 步骤5：演练模式处理 ----
    # 如果是 dry-run，只打印任务列表，不实际执行下载
    if dry_run:
        for t in tasks:
            cat_label = t["category"] if t["category"] else "全品类"
            log_node("[DRY-RUN] 将下载", level="INFO",
                     module=t["module"], win=t["win"], category=cat_label)
        log_node("演练完成，退出", level="DONE")
        return  # 演练模式到此结束，不执行后续的实际下载

    # ---- 步骤6：正式执行采集任务 ----
    # 如果指定了 --no-pipeline，则不传入 pipeline 函数，跳过数据处理步骤
    pipeline_fn = None if no_pipeline else run_pipeline

    # run_with_retry_v2：带重试机制的任务执行器
    # 参数说明：
    #   tasks: 本次要执行的任务列表
    #   captured: 采集日期，用于数据存储路径
    #   download_fn: 下载函数，负责通过浏览器自动化抓取数据
    #   route_fn: 路由函数，将下载的文件分发到正确的目录
    #   pipeline_fn: 管道函数，对数据进行清洗和分析（可为 None 表示跳过）
    success = await run_with_retry_v2(
        tasks=tasks,
        captured=captured,
        download_fn=download_all_v2,
        route_fn=route_files,
        pipeline_fn=pipeline_fn,
    )

    # ---- 步骤7：输出最终结果 ----
    if success:
        # 所有任务执行成功
        log_node("本日采集全部完成", level="DONE", captured=captured)
        write_event(STAGE_SESSION_END, "SUCCESS", context={"captured": captured})
    else:
        # 存在失败的任务，记录错误日志（run_with_retry_v2 内部已发送报警通知）
        log_node("本日采集存在失败，已发送报警通知", level="ERROR", captured=captured)
        write_event(STAGE_SESSION_END, "FAILED", context={"captured": captured}, detail="存在失败任务")
        sys.exit(1)  # 以非零退出码退出，便于 crontab 等调度系统检测失败


# ============================================================
# 脚本入口点
# ============================================================
# 当直接运行本文件时（而非被 import），启动异步主函数
# asyncio.run() 会创建事件循环并运行 main() 协程直到完成
if __name__ == "__main__":
    asyncio.run(main())
