import asyncio

import httpx

from dycrawler.downloader import _filename, _request, _safe_url


def record_and_variant():
    variant = {
        "variant_id": "abc",
        "quality": 1080,
        "codec": "h264",
        "urls": ["https://cdn.example.com/video.mp4"],
    }
    record = {"aweme_id": "742/unsafe", "browser_user_agent": "test-agent", "variants": [variant]}
    return record, variant


def test_url_and_filename_safety():
    record, variant = record_and_variant()
    assert _safe_url("https://cdn.example.com/video.mp4")
    assert not _safe_url("http://cdn.example.com/video.mp4")
    assert not _safe_url("https://127.0.0.1/video.mp4")
    assert _filename(record, variant) == "742_unsafe_1080p_h264.mp4"


def test_probe_uses_range_without_cookie(tmp_path):
    record, variant = record_and_variant()
    media = b"\x00\x00\x00\x18ftypmp42" + b"x" * 100

    async def handler(request):
        assert request.headers["range"] == "bytes=0-63"
        assert "cookie" not in request.headers
        return httpx.Response(206, headers={"content-type": "video/mp4"}, content=media)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _request(client, record, variant, tmp_path, 64, 0, False)

    result = asyncio.run(run())
    assert result["status"] == "probed"
    assert result["bytes"] == 64
    assert list(tmp_path.iterdir()) == []


def test_delete_after_preserves_existing_file(tmp_path):
    record, variant = record_and_variant()
    output = tmp_path / _filename(record, variant)
    output.write_bytes(b"keep")
    media = b"\x00\x00\x00\x18ftypmp42" + b"x" * 100

    async def handler(request):
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=media)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _request(client, record, variant, tmp_path, 0, 0, True)

    result = asyncio.run(run())
    assert result["status"] == "downloaded"
    assert result["deleted"] is True
    assert output.read_bytes() == b"keep"
    assert not output.with_suffix(".mp4.part").exists()
