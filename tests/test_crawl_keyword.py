import asyncio
import json
import sys
from unittest.mock import AsyncMock

import pytest

import crawl_keyword as crawl
from dycrawler.browser import BrowserRisk
from dycrawler.database import load_records, recent_events, save_records


@pytest.fixture(autouse=True)
def browser_session(monkeypatch):
    session = (object(), object(), object())
    opened = AsyncMock(return_value=session)
    closed = AsyncMock()
    monkeypatch.setattr(crawl, "open_search_session", opened)
    monkeypatch.setattr(crawl, "close_search_session", closed)
    return session, opened, closed


def test_default_workers_is_one(monkeypatch):
    async def run(keywords, workers, count, output_dir, quality, search_delay, keyword_delay):
        assert (keywords, workers, count, output_dir, quality, search_delay, keyword_delay) == (["猫"], 1, 100, "downloads", "best", 5, 60)
        return 0

    monkeypatch.setattr(crawl, "run", run)
    monkeypatch.setattr(sys, "argv", ["crawl_keyword.py", "--keyword", "猫"])
    assert crawl.main() == 0


def test_crawl_downloads_only_current_search(tmp_path, monkeypatch):
    monkeypatch.setattr(crawl, "ROOT", tmp_path)
    database = tmp_path / "data" / "douyin.db"
    save_records(database, [{"aweme_id": "old", "variants": []}], keyword="猫")
    records = [{"aweme_id": str(index), "variants": []} for index in range(3)]

    async def search(keyword, profile, **options):
        assert keyword == "猫"
        assert options["max_items"] == 3
        assert options["max_pages"] == 20
        assert options["scroll_delay"] == 5
        return records, []

    async def download(items, output, quality, fallback, workers, on_result):
        assert items == records
        assert output == (tmp_path / "outputs" / "cat videos").resolve()
        assert quality == "720p"
        assert fallback == "lower"
        assert workers == 2
        results = [{"aweme_id": item["aweme_id"], "status": "downloaded"} for item in items]
        for result in results:
            on_result(result)
        return results

    monkeypatch.setattr(crawl, "search", search)
    monkeypatch.setattr(crawl, "download_many", download)
    assert asyncio.run(crawl.run("猫", 2, 3, "outputs/cat videos", "720p")) == 0
    exported = [json.loads(line) for line in (tmp_path / "data" / "metadata.jsonl").read_text().splitlines()]
    assert exported == records
    assert len(load_records(database, keyword="猫")) == 4


def test_login_then_report_incomplete_download(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(crawl, "ROOT", tmp_path)
    attempts = []

    async def search(*args, **kwargs):
        attempts.append("search")
        if len(attempts) == 1:
            raise BrowserRisk("login_required", "login")
        return [{"aweme_id": "1", "variants": []}], []

    async def login(profile):
        attempts.append("login")

    async def download(records, output, quality, fallback, workers, on_result):
        result = {"aweme_id": "1", "status": "failed", "category": "media_rejected", "detail": "HTTP 403"}
        on_result(result)
        return [result]

    monkeypatch.setattr(crawl, "search", search)
    monkeypatch.setattr(crawl, "login", login)
    monkeypatch.setattr(crawl, "download_many", download)
    assert asyncio.run(crawl.run("猫", 3, 5, keyword_delay=0)) == 3
    assert attempts == ["search", "login", "search"]
    output = capsys.readouterr().out
    assert "正在搜索 [1/1]：猫" in output
    assert "[1/1] 1 下载失败 HTTP 403" in output
    assert "已下载：0 | 已存在：0 | 失败：1" in output
    assert recent_events(tmp_path / "data" / "douyin.db")[0]["category"] == "media_rejected"


def test_absolute_output_directory_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(crawl, "ROOT", tmp_path / "repo")
    output_dir = tmp_path / "external videos"

    async def search(*args, **kwargs):
        return [], []

    async def download(records, output, quality, fallback, workers, on_result):
        assert output == output_dir.resolve()
        return []

    monkeypatch.setattr(crawl, "search", search)
    monkeypatch.setattr(crawl, "download_many", download)
    assert asyncio.run(crawl.run("猫", 1, 1, output_dir)) == 3


def test_search_failure_preserves_existing_export(tmp_path, monkeypatch):
    monkeypatch.setattr(crawl, "ROOT", tmp_path)
    metadata = tmp_path / "data" / "metadata.jsonl"
    metadata.parent.mkdir()
    metadata.write_text("existing data\n")

    async def search(*args, **kwargs):
        raise BrowserRisk("challenge", "验证码")

    monkeypatch.setattr(crawl, "search", search)
    with pytest.raises(BrowserRisk):
        asyncio.run(crawl.run("猫", 3, 5))
    assert metadata.read_text() == "existing data\n"


def test_empty_search_does_not_download_history(tmp_path, monkeypatch):
    monkeypatch.setattr(crawl, "ROOT", tmp_path)
    database = tmp_path / "data" / "douyin.db"
    save_records(database, [{"aweme_id": "old", "variants": []}], keyword="猫")

    async def search(*args, **kwargs):
        return [], []

    async def download(records, *args, **kwargs):
        assert records == []
        return []

    monkeypatch.setattr(crawl, "search", search)
    monkeypatch.setattr(crawl, "download_many", download)
    assert asyncio.run(crawl.run("猫", 3, 5)) == 3
    assert (tmp_path / "data" / "metadata.jsonl").read_text() == ""
    assert len(load_records(database)) == 1


def test_count_uses_bounded_internal_page_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(crawl, "ROOT", tmp_path)

    async def search(keyword, profile, **options):
        assert options["max_items"] == 100
        assert options["max_pages"] == 25
        assert options["scroll_delay"] == 5
        return [], []

    async def download(*args, **kwargs):
        return []

    monkeypatch.setattr(crawl, "search", search)
    monkeypatch.setattr(crawl, "download_many", download)
    assert asyncio.run(crawl.run("猫", 1, 100)) == 3


def test_quality_argument_is_normalized_before_run(monkeypatch):
    async def run(keywords, workers, count, output_dir, quality, search_delay, keyword_delay):
        assert keywords == ["猫"]
        assert quality == "720p"
        return 0

    monkeypatch.setattr(crawl, "run", run)
    monkeypatch.setattr(sys, "argv", ["crawl_keyword.py", "--keyword", "猫", "--quality", "720"])
    assert crawl.main() == 0


@pytest.mark.parametrize("arguments", [
    ["--keyword", "猫", "--workers", "0"],
    ["--keyword", "猫", "--count", "-1"],
    ["--keyword", "猫", "--quality", "abc"],
    ["--keyword", "猫", "--quality", "0"],
    ["--keyword", " "],
])
def test_invalid_arguments_do_not_launch_browser(arguments, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["crawl_keyword.py", *arguments])
    with pytest.raises(SystemExit) as exc:
        crawl.main()
    assert exc.value.code == 2


def test_multiple_keywords_use_per_keyword_count_and_global_deduplication(tmp_path, monkeypatch, capsys, browser_session):
    monkeypatch.setattr(crawl, "ROOT", tmp_path)
    first = [{"aweme_id": "1", "variants": []}, {"aweme_id": "2", "variants": []}]
    second = [{"aweme_id": "2", "variants": []}, {"aweme_id": "3", "variants": []}]
    calls = []

    async def search(keyword, profile, **options):
        calls.append((keyword, options["max_items"], options["max_pages"], options["session"]))
        return (first if keyword == "街舞" else second), []

    async def download(records, output, quality, fallback, workers, on_result):
        assert [record["aweme_id"] for record in records] == ["1", "2", "3"]
        results = [{"aweme_id": record["aweme_id"], "status": "downloaded"} for record in records]
        for result in results:
            on_result(result)
        return results

    monkeypatch.setattr(crawl, "search", search)
    monkeypatch.setattr(crawl, "download_many", download)
    assert asyncio.run(crawl.run(["街舞", "爵士舞"], 1, 200, keyword_delay=0)) == 0
    session, opened, closed = browser_session
    assert calls == [("街舞", 200, 45, session), ("爵士舞", 200, 45, session)]
    opened.assert_awaited_once()
    closed.assert_awaited_once_with(session)
    exported = [json.loads(line) for line in (tmp_path / "data" / "metadata.jsonl").read_text().splitlines()]
    assert [record["aweme_id"] for record in exported] == ["1", "2", "3"]
    assert len(load_records(tmp_path / "data" / "douyin.db", keyword="街舞")) == 2
    assert len(load_records(tmp_path / "data" / "douyin.db", keyword="爵士舞")) == 2
    output = capsys.readouterr().out
    assert "已检查 0 | 去重后 2 | 本次新增 1 | 跨词重复 1 | 累计唯一视频 3" in output
    assert "共找到 3 条不重复视频，开始下载。" in output
    assert "[3/3] 3 下载完成" in output


def test_keywords_accept_chinese_commas_and_remove_duplicates(monkeypatch):
    async def run(keywords, workers, count, output_dir, quality, search_delay, keyword_delay):
        assert keywords == ["街舞", "爵士舞"]
        return 0

    monkeypatch.setattr(crawl, "run", run)
    monkeypatch.setattr(sys, "argv", ["crawl_keyword.py", "--keywords", "街舞，爵士舞,街舞"])
    assert crawl.main() == 0


def test_keyword_delay_runs_only_between_keywords(tmp_path, monkeypatch):
    monkeypatch.setattr(crawl, "ROOT", tmp_path)
    waits = []

    async def search(*args, **kwargs):
        assert kwargs["scroll_delay"] == 7
        return [], []

    async def download(*args, **kwargs):
        return []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(crawl, "search", search)
    monkeypatch.setattr(crawl, "download_many", download)
    monkeypatch.setattr(crawl.asyncio, "sleep", sleep)
    assert asyncio.run(crawl.run(["街舞", "爵士舞", "古典舞"], 1, 1, search_delay=7, keyword_delay=45)) == 3
    assert waits == [45, 45]
