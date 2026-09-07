import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError

import crawl_user
from dycrawler.browser import BrowserRisk


IDENTITY = {"sec_uid": "MS4wLjCreator", "nickname": "影视飓风", "unique_id": "12345", "url": "https://www.douyin.com/user/MS4wLjCreator"}


def record(aweme_id):
    return {"aweme_id": str(aweme_id), "variants": [], "collection": None}


def page_info(index, added, total, more=False, skipped=None):
    return {"page": index, "received": added, "added": added, "unique": total,
            "cursor": index, "has_more": more, "skipped": skipped or []}


def summaries(root):
    return list((root / "downloads/users" / IDENTITY["sec_uid"] / "runs").glob("*/summary.json"))


@pytest.fixture(autouse=True)
def isolated_repository(tmp_path, monkeypatch):
    session = (object(), object(), object())
    monkeypatch.setattr(crawl_user, "ROOT", tmp_path)
    monkeypatch.setattr(crawl_user, "open_search_session", AsyncMock(return_value=session))
    monkeypatch.setattr(crawl_user, "close_search_session", AsyncMock())
    monkeypatch.setattr(crawl_user, "resolve_user", AsyncMock(return_value=IDENTITY.copy()))
    monkeypatch.setattr(crawl_user, "user_posts", AsyncMock(side_effect=AssertionError("Collector must be mocked")))
    monkeypatch.setattr(crawl_user, "download_many", AsyncMock(side_effect=AssertionError("Downloader must be mocked")))
    monkeypatch.setattr(crawl_user, "save_records", Mock())
    monkeypatch.setattr(crawl_user, "save_event", Mock())
    return session


@pytest.mark.parametrize("count", [0, 3])
def test_page_downloads_are_awaited_and_outputs_are_per_run(tmp_path, monkeypatch, isolated_repository, count, capsys):
    batches = [[record(1), record(2)], [record(3)]]
    completed = []
    pages = [page_info(1, 2, 2, True), page_info(2, 1, 3)]

    async def collect(user, session, **options):
        assert user == IDENTITY and session == isolated_repository
        assert options["count"] == count and options["page_delay"] == 7
        for index, batch in enumerate(batches):
            assert len(completed) == index
            await options["on_page"](batch, pages[index])
            assert len(completed) == index + 1
        return batches[0] + batches[1], pages

    async def download(batch, output, **options):
        assert output == tmp_path / "downloads/users" / IDENTITY["sec_uid"] / "videos"
        assert options["workers"] == 4 and options["quality"] == "720p" and options["fallback"] == "lower"
        results = [{"aweme_id": row["aweme_id"], "status": "downloaded", "bytes": 100, "http_status": 200} for row in batch]
        for result in results:
            options["on_result"](result)
        await asyncio.sleep(0)
        completed.append(batch)
        return results

    monkeypatch.setattr(crawl_user, "user_posts", collect)
    monkeypatch.setattr(crawl_user, "download_many", download)
    assert asyncio.run(crawl_user.run("影视飓风", workers=4, count=count, quality="720p", page_delay=7)) == 0
    [summary_path] = summaries(tmp_path)
    summary = json.loads(summary_path.read_text())
    assert summary["status"] == "completed" and summary["videos"] == 3
    assert summary["downloaded"] == 3 and summary["successful_bytes"] == 300
    assert summary["completion_reason"] == ("count_reached" if count else "results_exhausted")
    metadata = [json.loads(line) for line in summary_path.with_name("metadata.jsonl").read_text().splitlines()]
    assert metadata == batches[0] + batches[1]
    assert len(summary_path.with_name("downloads.jsonl").read_text().splitlines()) == 3
    assert crawl_user.save_records.call_args_list[0].args == (tmp_path / "data/douyin.db", batches[0])
    crawl_user.open_search_session.assert_awaited_once_with(tmp_path / "data/browser_profile")
    crawl_user.close_search_session.assert_awaited_once_with(isolated_repository)
    output = capsys.readouterr().out
    assert "用户：影视飓风 | 抖音号：12345" in output
    assert "视频 3 | 下载完成 | 已下载：3 | 失败：0" in output
    reason = "已达到设定数量" if count else "已读完可访问的作品"
    assert f"采集完成：{reason} | 视频数：3 | 已下载：3" in output


@pytest.mark.parametrize("partial", [False, True])
def test_cdn_429_or_unattempted_work_stops_before_next_page(tmp_path, monkeypatch, partial):
    next_page = []
    batch = [record(1), record(2)]

    async def collect(user, session, **options):
        await options["on_page"](batch, page_info(1, 2, 2, True))
        next_page.append(True)
        raise AssertionError("Must not advance after a rate-limited batch")

    async def download(items, output, **options):
        results = [{"aweme_id": "1", "status": "downloaded", "bytes": 100, "http_status": 200}]
        if not partial:
            results.append({"aweme_id": "2", "status": "failed", "bytes": 0, "http_status": 429})
        for result in results:
            options["on_result"](result)
        return results

    monkeypatch.setattr(crawl_user, "user_posts", collect)
    monkeypatch.setattr(crawl_user, "download_many", download)
    assert asyncio.run(crawl_user.run("影视飓风")) == 2
    summary = json.loads(summaries(tmp_path)[0].read_text())
    assert summary["status"] == "incomplete" and summary["completion_reason"] == "download_rate_limited"
    assert not next_page
    assert summary["downloaded"] == 1 and summary["videos"] == 2


def test_final_cdn_403_is_recorded_but_does_not_act_like_metadata_rejection(tmp_path, monkeypatch):
    pages = [page_info(1, 1, 1, True), page_info(2, 1, 2)]

    async def collect(user, session, **options):
        for index in (1, 2):
            await options["on_page"]([record(index)], pages[index - 1])
        return [record(1), record(2)], pages

    async def download(batch, output, **options):
        first = batch[0]["aweme_id"] == "1"
        result = {"aweme_id": batch[0]["aweme_id"], "status": "failed" if first else "downloaded",
                  "http_status": 403 if first else 200, "bytes": 0 if first else 100}
        options["on_result"](result)
        return [result]

    monkeypatch.setattr(crawl_user, "user_posts", collect)
    monkeypatch.setattr(crawl_user, "download_many", download)
    assert asyncio.run(crawl_user.run("影视飓风")) == 3
    summary = json.loads(summaries(tmp_path)[0].read_text())
    assert summary["status"] == "completed_with_errors"
    assert summary["downloaded"] == 1 and summary["failed"] == 1 and len(summary["pages"]) == 2


def test_repeated_run_has_independent_logs_and_stable_video_directory(tmp_path, monkeypatch):
    outputs = []

    async def collect(user, session, **options):
        page = page_info(1, 1, 1)
        await options["on_page"]([record(1)], page)
        return [record(1)], [page]

    async def download(batch, output, **options):
        outputs.append(output)
        result = {"aweme_id": "1", "status": "exists", "bytes": 100}
        options["on_result"](result)
        return [result]

    monkeypatch.setattr(crawl_user, "user_posts", collect)
    monkeypatch.setattr(crawl_user, "download_many", download)
    assert asyncio.run(crawl_user.run("影视飓风")) == 0
    first_path = summaries(tmp_path)[0]
    first_content = first_path.read_bytes()
    assert asyncio.run(crawl_user.run("影视飓风")) == 0
    assert len(summaries(tmp_path)) == 2 and first_path.read_bytes() == first_content
    assert outputs[0] == outputs[1]
    for path in summaries(tmp_path):
        summary = json.loads(path.read_text())
        assert summary["existing"] == 1 and summary["downloaded"] == 0
        assert len(path.with_name("downloads.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("reason,exit_code", [("image_post", 0), ("no_video_url", 3)])
def test_skipped_image_and_unavailable_video_have_distinct_completion_status(tmp_path, monkeypatch, reason, exit_code):
    async def collect(user, session, **options):
        page = page_info(1, 0, 0, skipped=[{"aweme_id": "1", "reason": reason}])
        await options["on_page"]([], page)
        return [], [page]

    monkeypatch.setattr(crawl_user, "user_posts", collect)
    monkeypatch.setattr(crawl_user, "download_many", AsyncMock(return_value=[]))
    assert asyncio.run(crawl_user.run("影视飓风")) == exit_code
    summary = json.loads(summaries(tmp_path)[0].read_text())
    assert summary["skipped"] == 1 and summary["videos"] == 0
    assert summary["status"] == ("completed" if reason == "image_post" else "completed_with_errors")


def test_interruption_preserves_completed_page_metadata_and_download_log(tmp_path, monkeypatch, isolated_repository):
    async def collect(user, session, **options):
        await options["on_page"]([record(1)], page_info(1, 1, 1, True))
        raise asyncio.CancelledError

    async def download(items, output, **options):
        result = {"aweme_id": "1", "status": "downloaded", "bytes": 100}
        options["on_result"](result)
        return [result]

    monkeypatch.setattr(crawl_user, "user_posts", collect)
    monkeypatch.setattr(crawl_user, "download_many", download)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(crawl_user.run("影视飓风"))
    [path] = summaries(tmp_path)
    summary = json.loads(path.read_text())
    assert summary["status"] == "interrupted" and summary["downloaded"] == 1
    assert json.loads(path.with_name("metadata.jsonl").read_text())["aweme_id"] == "1"
    assert json.loads(path.with_name("downloads.jsonl").read_text())["aweme_id"] == "1"
    crawl_user.close_search_session.assert_awaited_once_with(isolated_repository)


def test_ambiguous_user_closes_session_without_download(tmp_path, monkeypatch, isolated_repository):
    monkeypatch.setattr(crawl_user, "resolve_user", AsyncMock(side_effect=BrowserRisk("user_ambiguous", "Choose a profile URL")))
    assert asyncio.run(crawl_user.run("影视飓风")) == 2
    crawl_user.download_many.assert_not_awaited()
    crawl_user.close_search_session.assert_awaited_once_with(isolated_repository)
    assert not summaries(tmp_path)


def test_browser_errors_do_not_log_request_credentials(monkeypatch, capsys, isolated_repository):
    monkeypatch.setattr(crawl_user, "resolve_user", AsyncMock(side_effect=PlaywrightError("GET https://example.com/?secret=do-not-log")))
    assert asyncio.run(crawl_user.run("影视飓风")) == 2
    assert "do-not-log" not in capsys.readouterr().err
    assert "do-not-log" not in str(crawl_user.save_event.call_args)
    crawl_user.close_search_session.assert_awaited_once_with(isolated_repository)


def test_session_closes_even_if_final_summary_cannot_be_written(monkeypatch, isolated_repository):
    original = Path.write_text

    def fail_summary(path, *args, **kwargs):
        if path.name == "summary.json.part":
            raise OSError("Disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_summary)
    with pytest.raises(OSError, match="Disk full"):
        asyncio.run(crawl_user.run("影视飓风"))
    crawl_user.close_search_session.assert_awaited_once_with(isolated_repository)


def test_cli_defaults_to_all_videos_and_one_worker(monkeypatch):
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(crawl_user, "run", runner)
    monkeypatch.setattr(sys, "argv", ["crawl_user.py", "--user", "影视飓风"])
    assert crawl_user.main() == 0
    runner.assert_awaited_once_with("影视飓风", 1, 0, "downloads/users", "best", 5)


@pytest.mark.parametrize("args", [
    ["--user", "影视飓风", "--count", "-1"], ["--user", "影视飓风", "--workers", "0"],
    ["--user", "影视飓风", "--page-delay", "-1"], ["--user", "   "],
])
def test_invalid_cli_arguments_never_open_browser(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["crawl_user.py", *args])
    with pytest.raises(SystemExit) as error:
        crawl_user.main()
    assert error.value.code == 2
    crawl_user.open_search_session.assert_not_awaited()
