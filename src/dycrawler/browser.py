import asyncio
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from playwright.async_api import async_playwright

from .normalize import PayloadError, extract_awemes, normalize_aweme


SEARCH_PATH = "/aweme/v1/web/general/search/single/"
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
        "locale": "zh-CN",
        "accept_downloads": False,
        "viewport": None,
        "args": ["--no-proxy-server"],
    }
    if channel != "chromium":
        options["channel"] = channel
    context = await playwright.chromium.launch_persistent_context(**options)
    context.set_default_timeout(timeout_ms)
    context.set_default_navigation_timeout(timeout_ms)
    if block_media:
        async def route_handler(route):
            host = (urlparse(route.request.url).hostname or "").lower()
            if route.request.resource_type == "media" or host.endswith("douyinvod.com"):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", route_handler)
    page = context.pages[0] if context.pages else await context.new_page()
    return playwright, context, page


async def _close(playwright, context):
    await context.close()
    await playwright.stop()


async def _logged_in(context, page):
    try:
        storage_login = await page.evaluate("() => localStorage.getItem('HasUserLogin') === '1'")
    except Exception:
        storage_login = False
    cookies = await context.cookies("https://www.douyin.com/")
    cookie_login = any(
        (item["name"] == "LOGIN_STATUS" and item["value"] == "1")
        or (item["name"] in {"sessionid", "sessionid_ss", "sid_guard"} and item["value"])
        for item in cookies
    )
    return storage_login or cookie_login


async def _challenge(page):
    try:
        title = await page.title()
    except Exception:
        title = ""
    marker = next((item for item in CHALLENGE_MARKERS if item in title), "")
    if marker:
        return marker
    try:
        captcha = page.locator("iframe[src*='captcha'], iframe[src*='verify']").first
        if await captcha.is_visible(timeout=1000):
            return "验证码"
    except Exception:
        pass
    try:
        body = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""
    return next((marker for marker in CHALLENGE_MARKERS if marker in body), "")


async def _login_panel(page):
    try:
        return await page.locator("[id^='login-full-panel-']").first.is_visible(timeout=1000)
    except Exception:
        return False


async def _capture(response, queue, path):
    parsed = urlparse(response.url)
    if parsed.hostname != "www.douyin.com" or parsed.path != path:
        return
    if response.request.resource_type not in {"xhr", "fetch"}:
        return
    try:
        payload = await response.json()
        error = ""
    except Exception as exc:
        payload = None
        error = exc.__class__.__name__
    await queue.put({"status": response.status, "payload": payload, "error": error})


async def login(profile, channel="chrome", timeout_ms=600000):
    playwright, context, page = await _open(profile, channel, False, timeout_ms, False)
    try:
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if await _logged_in(context, page):
                return {"logged_in": True}
            marker = await _challenge(page)
            if marker:
                print(f"请在浏览器完成验证：{marker}", flush=True)
            else:
                print("请在浏览器完成抖音登录", flush=True)
            await page.wait_for_timeout(2000)
        raise BrowserRisk("login_timeout", "登录等待超时")
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
):
    playwright, context, page = await _open(profile, channel, headless, 60000, True)
    queue = asyncio.Queue()
    context.on("response", lambda response: asyncio.create_task(_capture(response, queue, SEARCH_PATH)))
    records = []
    pages = []
    seen = set()
    idle_rounds = 0
    no_new_rounds = 0
    try:
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        if not allow_guest and not await _logged_in(context, page):
            raise BrowserRisk("login_required", "请先运行 login")
        user_agent = await page.evaluate("() => navigator.userAgent")
        target = f"https://www.douyin.com/search/{quote(keyword)}?type=general"
        await page.goto(target, wait_until="domcontentloaded")
        while len(records) < max_items and len(pages) < max_pages:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=response_timeout)
            except TimeoutError:
                marker = await _challenge(page)
                if marker:
                    raise BrowserRisk("challenge", marker)
                if await _login_panel(page):
                    raise BrowserRisk("login_required", "登录面板阻止了搜索，请先运行 login")
                idle_rounds += 1
                if idle_rounds >= 3:
                    break
                await page.mouse.wheel(0, 9000)
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
            nil_info = event["payload"].get("search_nil_info")
            if isinstance(nil_info, dict) and nil_info.get("search_nil_type") == "verify_check":
                raise BrowserRisk("challenge", "search verification required", event["status"])
            added = 0
            for aweme in parsed["awemes"]:
                aweme_id = str(aweme.get("aweme_id") or "").strip()
                if not aweme_id or aweme_id in seen:
                    continue
                record = normalize_aweme(aweme, keyword=keyword, user_agent=user_agent)
                if not record["variants"]:
                    continue
                seen.add(aweme_id)
                records.append(record)
                added += 1
                if len(records) >= max_items:
                    break
            pages.append(
                {
                    "page": len(pages) + 1,
                    "http_status": event["status"],
                    "received": parsed["received"],
                    "added": added,
                    "cursor": parsed["cursor"],
                    "has_more": parsed["has_more"],
                    "search_id": parsed["search_id"],
                }
            )
            no_new_rounds = no_new_rounds + 1 if added == 0 else 0
            if parsed["has_more"] in (0, "0", False) or no_new_rounds >= 3:
                break
            await page.mouse.wheel(0, 9000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(int(scroll_delay * 1000))
        marker = await _challenge(page)
        if marker:
            raise BrowserRisk("challenge", marker)
        return records, pages
    finally:
        await _close(playwright, context)


async def refresh(records, profile, channel="chrome", headless=False, response_timeout=20):
    playwright, context, page = await _open(profile, channel, headless, 60000, True)
    queue = asyncio.Queue()
    context.on("response", lambda response: asyncio.create_task(_capture(response, queue, DETAIL_PATH)))
    refreshed = []
    failures = []
    try:
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        if not await _logged_in(context, page):
            raise BrowserRisk("login_required", "请先运行 login")
        user_agent = await page.evaluate("() => navigator.userAgent")
        for record in records:
            while not queue.empty():
                queue.get_nowait()
            aweme_id = record["aweme_id"]
            await page.goto(f"https://www.douyin.com/video/{aweme_id}", wait_until="domcontentloaded")
            try:
                event = await asyncio.wait_for(queue.get(), timeout=response_timeout)
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
        return refreshed, failures
    finally:
        await _close(playwright, context)
