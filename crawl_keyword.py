import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from dycrawler.browser import BrowserRisk, close_search_session, login, open_search_session, search
from dycrawler.database import save_event, save_records
from dycrawler.downloader import download_many
from dycrawler.options import nonnegative_number, positive_integer, quality_value


async def run(keywords, workers, count, output_dir="downloads", quality="best", search_delay=5, keyword_delay=60):
    if isinstance(keywords, str):
        keywords = keywords.replace("，", ",").split(",")
    keywords = list(dict.fromkeys(str(item).strip() for item in keywords if str(item).strip()))
    database = ROOT / "data" / "douyin.db"
    profile = ROOT / "data" / "browser_profile"
    output_dir = Path(output_dir or "downloads").expanduser()
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir = output_dir.resolve()
    page_limit = max(20, (count + 4) // 5 + 5)
    records = []
    seen = set()
    metadata = ROOT / "data" / "metadata.jsonl"
    print(f"关键词数：{len(keywords)} | 每词检查数：{count} | 翻页间隔：{search_delay} 秒 | 换词间隔：{keyword_delay} 秒 | 下载并发：{workers} | 清晰度：{quality}", flush=True)
    session = None
    try:
        session = await open_search_session(profile)
        for index, keyword in enumerate(keywords, 1):
            print(f"正在搜索 [{index}/{len(keywords)}]：{keyword}", flush=True)
            try:
                found, pages = await search(keyword, profile, max_items=count, max_pages=page_limit, scroll_delay=search_delay, session=session)
            except BrowserRisk as exc:
                if exc.category != "login_required":
                    raise
                await close_search_session(session)
                session = None
                print("请在浏览器中完成登录，等待冷却后将继续采集。", flush=True)
                await login(profile)
                if keyword_delay > 0:
                    await asyncio.sleep(keyword_delay)
                session = await open_search_session(profile)
                found, pages = await search(keyword, profile, max_items=count, max_pages=page_limit, scroll_delay=search_delay, session=session)
            save_records(database, found, keyword=keyword)
            added = 0
            for record in found:
                aweme_id = str(record.get("aweme_id") or "")
                if not aweme_id or aweme_id in seen:
                    continue
                seen.add(aweme_id)
                records.append(record)
                added += 1
            metadata.parent.mkdir(parents=True, exist_ok=True)
            temporary = metadata.with_suffix(".jsonl.part")
            with temporary.open("w", encoding="utf-8") as file:
                for record in records:
                    file.write(json.dumps(record, ensure_ascii=False) + "\n")
            temporary.replace(metadata)
            inspected = sum(page.get("inspected", 0) for page in pages)
            print(f"本词结果：已检查 {inspected} | 去重后 {len(found)} | 本次新增 {added} | 跨词重复 {len(found) - added} | 累计唯一视频 {len(records)}", flush=True)
            if index < len(keywords) and keyword_delay > 0:
                print(f"等待 {keyword_delay} 秒后搜索下一个关键词。", flush=True)
                await asyncio.sleep(keyword_delay)
    finally:
        if session is not None:
            await close_search_session(session)
    print(f"共找到 {len(records)} 条不重复视频，开始下载。", flush=True)

    completed = 0

    def progress(result):
        nonlocal completed
        completed += 1
        labels = {"downloaded": "下载完成", "exists": "文件已存在", "failed": "下载失败"}
        label = labels.get(result["status"], result["status"])
        detail = result.get("detail", "")
        print(f"[{completed}/{len(records)}] {result['aweme_id']} {label} {detail}".rstrip(), flush=True)
        if result["status"] == "failed":
            save_event(database, "download", result.get("category", "download_failed"), "warning", result)

    results = await download_many(records, output_dir, quality=quality, fallback="lower", workers=workers, on_result=progress)
    downloaded = sum(item["status"] == "downloaded" for item in results)
    existing = sum(item["status"] == "exists" for item in results)
    failed = sum(item["status"] == "failed" for item in results)
    pending = len(records) - len(results)
    print(f"每词检查数：{count} | 关键词数：{len(keywords)} | 唯一视频：{len(records)} | 已下载：{downloaded} | 已存在：{existing} | 失败：{failed} | 待处理：{pending}")
    print(f"视频目录：{output_dir}\n元数据文件：{metadata}")
    return 0 if records and downloaded + existing == len(records) else 3


def main():
    parser = argparse.ArgumentParser(description="按关键词搜索并下载抖音视频")
    parser.add_argument("--keywords", "--keyword", dest="keywords", required=True, help="搜索关键词，多个关键词用逗号分隔")
    parser.add_argument("--workers", type=positive_integer, default=1, help="并发下载数，默认 1")
    parser.add_argument("--count", type=positive_integer, default=100, help="每个关键词检查的搜索视频数，默认 100")
    parser.add_argument("--output-dir", default="downloads", help="视频输出目录，默认 downloads")
    parser.add_argument("--quality", type=quality_value, default="best", help="清晰度，默认 best")
    parser.add_argument("--search-delay", type=nonnegative_number, default=5, help="搜索翻页间隔秒数，默认 5")
    parser.add_argument("--keyword-delay", type=nonnegative_number, default=60, help="关键词之间的冷却秒数，默认 60")
    args = parser.parse_args()
    keywords = list(dict.fromkeys(item.strip() for item in args.keywords.replace("，", ",").split(",") if item.strip()))
    if not keywords:
        parser.error("关键词不能为空")
    try:
        return asyncio.run(run(keywords, args.workers, args.count, args.output_dir, args.quality, args.search_delay, args.keyword_delay))
    except BrowserRisk as exc:
        save_event(ROOT / "data" / "douyin.db", "crawl", exc.category, "critical", exc.detail)
        print(f"采集已停止：{exc.detail}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已停止。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
