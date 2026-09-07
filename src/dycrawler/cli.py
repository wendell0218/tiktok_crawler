import argparse
import asyncio
import json
import sys
from pathlib import Path

from .browser import BrowserRisk, login, refresh, search
from .database import database_status, export_jsonl, load_records, recent_events, save_event, save_records
from .downloader import download_many


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = ROOT / "data" / "douyin.db"
DEFAULT_PROFILE = ROOT / "data" / "browser_profile"
DEFAULT_EXPORT = ROOT / "data" / "metadata.jsonl"
DEFAULT_DOWNLOADS = ROOT / "downloads"


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def add_database(parser):
    parser.add_argument("--database", default=str(DEFAULT_DATABASE), help="元数据数据库路径")


def add_browser(parser):
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE), help="浏览器用户数据目录")
    parser.add_argument("--channel", default="chrome", choices=("chrome", "chromium"), help="浏览器类型，默认 chrome")
    parser.add_argument("--headless", action="store_true", help="不显示浏览器窗口")
    parser.add_argument("--response-timeout", type=float, default=20, help="响应等待秒数，默认 20")


def add_selection(parser, default_limit):
    parser.add_argument("--keyword", default="", help="按已保存的搜索关键词筛选")
    parser.add_argument("--aweme-id", action="append", default=[], help="指定视频 ID，可重复提供")
    parser.add_argument("--limit", type=int, default=default_limit, help=f"最多处理的记录数，默认 {default_limit}，0 表示不限")


def parser_for_cli():
    parser = argparse.ArgumentParser(prog="dycrawler", description="抖音视频采集辅助工具")
    commands = parser.add_subparsers(dest="command", required=True)

    login_parser = commands.add_parser("login", help="打开浏览器登录")
    login_parser.add_argument("--profile", default=str(DEFAULT_PROFILE), help="浏览器用户数据目录")
    login_parser.add_argument("--channel", default="chrome", choices=("chrome", "chromium"), help="浏览器类型，默认 chrome")
    login_parser.add_argument("--timeout", type=float, default=600, help="登录等待秒数，默认 600")

    search_parser = commands.add_parser("search", help="按关键词采集元数据")
    search_parser.add_argument("keyword", help="搜索关键词")
    add_database(search_parser)
    add_browser(search_parser)
    search_parser.add_argument("--max-items", type=int, default=100, help="最多检查的视频数，默认 100")
    search_parser.add_argument("--max-pages", type=int, default=20, help="最多读取的页数，默认 20")
    search_parser.add_argument("--scroll-delay", type=float, default=1.5, help="翻页间隔秒数，默认 1.5")
    search_parser.add_argument("--allow-guest", action="store_true", help="允许在未登录状态下搜索")
    search_parser.add_argument("--export", default="", help="同时导出元数据到指定 JSONL 文件")

    refresh_parser = commands.add_parser("refresh", help="刷新已保存的视频信息和链接")
    add_database(refresh_parser)
    add_browser(refresh_parser)
    add_selection(refresh_parser, 20)

    list_parser = commands.add_parser("list", help="查看已保存的元数据")
    add_database(list_parser)
    add_selection(list_parser, 20)

    export_parser = commands.add_parser("export", help="导出元数据")
    add_database(export_parser)
    add_selection(export_parser, 0)
    export_parser.add_argument("--output", default=str(DEFAULT_EXPORT), help="导出的 JSONL 文件路径")

    for name in ("download", "probe"):
        media_parser = commands.add_parser(name, help="下载视频" if name == "download" else "小流量检查视频链接")
        add_database(media_parser)
        add_selection(media_parser, 20)
        media_parser.add_argument("--output", default=str(DEFAULT_DOWNLOADS), help="视频输出目录")
        media_parser.add_argument("--quality", default="best", help="清晰度：best、worst 或分辨率，例如 1080p")
        media_parser.add_argument("--codec", default="any", choices=("any", "h264", "h265", "unknown"), help="视频编码，默认 any 表示不限")
        media_parser.add_argument("--fallback", default="error", choices=("error", "lower"), help="清晰度不足时的策略：error 报错，lower 使用较低清晰度")
        media_parser.add_argument("--delay", type=float, default=1, help="下载间隔秒数，默认 1")
        media_parser.add_argument("--timeout", type=float, default=300, help="下载超时秒数，默认 300")
        media_parser.add_argument("--max-mib", type=float, default=0, help="单视频大小上限，单位 MiB，默认 0 表示不限")
        if name == "download":
            media_parser.add_argument("--delete-after", action="store_true", help="下载完成后删除视频文件")
        else:
            media_parser.add_argument("--probe-bytes", type=int, default=65536, help="每个链接检查的字节数，默认 65536")

    status_parser = commands.add_parser("status", help="查看数据库状态")
    add_database(status_parser)

    events_parser = commands.add_parser("events", help="查看最近的采集事件")
    add_database(events_parser)
    events_parser.add_argument("--limit", type=int, default=20, help="显示的事件数，默认 20")
    return parser


async def run_browser_command(args):
    if args.command == "login":
        result = await login(args.profile, channel=args.channel, timeout_ms=int(args.timeout * 1000))
        emit(result)
        return 0
    if args.command == "search":
        records, pages = await search(
            args.keyword,
            args.profile,
            max_items=args.max_items,
            max_pages=args.max_pages,
            scroll_delay=args.scroll_delay,
            response_timeout=args.response_timeout,
            channel=args.channel,
            headless=args.headless,
            allow_guest=args.allow_guest,
        )
        save_records(args.database, records, keyword=args.keyword)
        exported = 0
        if args.export:
            exported = export_jsonl(args.database, args.export, keyword=args.keyword)
        emit({"saved": len(records), "exported": exported, "pages": pages})
        return 0
    records = load_records(
        args.database,
        keyword=args.keyword,
        aweme_ids=args.aweme_id,
        limit=args.limit,
    )
    if not records:
        emit({"refreshed": 0, "failed": [], "detail": "没有匹配的元数据"})
        return 1
    refreshed, failures = await refresh(
        records,
        args.profile,
        channel=args.channel,
        headless=args.headless,
        response_timeout=args.response_timeout,
    )
    save_records(args.database, refreshed)
    for failure in failures:
        save_event(args.database, "refresh", "refresh_failed", "warning", failure)
    emit({"requested": len(records), "refreshed": len(refreshed), "failed": failures})
    return 0 if not failures else 3


async def run_media_command(args):
    records = load_records(
        args.database,
        keyword=args.keyword,
        aweme_ids=args.aweme_id,
        limit=args.limit,
    )
    if not records:
        emit({"processed": 0, "detail": "没有匹配的元数据"})
        return 1
    probe_bytes = args.probe_bytes if args.command == "probe" else 0
    delete_after = args.delete_after if args.command == "download" else False
    results = await download_many(
        records,
        args.output,
        quality=args.quality,
        codec=args.codec,
        fallback=args.fallback,
        delay=args.delay,
        timeout=args.timeout,
        probe_bytes=probe_bytes,
        max_bytes=int(args.max_mib * 1048576),
        delete_after=delete_after,
    )
    failures = [item for item in results if item["status"] == "failed"]
    for failure in failures:
        severity = "critical" if failure.get("http_status") == 429 else "warning"
        save_event(args.database, args.command, failure["category"], severity, failure)
    payload = {
        "processed": len(results),
        "succeeded": len(results) - len(failures),
        "failed": len(failures),
        "results": results,
    }
    if any(item.get("http_status") == 403 for item in failures):
        payload["next_action"] = "请先运行 refresh 更新过期或被拒绝的视频链接，再重试下载"
    if any(item.get("http_status") == 429 for item in failures):
        payload["next_action"] = "请停止请求，稍后再试"
    emit(payload)
    return 0 if not failures else 3


def main():
    parser = parser_for_cli()
    args = parser.parse_args()
    try:
        if args.command in {"login", "search", "refresh"}:
            return_code = asyncio.run(run_browser_command(args))
        elif args.command in {"download", "probe"}:
            return_code = asyncio.run(run_media_command(args))
        elif args.command == "list":
            emit(load_records(args.database, keyword=args.keyword, aweme_ids=args.aweme_id, limit=args.limit))
            return_code = 0
        elif args.command == "export":
            count = export_jsonl(
                args.database,
                args.output,
                keyword=args.keyword,
                aweme_ids=args.aweme_id,
                limit=args.limit,
            )
            emit({"exported": count, "output": str(Path(args.output).expanduser().resolve())})
            return_code = 0
        elif args.command == "events":
            emit(recent_events(args.database, args.limit))
            return_code = 0
        else:
            emit(database_status(args.database))
            return_code = 0
    except BrowserRisk as exc:
        database = getattr(args, "database", str(DEFAULT_DATABASE))
        save_event(database, args.command, exc.category, "critical", {"status": exc.status, "detail": exc.detail})
        emit({"status": "stopped", "category": exc.category, "http_status": exc.status, "detail": exc.detail})
        return_code = 2
    except KeyboardInterrupt:
        emit({"status": "stopped", "detail": "用户已中断"})
        return_code = 130
    sys.exit(return_code)
