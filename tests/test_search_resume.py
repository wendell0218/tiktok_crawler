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
    search_box = SimpleNamespace(fill=AsyncMock(), press=AsyncMock(), wait_for=AsyncMock(), click=AsyncMock(), input_value=AsyncMock())
    search_box.fill.side_effect = lambda keyword, **kwargs: setattr(search_box.input_value, "return_value", keyword)
    page = SimpleNamespace(
        url="https://www.douyin.com/jingxuan",
        goto=AsyncMock(), wait_for_url=AsyncMock(),
        wait_for_load_state=AsyncMock(),
        get_by_placeholder=Mock(return_value=search_box),
        evaluate=AsyncMock(return_value="Chrome"),
        is_closed=Mock(return_value=False), bring_to_front=AsyncMock(),
        wait_for_timeout=AsyncMock(),
    )
    search_box.press.side_effect = lambda *args, **kwargs: setattr(page, "url", f"https://www.douyin.com/search/{search_box.input_value.return_value}?submitted=1")
    context = SimpleNamespace(on=Mock(), remove_listener=Mock())
    queue = asyncio.Queue()
    close = AsyncMock()
    scroll = AsyncMock()
    logged_in = AsyncMock(return_value=True)
    monkeypatch.setattr(browser, "_open", AsyncMock(return_value=(None, context, page)))
    monkeypatch.setattr(browser, "_close", close)
    monkeypatch.setattr(browser, "_logged_in", logged_in)
    monkeypatch.setattr(browser, "_challenge", AsyncMock(return_value=""))
    monkeypatch.setattr(browser, "_login_panel", AsyncMock(return_value=False))
    monkeypatch.setattr(browser, "_scroll_search", scroll)
    monkeypatch.setattr(browser.asyncio, "Queue", lambda: queue)
    return SimpleNamespace(page=page, box=search_box, context=context, queue=queue, close=close, scroll=scroll, logged_in=logged_in)


@pytest.mark.parametrize("route", [
    "https://www.douyin.com/search/%E8%A1%97%E8%88%9E?type=general",
    "https://www.douyin.com/jingxuan/search/街舞",
])
def test_both_routes_resume_after_navigation_wait_error(environment, route):
    environment.page.url = route
    environment.page.wait_for_url.side_effect = TimeoutError
    environment.queue.put_nowait(response("1", has_more=0))
    records, pages = asyncio.run(browser.search("街舞", "unused"))
    assert len(records) == len(pages) == 1
    environment.box.press.assert_awaited_once_with("Enter", timeout=15000)
    environment.scroll.assert_not_awaited()


@pytest.mark.parametrize("url", [
    "https://www.douyin.com/search/韩舞",
    "https://www.douyin.com.evil.example/search/街舞",
    "https://www.douyin.com/jingxuan",
])
def test_wrong_route_does_not_repeat_submission(environment, url):
    environment.page.url = url
    environment.box.press.side_effect = None
    environment.page.wait_for_url.side_effect = TimeoutError
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser.search("街舞", "unused"))
    assert error.value.category == "search_navigation_timeout"
    environment.box.press.assert_awaited_once_with("Enter", timeout=15000)
    assert environment.box.fill.await_count == 2
    environment.scroll.assert_not_awaited()


def test_response_can_complete_search_without_waiting_for_a_route_change(environment):
    environment.page.wait_for_url.side_effect = TimeoutError
    environment.queue.put_nowait(response("1", has_more=0))
    records, _ = asyncio.run(browser.search("街舞", "unused"))
    assert records[0]["aweme_id"] == "1"
    environment.box.press.assert_awaited_once_with("Enter", timeout=15000)


def test_manual_verification_keeps_results_count_and_deduplication(environment):
    events = iter([
        response("1", "2"),
        {"status": 200, "payload": {"data": None, "has_more": 0, "search_nil_info": {"search_nil_type": "verify_check"}}, "error": ""},
        response("2", "3"),
    ])

    async def receive():
        event = next(events)
        if environment.page.bring_to_front.await_count:
            assert environment.scroll.await_count == 1
            assert environment.box.press.await_count == 1
            assert environment.page.goto.await_count == 1
            environment.close.assert_not_awaited()
        return event

    environment.queue.get = AsyncMock(side_effect=receive)
    records, pages = asyncio.run(browser.search("街舞", "unused", max_items=4))
    assert [item["aweme_id"] for item in records] == ["1", "2", "3"]
    assert [item["inspected_total"] for item in pages] == [2, 4]
    assert [item["added"] for item in pages] == [2, 1]
    environment.page.bring_to_front.assert_awaited_once()
    environment.close.assert_awaited_once()
    assert environment.scroll.await_count == 1
    assert environment.context.remove_listener.call_count == 2


def test_entry_verification_waits_before_login_or_submission(environment, monkeypatch):
    challenge = AsyncMock()
    challenge.side_effect = lambda page: "安全验证" if challenge.await_count == 1 else ""
    monkeypatch.setattr(browser, "_challenge", challenge)

    async def manual_wait(seconds):
        environment.logged_in.assert_not_awaited()
        environment.box.press.assert_not_awaited()
        environment.close.assert_not_awaited()

    monkeypatch.setattr(browser.asyncio, "sleep", manual_wait)
    environment.queue.put_nowait(response("1", has_more=0))
    records, _ = asyncio.run(browser.search("街舞", "unused"))
    assert records[0]["aweme_id"] == "1"
    environment.box.press.assert_awaited_once_with("Enter", timeout=15000)


def test_final_dialog_does_not_discard_a_completed_batch(environment, monkeypatch):
    monkeypatch.setattr(browser, "_challenge", AsyncMock(side_effect=lambda page: "安全验证" if environment.queue.empty() and not page.bring_to_front.await_count else ""))
    monkeypatch.setattr(browser.asyncio, "sleep", AsyncMock())
    environment.queue.put_nowait(response("1"))
    records, pages = asyncio.run(browser.search("街舞", "unused", max_items=1))
    assert records[0]["aweme_id"] == "1"
    assert pages[0]["inspected_total"] == 1
    environment.page.bring_to_front.assert_awaited_once()
    environment.scroll.assert_not_awaited()


@pytest.mark.parametrize("other_page,keyword", [(True, "街舞"), (False, "韩舞")])
def test_capture_ignores_another_page_or_manual_keyword(other_page, keyword):
    page = object()
    request = SimpleNamespace(resource_type="fetch", frame=SimpleNamespace(page=object() if other_page else page))
    captured = SimpleNamespace(
        url=f"https://www.douyin.com{browser.SEARCH_PATH}?keyword={keyword}",
        request=request, status=200, body=AsyncMock(),
    )
    queue = asyncio.Queue()
    asyncio.run(browser._capture(captured, queue, browser.SEARCH_PATHS, page=page, keyword="街舞"))
    assert queue.empty()
    captured.body.assert_not_awaited()


def test_capture_accepts_the_current_page_and_keyword():
    page = object()
    captured = SimpleNamespace(
        url=f"https://www.douyin.com{browser.SEARCH_PATH}?keyword=%E8%A1%97%E8%88%9E",
        request=SimpleNamespace(resource_type="fetch", frame=SimpleNamespace(page=page)),
        status=200, body=AsyncMock(return_value=b'{"status_code":0,"data":[],"has_more":0}'),
    )
    queue = asyncio.Queue()
    asyncio.run(browser._capture(captured, queue, browser.SEARCH_PATHS, page=page, keyword="街舞"))
    assert queue.get_nowait()["payload"]["has_more"] == 0


def test_refresh_keeps_final_verification_window_open(environment, monkeypatch):
    monkeypatch.setattr(browser, "_challenge", AsyncMock(side_effect=["", "", "", "安全验证", ""]))
    detail = {"aweme_id": "1", "video": {"play_addr": {"url_list": ["https://cdn.example.com/1.mp4"]}}}
    event = {"status": 200, "payload": {"status_code": 0, "aweme_detail": detail}, "error": ""}
    environment.queue.get = AsyncMock(return_value=event)

    async def manual_wait(seconds):
        environment.close.assert_not_awaited()
        assert environment.page.goto.await_count == 2

    monkeypatch.setattr(browser.asyncio, "sleep", manual_wait)
    refreshed, failures = asyncio.run(browser.refresh([{"aweme_id": "1"}], "unused"))
    assert refreshed[0]["aweme_id"] == "1"
    assert failures == []
    environment.page.bring_to_front.assert_awaited_once()
    environment.close.assert_awaited_once()
