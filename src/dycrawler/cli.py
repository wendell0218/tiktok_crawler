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
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))


def add_browser(parser):
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE))
    parser.add_argument("--channel", default="chrome", choices=("chrome", "chromium"))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--response-timeout", type=float, default=20)


def add_selection(parser, default_limit):
    parser.add_argument("--keyword", default="")
    parser.add_argument("--aweme-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=default_limit)


def parser_for_cli():
    parser = argparse.ArgumentParser(prog="dycrawler")
    commands = parser.add_subparsers(dest="command", required=True)

    login_parser = commands.add_parser("login")
    login_parser.add_argument("--profile", default=str(DEFAULT_PROFILE))
    login_parser.add_argument("--channel", default="chrome", choices=("chrome", "chromium"))
    login_parser.add_argument("--timeout", type=float, default=600)

    search_parser = commands.add_parser("search")
    search_parser.add_argument("keyword")
    add_database(search_parser)
    add_browser(search_parser)
    search_parser.add_argument("--max-items", type=int, default=100)
    search_parser.add_argument("--max-pages", type=int, default=20)
    search_parser.add_argument("--scroll-delay", type=float, default=1.5)
    search_parser.add_argument("--allow-guest", action="store_true")
    search_parser.add_argument("--export", default="")

    refresh_parser = commands.add_parser("refresh")
    add_database(refresh_parser)
    add_browser(refresh_parser)
    add_selection(refresh_parser, 20)

    list_parser = commands.add_parser("list")
    add_database(list_parser)
    add_selection(list_parser, 20)

    export_parser = commands.add_parser("export")
    add_database(export_parser)
    add_selection(export_parser, 0)
    export_parser.add_argument("--output", default=str(DEFAULT_EXPORT))

    for name in ("download", "probe"):
        media_parser = commands.add_parser(name)
        add_database(media_parser)
        add_selection(media_parser, 20)
        media_parser.add_argument("--output", default=str(DEFAULT_DOWNLOADS))
        media_parser.add_argument("--quality", default="best")
        media_parser.add_argument("--codec", default="any", choices=("any", "h264", "h265", "unknown"))
        media_parser.add_argument("--fallback", default="error", choices=("error", "lower"))
        media_parser.add_argument("--delay", type=float, default=1)
        media_parser.add_argument("--timeout", type=float, default=300)
        media_parser.add_argument("--max-mib", type=float, default=0)
        if name == "download":
            media_parser.add_argument("--delete-after", action="store_true")
        else:
            media_parser.add_argument("--probe-bytes", type=int, default=65536)

    status_parser = commands.add_parser("status")
    add_database(status_parser)

    events_parser = commands.add_parser("events")
    add_database(events_parser)
    events_parser.add_argument("--limit", type=int, default=20)
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
        emit({"refreshed": 0, "failed": [], "detail": "no matching metadata"})
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
        emit({"processed": 0, "detail": "no matching metadata"})
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
        payload["next_action"] = "run refresh before retrying expired or rejected media URLs"
    if any(item.get("http_status") == 429 for item in failures):
        payload["next_action"] = "stop requests and retry later"
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
        emit({"status": "stopped", "detail": "interrupted"})
        return_code = 130
    sys.exit(return_code)
