import asyncio
import re
import time
from urllib.parse import parse_qs, quote, urlparse

from playwright.async_api import Error as PlaywrightError

from .browser import BrowserRisk, _challenge, _login_panel, _scroll_search
from .normalize import PayloadError, extract_co_creators, normalize_aweme


USER_SEARCH_PATH = "/aweme/v1/web/discover/search/"
USER_POST_PATH = "/aweme/v1/web/aweme/post/"


def profile_id(target):
    target = str(target).strip()
    if target.startswith("MS4wLj") and re.fullmatch(r"[A-Za-z0-9_-]+", target):
        return target
    if "://" not in target:
        return ""
    parsed = urlparse(target)
    match = re.fullmatch(r"/user/([A-Za-z0-9_-]+)/?", parsed.path)
    if parsed.scheme != "https" or parsed.hostname not in {"www.douyin.com", "douyin.com"} or parsed.username or parsed.password or not match:
        raise ValueError("请输入抖音昵称、sec_uid 或 https://www.douyin.com/user/... 格式的主页链接。")
    return match[1]


def extract_users(payload):
    if not isinstance(payload, dict) or payload.get("status_code", 0) not in (0, "0", None):
        raise PayloadError("用户搜索响应无效或请求未成功。")
    if not isinstance(payload.get("user_list"), list):
        raise PayloadError("用户搜索响应缺少 user_list。")
    users = {}
    for row in payload["user_list"]:
        info = row.get("user_info") if isinstance(row, dict) else None
        if not isinstance(info, dict):
            continue
        sec_uid = str(info.get("sec_uid") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", sec_uid):
            continue
        users[sec_uid] = {key: str(info.get(key) or "") for key in ("sec_uid", "uid", "nickname", "unique_id", "short_id")}
        users[sec_uid]["url"] = f"https://www.douyin.com/user/{sec_uid}"
    return list(users.values())


def extract_posts(payload):
    if not isinstance(payload, dict) or payload.get("status_code", 0) not in (0, "0", None):
        raise PayloadError("用户作品响应无效或请求未成功。")
    if not isinstance(payload.get("aweme_list"), list):
        raise PayloadError("用户作品响应缺少 aweme_list，无法确认是否采集完整。")
    if payload.get("has_more") not in (0, 1, "0", "1", False, True):
        raise PayloadError("用户作品响应缺少有效的 has_more 分页标记。")
    if any(not isinstance(row, dict) or not str(row.get("aweme_id") or "").strip() for row in payload["aweme_list"]):
        raise PayloadError("用户作品响应中存在无效的视频记录。")
    more = payload["has_more"] in (1, "1", True)
    cursor = payload.get("max_cursor")
    if more and (cursor is None or isinstance(cursor, (dict, list, bool)) or str(cursor) == ""):
        raise PayloadError("用户作品响应缺少下一页游标。")
    return {"awemes": payload["aweme_list"], "cursor": cursor, "has_more": more}


def matches(request, page, path, query):
    parsed = urlparse(request.url)
    host = parsed.hostname or ""
    if not (host == "douyin.com" or host.endswith(".douyin.com")) or parsed.path != path or request.resource_type not in {"xhr", "fetch"}:
        return False
    if any(parse_qs(parsed.query).get(key) != [value] for key, value in query.items()):
        return False
    try:
        return request.frame.page == page
    except Exception:
        return False


async def wait_response(queue, page, timeout=60, headless=False, on_state=None):
    deadline = time.monotonic() + timeout
    paused = False
    notified = False
    pending = None
    while True:
        if page.is_closed():
            raise BrowserRisk("browser_closed", "采集用户作品的浏览器已关闭。")
        marker = await _challenge(page)
        panel = await _login_panel(page) if marker == "" else None
        if on_state is not None:
            await on_state(marker, panel)
        if marker or panel:
            paused = True
        if paused and not notified:
            if headless:
                raise BrowserRisk("challenge", "登录或安全验证需要显示浏览器窗口。")
            print("需要手动操作，请在当前浏览器中完成登录或验证。如果结果未更新，请手动刷新当前页面。等待期间不会自动重试；按 Ctrl+C 可停止。", flush=True)
            await page.bring_to_front()
            notified = True
        if pending is not None and marker == "" and panel is False:
            if notified:
                print("手动操作已完成并收到正常响应，继续采集。", flush=True)
            pending["resumed"] = notified
            return pending
        remaining = deadline - time.monotonic()
        if not paused and remaining <= 0:
            raise BrowserRisk("user_response_timeout", "未收到有效的用户响应，采集尚未完成。")
        if pending is not None:
            await asyncio.sleep(2)
            continue
        try:
            event = await asyncio.wait_for(queue.get(), timeout=2 if paused else min(2, remaining))
        except TimeoutError:
            continue
        status = event["status"]
        if status in (403, 429):
            raise BrowserRisk("http_rejected", f"用户请求返回 HTTP {status}。", status)
        if not 200 <= status < 300:
            raise BrowserRisk("http_error", f"用户请求返回 HTTP {status}。", status)
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise BrowserRisk("invalid_response", "用户响应不是 JSON 对象。", status)
        nil_info = payload.get("search_nil_info")
        if isinstance(nil_info, dict) and nil_info.get("search_nil_type") == "verify_check":
            paused = True
            continue
        pending = event


async def resolve_user(target, session, headless=False):
    sec_uid = profile_id(target)
    if sec_uid:
        return {"sec_uid": sec_uid, "url": f"https://www.douyin.com/user/{sec_uid}"}
    keyword = str(target).strip()
    if not keyword:
        raise ValueError("目标用户不能为空。")
    _, context, page = session
    queue = asyncio.Queue()
    tasks = set()

    async def capture(response):
        try:
            payload = await response.json()
        except Exception:
            payload = None
        await queue.put({"status": response.status, "payload": payload})

    def handler(response):
        if matches(response.request, page, USER_SEARCH_PATH, {"keyword": keyword}):
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    context.on("response", handler)
    try:
        await page.goto(f"https://www.douyin.com/search/{quote(keyword, safe='')}?type=user", wait_until="domcontentloaded")
        event = await wait_response(queue, page, timeout=120, headless=headless)
        users = extract_users(event["payload"])
        exact = [user for user in users if keyword in (user["nickname"], user["unique_id"], user["short_id"])]
        if len(exact) == 1:
            return exact[0]
        candidates = exact or users
        for user in candidates:
            print(f"候选用户：{user['nickname']} | 抖音号：{user['unique_id'] or user['short_id']} | {user['url']}", flush=True)
        raise BrowserRisk("user_ambiguous" if candidates else "user_not_found", "未找到唯一且准确匹配的用户，请使用目标用户的主页链接重新运行。")
    finally:
        context.remove_listener("response", handler)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def user_posts(user, session, count=0, page_delay=5, max_pages=10000, headless=False, on_page=None):
    _, context, page = session
    sec_uid = user["sec_uid"]
    queue = asyncio.Queue()
    tasks = set()
    gate_condition = asyncio.Condition()
    route_tasks = set()
    permitted = True
    stopping = False
    active_request = None
    active_generation = 0
    generation = 0
    navigation_pending = False
    reload_generation = 0
    pause_generation = 0
    paused = False
    saw_dialog = False
    retry_cursor = None
    retry_generation = 0
    pattern = re.compile(r"^https://(?:[A-Za-z0-9-]+\.)*douyin\.com/aweme/v1/web/aweme/post/(?:\?.*)?$")
    records, pages = [], []
    seen, cursors = set(), set()

    async def release(request, retry=False):
        nonlocal permitted, active_request, retry_cursor, retry_generation
        async with gate_condition:
            if request is not active_request:
                return
            retry_cursor = (parse_qs(urlparse(request.url).query).get("max_cursor", ["0"])[0] or "0") if retry else None
            retry_generation = active_generation
            active_request = None
            permitted = True
            gate_condition.notify_all()

    async def observe(marker, panel):
        nonlocal paused, saw_dialog, pause_generation
        async with gate_condition:
            if marker or panel:
                if not paused:
                    pause_generation = generation
                paused = saw_dialog = True
            elif marker == "" and panel is False and paused and (saw_dialog or reload_generation > pause_generation):
                paused = saw_dialog = False
            gate_condition.notify_all()

    async def capture(response):
        nonlocal permitted, active_request, paused, pause_generation, retry_cursor, retry_generation
        request = response.request
        request_generation = active_generation
        try:
            payload = await response.json()
        except Exception:
            payload = None
        nil_info = payload.get("search_nil_info") if isinstance(payload, dict) else None
        verification = 200 <= response.status < 300 and isinstance(nil_info, dict) and nil_info.get("search_nil_type") == "verify_check"
        async with gate_condition:
            if request is not active_request:
                return
            if verification:
                if not paused:
                    pause_generation = request_generation
                paused = True
                retry_cursor = parse_qs(urlparse(request.url).query).get("max_cursor", ["0"])[0] or "0"
                retry_generation = request_generation
                active_request = None
                permitted = True
                gate_condition.notify_all()
        await queue.put({"status": response.status, "payload": payload, "request": request, "generation": request_generation})

    def handler(response):
        if response.request is active_request and matches(response.request, page, USER_POST_PATH, {"sec_user_id": sec_uid}):
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    def failed(request):
        if request is active_request:
            task = asyncio.create_task(release(request, retry=True))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    def navigating(request):
        nonlocal generation, navigation_pending
        try:
            if request.resource_type == "document" and request.frame == page.main_frame:
                generation += 1
                navigation_pending = True
        except Exception:
            pass

    def navigated(frame):
        nonlocal reload_generation, navigation_pending
        if frame != page.main_frame or not navigation_pending:
            return
        navigation_pending = False
        parsed = urlparse(frame.url)
        if parsed.hostname == "www.douyin.com" and parsed.path.rstrip("/") == f"/user/{sec_uid}":
            reload_generation = generation

    async def handle_route(route):
        nonlocal permitted, active_request, active_generation
        if matches(route.request, page, USER_POST_PATH, {"sec_user_id": sec_uid}):
            request_generation = generation
            request_cursor = parse_qs(urlparse(route.request.url).query).get("max_cursor", ["0"])[0] or "0"
            while True:
                if stopping or request_generation != generation:
                    try:
                        await route.abort("aborted")
                    except PlaywrightError:
                        pass
                    return
                marker = await _challenge(page)
                panel = await _login_panel(page) if marker == "" else None
                await observe(marker, panel)
                async with gate_condition:
                    if stopping or request_generation != generation:
                        continue
                    retry_allowed = retry_cursor is None or request_generation != retry_generation or request_cursor == retry_cursor
                    if permitted and retry_allowed and not paused and marker == "" and panel is False:
                        permitted = False
                        active_request = route.request
                        active_generation = request_generation
                        break
                    try:
                        await asyncio.wait_for(gate_condition.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
        try:
            await route.continue_()
        except Exception:
            await release(route.request, retry=True)
            raise BrowserRisk("user_request_failed", "用户请求无法继续，采集尚未完成。") from None

    async def gate(route):
        task = asyncio.current_task()
        route_tasks.add(task)
        try:
            await handle_route(route)
        finally:
            route_tasks.discard(task)

    context.on("response", handler)
    context.on("request", navigating)
    context.on("requestfailed", failed)
    page.on("framenavigated", navigated)
    await context.route(pattern, gate)
    try:
        await page.goto(user["url"], wait_until="domcontentloaded")
        user_agent = await page.evaluate("() => navigator.userAgent")
        while True:
            event = await wait_response(queue, page, timeout=120 if not pages else 60, headless=headless, on_state=observe)
            parsed = extract_posts(event["payload"])
            if event.get("resumed") or (pages and event["generation"] != pages[-1]["generation"]):
                cursors.clear()
            batch, skipped = [], []
            for aweme in parsed["awemes"]:
                aweme_id = str(aweme["aweme_id"])
                if aweme_id in seen:
                    continue
                author = aweme.get("author") or {}
                is_author = isinstance(author, dict) and author.get("sec_uid") == sec_uid
                is_co_creator = any(member["sec_uid"] == sec_uid for member in extract_co_creators(aweme))
                if not is_author and not is_co_creator:
                    raise BrowserRisk("user_mismatch", f"视频 {aweme_id} 的主作者和已确认共创者均不包含目标用户，已停止采集。")
                seen.add(aweme_id)
                if aweme.get("images") or aweme.get("aweme_type") in (68, 150):
                    skipped.append({"aweme_id": aweme_id, "reason": "image_post"})
                    continue
                record = normalize_aweme(aweme, user_agent=user_agent)
                if not record["variants"]:
                    skipped.append({"aweme_id": aweme_id, "reason": "no_video_url"})
                    continue
                record["source_user"] = sec_uid
                batch.append(record)
                if count and len(records) + len(batch) >= count:
                    break
            records.extend(batch)
            info = {"page": len(pages) + 1, "received": len(parsed["awemes"]), "added": len(batch),
                    "unique": len(records), "cursor": parsed["cursor"], "has_more": parsed["has_more"], "skipped": skipped,
                    "generation": event["generation"]}
            pages.append(info)
            print(f"用户作品第 {info['page']} 页 | 收到 {info['received']} 条 | 新增 {len(batch)} 条视频 | 累计 {len(records)} 条 | 还有下一页：{'是' if parsed['has_more'] else '否'}", flush=True)
            if on_page is not None:
                await on_page(batch, info.copy())
            if not parsed["has_more"] or (count and len(records) >= count):
                return records, pages
            cursor = str(parsed["cursor"])
            if cursor in cursors:
                raise BrowserRisk("user_cursor_stalled", "用户作品分页游标重复，采集尚未完成。")
            cursors.add(cursor)
            if len(pages) >= max_pages:
                raise BrowserRisk("user_page_limit", "已达到用户作品页数安全上限，采集尚未完成。")
            await asyncio.sleep(page_delay)
            await release(event["request"])
            if queue.empty():
                await _scroll_search(page)
    finally:
        stopping = True
        async with gate_condition:
            gate_condition.notify_all()
        context.remove_listener("response", handler)
        context.remove_listener("request", navigating)
        context.remove_listener("requestfailed", failed)
        page.remove_listener("framenavigated", navigated)
        if route_tasks:
            await asyncio.gather(*route_tasks, return_exceptions=True)
        await context.unroute(pattern, gate)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
