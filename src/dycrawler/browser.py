import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from .normalize import PayloadError, extract_awemes, normalize_aweme


SEARCH_PATH = "/aweme/v1/web/general/search/single/"
SEARCH_PATHS = (SEARCH_PATH, "/aweme/v1/web/general/search/stream/")
DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
CHALLENGE_MARKERS = (
    "验证码",
    "安全验证",
    "真人验证",
    "手机号验证",
    "访问过于频繁",
    "完成下列验证",
)


class BrowserRisk(RuntimeError):
    def __init__(self, category, detail, status=0):
        super().__init__(detail)
        self.category = category
        self.detail = detail
        self.status = status


async def _open(profile, channel, headless, timeout_ms, block_media):
    playwright = await async_playwright().start()
    profile = Path(profile).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    options = {
        "user_data_dir": str(profile),
        "headless": headless,
        "chromium_sandbox": True,
        "accept_downloads": False,
        "viewport": None,
    }
    if channel != "chromium":
        options["channel"] = channel
    if sys.platform == "darwin":
        options["ignore_default_args"] = ["--use-mock-keychain", "--password-store=basic"]
    context = await playwright.chromium.launch_persistent_context(**options)
    context.set_default_timeout(timeout_ms)
    context.set_default_navigation_timeout(timeout_ms)
    page = context.pages[0] if context.pages else await context.new_page()
    if block_media:
        session = await context.new_cdp_session(page)
        await session.send("Network.enable")
        await session.send("Network.setBlockedURLs", {"urls": ["*://*.douyinvod.com/*", "*.mp4*", "*.m3u8*"]})
    return playwright, context, page


async def _close(playwright, context):
    await context.close()
    await playwright.stop()


async def open_search_session(profile, channel="chrome", headless=False):
    return await _open(profile, channel, headless, 60000, True)


async def close_search_session(session):
    playwright, context, _ = session
    await _close(playwright, context)


async def _logged_in(context, page):
    cookies = await context.cookies("https://www.douyin.com/")
    now = time.time()
    session_present = any(
        item["name"] in {"sessionid", "sessionid_ss", "sid_guard"}
        and item.get("value", "").strip()
        and (item.get("expires", -1) in (-1, None) or item["expires"] > now)
        for item in cookies
    )
    return bool(session_present) and (await _login_panel(page)) is False


async def _challenge(page):
    markers = CHALLENGE_MARKERS + (
        "拖动滑块",
        "拖动下方滑块",
        "拖拽滑块",
        "按住滑块",
        "滑动滑块",
        "请完成下方验证",
    )
    try:
        title = await page.title()
    except Exception:
        title = ""
    marker = next((item for item in markers if item in title), "")
    if marker:
        return marker
    try:
        captcha = page.locator("iframe[src*='captcha'], iframe[src*='verify']").first
        if await captcha.is_visible(timeout=1000):
            return "验证码"
    except Exception:
        pass
    try:
        body = await page.locator("body").inner_text(timeout=500)
    except Exception:
        return None
    return next((marker for marker in markers if marker != "验证码" and marker in body), "")


async def _wait_for_verification(page, headless=False, initial_marker=None):
    if page.is_closed():
        raise BrowserRisk("browser_closed", "手动验证期间浏览器已关闭。")
    marker = await _challenge(page) if initial_marker is None else initial_marker
    if not marker:
        return
    if headless:
        raise BrowserRisk("challenge", "手动验证需要显示浏览器窗口，请移除 --headless 参数后运行。")
    print("需要安全验证，采集已暂停。请在浏览器中完成验证；按 Ctrl+C 可停止。", flush=True)
    await page.bring_to_front()
    while marker != "":
        if page.is_closed():
            raise BrowserRisk("browser_closed", "手动验证期间浏览器已关闭。")
        await asyncio.sleep(2)
        marker = await _challenge(page)
    if page.is_closed():
        raise BrowserRisk("browser_closed", "手动验证期间浏览器已关闭。")
    print("验证窗口已关闭，继续处理页面。", flush=True)


async def _wait_entry_ready(page, headless=False, timeout=180):
    deadline = time.monotonic() + timeout
    print(f"正在加载搜索入口 | 超时：{timeout} 秒", flush=True)
    while True:
        if page.is_closed():
            raise BrowserRisk("browser_closed", "加载搜索入口时浏览器已关闭。")
        marker = await _challenge(page)
        if marker:
            paused_at = time.monotonic()
            await _wait_for_verification(page, headless=headless, initial_marker=marker)
            deadline += time.monotonic() - paused_at
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("搜索入口加载超时，正在检查搜索控件是否可用。", flush=True)
            return False
        try:
            await page.wait_for_load_state("load", timeout=max(1, min(5000, int(remaining * 1000))))
        except (TimeoutError, PlaywrightTimeoutError):
            continue
        except Exception as exc:
            if page.is_closed():
                raise BrowserRisk("browser_closed", "加载搜索入口时浏览器已关闭。") from exc
            raise BrowserRisk("entry_load_failed", f"等待搜索入口加载失败（{type(exc).__name__}），尚未提交搜索。") from exc
        print("搜索入口加载完成。", flush=True)
        return True


async def _wait_search_response(queue, page, timeout, headless=False, verification=False):
    deadline = time.monotonic() + timeout
    paused = verification
    notified = False
    pending = []
    while True:
        if page.is_closed():
            raise BrowserRisk("browser_closed", "等待响应时浏览器已关闭。")
        marker = await _challenge(page)
        if marker:
            paused = True
        if paused and not notified:
            if headless:
                raise BrowserRisk("challenge", "搜索需要安全验证，请移除 --headless 参数后运行并手动完成验证。", 200 if verification else 0)
            print("需要安全验证，请在当前浏览器中完成。如果结果未更新，请手动重新搜索同一关键词。等待期间不会滚动或重试；按 Ctrl+C 可停止。", flush=True)
            await page.bring_to_front()
            notified = True
        if pending and marker == "" and queue.empty():
            for buffered in pending[1:]:
                queue.put_nowait(buffered)
            print("验证已通过并收到正常响应，继续采集。", flush=True)
            return pending[0]
        remaining = deadline - time.monotonic()
        if not paused and remaining <= 0:
            raise TimeoutError
        try:
            if pending and queue.empty():
                await asyncio.sleep(2)
                continue
            event = await asyncio.wait_for(queue.get(), timeout=2 if paused else min(2, remaining))
        except TimeoutError:
            continue
        payload = event.get("payload")
        nil_info = payload.get("search_nil_info") if isinstance(payload, dict) else None
        if event["status"] in (403, 429):
            return event
        if isinstance(nil_info, dict) and nil_info.get("search_nil_type") == "verify_check":
            paused = verification = True
            pending.clear()
            continue
        if paused and 200 <= event["status"] < 300 and isinstance(payload, dict) and payload.get("status_code", 0) in (0, "0", None):
            try:
                if "aweme_detail" in payload:
                    if not isinstance(payload["aweme_detail"], dict):
                        raise PayloadError("响应详情不是有效对象")
                else:
                    extract_awemes(payload)
            except PayloadError as exc:
                raise BrowserRisk("business_or_schema", str(exc), event["status"]) from exc
            pending.append(event)
            continue
        return event


def _search_url_matches(url, keyword):
    parsed = urlparse(str(url))
    path = unquote(parsed.path).rstrip("/")
    return parsed.hostname == "www.douyin.com" and path in {
        f"/search/{keyword}", f"/jingxuan/search/{keyword}",
    }


def _search_request_matches(request, page, keyword):
    parsed = urlparse(request.url)
    try:
        return (
            parsed.hostname == "www.douyin.com" and parsed.path in SEARCH_PATHS
            and parse_qs(parsed.query).get("keyword") == [keyword]
            and request.resource_type in {"xhr", "fetch"} and request.frame.page == page
        )
    except Exception:
        return False


async def _submit_search(page, queue, keyword, state, headless=False):
    outcomes = {
        "response_captured": "已收到搜索响应",
        "response_started": "正在接收搜索响应",
        "request_started": "请求已发出",
        "route_changed": "已进入目标搜索页面",
        "verification_required": "需要手动验证",
        "page_unreadable": "页面暂时无法读取",
        "idle": "尚未提交请求",
    }

    async def progress():
        if page.is_closed():
            raise BrowserRisk("browser_closed", "提交搜索时浏览器已关闭。")
        marker = await _challenge(page)
        panel = await _login_panel(page) if marker == "" else None
        if state["rejected"]:
            raise BrowserRisk("http_rejected", f"HTTP {state['rejected']}", state["rejected"])
        if state["active"]:
            if state["captured"] or not queue.empty():
                return "response_captured"
            if state["response_started"]:
                return "response_started"
            if state["requested"]:
                return "request_started"
            if str(page.url) != state["initial_url"] and _search_url_matches(page.url, keyword):
                return "route_changed"
        if marker:
            if state["active"]:
                return "verification_required"
            await _wait_for_verification(page, headless=headless, initial_marker=marker)
            panel = await _login_panel(page)
        elif marker is None:
            return "page_unreadable"
        if panel:
            raise BrowserRisk("login_required", "登录窗口阻止了搜索提交，请先完成登录。")
        return "idle" if panel is False else "page_unreadable"

    box = page.get_by_placeholder("搜索你感兴趣的内容", exact=True)
    state["initial_url"] = str(page.url)
    for attempt in range(2):
        outcome = "page_unreadable"
        for check in range(10):
            outcome = await progress()
            if outcome != "page_unreadable":
                break
            await page.wait_for_timeout(500)
        if outcome not in {"idle", "page_unreadable"}:
            print(f"搜索提交 | 状态：{outcomes.get(outcome, outcome)} | 不重复提交", flush=True)
            return
        if outcome == "page_unreadable":
            raise BrowserRisk("search_page_unreadable", "提交前无法检查搜索控件。")
        try:
            await box.wait_for(state="visible", timeout=15000)
            await box.click(trial=True, timeout=15000)
            for refill in range(2):
                outcome = await progress()
                if outcome != "idle":
                    break
                await box.fill(keyword, timeout=15000)
                first_value = await box.input_value(timeout=1000)
                await page.wait_for_timeout(250)
                if first_value == keyword and await box.input_value(timeout=1000) == keyword:
                    break
                print(f"搜索框内容被重置，正在第 {refill + 1} 次重新填写。", flush=True)
            else:
                raise BrowserRisk("search_input_unstable", "填写两次后，搜索框仍未保留指定关键词。")
            outcome = await progress()
            if outcome != "idle":
                if outcome == "page_unreadable":
                    raise BrowserRisk("search_page_unreadable", "提交搜索前无法读取页面状态。")
                print(f"搜索提交 | 状态：{outcomes.get(outcome, outcome)} | 不重复提交", flush=True)
                return
            if attempt == 0:
                if await box.input_value(timeout=1000) != keyword:
                    raise BrowserRisk("search_input_unstable", "按下回车前，搜索框中的关键词已改变。")
                state["initial_url"] = str(page.url)
                state["active"] = True
                print("关键词已确认，正在按回车提交搜索。", flush=True)
                await box.press("Enter", timeout=15000)
            else:
                button = page.get_by_role("button", name="搜索", exact=True)
                await button.wait_for(state="visible", timeout=10000)
                await button.click(trial=True, timeout=5000)
                retained = await box.input_value(timeout=1000) == keyword
                outcome = await progress()
                if outcome != "idle":
                    if outcome == "page_unreadable":
                        raise BrowserRisk("search_page_unreadable", "点击搜索按钮前无法读取页面状态。")
                    print(f"搜索提交 | 状态：{outcomes.get(outcome, outcome)} | 不重复提交", flush=True)
                    return
                if not retained:
                    raise BrowserRisk("search_input_unstable", "点击搜索按钮前，搜索框中的关键词已改变。")
                if not state["active"]:
                    state["initial_url"] = str(page.url)
                state["active"] = True
                print("关键词已确认，正在使用搜索按钮提交。", flush=True)
                await button.click(timeout=5000)
        except BrowserRisk:
            raise
        except Exception as exc:
            print(f"搜索操作异常：{type(exc).__name__} | 第 {attempt + 1} 次尝试", flush=True)
        for check in range(15):
            outcome = await progress()
            if outcome not in {"idle", "page_unreadable"}:
                print(f"搜索提交 | 状态：{outcomes.get(outcome, outcome)} | 不重复提交", flush=True)
                return
            await page.wait_for_timeout(1000)
        if outcome == "page_unreadable":
            raise BrowserRisk("search_page_unreadable", "搜索提交结束后仍无法读取页面状态。")
        if attempt == 0:
            print("尚未确认回车提交结果，将使用搜索按钮补充提交一次。", flush=True)
    raise BrowserRisk("search_navigation_timeout", "回车和一次搜索按钮提交后，仍未发现目标请求、响应或对应搜索页面。")


async def _login_panel(page):
    try:
        return await page.locator("[id^='login-full-panel-']").first.is_visible(timeout=1000)
    except Exception:
        return None


async def _scroll_search(page):
    return await page.evaluate(
        """() => {
          const candidates = [...document.querySelectorAll('*')]
            .filter(el => el.clientHeight > 200 && el.scrollHeight > el.clientHeight + 100)
            .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))
          const target = candidates[0]
          if (!target) {
            window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})
            return 'window'
          }
          target.scrollTo({top: target.scrollHeight, behavior: 'smooth'})
          return 'container'
        }"""
    )


def decode_search_response(body):
    if not body.lstrip().startswith(b"{"):
        chunks = []
        position = 0
        while True:
            end = body.find(b"\r\n", position)
            size_text = body[position:end] if end >= 0 else b""
            if not size_text or any(char not in b"0123456789abcdefABCDEF" for char in size_text):
                raise ValueError("搜索流响应的数据块大小无效")
            size = int(size_text, 16)
            position = end + 2
            if size == 0:
                if body[position:] != b"\r\n":
                    raise ValueError("搜索流响应的结束标记无效")
                break
            end = position + size
            if body[end:end + 2] != b"\r\n":
                raise ValueError("搜索流响应的数据块不完整")
            chunks.append(body[position:end])
            position = end + 2
        body = b"".join(chunks)
    text = body.decode("utf-8")
    decoder = json.JSONDecoder()
    payloads = []
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        payload, position = decoder.raw_decode(text, position)
        if not isinstance(payload, dict):
            raise ValueError("搜索响应不是有效对象")
        payloads.append(payload)
    if not payloads:
        raise ValueError("搜索响应为空")
    merged = {}
    for payload in payloads:
        nil_info = payload.get("search_nil_info")
        if isinstance(nil_info, dict) and nil_info.get("search_nil_type") == "verify_check":
            return payload
    for payload in payloads:
        if payload.get("status_code", 0) not in (0, "0", None):
            return payload
    for payload in payloads:
        for key, value in payload.items():
            if value is None:
                continue
            if key == "data":
                if not isinstance(value, list):
                    raise ValueError("搜索响应中的 data 不是列表")
                merged.setdefault("data", []).extend(value)
            elif isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update({name: item for name, item in value.items() if item is not None})
            else:
                merged[key] = value
    return merged


async def _capture(response, queue, path, page=None, keyword=None):
    parsed = urlparse(response.url)
    paths = (path,) if isinstance(path, str) else path
    if parsed.hostname != "www.douyin.com" or parsed.path not in paths:
        return
    if response.request.resource_type not in {"xhr", "fetch"}:
        return
    if keyword is not None and parse_qs(parsed.query).get("keyword") != [keyword]:
        return
    if page is not None:
        try:
            if response.request.frame.page != page:
                return
        except Exception:
            return
    try:
        payload = decode_search_response(await response.body()) if parsed.path in SEARCH_PATHS else await response.json()
        error = ""
    except Exception as exc:
        payload = None
        error = exc.__class__.__name__
    await queue.put({"status": response.status, "payload": payload, "error": error})


async def login(profile, channel="chrome", timeout_ms=600000):
    playwright, context, page = await _open(profile, channel, False, timeout_ms, False)
    try:
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await page.bring_to_front()
        deadline = time.monotonic() + timeout_ms / 1000
        last_message = ""
        login_opened = False
        verification_started = None
        while True:
            if page.is_closed():
                raise BrowserRisk("browser_closed", "登录浏览器已关闭")
            marker = await _challenge(page)
            if marker is None:
                if verification_started is None and time.monotonic() >= deadline:
                    raise BrowserRisk("login_timeout", "等待抖音登录超时")
            elif marker:
                if verification_started is None:
                    verification_started = time.monotonic()
            else:
                if verification_started is not None:
                    deadline += time.monotonic() - verification_started
                    verification_started = None
                if time.monotonic() >= deadline:
                    raise BrowserRisk("login_timeout", "等待抖音登录超时")
                if await _logged_in(context, page):
                    return {"logged_in": True}
                if not login_opened and (await _login_panel(page)) is False:
                    await page.get_by_text("登录", exact=True).last.click(timeout=15000)
                    login_opened = True
            if marker is None:
                message = "正在等待登录页面恢复可读取状态。"
            else:
                message = f"请在浏览器中完成验证：{marker}" if marker else "请在浏览器中完成抖音登录"
            if message != last_message:
                print(message, flush=True)
                last_message = message
            await page.wait_for_timeout(2000)
    except Exception as exc:
        if page.is_closed():
            raise BrowserRisk("browser_closed", "登录浏览器已关闭") from exc
        raise
    finally:
        await _close(playwright, context)


async def search(
    keyword,
    profile,
    max_items=100,
    max_pages=20,
    scroll_delay=1.5,
    response_timeout=20,
    channel="chrome",
    headless=False,
    allow_guest=False,
    session=None,
    on_page=None,
    complete_last_page=False,
):
    owns_session = session is None
    if owns_session:
        playwright, context, page = await open_search_session(profile, channel=channel, headless=headless)
    else:
        playwright, context, page = session
    queue = asyncio.Queue()
    submission = {"active": False, "requested": False, "response_started": False, "captured": False, "rejected": 0, "requests": {}}

    def request_handler(request):
        if submission["active"] and _search_request_matches(request, page, keyword):
            submission["requests"][id(request)] = request
            if not submission["requested"]:
                print("目标搜索请求已发出，正在等待响应。", flush=True)
            submission["requested"] = True

    async def capture_response(response):
        await _capture(response, queue, SEARCH_PATHS, page=page, keyword=keyword)
        submission["captured"] = True
        print(f"已收到搜索响应 | HTTP 状态码：{response.status}", flush=True)

    def handler(response):
        if not submission["active"] or id(response.request) not in submission["requests"]:
            return
        submission["response_started"] = True
        if response.status in (403, 429):
            submission["rejected"] = response.status
        return asyncio.create_task(capture_response(response))

    context.on("request", request_handler)
    context.on("response", handler)
    records = []
    pages = []
    seen = set()
    inspected = 0
    idle_rounds = 0
    empty_rounds = 0
    try:
        await page.goto("https://www.douyin.com/jingxuan", wait_until="domcontentloaded")
        await _wait_entry_ready(page, headless=headless)
        await _wait_for_verification(page, headless=headless)
        if not allow_guest and not await _logged_in(context, page):
            raise BrowserRisk("login_required", "请先运行 login")
        user_agent = await page.evaluate("() => navigator.userAgent")
        await _submit_search(page, queue, keyword, submission, headless=headless)
        while inspected < max_items and len(pages) < max_pages:
            try:
                timeout = max(120, response_timeout) if not pages else response_timeout
                event = await _wait_search_response(queue, page, timeout, headless=headless)
            except TimeoutError:
                marker = await _challenge(page)
                if marker:
                    continue
                if await _login_panel(page):
                    raise BrowserRisk("login_required", "登录面板阻止了搜索，请先运行 login")
                idle_rounds += 1
                if idle_rounds >= 3:
                    if not pages:
                        raise BrowserRisk("search_response_timeout", "连续等待超时，未收到任何搜索响应")
                    break
                await _scroll_search(page)
                await page.wait_for_timeout(int(scroll_delay * 1000))
                continue
            idle_rounds = 0
            if event["status"] in (403, 429):
                raise BrowserRisk("http_rejected", f"HTTP {event['status']}", event["status"])
            if event["payload"] is None:
                raise BrowserRisk("invalid_response", event["error"] or "响应不是 JSON", event["status"])
            try:
                parsed = extract_awemes(event["payload"])
            except PayloadError as exc:
                raise BrowserRisk("business_or_schema", str(exc), event["status"]) from exc
            candidates = parsed["awemes"] if complete_last_page else parsed["awemes"][:max_items - inspected]
            inspected += len(candidates)
            added = 0
            for aweme in candidates:
                aweme_id = str(aweme.get("aweme_id") or "").strip()
                if not aweme_id or aweme_id in seen:
                    continue
                record = normalize_aweme(aweme, keyword=keyword, user_agent=user_agent)
                if not record["variants"]:
                    continue
                seen.add(aweme_id)
                records.append(record)
                added += 1
            pages.append(
                {
                    "page": len(pages) + 1,
                    "http_status": event["status"],
                    "received": parsed["received"],
                    "inspected": len(candidates),
                    "inspected_total": inspected,
                    "added": added,
                    "cursor": parsed["cursor"],
                    "has_more": parsed["has_more"],
                    "search_id": parsed["search_id"],
                }
            )
            empty_rounds = empty_rounds + 1 if not candidates else 0
            print(f"已查看 {inspected}/{max_items} 条搜索视频 | 去重后 {len(records)} 条", flush=True)
            if on_page is not None:
                await on_page(records[len(records) - added:], pages[-1].copy())
            if inspected >= max_items or len(pages) >= max_pages or parsed["has_more"] in (0, "0", False) or empty_rounds >= 3:
                break
            await _scroll_search(page)
            await page.wait_for_timeout(int(scroll_delay * 1000))
        await _wait_for_verification(page, headless=headless)
        return records, pages
    finally:
        remove_listener = getattr(context, "remove_listener", None)
        if remove_listener:
            remove_listener("request", request_handler)
            remove_listener("response", handler)
        if owns_session:
            await close_search_session((playwright, context, page))


async def refresh(records, profile, channel="chrome", headless=False, response_timeout=20):
    playwright, context, page = await _open(profile, channel, headless, 60000, True)
    queue = asyncio.Queue()
    context.on("response", lambda response: asyncio.create_task(_capture(response, queue, DETAIL_PATH)))
    refreshed = []
    failures = []
    try:
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        await _wait_for_verification(page, headless=headless)
        if not await _logged_in(context, page):
            raise BrowserRisk("login_required", "请先运行 login")
        user_agent = await page.evaluate("() => navigator.userAgent")
        for record in records:
            await _wait_for_verification(page, headless=headless)
            while not queue.empty():
                queue.get_nowait()
            aweme_id = record["aweme_id"]
            await page.goto(f"https://www.douyin.com/video/{aweme_id}", wait_until="domcontentloaded")
            try:
                event = await _wait_search_response(queue, page, response_timeout, headless=headless)
            except TimeoutError:
                marker = await _challenge(page)
                failures.append({"aweme_id": aweme_id, "reason": marker or "detail_timeout"})
                continue
            if event["status"] in (403, 429) or event["payload"] is None:
                if event["status"] in (403, 429):
                    raise BrowserRisk("http_rejected", f"HTTP {event['status']}", event["status"])
                failures.append({"aweme_id": aweme_id, "reason": "invalid_response"})
                continue
            detail = event["payload"].get("aweme_detail") if isinstance(event["payload"], dict) else None
            if not isinstance(detail, dict) or str(detail.get("aweme_id")) != aweme_id:
                failures.append({"aweme_id": aweme_id, "reason": "detail_mismatch"})
                continue
            refreshed.append(
                normalize_aweme(
                    detail,
                    keyword=record.get("source_keyword", ""),
                    user_agent=user_agent,
                )
            )
        await _wait_for_verification(page, headless=headless)
        return refreshed, failures
    finally:
        await _close(playwright, context)
