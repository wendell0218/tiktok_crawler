import asyncio
import ipaddress
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .normalize import select_variant


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)


class DownloadError(RuntimeError):
    def __init__(self, category, detail, status=0):
        super().__init__(detail)
        self.category = category
        self.detail = detail
        self.status = status


def _safe_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        return False
    try:
        return not ipaddress.ip_address(host).is_private
    except ValueError:
        return True


def _filename(record, variant):
    quality = f"{variant.get('quality')}p" if variant.get("quality") else "unknown"
    codec = variant.get("codec") or "unknown"
    aweme_id = re.sub(r"[^0-9A-Za-z_-]", "_", str(record["aweme_id"]))
    return f"{aweme_id}_{quality}_{codec}.mp4"


async def _request(client, record, variant, output_dir, probe_bytes, max_bytes, delete_after):
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / _filename(record, variant)
    temporary = output.with_name(f"{output.name}.{uuid4().hex}.part")
    if output.exists() and probe_bytes == 0 and not delete_after:
        return {
            "aweme_id": record["aweme_id"],
            "variant_id": variant.get("variant_id", ""),
            "quality": variant.get("quality"),
            "codec": variant.get("codec"),
            "status": "exists",
            "bytes": output.stat().st_size,
            "output": str(output),
        }
    temporary.unlink(missing_ok=True)
    headers = {
        "User-Agent": record.get("browser_user_agent") or DEFAULT_USER_AGENT,
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
    }
    if probe_bytes > 0:
        headers["Range"] = f"bytes=0-{probe_bytes - 1}"
    last_error = None
    for url in variant.get("urls") or []:
        if not _safe_url(url):
            last_error = DownloadError("unsafe_url", "媒体链接不符合安全要求")
            continue
        started = time.monotonic()
        try:
            async with client.stream("GET", url, headers=headers) as response:
                status = response.status_code
                if status in (403, 429):
                    raise DownloadError("media_rejected", f"HTTP {status}", status)
                if status not in (200, 206):
                    raise DownloadError("http_error", f"HTTP {status}", status)
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                first = bytearray()
                total = 0
                file = None if probe_bytes > 0 else temporary.open("wb")
                try:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        if probe_bytes > 0:
                            chunk = chunk[: max(0, probe_bytes - total)]
                        total += len(chunk)
                        if len(first) < 64:
                            first.extend(chunk[: 64 - len(first)])
                        if file:
                            file.write(chunk)
                        if max_bytes > 0 and total > max_bytes:
                            raise DownloadError("size_limit", "下载大小超过设定上限")
                        if probe_bytes > 0 and total >= probe_bytes:
                            break
                finally:
                    if file:
                        file.close()
                valid_media = content_type.startswith("video/") or b"ftyp" in bytes(first[:64])
                if not valid_media:
                    raise DownloadError("invalid_media", f"响应内容不是有效视频，类型为 {content_type}")
                elapsed = time.monotonic() - started
                if probe_bytes == 0:
                    if delete_after:
                        temporary.unlink(missing_ok=True)
                    else:
                        temporary.replace(output)
                return {
                    "aweme_id": record["aweme_id"],
                    "variant_id": variant.get("variant_id", ""),
                    "quality": variant.get("quality"),
                    "codec": variant.get("codec"),
                    "status": "probed" if probe_bytes > 0 else "downloaded",
                    "http_status": status,
                    "bytes": total,
                    "elapsed_seconds": round(elapsed, 3),
                    "mib_per_second": round(total / 1048576 / max(elapsed, 0.001), 3),
                    "redirects": len(response.history),
                    "content_type": content_type,
                    "effective_host": response.url.host,
                    "deleted": delete_after and probe_bytes == 0,
                    "output": "" if probe_bytes > 0 or delete_after else str(output),
                }
        except DownloadError as exc:
            temporary.unlink(missing_ok=True)
            last_error = exc
            if exc.status == 429:
                raise
        except (httpx.HTTPError, OSError) as exc:
            temporary.unlink(missing_ok=True)
            last_error = DownloadError("transport_error", exc.__class__.__name__)
        except asyncio.CancelledError:
            temporary.unlink(missing_ok=True)
            raise
    if last_error:
        raise last_error
    raise DownloadError("missing_url", "该视频版本没有可用的下载链接")


async def download_many(
    records,
    output_dir,
    quality="best",
    codec="any",
    fallback="error",
    delay=0,
    timeout=300,
    probe_bytes=0,
    max_bytes=0,
    delete_after=False,
    workers=1,
    on_result=None,
):
    if 0 < probe_bytes < 64:
        raise ValueError("probe_bytes 必须至少为 64 字节")
    if not isinstance(workers, int) or workers < 1:
        raise ValueError("workers 必须为正整数")
    unique_records = []
    seen_ids = set()
    for record in records:
        aweme_id = str(record.get("aweme_id", ""))
        if aweme_id not in seen_ids:
            seen_ids.add(aweme_id)
            unique_records.append(record)
    if not unique_records:
        return []
    timeout_config = httpx.Timeout(timeout, connect=min(20, timeout))
    limits = httpx.Limits(max_connections=workers, max_keepalive_connections=workers)
    results = [None] * len(unique_records)
    next_index = 0
    stopped = False

    async def remove_cookie(request):
        request.headers.pop("cookie", None)

    async with httpx.AsyncClient(
        follow_redirects=True,
        trust_env=False,
        timeout=timeout_config,
        limits=limits,
        event_hooks={"request": [remove_cookie]},
    ) as client:
        async def worker():
            nonlocal next_index, stopped
            while not stopped and next_index < len(unique_records):
                index = next_index
                next_index += 1
                record = unique_records[index]
                try:
                    variant = select_variant(record, quality=quality, codec=codec, fallback=fallback)
                    result = await _request(
                        client,
                        record,
                        variant,
                        output_dir,
                        probe_bytes,
                        max_bytes,
                        delete_after,
                    )
                except (DownloadError, LookupError, ValueError) as exc:
                    result = {
                        "aweme_id": record.get("aweme_id", ""),
                        "status": "failed",
                        "category": getattr(exc, "category", "selection_error"),
                        "http_status": getattr(exc, "status", 0),
                        "detail": str(exc),
                    }
                results[index] = result
                if result.get("http_status") == 429:
                    stopped = True
                if on_result is not None:
                    on_result(result)
                if delay > 0 and not stopped and next_index < len(unique_records):
                    await asyncio.sleep(delay)

        async with asyncio.TaskGroup() as tasks:
            for _ in range(min(workers, len(unique_records))):
                tasks.create_task(worker())
    return [result for result in results if result is not None]
