import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dycrawler import browser


@pytest.fixture
def environment(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    page = SimpleNamespace(
        is_closed=Mock(return_value=False), wait_for_load_state=AsyncMock(),
        bring_to_front=AsyncMock(), goto=AsyncMock(), evaluate=AsyncMock(return_value="Chrome"),
    )
    challenge = AsyncMock(return_value="")
    monkeypatch.setattr(browser, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(browser, "_challenge", challenge)
    return SimpleNamespace(page=page, clock=clock, challenge=challenge)


def test_entry_wait_completes_only_after_real_load(environment, capsys):
    def loading(state, timeout):
        assert state == "load"
        if environment.page.wait_for_load_state.await_count <= 2:
            environment.clock.now += timeout / 1000
            raise browser.PlaywrightTimeoutError("still loading")

    environment.page.wait_for_load_state.side_effect = loading
    asyncio.run(browser._wait_entry_ready(environment.page))
    assert [call.kwargs["timeout"] for call in environment.page.wait_for_load_state.await_args_list] == [5000, 5000, 5000]
    output = capsys.readouterr().out
    assert "正在加载搜索入口" in output
    assert "搜索入口加载完成。" in output
    assert "搜索入口加载超时" not in output


def test_entry_load_has_a_bounded_ordinary_deadline(environment, capsys):
    def loading(state, timeout):
        environment.clock.now += timeout / 1000
        raise browser.PlaywrightTimeoutError("still loading")

    environment.page.wait_for_load_state.side_effect = loading
    assert asyncio.run(browser._wait_entry_ready(environment.page, timeout=12)) is False
    assert [call.kwargs["timeout"] for call in environment.page.wait_for_load_state.await_args_list] == [5000, 5000, 2000]
    assert "搜索入口加载超时，正在检查搜索控件是否可用。" in capsys.readouterr().out


def test_manual_verification_and_unreadable_transition_do_not_consume_load_budget(environment, monkeypatch):
    environment.challenge.side_effect = ["", "安全验证", None, "", ""]

    def loading(state, timeout):
        if environment.page.wait_for_load_state.await_count == 1:
            environment.clock.now += timeout / 1000
            raise browser.PlaywrightTimeoutError("still loading")

    async def manual_wait(seconds):
        environment.clock.now += 300
        assert environment.page.wait_for_load_state.await_count == 1
        environment.page.goto.assert_not_awaited()

    environment.page.wait_for_load_state.side_effect = loading
    sleep = AsyncMock(side_effect=manual_wait)
    monkeypatch.setattr(browser.asyncio, "sleep", sleep)
    asyncio.run(browser._wait_entry_ready(environment.page, timeout=7))
    assert [call.kwargs["timeout"] for call in environment.page.wait_for_load_state.await_args_list] == [5000, 2000]
    assert sleep.await_count == 2
    assert environment.clock.now == 605
    environment.page.bring_to_front.assert_awaited_once()


def test_headless_entry_challenge_is_not_ignored(environment):
    environment.challenge.return_value = "安全验证"
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser._wait_entry_ready(environment.page, headless=True))
    assert error.value.category == "challenge"
    environment.page.wait_for_load_state.assert_not_awaited()


def test_closed_entry_browser_has_an_explicit_reason(environment):
    def closed(state, timeout):
        environment.page.is_closed.return_value = True
        raise RuntimeError("closed")

    environment.page.wait_for_load_state.side_effect = closed
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser._wait_entry_ready(environment.page))
    assert error.value.category == "browser_closed"


def test_search_still_requires_guarded_submission_after_entry_load_timeout(environment, monkeypatch):
    def loading(state, timeout):
        environment.clock.now += timeout / 1000
        raise browser.PlaywrightTimeoutError("still loading")

    environment.page.wait_for_load_state.side_effect = loading
    context = SimpleNamespace(on=Mock(), remove_listener=Mock())
    logged_in = AsyncMock(return_value=True)
    submit = AsyncMock(side_effect=browser.BrowserRisk("search_input_unstable", "The input was not retained"))
    monkeypatch.setattr(browser, "_logged_in", logged_in)
    monkeypatch.setattr(browser, "_submit_search", submit)
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser.search("街舞", "unused", session=(None, context, environment.page)))
    assert error.value.category == "search_input_unstable"
    assert environment.page.wait_for_load_state.await_count == 36
    environment.page.goto.assert_awaited_once_with("https://www.douyin.com/jingxuan", wait_until="domcontentloaded")
    logged_in.assert_awaited_once()
    submit.assert_awaited_once()
    assert context.remove_listener.call_count == 2


def test_search_waits_through_verification_then_submits_only_once(environment, monkeypatch):
    environment.challenge.side_effect = ["安全验证", None, "", "", "", ""]
    context = SimpleNamespace(on=Mock(), remove_listener=Mock())
    logged_in = AsyncMock(return_value=True)
    submit = AsyncMock()

    async def manual_wait(seconds):
        environment.clock.now += 300
        logged_in.assert_not_awaited()
        submit.assert_not_awaited()
        environment.page.wait_for_load_state.assert_not_awaited()

    monkeypatch.setattr(browser.asyncio, "sleep", manual_wait)
    monkeypatch.setattr(browser, "_logged_in", logged_in)
    monkeypatch.setattr(browser, "_submit_search", submit)
    monkeypatch.setattr(browser, "_wait_search_response", AsyncMock(return_value={
        "status": 200, "payload": {"status_code": 0, "data": [], "has_more": 0}, "error": "",
    }))
    records, pages = asyncio.run(browser.search("街舞", "unused", session=(None, context, environment.page)))
    assert records == [] and len(pages) == 1
    environment.page.goto.assert_awaited_once()
    environment.page.wait_for_load_state.assert_awaited_once()
    logged_in.assert_awaited_once()
    submit.assert_awaited_once()
