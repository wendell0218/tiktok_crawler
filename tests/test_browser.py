import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dycrawler import browser


def test_browser_uses_default_system_proxy_settings(tmp_path, monkeypatch):
    page = object()
    context = SimpleNamespace(pages=[page], set_default_timeout=Mock(), set_default_navigation_timeout=Mock())
    launch = AsyncMock(return_value=context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(browser, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=playwright)))
    assert asyncio.run(browser._open(tmp_path, "chrome", False, 60000, False)) == (playwright, context, page)
    options = launch.call_args.kwargs
    assert options["chromium_sandbox"] is True
    assert "proxy" not in options
    assert "--no-proxy-server" not in options.get("args", [])


def test_search_scroll_uses_native_smooth_scroll_without_synthetic_event():
    page = SimpleNamespace(evaluate=AsyncMock(return_value="container"))
    assert asyncio.run(browser._scroll_search(page)) == "container"
    script = page.evaluate.await_args.args[0]
    assert "behavior: 'smooth'" in script
    assert "dispatchEvent" not in script


@pytest.fixture
def search_environment(monkeypatch):
    search_box = SimpleNamespace(fill=AsyncMock(), press=AsyncMock(), wait_for=AsyncMock(), click=AsyncMock(), input_value=AsyncMock())
    search_box.fill.side_effect = lambda keyword, **kwargs: setattr(search_box.input_value, "return_value", keyword)
    page = SimpleNamespace(
        goto=AsyncMock(),
        wait_for_load_state=AsyncMock(),
        url="https://www.douyin.com/jingxuan",
        wait_for_url=AsyncMock(),
        get_by_placeholder=Mock(return_value=search_box),
        evaluate=AsyncMock(return_value="test-browser"),
        mouse=SimpleNamespace(wheel=AsyncMock()),
        wait_for_timeout=AsyncMock(),
        is_closed=Mock(return_value=False),
        bring_to_front=AsyncMock(),
    )
    context = SimpleNamespace(on=Mock())
    queue = SimpleNamespace(get=AsyncMock(side_effect=TimeoutError), empty=Mock(return_value=False))
    close = AsyncMock()

    async def wait_response(queue, page, timeout, **kwargs):
        return await queue.get()

    monkeypatch.setattr(browser, "_open", AsyncMock(return_value=(None, context, page)))
    monkeypatch.setattr(browser, "_close", close)
    monkeypatch.setattr(browser, "_logged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(browser, "_challenge", AsyncMock(return_value=""))
    monkeypatch.setattr(browser, "_login_panel", AsyncMock(return_value=False))
    monkeypatch.setattr(browser, "_wait_search_response", wait_response)
    monkeypatch.setattr(browser.asyncio, "Queue", lambda: queue)
    return SimpleNamespace(page=page, search_box=search_box, queue=queue, close=close)


def test_search_submits_keyword_from_jingxuan(search_environment):
    search_environment.queue.get.side_effect = [{
        "status": 200,
        "payload": {"status_code": 0, "data": [], "has_more": 0},
        "error": "",
    }]
    asyncio.run(browser.search("人工智能", "unused-profile"))
    search_environment.page.goto.assert_awaited_once_with("https://www.douyin.com/jingxuan", wait_until="domcontentloaded")
    search_environment.page.get_by_placeholder.assert_called_once_with("搜索你感兴趣的内容", exact=True)
    search_environment.search_box.fill.assert_awaited_once_with("人工智能", timeout=15000)
    search_environment.search_box.press.assert_awaited_once_with("Enter", timeout=15000)
    search_environment.search_box.wait_for.assert_awaited_once_with(state="visible", timeout=15000)
    assert search_environment.search_box.input_value.await_count == 3
    search_environment.page.wait_for_url.assert_not_awaited()


def test_search_requires_login_before_submitting_keyword(search_environment, monkeypatch):
    monkeypatch.setattr(browser, "_logged_in", AsyncMock(return_value=False))
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser.search("人工智能", "unused-profile"))
    assert error.value.category == "login_required"
    search_environment.page.goto.assert_awaited_once_with("https://www.douyin.com/jingxuan", wait_until="domcontentloaded")
    search_environment.page.get_by_placeholder.assert_not_called()
    search_environment.search_box.fill.assert_not_awaited()
    search_environment.search_box.press.assert_not_awaited()
    search_environment.page.wait_for_url.assert_not_awaited()
    search_environment.queue.get.assert_not_awaited()
    search_environment.close.assert_awaited_once()


def test_search_accepts_matching_route_after_wait_error(search_environment):
    search_environment.page.url = "https://www.douyin.com/jingxuan/search/测试?type=general"
    search_environment.page.wait_for_url.side_effect = TimeoutError
    search_environment.queue.get.side_effect = [{
        "status": 200,
        "payload": {"status_code": 0, "data": [], "has_more": 0},
        "error": "",
    }]
    records, pages = asyncio.run(browser.search("测试", "unused-profile"))
    assert records == []
    assert len(pages) == 1
    search_environment.search_box.press.assert_awaited_once_with("Enter", timeout=15000)


def test_search_without_responses_reports_timeout(search_environment):
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser.search("测试", "unused-profile"))
    assert error.value.category == "search_response_timeout"
    assert "未收到任何搜索响应" in error.value.detail
    assert search_environment.queue.get.await_count == 3
    search_environment.close.assert_awaited_once()


@pytest.mark.parametrize("text", [
    "请拖动滑块完成拼图",
    "拖动下方滑块，使拼图完整",
    "拖拽滑块完成验证",
    "请按住滑块，拖动到最右边",
    "滑动滑块完成验证",
    "请完成下方验证后继续操作",
])
def test_challenge_detects_slider_instructions(text):
    captcha = SimpleNamespace(first=SimpleNamespace(is_visible=AsyncMock(return_value=False)))
    body = SimpleNamespace(inner_text=AsyncMock(return_value=text))
    page = SimpleNamespace(title=AsyncMock(return_value="抖音"), locator=Mock(side_effect=[captcha, body]))
    marker = asyncio.run(browser._challenge(page))
    assert marker
    assert marker in text
    body.inner_text.assert_awaited_once_with(timeout=500)


def test_challenge_tolerates_navigation_and_body_timeout():
    captcha = SimpleNamespace(first=SimpleNamespace(is_visible=AsyncMock(side_effect=RuntimeError("navigation"))))
    body = SimpleNamespace(inner_text=AsyncMock(side_effect=TimeoutError))
    page = SimpleNamespace(
        title=AsyncMock(side_effect=RuntimeError("navigation")),
        locator=Mock(side_effect=[captcha, body]),
    )
    assert asyncio.run(browser._challenge(page)) is None


def test_challenge_ignores_normal_sms_login_text():
    captcha = SimpleNamespace(first=SimpleNamespace(is_visible=AsyncMock(return_value=False)))
    body = SimpleNamespace(inner_text=AsyncMock(return_value="请输入验证码 登录"))
    page = SimpleNamespace(title=AsyncMock(return_value="抖音"), locator=Mock(side_effect=[captcha, body]))
    assert asyncio.run(browser._challenge(page)) == ""


def test_response_wait_checks_challenge_between_two_second_intervals(monkeypatch):
    challenge = AsyncMock(side_effect=["", "拖动滑块"])
    queue = SimpleNamespace(get=AsyncMock(side_effect=TimeoutError))
    waiter = AsyncMock(wraps=asyncio.wait_for)
    monkeypatch.setattr(browser, "_challenge", challenge)
    monkeypatch.setattr(browser.asyncio, "wait_for", waiter)
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser._wait_search_response(queue, SimpleNamespace(is_closed=lambda: False), 120, headless=True))
    assert error.value.category == "challenge"
    assert "移除 --headless" in error.value.detail
    assert challenge.await_count == 2
    assert waiter.call_args.kwargs["timeout"] == 2


def test_response_wait_returns_event_after_transient_interval_timeout(monkeypatch):
    event = {"status": 200, "payload": {"data": []}}
    queue = SimpleNamespace(get=AsyncMock(side_effect=[TimeoutError, event]))
    challenge = AsyncMock(return_value="")
    monkeypatch.setattr(browser, "_challenge", challenge)
    assert asyncio.run(browser._wait_search_response(queue, SimpleNamespace(is_closed=lambda: False), 120)) == event
    assert challenge.await_count == 2


def test_response_wait_respects_total_deadline(monkeypatch):
    queue = SimpleNamespace(get=AsyncMock())
    monkeypatch.setattr(browser, "_challenge", AsyncMock(return_value=""))
    with pytest.raises(TimeoutError):
        asyncio.run(browser._wait_search_response(queue, SimpleNamespace(is_closed=lambda: False), 0))
    queue.get.assert_not_awaited()


@pytest.mark.parametrize("has_more", [0, 1])
def test_search_with_valid_empty_response_is_not_capture_failure(search_environment, has_more):
    search_environment.queue.get.side_effect = [
        {"status": 200, "payload": {"status_code": 0, "data": [], "has_more": has_more}, "error": ""},
        TimeoutError,
        TimeoutError,
        TimeoutError,
    ]
    records, pages = asyncio.run(browser.search("测试", "unused-profile"))
    assert records == []
    assert len(pages) == 1
    assert pages[0]["received"] == 0
    assert pages[0]["has_more"] == has_more
    search_environment.close.assert_awaited_once()


def test_search_retains_collected_records_after_later_timeout(search_environment):
    search_environment.queue.get.side_effect = [
        {
            "status": 200,
            "payload": {
                "status_code": 0,
                "has_more": 1,
                "data": [{"aweme_info": {
                    "aweme_id": "123",
                    "video": {"play_addr": {"url_list": ["https://cdn.example.com/video.mp4"]}},
                }}],
            },
            "error": "",
        },
        TimeoutError,
        TimeoutError,
        TimeoutError,
    ]
    records, pages = asyncio.run(browser.search("测试", "unused-profile"))
    assert [record["aweme_id"] for record in records] == ["123"]
    assert len(pages) == 1
    assert pages[0]["added"] == 1
    assert search_environment.queue.get.await_count == 4


@pytest.mark.parametrize("max_items,max_pages", [(1, 5), (5, 1)])
def test_search_does_not_scroll_after_reaching_limit(search_environment, max_items, max_pages):
    search_environment.queue.get.side_effect = [{
        "status": 200,
        "payload": {"status_code": 0, "has_more": 1, "data": [{"aweme_info": {
            "aweme_id": "123",
            "video": {"play_addr": {"url_list": ["https://cdn.example.com/video.mp4"]}},
        }}]},
        "error": "",
    }]
    records, pages = asyncio.run(browser.search("测试", "unused-profile", max_items=max_items, max_pages=max_pages))
    assert len(records) == len(pages) == 1
    search_environment.page.mouse.wheel.assert_not_awaited()
    search_environment.close.assert_awaited_once()


def test_search_counts_duplicates_as_inspected_and_keeps_scanning(search_environment):
    def event(*aweme_ids, has_more=1):
        return {
            "status": 200,
            "payload": {
                "status_code": 0,
                "has_more": has_more,
                "data": [
                    {"aweme_info": {
                        "aweme_id": aweme_id,
                        "video": {"play_addr": {"url_list": [f"https://cdn.example.com/{aweme_id}.mp4"]}},
                    }}
                    for aweme_id in aweme_ids
                ],
            },
            "error": "",
        }

    search_environment.queue.get.side_effect = [event("1"), event("1"), event("1"), event("2")]
    records, pages = asyncio.run(browser.search("测试", "unused-profile", max_items=4, max_pages=10))
    assert [record["aweme_id"] for record in records] == ["1", "2"]
    assert len(pages) == 4
    assert [page["inspected_total"] for page in pages] == [1, 2, 3, 4]
    assert [page["added"] for page in pages] == [1, 0, 0, 1]


def test_search_stops_after_three_empty_video_responses(search_environment):
    search_environment.queue.get.side_effect = [
        {"status": 200, "payload": {"status_code": 0, "data": [], "has_more": 1}, "error": ""},
        {"status": 200, "payload": {"status_code": 0, "data": [], "has_more": 1}, "error": ""},
        {"status": 200, "payload": {"status_code": 0, "data": [], "has_more": 1}, "error": ""},
    ]
    records, pages = asyncio.run(browser.search("测试", "unused-profile", max_items=10, max_pages=10))
    assert records == []
    assert len(pages) == 3
    assert all(page["inspected"] == 0 for page in pages)


def test_search_applies_inspection_limit_inside_response(search_environment):
    search_environment.queue.get.side_effect = [{
        "status": 200,
        "payload": {
            "status_code": 0,
            "has_more": 1,
            "data": [
                {"aweme_info": {
                    "aweme_id": aweme_id,
                    "video": {"play_addr": {"url_list": [f"https://cdn.example.com/{aweme_id}.mp4"]}},
                }}
                for aweme_id in ["1", "1", "2", "3"]
            ],
        },
        "error": "",
    }]
    records, pages = asyncio.run(browser.search("测试", "unused-profile", max_items=3, max_pages=10))
    assert [record["aweme_id"] for record in records] == ["1", "2"]
    assert pages[0]["received"] == 4
    assert pages[0]["inspected"] == 3
    assert pages[0]["inspected_total"] == 3


def test_search_counts_video_without_download_address_as_inspected(search_environment):
    search_environment.queue.get.side_effect = [{
        "status": 200,
        "payload": {
            "status_code": 0,
            "has_more": 1,
            "data": [
                {"aweme_info": {"aweme_id": "1", "video": {}}},
                {"aweme_info": {
                    "aweme_id": "2",
                    "video": {"play_addr": {"url_list": ["https://cdn.example.com/2.mp4"]}},
                }},
                {"aweme_info": {
                    "aweme_id": "3",
                    "video": {"play_addr": {"url_list": ["https://cdn.example.com/3.mp4"]}},
                }},
            ],
        },
        "error": "",
    }]
    records, pages = asyncio.run(browser.search("测试", "unused-profile", max_items=2, max_pages=10))
    assert [record["aweme_id"] for record in records] == ["2"]
    assert pages[0]["inspected"] == 2
