import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import Error as PlaywrightError

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from dycrawler.options import nonnegative_number, positive_integer, quality_value
from dycrawler.browser import BrowserRisk, close_search_session, open_search_session
from dycrawler.creator import resolve_user, user_posts
from dycrawler.database import save_event, save_records
from dycrawler.downloader import download_many
from dycrawler.normalize import PayloadError


async def run(user, workers=1, count=0, output_dir="downloads/users", quality="best", page_delay=5):
    started = time.monotonic()
    output_root = Path(output_dir).expanduser()
    if not output_root.is_absolute():
        output_root = ROOT / output_root
    database = ROOT / "data/douyin.db"
    summary = {"status": "running", "input": user, "workers": workers, "count": count,
               "downloaded": 0, "existing": 0, "failed": 0, "skipped": 0, "videos": 0, "pages": [],
               "successful_bytes": 0, "download_seconds": 0}
    output = None
    videos = None
    metadata = []
    session = None

    def save_summary():
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        temporary = output / "summary.json.part"
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output / "summary.json")

    async def on_page(batch, page):
        metadata.extend(batch)
        save_records(database, batch)
        temporary = output / "metadata.jsonl.part"
        with temporary.open("w", encoding="utf-8") as file:
            for record in metadata:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        temporary.replace(output / "metadata.jsonl")
        summary["pages"].append(page)
        summary["videos"] = len(metadata)
        summary["skipped"] += len(page["skipped"])
        save_summary()

        def progress(result):
            status = {"downloaded": "downloaded", "exists": "existing", "failed": "failed"}[result["status"]]
            summary[status] += 1
            if status == "downloaded":
                summary["successful_bytes"] += result["bytes"]
            with (output / "downloads.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(result, ensure_ascii=False) + "\n")
            label = {"downloaded": "下载完成", "exists": "文件已存在", "failed": "下载失败"}[result["status"]]
            print(f"视频 {result['aweme_id']} | {label} | 已下载：{summary['downloaded']} | 失败：{summary['failed']}", flush=True)
            save_summary()

        batch_started = time.monotonic()
        results = await download_many(batch, videos, quality=quality, fallback="lower", workers=workers, on_result=progress)
        summary["download_seconds"] += round(time.monotonic() - batch_started, 3)
        save_summary()
        if len(results) != len(batch) or any(row.get("http_status") == 429 for row in results):
            raise BrowserRisk("download_rate_limited", "下载受到限流，已暂停请求下一页用户作品。", 429)

    try:
        session = await open_search_session(ROOT / "data/browser_profile")
        identity = await resolve_user(user, session)
        print(f"用户：{identity.get('nickname', '')} | 抖音号：{identity.get('unique_id', '')} | {identity['url']}", flush=True)
        user_output = output_root.resolve() / identity["sec_uid"]
        videos = user_output / "videos"
        output = user_output / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output.mkdir(parents=True, exist_ok=False)
        summary["user"] = identity
        (output / "user.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        save_summary()
        records, pages = await user_posts(identity, session, count=count, page_delay=page_delay, on_page=on_page)
        summary["completion_reason"] = "count_reached" if count and len(records) >= count else "results_exhausted"
        summary["status"] = "completed_with_errors" if summary["failed"] or any(item["reason"] == "no_video_url" for page in pages for item in page["skipped"]) else "completed"
        reason = {"count_reached": "已达到设定数量", "results_exhausted": "已读完可访问的作品"}[summary["completion_reason"]]
        print(f"采集完成：{reason} | 视频数：{len(records)} | 已下载：{summary['downloaded']} | 已存在：{summary['existing']} | 失败：{summary['failed']} | 已跳过：{summary['skipped']}", flush=True)
        print(f"输出目录：{output}", flush=True)
        return 0 if summary["status"] == "completed" else 3
    except (BrowserRisk, PayloadError, ValueError, OSError, PlaywrightError) as exc:
        summary["status"] = "incomplete"
        summary["completion_reason"] = getattr(exc, "category", type(exc).__name__)
        detail = f"浏览器操作失败（{type(exc).__name__}）。" if isinstance(exc, PlaywrightError) else str(exc)
        save_event(database, "user", summary["completion_reason"], "warning", detail)
        print(f"用户作品采集未完成：{detail}", file=sys.stderr, flush=True)
        return 2
    finally:
        if summary["status"] == "running":
            summary["status"] = "interrupted"
        try:
            if output is not None:
                save_summary()
        finally:
            if session is not None:
                await close_search_session(session)


def main():
    parser = argparse.ArgumentParser(description="下载指定用户可公开访问的抖音视频")
    parser.add_argument("--user", required=True, help="用户昵称、抖音号、sec_uid 或完整主页链接")
    parser.add_argument("--workers", type=positive_integer, default=1, help="并发下载数，默认 1")
    parser.add_argument("--count", type=int, default=0, help="采集的不重复视频数，默认 0 表示全部可访问的公开视频")
    parser.add_argument("--output-dir", default="downloads/users", help="输出目录，默认 downloads/users")
    parser.add_argument("--quality", type=quality_value, default="best", help="清晰度，默认 best")
    parser.add_argument("--page-delay", type=nonnegative_number, default=5, help="作品翻页间隔秒数，默认 5")
    args = parser.parse_args()
    if args.count < 0 or not args.user.strip():
        parser.error("count 必须大于等于 0，user 不能为空")
    try:
        return asyncio.run(run(args.user, args.workers, args.count, args.output_dir, args.quality, args.page_delay))
    except KeyboardInterrupt:
        print("已停止。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
