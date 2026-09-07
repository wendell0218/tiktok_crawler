import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dycrawler import browser


def response(*ids, has_more=1):
    return {
        "status": 200,
        "payload": {
            "status_code": 0,
            "has_more": has_more,
            "data": [
                {"aweme_info": {
                    "aweme_id": item,
                    "video": {"play_addr": {"url_list": [f"https://cdn.example.com/{item}.mp4"]}},
                }}
                for item in ids
            ],
        },
        "error": "",
    }


@pytest.fixture
def environment(monkeypatch):
    box = SimpleNamespace(fill=AsyncMock(), press=AsyncMock())
    page = SimpleNamespace(
        goto=AsyncMock(), wait_for_url=AsyncMock(),
        get_by_placeholder=Mock(return_value=box),
        evaluate=AsyncMock(return_value="Chrome"),
        wait_for_timeout=AsyncMock(),
    )
    context = SimpleNamespace(on=Mock(), remove_listener=Mock())
    session = (None, context, page)
    wait = AsyncMock()
    scroll = AsyncMock()
    close = AsyncMock()
    monkeypatch.setattr(browser, "open_search_session", AsyncMock(return_value=session))
    monkeypatch.setattr(browser, "close_search_session", close)
    monkeypatch.setattr(browser, "_logged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(browser, "_wait_for_verification", AsyncMock())
    monkeypatch.setattr(browser, "_wait_entry_ready", AsyncMock())
    monkeypatch.setattr(browser, "_submit_search", AsyncMock())
    monkeypatch.setattr(browser, "_wait_search_response", wait)
    monkeypatch.setattr(browser, "_scroll_search", scroll)
    return SimpleNamespace(session=session, context=context, wait=wait, scroll=scroll, close=close)


def test_search_waits_for_page_work_before_next_scroll_or_response(environment):
    environment.wait.side_effect = [response("1"), response("2", has_more=0)]
    completed = []

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def on_page(records, page):
            if page["page"] == 1:
                started.set()
                await release.wait()
            completed.append([item["aweme_id"] for item in records])

        async def scroll(page):
            assert completed == [["1"]]

        environment.scroll.side_effect = scroll
        task = asyncio.create_task(browser.search("街舞", "unused", on_page=on_page))
        await asyncio.wait_for(started.wait(), timeout=1)
        environment.scroll.assert_not_awaited()
        assert environment.wait.await_count == 1
        assert not task.done()
        release.set()
        return await asyncio.wait_for(task, timeout=1)

    records, pages = asyncio.run(run())
    assert completed == [["1"], ["2"]]
    assert len(records) == len(pages) == 2
    environment.scroll.assert_awaited_once()


@pytest.mark.parametrize("has_more,options", [
    (0, {}),
    (1, {"max_items": 1}),
    (1, {"max_pages": 1}),
])
def test_final_page_is_delivered_before_return(environment, has_more, options):
    environment.wait.side_effect = [response("1", has_more=has_more)]
    on_page = AsyncMock()
    records, pages = asyncio.run(browser.search("街舞", "unused", on_page=on_page, **options))
    on_page.assert_awaited_once_with(records, pages[0])
    environment.scroll.assert_not_awaited()
    environment.close.assert_awaited_once()


def test_callback_failure_stops_without_advancing_or_closing_shared_session(environment):
    environment.wait.side_effect = [response("1"), response("2")]
    on_page = AsyncMock(side_effect=RuntimeError("download failed"))
    with pytest.raises(RuntimeError, match="download failed"):
        asyncio.run(browser.search("街舞", "unused", session=environment.session, on_page=on_page))
    assert environment.wait.await_count == 1
    environment.scroll.assert_not_awaited()
    environment.close.assert_not_awaited()
    assert environment.context.remove_listener.call_count == 2


def test_duplicate_pages_are_delivered_and_do_not_stop_count_early(environment):
    environment.wait.side_effect = [response("1")] * 4 + [response("2")]
    on_page = AsyncMock()
    records, pages = asyncio.run(browser.search("街舞", "unused", max_items=5, on_page=on_page))
    assert [item["aweme_id"] for item in records] == ["1", "2"]
    assert [item["inspected_total"] for item in pages] == [1, 2, 3, 4, 5]
    assert [item["added"] for item in pages] == [1, 0, 0, 0, 1]
    assert [[record["aweme_id"] for record in call.args[0]] for call in on_page.await_args_list] == [
        ["1"], [], [], [], ["2"],
    ]
    assert environment.scroll.await_count == 4


def test_empty_final_page_is_delivered(environment):
    environment.wait.side_effect = [response(has_more=0)]
    on_page = AsyncMock()
    records, pages = asyncio.run(browser.search("街舞", "unused", on_page=on_page))
    assert records == []
    on_page.assert_awaited_once_with([], pages[0])
    environment.scroll.assert_not_awaited()


def test_callback_summary_changes_do_not_change_pagination(environment):
    environment.wait.side_effect = [response("1"), response("2", has_more=0)]

    async def on_page(records, page):
        page["has_more"] = 0
        page["inspected_total"] = 999
        records.clear()

    records, pages = asyncio.run(browser.search("街舞", "unused", on_page=on_page))
    assert [item["aweme_id"] for item in records] == ["1", "2"]
    assert [item["inspected_total"] for item in pages] == [1, 2]
    assert [item["has_more"] for item in pages] == [1, 0]
    environment.scroll.assert_awaited_once()


def test_count_limits_last_page_before_callback(environment):
    environment.wait.side_effect = [response("1", "2", "3")]
    on_page = AsyncMock()
    records, pages = asyncio.run(browser.search("街舞", "unused", max_items=2, on_page=on_page))
    assert [item["aweme_id"] for item in records] == ["1", "2"]
    on_page.assert_awaited_once_with(records, pages[0])
    assert pages[0]["received"] == 3
    assert pages[0]["inspected"] == 2
    environment.scroll.assert_not_awaited()


@pytest.mark.parametrize("complete_last_page,expected", [(False, ["1", "2", "3"]), (True, ["1", "2", "3", "4"])])
def test_complete_last_page_option_finishes_current_response_only(environment, complete_last_page, expected):
    environment.wait.side_effect = [response("1", "2"), response("3", "4"), response("5")]
    on_page = AsyncMock()
    records, pages = asyncio.run(browser.search(
        "街舞", "unused", max_items=3, on_page=on_page, complete_last_page=complete_last_page,
    ))
    assert [item["aweme_id"] for item in records] == expected
    assert pages[-1]["inspected_total"] == len(expected)
    assert [item["aweme_id"] for item in on_page.await_args_list[-1].args[0]] == expected[2:]
    assert environment.wait.await_count == 2
    environment.scroll.assert_awaited_once()
