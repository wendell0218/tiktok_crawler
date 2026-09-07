import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dycrawler import browser


STREAM_PATH = "/aweme/v1/web/general/search/stream/"


def encoded(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def chunked(*chunks):
    return b"".join(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" for chunk in chunks) + b"0\r\n\r\n"


def test_decode_plain_json():
    payload = {"status_code": 0, "data": [{"aweme_info": {"aweme_id": "123", "desc": "人工智能"}}]}
    assert browser.decode_search_response(encoded(payload)) == payload


def test_decode_chunked_json():
    payload = {"status_code": 0, "data": [], "cursor": 15, "has_more": 1}
    body = encoded(payload)
    assert browser.decode_search_response(chunked(body[:7], body[7:19], body[19:])) == payload


def test_decode_utf8_character_across_chunks():
    payload = {"status_code": 0, "data": [{"aweme_info": {"aweme_id": "123", "desc": "人工智能"}}]}
    body = encoded(payload)
    boundary = body.index("人".encode("utf-8"))
    framed = chunked(body[:boundary + 1], body[boundary + 1:boundary + 2], body[boundary + 2:])
    assert browser.decode_search_response(framed) == payload


@pytest.mark.parametrize("body", [
    b"2\r\n{}\r\n",
    b"GG\r\n{}\r\n0\r\n\r\n",
    b"3\r\n{}\r\n0\r\n\r\n",
    b"1\r\n{}\r\n0\r\n\r\n",
    b"2\r\n{}\n0\r\n\r\n",
    b"0\r\n",
])
def test_decode_rejects_incomplete_or_malformed_chunks(body):
    with pytest.raises(ValueError):
        browser.decode_search_response(body)


@pytest.mark.parametrize("framed", [False, True])
def test_decode_merges_objects_and_keeps_last_non_none_pagination(framed):
    first = {"aweme_info": {"aweme_id": "1"}}
    second = {"aweme_info": {"aweme_id": "2"}}
    frames = [
        {"status_code": 0, "data": [first], "cursor": 15, "has_more": 1, "search_id": "first"},
        {"status_code": 0, "data": [second], "cursor": 0, "has_more": False, "search_id": "last"},
        {"status_code": None, "data": [], "cursor": None, "has_more": None, "search_id": None},
    ]
    body = b"".join(encoded(frame) for frame in frames)
    if framed:
        body = chunked(body[:11], body[11:83], body[83:])
    payload = browser.decode_search_response(body)
    assert payload["data"] == [first, second]
    assert payload["cursor"] == 0
    assert payload["has_more"] is False
    assert payload["search_id"] == "last"
    assert payload["status_code"] == 0


@pytest.mark.parametrize("risk_first", [False, True])
def test_decode_keeps_verification_from_any_frame(risk_first):
    risk = {"status_code": 0, "data": [], "search_nil_info": {"search_nil_type": "verify_check"}}
    normal = {"status_code": 0, "data": [], "search_nil_info": {"search_nil_type": "none"}}
    frames = [risk, normal] if risk_first else [normal, risk]
    payload = browser.decode_search_response(chunked(*(encoded(frame) for frame in frames)))
    assert payload["search_nil_info"]["search_nil_type"] == "verify_check"


@pytest.mark.parametrize("risk_first", [False, True])
@pytest.mark.parametrize("status_code", [2483, "2483"])
def test_decode_keeps_nonzero_business_status_from_any_frame(risk_first, status_code):
    risk = {"status_code": status_code, "data": []}
    normal = {"status_code": 0, "data": []}
    frames = [risk, normal] if risk_first else [normal, risk]
    payload = browser.decode_search_response(chunked(*(encoded(frame) for frame in frames)))
    assert payload["status_code"] == status_code


@pytest.mark.parametrize("path", [browser.SEARCH_PATH, STREAM_PATH])
def test_capture_search_uses_byte_decoder(path):
    payload = {"status_code": 0, "data": [], "has_more": 0}
    response = SimpleNamespace(
        url=f"https://www.douyin.com{path}?keyword=test",
        request=SimpleNamespace(resource_type="fetch"),
        status=200,
        body=AsyncMock(return_value=chunked(encoded(payload))),
        json=AsyncMock(),
    )
    queue = asyncio.Queue()
    asyncio.run(browser._capture(response, queue, browser.SEARCH_PATHS))
    event = queue.get_nowait()
    assert event == {"status": 200, "payload": payload, "error": ""}
    response.body.assert_awaited_once()
    response.json.assert_not_awaited()


@pytest.mark.parametrize("url,resource_type", [
    (f"https://www.douyin.com.evil.example{STREAM_PATH}", "fetch"),
    (f"https://douyin.com{STREAM_PATH}", "fetch"),
    (f"https://www.douyin.com{STREAM_PATH}extra", "fetch"),
    (f"https://www.douyin.com{browser.SEARCH_PATH}extra", "xhr"),
    (f"https://www.douyin.com{browser.DETAIL_PATH}", "fetch"),
    (f"https://www.douyin.com{STREAM_PATH}", "document"),
])
def test_capture_ignores_unrelated_host_path_or_resource(url, resource_type):
    response = SimpleNamespace(
        url=url,
        request=SimpleNamespace(resource_type=resource_type),
        status=200,
        body=AsyncMock(),
        json=AsyncMock(),
    )
    queue = asyncio.Queue()
    asyncio.run(browser._capture(response, queue, browser.SEARCH_PATHS))
    assert queue.empty()
    response.body.assert_not_awaited()
    response.json.assert_not_awaited()


@pytest.mark.parametrize("status", [200, 429])
def test_capture_enqueues_invalid_body_without_losing_http_status(status):
    response = SimpleNamespace(
        url=f"https://www.douyin.com{STREAM_PATH}",
        request=SimpleNamespace(resource_type="xhr"),
        status=status,
        body=AsyncMock(return_value=b"2\r\n{}\r\n"),
        json=AsyncMock(),
    )
    queue = asyncio.Queue()
    asyncio.run(browser._capture(response, queue, browser.SEARCH_PATHS))
    event = queue.get_nowait()
    assert event["status"] == status
    assert event["payload"] is None
    assert event["error"]


def test_capture_detail_keeps_json_behavior():
    payload = {"status_code": 0, "aweme_detail": {"aweme_id": "123"}}
    response = SimpleNamespace(
        url=f"https://www.douyin.com{browser.DETAIL_PATH}",
        request=SimpleNamespace(resource_type="xhr"),
        status=200,
        body=AsyncMock(),
        json=AsyncMock(return_value=payload),
    )
    queue = asyncio.Queue()
    asyncio.run(browser._capture(response, queue, browser.DETAIL_PATH))
    assert queue.get_nowait() == {"status": 200, "payload": payload, "error": ""}
    response.json.assert_awaited_once()
    response.body.assert_not_awaited()


def test_capture_single_path_does_not_implicitly_include_stream():
    response = SimpleNamespace(
        url=f"https://www.douyin.com{STREAM_PATH}",
        request=SimpleNamespace(resource_type="fetch"),
        status=200,
        body=AsyncMock(),
        json=AsyncMock(),
    )
    queue = asyncio.Queue()
    asyncio.run(browser._capture(response, queue, browser.SEARCH_PATH))
    assert queue.empty()
    response.body.assert_not_awaited()
    response.json.assert_not_awaited()


@pytest.mark.parametrize("data", [[], None])
def test_search_verification_response_stops_before_pagination(monkeypatch, data):
    page = SimpleNamespace(
        goto=AsyncMock(),
        wait_for_url=AsyncMock(),
        get_by_placeholder=Mock(return_value=SimpleNamespace(fill=AsyncMock(), press=AsyncMock())),
        evaluate=AsyncMock(return_value="test-browser"),
        mouse=SimpleNamespace(wheel=AsyncMock()),
        wait_for_timeout=AsyncMock(),
        is_closed=Mock(return_value=False),
    )
    context = SimpleNamespace(on=Mock())
    close = AsyncMock()
    payload = browser.decode_search_response(chunked(encoded({
        "status_code": 0,
        "data": data,
        "has_more": 0,
        "search_nil_info": {"search_nil_type": "verify_check"},
    })))
    queue = asyncio.Queue()
    queue.put_nowait({"status": 200, "payload": payload, "error": ""})
    monkeypatch.setattr(browser.asyncio, "Queue", lambda: queue)
    monkeypatch.setattr(browser, "_challenge", AsyncMock(return_value=""))
    monkeypatch.setattr(browser, "_wait_entry_ready", AsyncMock())
    monkeypatch.setattr(browser, "_submit_search", AsyncMock())
    wait = AsyncMock(wraps=browser._wait_search_response)
    monkeypatch.setattr(browser, "_open", AsyncMock(return_value=(None, context, page)))
    monkeypatch.setattr(browser, "_close", close)
    monkeypatch.setattr(browser, "_logged_in", AsyncMock(return_value=True))
    monkeypatch.setattr(browser, "_wait_search_response", wait)
    with pytest.raises(browser.BrowserRisk) as error:
        asyncio.run(browser.search("测试", "unused-profile", headless=True))
    assert error.value.category == "challenge"
    assert "需要安全验证" in error.value.detail
    assert error.value.status == 200
    wait.assert_awaited_once()
    page.mouse.wheel.assert_not_awaited()
    page.wait_for_timeout.assert_not_awaited()
    close.assert_awaited_once()
