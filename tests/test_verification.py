import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dycrawler import browser


NORMAL = {"status": 200, "payload": {"status_code": 0, "data": [], "has_more": 0}, "error": ""}
VERIFY = {"status": 200, "payload": {"status_code": 0, "search_nil_info": {"search_nil_type": "verify_check"}}, "error": ""}


@pytest.fixture
def verification_environment(monkeypatch):
    page = SimpleNamespace(
        is_closed=Mock(return_value=False),
        bring_to_front=AsyncMock(),
        goto=AsyncMock(),
        reload=AsyncMock(),
        keyboard=SimpleNamespace(press=AsyncMock()),
        mouse=SimpleNamespace(wheel=AsyncMock()),
    )
    challenge = AsyncMock(return_value="")
    scroll = AsyncMock()
    runtime = SimpleNamespace(wait_for=asyncio.wait_for, sleep=AsyncMock())
    monkeypatch.setattr(browser, "_challenge", challenge)
    monkeypatch.setattr(browser, "_scroll_search", scroll)
    monkeypatch.setattr(browser, "asyncio", runtime)
    return SimpleNamespace(page=page, challenge=challenge, scroll=scroll, runtime=runtime)


def test_api_verification_without_dialog_outlives_response_deadline(verification_environment, monkeypatch, capsys):
    environment = verification_environment
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(browser, "time", SimpleNamespace(monotonic=lambda: clock.now))
    events = iter([VERIFY, TimeoutError(), NORMAL])

    async def get():
        clock.now += 100
        event = next(events)
        if isinstance(event, Exception):
            raise event
        return event

    queue = SimpleNamespace(get=AsyncMock(side_effect=get), empty=Mock(return_value=True))
    result = asyncio.run(browser._wait_search_response(queue, environment.page, timeout=1))
    assert result is NORMAL
    assert queue.get.await_count == 3
    environment.page.bring_to_front.assert_awaited_once()
    assert capsys.readouterr().out.count("验证已通过并收到正常响应，继续采集。") == 1


def test_visible_verification_keeps_response_until_dialog_disappears(verification_environment, capsys):
    environment = verification_environment
    environment.challenge.side_effect = ["验证码", "验证码", ""]
    queue = SimpleNamespace(get=AsyncMock(return_value=NORMAL), empty=Mock(return_value=True))
    result = asyncio.run(browser._wait_search_response(queue, environment.page, timeout=0))
    assert result is NORMAL
    queue.get.assert_awaited_once()
    environment.runtime.sleep.assert_awaited_once()
    environment.scroll.assert_not_awaited()
    environment.page.goto.assert_not_awaited()
    environment.page.reload.assert_not_awaited()
    environment.page.keyboard.press.assert_not_awaited()
    environment.page.mouse.wheel.assert_not_awaited()
    assert "验证已通过并收到正常响应，继续采集。" in capsys.readouterr().out


def test_dialog_disappearance_without_normal_response_does_not_resume(verification_environment, capsys):
    environment = verification_environment
    environment.challenge.side_effect = ["验证码", "", ""]
    queue = SimpleNamespace(get=AsyncMock(side_effect=[TimeoutError(), TimeoutError(), asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(browser._wait_search_response(queue, environment.page, timeout=0))
    assert queue.get.await_count == 3
    environment.scroll.assert_not_awaited()
    environment.page.goto.assert_not_awaited()
    environment.page.keyboard.press.assert_not_awaited()
    assert "验证已通过并收到正常响应，继续采集。" not in capsys.readouterr().out


def test_cancelling_paused_response_wait_exits_immediately(verification_environment):
    environment = verification_environment

    async def run():
        entered = asyncio.Event()
        blocked = asyncio.Event()

        async def get():
            entered.set()
            await blocked.wait()

        task = asyncio.create_task(browser._wait_search_response(
            SimpleNamespace(get=get), environment.page, timeout=0, verification=True,
        ))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


@pytest.mark.parametrize("marker", ["", "验证码"])
def test_closed_page_stops_response_wait(verification_environment, marker):
    environment = verification_environment
    environment.page.is_closed.return_value = True
    environment.challenge.return_value = marker
    queue = SimpleNamespace(get=AsyncMock())
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser._wait_search_response(queue, environment.page, timeout=1, verification=True))
    assert error.value.category == "browser_closed"
    queue.get.assert_not_awaited()


@pytest.mark.parametrize("visible", [False, True])
def test_headless_verification_requires_visible_browser(verification_environment, visible, capsys):
    environment = verification_environment
    environment.challenge.return_value = "验证码" if visible else ""
    queue = SimpleNamespace(get=AsyncMock(return_value=VERIFY))
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser._wait_search_response(queue, environment.page, timeout=1, headless=True))
    assert error.value.category == "challenge"
    environment.page.bring_to_front.assert_not_awaited()
    assert "验证已通过并收到正常响应，继续采集。" not in capsys.readouterr().out


@pytest.mark.parametrize("status", [403, 429])
def test_http_rejection_is_returned_without_announcing_resume(verification_environment, status, capsys):
    environment = verification_environment
    event = {"status": status, "payload": NORMAL["payload"], "error": ""}
    queue = SimpleNamespace(get=AsyncMock(return_value=event))
    result = asyncio.run(browser._wait_search_response(queue, environment.page, timeout=0, verification=True))
    assert result is event
    assert "验证已通过并收到正常响应，继续采集。" not in capsys.readouterr().out


@pytest.mark.parametrize("payload,error,risk_category", [
    (None, "ValueError", None),
    ([], "", None),
    ({"status_code": 2483, "data": []}, "", None),
    ({"status_code": 0, "data": "invalid"}, "", "business_or_schema"),
])
def test_invalid_or_business_error_response_does_not_announce_resume(verification_environment, payload, error, risk_category, capsys):
    environment = verification_environment
    event = {"status": 200, "payload": payload, "error": error}
    queue = SimpleNamespace(get=AsyncMock(return_value=event))
    if risk_category:
        with pytest.raises(browser.BrowserRisk) as raised:
            asyncio.run(browser._wait_search_response(queue, environment.page, timeout=0, verification=True))
        assert raised.value.category == risk_category
    else:
        result = asyncio.run(browser._wait_search_response(queue, environment.page, timeout=0, verification=True))
        assert result is event
    assert "验证已通过并收到正常响应，继续采集。" not in capsys.readouterr().out


def test_homepage_verification_waits_for_dialog_without_page_actions(verification_environment, capsys):
    environment = verification_environment
    environment.challenge.side_effect = ["验证码", "验证码", ""]
    asyncio.run(browser._wait_for_verification(environment.page))
    assert environment.runtime.sleep.await_count == 2
    environment.page.bring_to_front.assert_awaited_once()
    environment.scroll.assert_not_awaited()
    environment.page.goto.assert_not_awaited()
    environment.page.reload.assert_not_awaited()
    environment.page.keyboard.press.assert_not_awaited()
    assert "验证已通过并收到正常响应，继续采集。" not in capsys.readouterr().out


def test_homepage_verification_headless_fails(verification_environment):
    environment = verification_environment
    environment.challenge.return_value = "验证码"
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser._wait_for_verification(environment.page, headless=True))
    assert error.value.category == "challenge"
    environment.runtime.sleep.assert_not_awaited()


@pytest.mark.parametrize("marker", ["", "验证码"])
def test_closed_page_stops_homepage_verification(verification_environment, marker):
    environment = verification_environment
    environment.page.is_closed.return_value = True
    environment.challenge.return_value = marker
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser._wait_for_verification(environment.page))
    assert error.value.category == "browser_closed"


def test_cancelling_homepage_verification_exits_immediately(verification_environment):
    environment = verification_environment
    environment.challenge.return_value = "验证码"

    async def run():
        entered = asyncio.Event()
        blocked = asyncio.Event()

        async def sleep(delay):
            entered.set()
            await blocked.wait()

        environment.runtime.sleep = sleep
        task = asyncio.create_task(browser._wait_for_verification(environment.page))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


@pytest.mark.parametrize("latest", [VERIFY, {"status": 403, "payload": None, "error": ""}])
def test_new_risk_takes_priority_over_buffered_success(verification_environment, latest, capsys):
    environment = verification_environment
    environment.challenge.side_effect = ["验证码", "验证码", "", ""]
    fresh = {**NORMAL, "payload": {**NORMAL["payload"], "cursor": 99}}
    queue = asyncio.Queue()
    for event in [NORMAL, latest, fresh]:
        queue.put_nowait(event)
    result = asyncio.run(browser._wait_search_response(queue, environment.page, timeout=0))
    if latest is VERIFY:
        assert result is fresh
        assert queue.empty()
    else:
        assert result is latest
        assert "验证已通过并收到正常响应，继续采集。" not in capsys.readouterr().out


def test_multiple_buffered_pages_keep_their_order(verification_environment):
    environment = verification_environment
    environment.challenge.side_effect = ["验证码", "验证码", ""]
    second = {**NORMAL, "payload": {**NORMAL["payload"], "cursor": 99}}
    queue = asyncio.Queue()
    queue.put_nowait(NORMAL)
    queue.put_nowait(second)
    assert asyncio.run(browser._wait_search_response(queue, environment.page, timeout=0)) is NORMAL
    assert queue.get_nowait() is second
    assert queue.empty()


def test_unreadable_page_does_not_release_buffered_response(verification_environment):
    environment = verification_environment
    environment.challenge.side_effect = ["验证码", None, ""]
    queue = asyncio.Queue()
    queue.put_nowait(NORMAL)
    assert asyncio.run(browser._wait_search_response(queue, environment.page, timeout=0)) is NORMAL
    environment.runtime.sleep.assert_awaited_once()
    assert environment.challenge.await_count == 3


def test_unreadable_page_does_not_end_visible_verification(verification_environment):
    environment = verification_environment
    environment.challenge.side_effect = ["验证码", None, ""]
    asyncio.run(browser._wait_for_verification(environment.page))
    assert environment.runtime.sleep.await_count == 2
    assert environment.challenge.await_count == 3
