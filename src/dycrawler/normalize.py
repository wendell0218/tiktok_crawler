import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse


class PayloadError(ValueError):
    pass


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _text(value):
    return "" if value is None else str(value).strip()


def _integer(value):
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _https_urls(value):
    output = []
    seen = set()
    for item in _list(value):
        url = _text(item)
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or url in seen:
            continue
        seen.add(url)
        output.append(url)
    return output


def _codec(source, entry, address):
    values = [
        source,
        entry.get("format"),
        entry.get("codec_type"),
        entry.get("codec"),
        address.get("url_key"),
    ]
    joined = " ".join(_text(value).lower() for value in values)
    if any(token in joined for token in ("h265", "hevc", "bytevc1", "265")):
        return "h265"
    if any(token in joined for token in ("h264", "avc", "264")):
        return "h264"
    return "unknown"


def _quality(width, height, fallback):
    if width and height:
        return min(width, height)
    match = re.search(r"(?<!\d)(2160|1440|1080|720|540|480|360|240)p?(?!\d)", _text(fallback).lower())
    return int(match.group(1)) if match else None


def _variant(video, address, source, entry):
    address = _mapping(address)
    entry = _mapping(entry)
    urls = _https_urls(address.get("url_list"))
    if not urls:
        return None
    width = _integer(address.get("width")) or _integer(entry.get("width")) or _integer(video.get("width"))
    height = _integer(address.get("height")) or _integer(entry.get("height")) or _integer(video.get("height"))
    bitrate = _integer(entry.get("bit_rate")) or _integer(address.get("bit_rate"))
    size_bytes = _integer(address.get("data_size")) or _integer(entry.get("data_size"))
    fps = _integer(entry.get("FPS")) or _integer(entry.get("fps"))
    gear_name = _text(entry.get("gear_name"))
    quality_type = entry.get("quality_type")
    codec = _codec(source, entry, address)
    media_uri = _text(address.get("uri"))
    identity = json.dumps(
        [media_uri, codec, width, height, bitrate, gear_name, quality_type, source],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return {
        "variant_id": hashlib.sha256(identity.encode()).hexdigest()[:16],
        "source": source,
        "codec": codec,
        "width": width,
        "height": height,
        "quality": _quality(width, height, gear_name or quality_type),
        "bitrate": bitrate,
        "fps": fps,
        "size_bytes": size_bytes,
        "gear_name": gear_name,
        "quality_type": quality_type,
        "media_uri": media_uri,
        "urls": urls,
    }


def extract_variants(video):
    video = _mapping(video)
    candidates = []
    for entry in _list(video.get("bit_rate")):
        entry = _mapping(entry)
        for key in ("play_addr", "play_addr_h264", "play_addr_h265", "play_addr_265"):
            variant = _variant(video, entry.get(key), f"bit_rate.{key}", entry)
            if variant:
                candidates.append(variant)
    for key in (
        "play_addr_h264",
        "play_addr_h265",
        "play_addr_265",
        "play_addr_256",
        "play_addr",
        "download_addr",
    ):
        variant = _variant(video, video.get(key), key, {})
        if variant:
            candidates.append(variant)
    merged = {}
    for variant in candidates:
        key = (
            variant["media_uri"] or variant["source"],
            variant["codec"],
            variant["width"],
            variant["height"],
            variant["bitrate"],
            variant["gear_name"],
        )
        if key not in merged:
            merged[key] = variant
            continue
        urls = merged[key]["urls"] + variant["urls"]
        merged[key]["urls"] = list(dict.fromkeys(urls))
    variants = list(merged.values())
    variants.sort(
        key=lambda item: (
            (item["width"] or 0) * (item["height"] or 0),
            item["bitrate"] or 0,
            item["size_bytes"] or 0,
            item["codec"] == "h264",
            item["variant_id"],
        ),
        reverse=True,
    )
    return variants


def extract_awemes(payload):
    if not isinstance(payload, dict):
        raise PayloadError("response is not an object")
    status_code = payload.get("status_code", 0)
    if status_code not in (0, "0", None):
        raise PayloadError(f"business status {status_code}")
    rows = payload.get("data", [])
    if not isinstance(rows, list):
        raise PayloadError("response data is not a list")
    awemes = []
    for row in rows:
        row = _mapping(row)
        aweme = _mapping(row.get("aweme_info"))
        if not aweme:
            mix_items = _list(_mapping(row.get("aweme_mix_info")).get("mix_items"))
            aweme = _mapping(mix_items[0]) if mix_items else {}
        if not aweme and row.get("aweme_id") not in (None, ""):
            aweme = row
        aweme_id = _text(aweme.get("aweme_id"))
        if aweme_id:
            awemes.append(aweme)
    extra = _mapping(payload.get("extra"))
    return {
        "awemes": awemes,
        "cursor": payload.get("cursor"),
        "has_more": payload.get("has_more"),
        "search_id": _text(payload.get("search_id") or extra.get("logid")),
        "received": len(rows),
    }


def normalize_aweme(aweme, keyword="", user_agent=""):
    aweme = _mapping(aweme)
    aweme_id = _text(aweme.get("aweme_id"))
    if not aweme_id:
        raise PayloadError("missing aweme_id")
    video = _mapping(aweme.get("video"))
    variants = extract_variants(video)
    author = _mapping(aweme.get("author"))
    statistics = _mapping(aweme.get("statistics"))
    cover = _mapping(video.get("origin_cover") or video.get("cover"))
    cover_urls = _https_urls(cover.get("url_list"))
    return {
        "aweme_id": aweme_id,
        "aweme_type": _integer(aweme.get("aweme_type")),
        "title": _text(aweme.get("desc")),
        "create_time": _integer(aweme.get("create_time")),
        "author": {
            "uid": _text(author.get("uid")),
            "sec_uid": _text(author.get("sec_uid")),
            "nickname": _text(author.get("nickname")),
        },
        "statistics": statistics,
        "duration_ms": _integer(video.get("duration")),
        "width": _integer(video.get("width")),
        "height": _integer(video.get("height")),
        "cover_url": cover_urls[0] if cover_urls else "",
        "share_url": f"https://www.douyin.com/video/{aweme_id}",
        "play_url": variants[0]["urls"][0] if variants else "",
        "variants": variants,
        "source_keyword": keyword,
        "browser_user_agent": user_agent,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def select_variant(record, quality="best", codec="any", fallback="error"):
    variants = list(record.get("variants") or [])
    if codec != "any":
        variants = [item for item in variants if item.get("codec") == codec]
    if not variants:
        raise LookupError("no matching video variant")
    if quality == "best":
        return variants[0]
    if quality == "worst":
        return variants[-1]
    target = int(quality.lower().removesuffix("p"))
    exact = [item for item in variants if item.get("quality") == target]
    if exact:
        return exact[0]
    if fallback == "lower":
        lower = [item for item in variants if item.get("quality") and item["quality"] < target]
        if lower:
            return lower[0]
    raise LookupError(f"quality {target}p is unavailable")
