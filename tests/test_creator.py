import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from dycrawler import creator
from dycrawler.browser import BrowserRisk
from dycrawler.normalize import PayloadError


USER = {"sec_uid": "MS4wLjCreator", "url": "https://www.douyin.com/user/MS4wLjCreator"}


def aweme(aweme_id, **fields):
    return {
        "aweme_id": str(aweme_id),
        "author": {"sec_uid": USER["sec_uid"], "nickname": "Creator"},
        "video": {"play_addr": {"url_list": [f"https://cdn.example.com/{aweme_id}.mp4"]}},
        **fields,
    }


def co_created_aweme(**fields):
    return aweme("7619622418637720878", **{
        "author": {
            "uid": "21617530639355",
            "sec_uid": "MS4wLjABAAAAssihLDGWRZQW6LPBR9aTi5UTO-vgXikwTObIvrMCz_Q",
            "nickname": "亿点点不一样",
        },
        "cooperation_info": {"co_creators": [
            {
                "uid": "98357128966",
                "sec_uid": "MS4wLjABAAAAnqAgClWafGXObJRi5PEV01XuV4m66GZ4GC2MvaaHXYU",
                "nickname": "F1世界锦标赛", "role_title": "出镜", "invite_status": 1,
            },
            {
                "uid": "105525949232", "sec_uid": USER["sec_uid"],
                "nickname": "影视飓风", "role_title": "出镜", "invite_status": 1,
            },
        ]},
        **fields,
    })


def payload(items, cursor=0, more=0):
    return {"status_code": 0, "aweme_list": items, "max_cursor": cursor, "has_more": more}


def queued_route(page, cursor=0):
    request = SimpleNamespace(
        url=f"https://www.douyin.com{creator.USER_POST_PATH}?sec_user_id={USER['sec_uid']}&max_cursor={cursor}",
        resource_type="xhr", frame=SimpleNamespace(page=page),
    )
    return SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock())


class FakeContext:
    def __init__(self, responses):
        self.responses = list(responses)
        self.listeners = {}
        self.gate = None
        self.route_pattern = None
        self.api_host = "www.douyin.com"
        self.continued = 0
        self.page_listeners = {}
        self.visible_challenge = False
        self.page = SimpleNamespace(
            is_closed=lambda: False,
            goto=AsyncMock(side_effect=self.navigate),
            evaluate=AsyncMock(return_value="Test Browser"),
            bring_to_front=AsyncMock(),
            url=USER["url"],
            main_frame=SimpleNamespace(url=USER["url"]),
            on=lambda event, handler: self.page_listeners.update({event: handler}),
            remove_listener=lambda event, handler: self.page_listeners.pop(event),
        )
        self.page.main_frame.page = self.page

    def on(self, event, handler):
        self.listeners[event] = handler

    def remove_listener(self, event, handler):
        assert self.listeners.pop(event) is handler

    async def route(self, pattern, handler):
        self.route_pattern = pattern
        self.gate = handler

    async def unroute(self, pattern, handler):
        assert self.gate is handler
        self.gate = None

    async def navigate(self, url, **kwargs):
        if "request" in self.listeners:
            self.listeners["request"](SimpleNamespace(resource_type="document", frame=self.page.main_frame, url=url))
        self.page.url = url
        self.page.main_frame.url = url
        if "framenavigated" in self.page_listeners:
            self.page_listeners["framenavigated"](self.page.main_frame)
        await self.emit_next()

    async def emit_next(self, *args, **kwargs):
        if not self.responses:
            return
        values = self.responses.pop(0)
        values = values if isinstance(values, list) else [values]
        request = SimpleNamespace(
            url=f"https://{self.api_host}{creator.USER_POST_PATH}?sec_user_id={USER['sec_uid']}",
            resource_type="xhr", frame=SimpleNamespace(page=self.page),
        )
        self.last_request = request

        async def proceed():
            self.continued += 1

        if self.gate and self.route_pattern.search(request.url):
            aborted = []
            await self.gate(SimpleNamespace(request=request, continue_=proceed, abort=AsyncMock(side_effect=lambda reason: aborted.append(reason))))
            if aborted:
                return
        for value in values:
            if value.get("search_nil_info", {}).get("search_nil_type") == "verify_check":
                self.visible_challenge = True
            response = SimpleNamespace(request=request, status=value.get("http_status", 200), json=AsyncMock(return_value=value))
            self.listeners["response"](response)
            await asyncio.sleep(0)


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(creator, "_challenge", AsyncMock(return_value=""))
    monkeypatch.setattr(creator, "_login_panel", AsyncMock(return_value=False))

    def build(responses):
        context = FakeContext(responses)
        monkeypatch.setattr(creator, "_scroll_search", AsyncMock(side_effect=context.emit_next))
        return context, (object(), context, context.page)

    return build


@pytest.mark.parametrize("target", [USER["sec_uid"], USER["url"], USER["url"] + "?from=search"])
def test_profile_input_resolves_without_network(target):
    assert creator.profile_id(target) == USER["sec_uid"]
    assert asyncio.run(creator.resolve_user(target, None))["sec_uid"] == USER["sec_uid"]


@pytest.mark.parametrize("target", [
    "http://www.douyin.com/user/abc", "https://evil.example/user/abc",
    "https://www.douyin.com@evil.example/user/abc", "https://user:password@www.douyin.com/user/abc",
    "https://www.douyin.com/video/123", "https://api.douyin.com/user/abc",
])
def test_profile_url_rejects_wrong_origin_or_path(target):
    with pytest.raises(ValueError):
        creator.profile_id(target)


def test_extract_users_uses_user_list_schema_and_deduplicates():
    info = {"sec_uid": "MS4wLjCreator", "uid": "123", "nickname": "影视飓风", "unique_id": "12345"}
    users = creator.extract_users({"status_code": 0, "user_list": [{"user_info": info}, {"user_info": info}, None]})
    assert len(users) == 1
    assert users[0]["nickname"] == "影视飓风"
    assert users[0]["url"] == USER["url"]
    with pytest.raises(PayloadError):
        creator.extract_users({"status_code": 0, "data": []})


@pytest.mark.parametrize("same_name", [False, True])
def test_nickname_search_requires_a_unique_exact_identity(monkeypatch, same_name):
    context = FakeContext([])
    users = [{"user_info": {"sec_uid": USER["sec_uid"], "nickname": "影视飓风"}}]
    if same_name:
        users.append({"user_info": {"sec_uid": "MS4wLjOther", "nickname": "影视飓风"}})
    monkeypatch.setattr(creator, "wait_response", AsyncMock(return_value={"payload": {"status_code": 0, "user_list": users}}))
    session = (object(), context, context.page)
    if same_name:
        with pytest.raises(BrowserRisk) as error:
            asyncio.run(creator.resolve_user("影视飓风", session))
        assert error.value.category == "user_ambiguous"
    else:
        assert asyncio.run(creator.resolve_user("影视飓风", session))["sec_uid"] == USER["sec_uid"]
    assert not context.listeners


@pytest.mark.parametrize("host", ["www.douyin.com", "api.douyin.com"])
@pytest.mark.parametrize("difference", ["host", "path", "query", "wrong_user", "frame", "resource"])
def test_response_capture_rejects_unrelated_requests(difference, host):
    page = object()
    request = SimpleNamespace(
        url=f"https://{host}{creator.USER_POST_PATH}?sec_user_id={USER['sec_uid']}",
        resource_type="xhr", frame=SimpleNamespace(page=page),
    )
    assert creator.matches(request, page, creator.USER_POST_PATH, {"sec_user_id": USER["sec_uid"]})
    if difference == "host":
        request.url = request.url.replace(host, "evil.example")
    elif difference == "path":
        request.url = request.url.replace("/post/", "/favorite/")
    elif difference == "query":
        request.url += "&sec_user_id=another_user"
    elif difference == "wrong_user":
        request.url = request.url.replace(USER["sec_uid"], "another_user")
    elif difference == "frame":
        request.frame.page = object()
    else:
        request.resource_type = "document"
    assert not creator.matches(request, page, creator.USER_POST_PATH, {"sec_user_id": USER["sec_uid"]})


@pytest.mark.parametrize("host,expected", [
    ("douyin.com", True), ("www.douyin.com", True), ("api.douyin.com", True),
    ("evil-douyin.com", False), ("douyin.com.evil.example", False),
])
@pytest.mark.parametrize("resource_type", ["xhr", "fetch"])
def test_api_domain_matching_uses_complete_dns_suffix(host, expected, resource_type):
    page = object()
    request = SimpleNamespace(
        url=f"https://{host}{creator.USER_POST_PATH}?sec_user_id={USER['sec_uid']}",
        resource_type=resource_type, frame=SimpleNamespace(page=page),
    )
    assert creator.matches(request, page, creator.USER_POST_PATH, {"sec_user_id": USER["sec_uid"]}) is expected


def test_subdomain_api_is_routed_captured_and_keeps_page_download_barrier(environment):
    async def run():
        context, session = environment([payload([aweme(1)], 100, 1), payload([aweme(2)])])
        context.api_host = "api.douyin.com"
        entered, release = asyncio.Event(), asyncio.Event()

        async def callback(batch, page):
            if page["page"] == 1:
                entered.set()
                await release.wait()

        collect = asyncio.create_task(creator.user_posts(USER, session, page_delay=0, on_page=callback))
        await entered.wait()
        assert context.continued == 1
        assert context.route_pattern.search(context.last_request.url)
        creator._scroll_search.assert_not_awaited()
        release.set()
        records, pages = await collect
        assert [row["aweme_id"] for row in records] == ["1", "2"]
        assert len(pages) == 2 and context.continued == 2

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize("url,expected", [
    ("https://douyin.com/aweme/v1/web/aweme/post/", True),
    ("https://api.douyin.com/aweme/v1/web/aweme/post/?max_cursor=100", True),
    ("http://api.douyin.com/aweme/v1/web/aweme/post/", False),
    ("https://evil-douyin.com/aweme/v1/web/aweme/post/", False),
    ("https://douyin.com.evil.example/aweme/v1/web/aweme/post/", False),
    ("https://api.douyin.com/aweme/v1/web/aweme/post/extra", False),
    ("https://api.douyin.com/aweme/v1/web/aweme/favorite/", False),
])
def test_user_post_route_pattern_is_https_and_fixed_endpoint(environment, url, expected):
    context, session = environment([payload([aweme(1)])])
    asyncio.run(creator.user_posts(USER, session, page_delay=0))
    assert bool(context.route_pattern.search(url)) is expected


@pytest.mark.parametrize("value", [
    None, [], {"status_code": 2483, "aweme_list": []},
    {"data": [], "has_more": 0}, {"aweme_list": None, "has_more": 0},
    {"aweme_list": []}, {"aweme_list": [], "has_more": 2},
    {"aweme_list": [], "has_more": 1},
    {"aweme_list": [], "has_more": 1, "max_cursor": False},
    {"aweme_list": [], "has_more": 1, "max_cursor": {}},
    {"aweme_list": [None], "has_more": 0}, {"aweme_list": [{}], "has_more": 0},
])
def test_extract_posts_rejects_incomplete_or_wrong_schema(value):
    with pytest.raises(PayloadError):
        creator.extract_posts(value)


@pytest.mark.parametrize("more", [0, "0", False])
def test_empty_final_page_is_valid_exhaustion(more):
    parsed = creator.extract_posts({"status_code": 0, "aweme_list": [], "has_more": more})
    assert parsed["awemes"] == []
    assert parsed["has_more"] is False


def test_duplicate_videos_with_advancing_cursor_do_not_end_pagination(environment):
    context, session = environment([
        payload([aweme(1)], 300, 1), payload([aweme(1)], 200, 1), payload([aweme(2)], 100, 0),
    ])
    callback = AsyncMock()
    records, pages = asyncio.run(creator.user_posts(USER, session, count=0, page_delay=0, on_page=callback))
    assert [row["aweme_id"] for row in records] == ["1", "2"]
    assert [page["added"] for page in pages] == [1, 0, 1]
    assert callback.await_count == 3
    assert context.continued == 3
    assert creator._scroll_search.await_count == 2
    assert not context.listeners and context.gate is None


def test_finite_count_counts_only_unique_downloadable_videos(environment):
    context, session = environment([
        payload([aweme(1), aweme(1), aweme(2, aweme_type=68), aweme(3, video={})], 300, 1),
        payload([aweme(4), aweme(5)], 200, 1), payload([aweme(6)], 100, 0),
    ])
    records, pages = asyncio.run(creator.user_posts(USER, session, count=2, page_delay=0))
    assert [row["aweme_id"] for row in records] == ["1", "4"]
    assert [item["reason"] for item in pages[0]["skipped"]] == ["image_post", "no_video_url"]
    assert len(pages) == 2 and context.continued == 2
    assert all(row["source_user"] == USER["sec_uid"] for row in records)


@pytest.mark.parametrize("fields", [{"images": [{"url_list": []}]}, {"aweme_type": 68}, {"aweme_type": 150}])
def test_image_posts_are_not_downloaded(environment, fields):
    _, session = environment([payload([aweme(1, **fields), aweme(2)])])
    records, pages = asyncio.run(creator.user_posts(USER, session, page_delay=0))
    assert [row["aweme_id"] for row in records] == ["2"]
    assert pages[0]["skipped"] == [{"aweme_id": "1", "reason": "image_post"}]


@pytest.mark.parametrize("author", [{"sec_uid": "another_user"}, {}, ["invalid_author"]])
def test_wrong_or_malformed_author_stops_before_download(environment, author):
    context, session = environment([payload([aweme(1, author=author)])])
    callback = AsyncMock()
    with pytest.raises(BrowserRisk) as error:
        asyncio.run(creator.user_posts(USER, session, page_delay=0, on_page=callback))
    assert error.value.category == "user_mismatch"
    callback.assert_not_awaited()
    assert not context.listeners and context.gate is None


@pytest.mark.parametrize("status", [1, "1"])
def test_known_fifth_page_co_created_video_is_accepted_without_rewriting_author(environment, monkeypatch, status):
    target = "MS4wLjABAAAAaCcBHb3Rhc4zxF8YkBOfHfLh6k-IWEK2l3Ne9xOXPnQ"
    monkeypatch.setitem(USER, "sec_uid", target)
    monkeypatch.setitem(USER, "url", f"https://www.douyin.com/user/{target}")
    collaboration = co_created_aweme()
    collaboration["cooperation_info"]["co_creators"][1]["invite_status"] = status
    responses = [payload([aweme(index * 2), aweme(index * 2 + 1)], 500 - index, 1) for index in range(4)]
    fifth = [aweme(100 + index) for index in range(13)] + [collaboration, aweme(114)]
    responses.append(payload(fifth, 400, 0))
    context, session = environment(responses)
    callback = AsyncMock()
    records, pages = asyncio.run(creator.user_posts(USER, session, count=0, page_delay=0, on_page=callback))
    assert len(records) == 23 and len(pages) == 5
    assert pages[4]["received"] == pages[4]["added"] == 15
    record = callback.await_args_list[4].args[0][13]
    assert record["aweme_id"] == "7619622418637720878"
    assert record["author"] == collaboration["author"]
    assert record["source_user"] == target
    assert [member["nickname"] for member in record["co_creators"]] == ["F1世界锦标赛", "影视飓风"]
    assert record["co_creators"][1]["sec_uid"] == target
    assert record["co_creators"][1]["invite_status"] == 1
    assert type(record["co_creators"][1]["invite_status"]) is int
    assert all(row["source_user"] == target for row in records)
    assert context.continued == 5 and creator._scroll_search.await_count == 4
    assert not context.listeners and not context.page_listeners and context.gate is None


@pytest.mark.parametrize("info", [
    None, {}, [], "invalid", False, 1,
    {"co_creators": None}, {"co_creators": {}}, {"co_creators": "invalid"},
    {"co_creators": [None, [], "invalid", 1, True, {}]},
    {"co_creators": [{"sec_uid": "MS4wLjOther", "invite_status": 1}]},
    {"co_creators": [{"sec_uid": "MS4wLjOther", "nickname": "影视飓风", "invite_status": 1}]},
    {"co_creators": [{"sec_uid": USER["sec_uid"]}]},
    {"co_creators": [{"nickname": "影视飓风", "invite_status": 1}]},
])
def test_unrelated_or_malformed_cooperation_still_stops_before_download(environment, info):
    context, session = environment([payload([co_created_aweme(cooperation_info=info)])])
    callback = AsyncMock()
    with pytest.raises(BrowserRisk) as error:
        asyncio.run(creator.user_posts(USER, session, page_delay=0, on_page=callback))
    assert error.value.category == "user_mismatch"
    callback.assert_not_awaited()
    creator._scroll_search.assert_not_awaited()
    assert not context.listeners and not context.page_listeners and context.gate is None


@pytest.mark.parametrize("status", [None, True, False, 0, 2, -1, 1.0, 1.5, "0", "2", "01", "1.0", " 1 ", "", [], {}])
def test_unaccepted_co_creator_cannot_establish_ownership(environment, status):
    collaboration = co_created_aweme()
    collaboration["cooperation_info"]["co_creators"][1]["invite_status"] = status
    context, session = environment([payload([collaboration])])
    callback = AsyncMock()
    with pytest.raises(BrowserRisk) as error:
        asyncio.run(creator.user_posts(USER, session, page_delay=0, on_page=callback))
    assert error.value.category == "user_mismatch"
    callback.assert_not_awaited()
    assert not context.listeners and context.gate is None


def test_primary_author_remains_valid_with_malformed_cooperation(environment):
    _, session = environment([payload([aweme(1, cooperation_info="invalid")])])
    records, _ = asyncio.run(creator.user_posts(USER, session, page_delay=0))
    assert records[0]["author"]["sec_uid"] == records[0]["source_user"] == USER["sec_uid"]
    assert records[0]["co_creators"] == []


def test_co_created_videos_obey_count_deduplication_and_page_download_barrier(environment):
    async def run():
        collaboration = co_created_aweme()
        context, session = environment([
            payload([collaboration, collaboration, aweme(1, aweme_type=68), aweme(2, video={})], 300, 1),
            payload([collaboration, aweme(3), aweme(4), aweme(5)], 200, 1),
            payload([aweme(6)], 100, 0),
        ])
        entered, release = asyncio.Event(), asyncio.Event()
        completed = []

        async def callback(batch, page):
            assert all(row["source_user"] == USER["sec_uid"] for row in batch)
            if page["page"] == 1:
                assert [row["aweme_id"] for row in batch] == ["7619622418637720878"]
                entered.set()
                await release.wait()
            completed.append(page["page"])

        task = asyncio.create_task(creator.user_posts(USER, session, count=3, page_delay=0, on_page=callback))
        await entered.wait()
        assert context.continued == 1 and completed == []
        creator._scroll_search.assert_not_awaited()
        release.set()
        records, pages = await task
        assert [row["aweme_id"] for row in records] == ["7619622418637720878", "3", "4"]
        assert [page["added"] for page in pages] == [1, 2]
        assert [entry["reason"] for entry in pages[0]["skipped"]] == ["image_post", "no_video_url"]
        assert records[0]["author"] == collaboration["author"]
        assert completed == [1, 2] and context.continued == 2
        assert creator._scroll_search.await_count == 1 and len(context.responses) == 1
        assert not context.listeners and context.gate is None

    asyncio.run(asyncio.wait_for(run(), 3))


def test_same_cursor_reports_incomplete_instead_of_success(environment):
    _, session = environment([payload([aweme(1)], 100, 1), payload([aweme(1)], 100, 1)])
    with pytest.raises(BrowserRisk) as error:
        asyncio.run(creator.user_posts(USER, session, page_delay=0))
    assert error.value.category == "user_cursor_stalled"
    assert creator._scroll_search.await_count == 1


def test_page_safety_limit_reports_incomplete(environment):
    _, session = environment([payload([aweme(1)], 100, 1)])
    with pytest.raises(BrowserRisk) as error:
        asyncio.run(creator.user_posts(USER, session, page_delay=0, max_pages=1))
    assert error.value.category == "user_page_limit"
    creator._scroll_search.assert_not_awaited()


@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("extra", [{}, {"search_nil_info": {"search_nil_type": "verify_check"}, "aweme_list": []}])
def test_rejected_metadata_request_stops_without_next_page(environment, status, extra):
    _, session = environment([{"http_status": status, "status_code": 1, **extra}])
    callback = AsyncMock()
    with pytest.raises(BrowserRisk) as error:
        asyncio.run(creator.user_posts(USER, session, page_delay=0, on_page=callback))
    assert error.value.category == "http_rejected"
    callback.assert_not_awaited()
    creator._scroll_search.assert_not_awaited()


def test_response_timeout_is_not_reported_as_exhaustion(environment):
    context, _ = environment([])
    with pytest.raises(BrowserRisk) as error:
        asyncio.run(creator.wait_response(asyncio.Queue(), context.page, timeout=0))
    assert error.value.category == "user_response_timeout"


def test_headless_verification_cannot_silently_continue(environment, monkeypatch):
    context, _ = environment([])
    monkeypatch.setattr(creator, "_challenge", AsyncMock(return_value="slider"))
    with pytest.raises(BrowserRisk) as error:
        asyncio.run(creator.wait_response(asyncio.Queue(), context.page, headless=True))
    assert error.value.category == "challenge"


def test_closed_browser_stops_response_wait(environment):
    context, _ = environment([])
    context.page.is_closed = lambda: True
    with pytest.raises(BrowserRisk) as error:
        asyncio.run(creator.wait_response(asyncio.Queue(), context.page))
    assert error.value.category == "browser_closed"


@pytest.mark.parametrize("extra", [{}, {"aweme_list": [], "has_more": 0}])
def test_manual_verification_releases_gate_and_keeps_progress_after_reload(environment, monkeypatch, capsys, extra):
    context, session = environment([
        payload([aweme(1)], 300, 1), payload([aweme(2)], 200, 1),
        {"search_nil_info": {"search_nil_type": "verify_check"}, **extra},
        payload([aweme(1)], 300, 1), payload([aweme(2)], 200, 1), payload([aweme(3)], 100, 0),
    ])
    monkeypatch.setattr(creator, "_challenge", AsyncMock(side_effect=lambda page: "slider" if context.visible_challenge else ""))
    reloads = []

    async def finish_verification_and_reload():
        context.visible_challenge = False
        reloads.append(asyncio.create_task(context.page.goto(USER["url"])))
        await asyncio.sleep(0)

    context.page.bring_to_front.side_effect = finish_verification_and_reload
    callback = AsyncMock()
    records, pages = asyncio.run(asyncio.wait_for(creator.user_posts(USER, session, page_delay=0, on_page=callback), 3))
    assert [row["aweme_id"] for row in records] == ["1", "2", "3"]
    assert callback.await_count == 5 and [page["added"] for page in pages] == [1, 1, 0, 0, 1]
    context.page.bring_to_front.assert_awaited_once()
    assert all(task.done() and task.exception() is None for task in reloads)
    output = capsys.readouterr().out
    assert "需要手动操作" in output
    assert "还有下一页：是" in output
    assert "还有下一页：否" in output


def test_page_callback_is_awaited_before_scroll_and_next_request(environment):
    async def run():
        context, session = environment([payload([aweme(1)], 100, 1), payload([aweme(2)])])
        entered = asyncio.Event()
        release = asyncio.Event()

        async def callback(batch, page):
            if page["page"] == 1:
                entered.set()
                await release.wait()

        task = asyncio.create_task(creator.user_posts(USER, session, page_delay=0, on_page=callback))
        await entered.wait()
        assert context.continued == 1
        creator._scroll_search.assert_not_awaited()
        release.set()
        records, _ = await task
        assert len(records) == 2 and context.continued == 2

    asyncio.run(asyncio.wait_for(run(), 3))


def test_page_failure_does_not_request_another_page(environment):
    _, session = environment([payload([aweme(1)], 100, 1), payload([aweme(2)])])
    callback = AsyncMock(side_effect=BrowserRisk("download_rate_limited", "HTTP 429", 429))
    with pytest.raises(BrowserRisk):
        asyncio.run(creator.user_posts(USER, session, page_delay=0, on_page=callback))
    creator._scroll_search.assert_not_awaited()


def test_page_gate_releases_only_one_queued_request(environment):
    async def run():
        context, session = environment([payload([aweme(1)], 100, 1)])
        entered, release = asyncio.Event(), asyncio.Event()

        async def callback(batch, page):
            if page["page"] == 1:
                entered.set()
                await release.wait()

        collect = asyncio.create_task(creator.user_posts(USER, session, page_delay=0, on_page=callback))
        await entered.wait()
        old_request = context.last_request
        routes = [queued_route(context.page) for _ in range(2)]
        waiting = [asyncio.create_task(context.gate(route)) for route in routes]
        await asyncio.sleep(0)
        assert sum(route.continue_.await_count for route in routes) == 0
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if any(route.continue_.await_count for route in routes):
                break
        assert sum(route.continue_.await_count for route in routes) == 1
        context.listeners["requestfailed"](old_request)
        await asyncio.sleep(0)
        assert sum(route.continue_.await_count for route in routes) == 1
        request = next(route.request for route in routes if route.continue_.await_count)
        response = SimpleNamespace(request=request, status=200, json=AsyncMock(return_value=payload([aweme(2)])))
        context.listeners["response"](response)
        records, _ = await collect
        await asyncio.gather(*waiting)
        assert len(records) == 2
        assert sum(route.continue_.await_count for route in routes) == 1
        assert sum(route.abort.await_count for route in routes) == 1

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize("reload_page", [False, True])
def test_failed_request_allows_same_cursor_retry_or_new_navigation_but_not_page_skip(environment, reload_page):
    async def run():
        context, session = environment([])
        collect = asyncio.create_task(creator.user_posts(USER, session, page_delay=0))
        await asyncio.sleep(0)
        failed = queued_route(context.page, 100)
        await context.gate(failed)
        context.listeners["requestfailed"](failed.request)
        await asyncio.sleep(0)
        skipped = queued_route(context.page, 50)
        skipped_task = asyncio.create_task(context.gate(skipped))
        await asyncio.sleep(0)
        skipped.continue_.assert_not_awaited()
        if reload_page:
            await context.page.goto(USER["url"])
        retried = queued_route(context.page, 0 if reload_page else 100)
        await context.gate(retried)
        retried.continue_.assert_awaited_once()
        extra = queued_route(context.page, 0 if reload_page else 100)
        extra_task = asyncio.create_task(context.gate(extra))
        context.listeners["requestfailed"](failed.request)
        await asyncio.sleep(0)
        extra.continue_.assert_not_awaited()
        response = SimpleNamespace(request=retried.request, status=200, json=AsyncMock(return_value=payload([aweme(1)])))
        context.listeners["response"](response)
        records, _ = await collect
        await asyncio.gather(skipped_task, extra_task)
        assert [row["aweme_id"] for row in records] == ["1"]
        skipped.continue_.assert_not_awaited()
        skipped.abort.assert_awaited_once()
        extra.abort.assert_awaited_once()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_verification_blocks_automatic_prefetch_until_manual_clear_and_reload(environment, monkeypatch):
    async def run():
        context, session = environment([
            payload([aweme(1)], 100, 1),
            {"search_nil_info": {"search_nil_type": "verify_check"}, "aweme_list": []},
            payload([aweme(2)]),
        ])
        monkeypatch.setattr(creator, "_challenge", AsyncMock(side_effect=lambda page: "slider" if context.visible_challenge else ""))
        notified = asyncio.Event()
        context.page.bring_to_front.side_effect = notified.set
        collect = asyncio.create_task(creator.user_posts(USER, session, page_delay=0))
        await notified.wait()
        prefetched = queued_route(context.page)
        prefetch_task = asyncio.create_task(context.gate(prefetched))
        for _ in range(3):
            await asyncio.sleep(0)
        prefetched.continue_.assert_not_awaited()
        assert context.continued == 2
        context.visible_challenge = False
        await context.page.goto(USER["url"])
        records, _ = await collect
        await prefetch_task
        assert [row["aweme_id"] for row in records] == ["1", "2"]
        prefetched.continue_.assert_not_awaited()
        prefetched.abort.assert_awaited_once()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_same_document_navigation_does_not_clear_silent_verification(environment):
    async def run():
        context, session = environment([
            {"search_nil_info": {"search_nil_type": "verify_check"}, "aweme_list": []},
            payload([aweme(1)]),
        ])
        notified = asyncio.Event()
        context.page.bring_to_front.side_effect = notified.set
        collect = asyncio.create_task(creator.user_posts(USER, session, page_delay=0))
        await notified.wait()
        context.page.main_frame.url = USER["url"] + "?tab=post"
        context.page_listeners["framenavigated"](context.page.main_frame)
        prefetched = queued_route(context.page)
        prefetch_task = asyncio.create_task(context.gate(prefetched))
        for _ in range(3):
            await asyncio.sleep(0)
        prefetched.continue_.assert_not_awaited()
        await context.page.goto(USER["url"])
        records, _ = await collect
        await prefetch_task
        assert [row["aweme_id"] for row in records] == ["1"]
        prefetched.continue_.assert_not_awaited()
        prefetched.abort.assert_awaited_once()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_cleanup_waits_for_pending_route_abort_before_unrouting(environment, monkeypatch):
    async def run():
        context, session = environment([payload([aweme(1)])])
        downloaded, finish_download = asyncio.Event(), asyncio.Event()
        abort_started, finish_abort = asyncio.Event(), asyncio.Event()

        async def callback(batch, page):
            downloaded.set()
            await finish_download.wait()

        async def abort(reason):
            assert reason == "aborted"
            abort_started.set()
            await finish_abort.wait()

        unroute = AsyncMock(wraps=context.unroute)
        monkeypatch.setattr(context, "unroute", unroute)
        collect = asyncio.create_task(creator.user_posts(USER, session, page_delay=0, on_page=callback))
        await downloaded.wait()
        pending_route = queued_route(context.page)
        pending_route.abort.side_effect = abort
        pending = asyncio.create_task(context.gate(pending_route))
        await asyncio.sleep(0)
        pending_route.continue_.assert_not_awaited()
        finish_download.set()
        await abort_started.wait()
        assert not collect.done() and not pending.done()
        unroute.assert_not_awaited()
        finish_abort.set()
        records, _ = await collect
        await pending
        assert [row["aweme_id"] for row in records] == ["1"]
        unroute.assert_awaited_once()
        pending_route.abort.assert_awaited_once_with("aborted")
        assert context.gate is None

    asyncio.run(asyncio.wait_for(run(), 3))


def test_already_handled_abort_does_not_pollute_successful_cleanup(environment):
    async def run():
        context, session = environment([payload([aweme(1)])])
        downloaded, finish_download = asyncio.Event(), asyncio.Event()

        async def callback(batch, page):
            downloaded.set()
            await finish_download.wait()

        collect = asyncio.create_task(creator.user_posts(USER, session, page_delay=0, on_page=callback))
        await downloaded.wait()
        pending_route = queued_route(context.page)
        pending_route.abort.side_effect = PlaywrightError("Route already handled!")
        pending = asyncio.create_task(context.gate(pending_route))
        await asyncio.sleep(0)
        pending_route.continue_.assert_not_awaited()
        finish_download.set()
        records, pages = await collect
        await pending
        assert [row["aweme_id"] for row in records] == ["1"] and len(pages) == 1
        assert pending.exception() is None
        pending_route.abort.assert_awaited_once_with("aborted")
        assert context.gate is None and not context.listeners and not context.page_listeners

    asyncio.run(asyncio.wait_for(run(), 3))
