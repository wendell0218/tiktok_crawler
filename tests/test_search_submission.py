import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dycrawler import browser


@pytest.fixture
def environment(monkeypatch):
    state = {"active": False, "requested": False, "response_started": False, "captured": False, "rejected": 0}
    box = SimpleNamespace(wait_for=AsyncMock(), click=AsyncMock(), fill=AsyncMock(), input_value=AsyncMock(), press=AsyncMock())
    box.fill.side_effect = lambda keyword, **kwargs: setattr(box.input_value, "return_value", keyword)
    box.press.side_effect = lambda *args, **kwargs: state.update(requested=True)
    button = SimpleNamespace(wait_for=AsyncMock(), click=AsyncMock())
    button.click.side_effect = lambda **kwargs: state.update(requested=True) if not kwargs.get("trial") else None
    page = SimpleNamespace(
        url="https://www.douyin.com/jingxuan", is_closed=Mock(return_value=False),
        get_by_placeholder=Mock(return_value=box), get_by_role=Mock(return_value=button),
        wait_for_timeout=AsyncMock(), goto=AsyncMock(), evaluate=AsyncMock(return_value="Chrome"),
        wait_for_load_state=AsyncMock(),
    )
    challenge = AsyncMock(return_value="")
    monkeypatch.setattr(browser, "_challenge", challenge)
    monkeypatch.setattr(browser, "_login_panel", AsyncMock(return_value=False))
    return SimpleNamespace(page=page, box=box, button=button, state=state, queue=asyncio.Queue(), challenge=challenge)


def submit(environment):
    return asyncio.run(browser._submit_search(environment.page, environment.queue, "街舞", environment.state))


def test_actionable_input_is_retained_before_single_enter(environment, capsys):
    submit(environment)
    environment.box.wait_for.assert_awaited_once_with(state="visible", timeout=15000)
    environment.box.click.assert_awaited_once_with(trial=True, timeout=15000)
    environment.box.fill.assert_awaited_once_with("街舞", timeout=15000)
    environment.box.press.assert_awaited_once_with("Enter", timeout=15000)
    assert environment.box.input_value.await_count == 3
    environment.page.get_by_role.assert_not_called()
    assert "搜索提交 | 状态：请求已发出 | 不重复提交" in capsys.readouterr().out


def test_reset_input_is_refilled_before_enter(environment):
    environment.box.input_value.side_effect = ["", "街舞", "街舞", "街舞"]
    submit(environment)
    assert environment.box.fill.await_count == 2
    environment.box.press.assert_awaited_once()
    environment.page.get_by_role.assert_not_called()


def test_repeated_input_reset_has_a_bounded_failure(environment):
    environment.box.input_value.side_effect = lambda **kwargs: ""
    with pytest.raises(browser.BrowserRisk) as error:
        submit(environment)
    assert error.value.category == "search_input_unstable"
    assert environment.box.fill.await_count == 2
    environment.box.press.assert_not_awaited()
    environment.button.click.assert_not_awaited()


def test_unconfirmed_enter_uses_one_visible_button_fallback(environment):
    environment.box.press.side_effect = None
    submit(environment)
    environment.box.press.assert_awaited_once()
    assert environment.box.fill.await_count == 2
    environment.page.get_by_role.assert_called_once_with("button", name="搜索", exact=True)
    assert [call.kwargs for call in environment.button.click.await_args_list] == [
        {"trial": True, "timeout": 5000}, {"timeout": 5000},
    ]
    environment.page.goto.assert_not_awaited()


@pytest.mark.parametrize("flag", ["requested", "response_started", "captured"])
def test_inflight_request_or_response_disables_button_fallback(environment, flag):
    environment.box.press.side_effect = lambda *args, **kwargs: environment.state.update({flag: True})
    submit(environment)
    assert environment.queue.empty()
    environment.page.get_by_role.assert_not_called()


def test_matching_route_must_change_after_enter(environment):
    environment.page.url = "https://www.douyin.com/search/街舞"
    environment.box.press.side_effect = None
    submit(environment)
    environment.box.press.assert_awaited_once()
    assert environment.button.click.await_count == 2


def test_new_matching_route_confirms_navigation_without_duplicate_click(environment):
    environment.box.press.side_effect = lambda *args, **kwargs: setattr(environment.page, "url", "https://www.douyin.com/search/街舞?new=1")
    submit(environment)
    environment.page.get_by_role.assert_not_called()


def test_late_request_during_button_actionability_check_disables_click(environment):
    environment.box.press.side_effect = None
    environment.button.click.side_effect = lambda **kwargs: environment.state.update(requested=True)
    submit(environment)
    environment.button.click.assert_awaited_once_with(trial=True, timeout=5000)


def test_late_request_during_last_ui_inspection_disables_click(environment):
    environment.box.press.side_effect = None

    def inspect(page):
        if environment.button.click.await_count:
            environment.state["requested"] = True
        return ""

    environment.challenge.side_effect = inspect
    submit(environment)
    environment.button.click.assert_awaited_once_with(trial=True, timeout=5000)


def test_verification_after_enter_never_triggers_automatic_fallback(environment):
    environment.box.press.side_effect = lambda *args, **kwargs: setattr(environment.challenge, "return_value", "安全验证")
    submit(environment)
    environment.box.press.assert_awaited_once()
    environment.page.get_by_role.assert_not_called()


def test_pre_submission_verification_survives_unreadable_transition(environment, monkeypatch):
    markers = iter(["安全验证", None, ""])
    environment.challenge.side_effect = lambda page: next(markers, "")
    environment.page.bring_to_front = AsyncMock()

    async def manual_wait(seconds):
        environment.box.fill.assert_not_awaited()
        environment.box.press.assert_not_awaited()
        environment.button.click.assert_not_awaited()

    sleep = AsyncMock(side_effect=manual_wait)
    monkeypatch.setattr(browser.asyncio, "sleep", sleep)
    submit(environment)
    assert sleep.await_count == 2
    environment.page.bring_to_front.assert_awaited_once()
    environment.box.fill.assert_awaited_once_with("街舞", timeout=15000)
    environment.box.press.assert_awaited_once_with("Enter", timeout=15000)
    environment.page.get_by_role.assert_not_called()


@pytest.mark.parametrize("status", [403, 429])
def test_http_rejection_is_not_retried(environment, status):
    environment.box.press.side_effect = lambda *args, **kwargs: environment.state.update(rejected=status)
    with pytest.raises(browser.BrowserRisk) as error:
        submit(environment)
    assert error.value.category == "http_rejected"
    assert error.value.status == status
    environment.page.get_by_role.assert_not_called()


def test_unreadable_page_never_receives_input_or_a_submission(environment):
    environment.challenge.return_value = None
    with pytest.raises(browser.BrowserRisk) as error:
        submit(environment)
    assert error.value.category == "search_page_unreadable"
    assert environment.page.wait_for_timeout.await_count == 10
    environment.box.fill.assert_not_awaited()
    environment.box.press.assert_not_awaited()


def test_missing_button_ends_with_explicit_failure_instead_of_manual_search(environment):
    environment.box.press.side_effect = None
    environment.button.wait_for.side_effect = TimeoutError
    with pytest.raises(browser.BrowserRisk) as error:
        submit(environment)
    assert error.value.category == "search_navigation_timeout"
    environment.box.press.assert_awaited_once()
    environment.button.wait_for.assert_awaited_once()
    environment.button.click.assert_not_awaited()


@pytest.mark.parametrize("url,other_page", [
    ("https://www.douyin.com/aweme/v1/web/general/search/single/?keyword=别的", False),
    ("https://www.douyin.com.evil.example/aweme/v1/web/general/search/single/?keyword=街舞", False),
    ("https://www.douyin.com/aweme/v1/web/general/search/single/?keyword=街舞", True),
])
def test_request_ownership_rejects_unrelated_search(environment, url, other_page):
    request = SimpleNamespace(url=url, resource_type="fetch", frame=SimpleNamespace(page=object() if other_page else environment.page))
    assert not browser._search_request_matches(request, environment.page, "街舞")


def test_request_observer_accepts_inflight_request_and_ignores_old_response(environment, monkeypatch):
    handlers = {}
    context = SimpleNamespace(on=lambda event, handler: handlers.update({event: handler}), remove_listener=Mock())
    request = SimpleNamespace(
        url="https://www.douyin.com/aweme/v1/web/general/search/stream/?keyword=街舞",
        resource_type="fetch", frame=SimpleNamespace(page=environment.page),
    )
    old_request = SimpleNamespace(**vars(request))
    old_response = SimpleNamespace(request=old_request, status=200)
    captured = AsyncMock()
    monkeypatch.setattr(browser, "_capture", captured)
    monkeypatch.setattr(browser, "_logged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(browser, "_wait_for_verification", AsyncMock())
    monkeypatch.setattr(browser, "_wait_search_response", AsyncMock(return_value={
        "status": 200, "payload": {"status_code": 0, "data": [], "has_more": 0}, "error": "",
    }))

    async def press(*args, **kwargs):
        assert handlers["response"](old_response) is None
        captured.assert_not_awaited()
        handlers["request"](request)

    environment.box.press.side_effect = press
    records, pages = asyncio.run(browser.search("街舞", "unused", session=(None, context, environment.page)))
    assert records == [] and len(pages) == 1
    captured.assert_not_awaited()
    environment.box.press.assert_awaited_once()
    environment.page.get_by_role.assert_not_called()
    assert context.remove_listener.call_count == 2
