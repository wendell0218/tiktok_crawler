import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dycrawler import browser


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("channel", ["chrome", "chromium"])
def test_open_uses_native_macos_keychain_without_changing_proxy(tmp_path, monkeypatch, platform, channel):
    page = object()
    context = SimpleNamespace(pages=[page], set_default_timeout=Mock(), set_default_navigation_timeout=Mock())
    launch = AsyncMock(return_value=context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(browser.sys, "platform", platform)
    monkeypatch.setattr(browser, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=playwright)))

    assert asyncio.run(browser._open(tmp_path, channel, False, 60000, False)) == (playwright, context, page)

    options = launch.call_args.kwargs
    expected = {
        "user_data_dir": str(tmp_path.resolve()),
        "headless": False,
        "chromium_sandbox": True,
        "accept_downloads": False,
        "viewport": None,
    }
    if channel != "chromium":
        expected["channel"] = channel
    if platform == "darwin":
        expected["ignore_default_args"] = ["--use-mock-keychain", "--password-store=basic"]
    assert options == expected


@pytest.mark.parametrize("cookies", [
    [],
    [{"name": "LOGIN_STATUS", "value": "1"}],
    [{"name": "sessionid", "value": ""}],
    [{"name": "sessionid", "value": "   "}],
    [{"name": "sessionid", "value": "test-session", "expires": 999}],
    [{"name": "sessionid_ss", "value": "test-session", "expires": 1000}],
])
def test_logged_in_rejects_stale_flags_and_unusable_session_cookies(monkeypatch, cookies):
    context = SimpleNamespace(cookies=AsyncMock(return_value=cookies))
    page = SimpleNamespace(evaluate=AsyncMock(return_value=True))
    panel = AsyncMock(return_value=False)
    monkeypatch.setattr(browser.time, "time", lambda: 1000)
    monkeypatch.setattr(browser, "_login_panel", panel)

    assert asyncio.run(browser._logged_in(context, page)) is False
    page.evaluate.assert_not_awaited()
    context.cookies.assert_awaited_once_with("https://www.douyin.com/")


@pytest.mark.parametrize("name", ["sessionid", "sessionid_ss", "sid_guard"])
@pytest.mark.parametrize("expires", [-1, None, 1001])
@pytest.mark.parametrize("panel_visible", [False, True, None])
def test_logged_in_requires_usable_cookie_and_no_login_panel(monkeypatch, name, expires, panel_visible):
    context = SimpleNamespace(cookies=AsyncMock(return_value=[{"name": name, "value": "test-session", "expires": expires}]))
    page = object()
    panel = AsyncMock(return_value=panel_visible)
    monkeypatch.setattr(browser.time, "time", lambda: 1000)
    monkeypatch.setattr(browser, "_login_panel", panel)

    assert asyncio.run(browser._logged_in(context, page)) is (panel_visible is False)
    panel.assert_awaited_once_with(page)


@pytest.fixture
def login_environment(monkeypatch):
    button = SimpleNamespace(click=AsyncMock())
    page = SimpleNamespace(
        goto=AsyncMock(),
        bring_to_front=AsyncMock(),
        get_by_text=Mock(return_value=SimpleNamespace(last=button)),
        wait_for_timeout=AsyncMock(),
        is_closed=Mock(return_value=False),
    )
    context = object()
    close = AsyncMock()
    logged_in = AsyncMock(return_value=True)
    challenge = AsyncMock(return_value="")
    panel = AsyncMock(return_value=False)
    monkeypatch.setattr(browser, "_open", AsyncMock(return_value=(None, context, page)))
    monkeypatch.setattr(browser, "_close", close)
    monkeypatch.setattr(browser, "_logged_in", logged_in)
    monkeypatch.setattr(browser, "_challenge", challenge)
    monkeypatch.setattr(browser, "_login_panel", panel)
    return SimpleNamespace(page=page, context=context, close=close, button=button, logged_in=logged_in, challenge=challenge, panel=panel)


def test_login_waits_for_manual_verification_before_accepting_session(login_environment):
    env = login_environment
    env.challenge.side_effect = ["slider verification", ""]

    async def wait_for_manual_verification(timeout):
        assert timeout == 2000
        env.logged_in.assert_not_awaited()
        env.button.click.assert_not_awaited()

    env.page.wait_for_timeout.side_effect = wait_for_manual_verification

    assert asyncio.run(browser.login("unused-profile")) == {"logged_in": True}
    assert env.challenge.await_count == 2
    env.logged_in.assert_awaited_once_with(env.context, env.page)
    env.button.click.assert_not_awaited()
    env.close.assert_awaited_once()


def test_login_opens_panel_once_after_verification_clears(login_environment):
    env = login_environment
    env.challenge.side_effect = ["slider verification", "", "", ""]
    env.logged_in.side_effect = [False, False, True]

    assert asyncio.run(browser.login("unused-profile")) == {"logged_in": True}
    env.button.click.assert_awaited_once_with(timeout=15000)
    assert env.page.wait_for_timeout.await_count == 3
    env.close.assert_awaited_once()


def test_login_keeps_existing_login_panel_open(login_environment):
    env = login_environment
    env.logged_in.side_effect = [False, True]
    env.panel.return_value = True

    assert asyncio.run(browser.login("unused-profile")) == {"logged_in": True}
    env.button.click.assert_not_awaited()
    env.page.wait_for_timeout.assert_awaited_once_with(2000)
    env.close.assert_awaited_once()


def test_login_pauses_timeout_during_manual_verification(login_environment, monkeypatch):
    env = login_environment
    elapsed = [0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: elapsed[0])
    env.challenge.side_effect = ["slider verification", "slider verification", ""]

    async def wait_for_manual_verification(timeout):
        elapsed[0] += 30

    env.page.wait_for_timeout.side_effect = wait_for_manual_verification

    assert asyncio.run(browser.login("unused-profile", timeout_ms=1000)) == {"logged_in": True}
    assert elapsed[0] == 60
    env.logged_in.assert_awaited_once()
    env.close.assert_awaited_once()


def test_login_without_verification_keeps_normal_timeout(login_environment, monkeypatch):
    env = login_environment
    elapsed = [0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: elapsed[0])
    env.logged_in.return_value = False

    async def wait_for_login(timeout):
        elapsed[0] += 2

    env.page.wait_for_timeout.side_effect = wait_for_login

    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser.login("unused-profile", timeout_ms=1000))
    assert error.value.category == "login_timeout"
    env.close.assert_awaited_once()


def test_login_reports_browser_closed_during_manual_verification(login_environment):
    env = login_environment
    env.challenge.return_value = "slider verification"

    async def close_browser(timeout):
        env.page.is_closed.return_value = True

    env.page.wait_for_timeout.side_effect = close_browser

    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser.login("unused-profile"))
    assert error.value.category == "browser_closed"
    env.logged_in.assert_not_awaited()
    env.close.assert_awaited_once()


def test_login_waits_for_readable_page_before_checking_session(login_environment):
    env = login_environment
    env.challenge.side_effect = [None, ""]

    async def wait_for_readable_page(timeout):
        env.logged_in.assert_not_awaited()
        env.button.click.assert_not_awaited()

    env.page.wait_for_timeout.side_effect = wait_for_readable_page

    assert asyncio.run(browser.login("unused-profile")) == {"logged_in": True}
    env.logged_in.assert_awaited_once()
    env.page.wait_for_timeout.assert_awaited_once_with(2000)
    env.close.assert_awaited_once()


def test_unreadable_page_without_verification_still_times_out(login_environment, monkeypatch):
    env = login_environment
    elapsed = [0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: elapsed[0])
    env.challenge.return_value = None

    async def wait_for_readable_page(timeout):
        elapsed[0] += 2

    env.page.wait_for_timeout.side_effect = wait_for_readable_page

    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser.login("unused-profile", timeout_ms=1000))
    assert error.value.category == "login_timeout"
    env.logged_in.assert_not_awaited()
    env.button.click.assert_not_awaited()
    env.close.assert_awaited_once()


def test_unreadable_page_during_verification_keeps_timeout_paused(login_environment, monkeypatch):
    env = login_environment
    elapsed = [0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: elapsed[0])
    env.challenge.side_effect = ["slider verification", None, None, ""]

    async def wait_for_verification(timeout):
        env.logged_in.assert_not_awaited()
        env.button.click.assert_not_awaited()
        elapsed[0] += 30

    env.page.wait_for_timeout.side_effect = wait_for_verification

    assert asyncio.run(browser.login("unused-profile", timeout_ms=1000)) == {"logged_in": True}
    assert elapsed[0] == 90
    env.logged_in.assert_awaited_once()
    env.close.assert_awaited_once()


def test_login_does_not_click_when_login_panel_is_unreadable(login_environment):
    env = login_environment
    env.logged_in.side_effect = [False, True]
    env.panel.return_value = None

    assert asyncio.run(browser.login("unused-profile")) == {"logged_in": True}
    env.button.click.assert_not_awaited()
    env.page.wait_for_timeout.assert_awaited_once_with(2000)
    env.close.assert_awaited_once()
