import asyncio

import httpx
import pytest

from dycrawler import downloader
from dycrawler.downloader import _filename, _request, _safe_url, download_many


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


def sample_records(count):
    records = []
    for index in range(count):
        record, variant = record_and_variant()
        record["aweme_id"] = str(index)
        variant["urls"] = [f"https://cdn.example.com/{index}.mp4"]
        records.append(record)
    return records


@pytest.fixture
def mock_http(monkeypatch):
    original = httpx.AsyncClient

    def install(handler):
        options = {}

        def create(**kwargs):
            options.update(kwargs)
            return original(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(downloader.httpx, "AsyncClient", create)
        return options

    return install


@pytest.mark.parametrize("workers", [1, 3])
def test_download_workers_are_bounded_and_results_ordered(tmp_path, mock_http, workers):
    async def run():
        gate = asyncio.Event()
        release_first = asyncio.Event()
        active = 0
        peak = 0
        started = []
        completed = []

        async def handler(request):
            nonlocal active, peak
            assert "cookie" not in request.headers
            identifier = request.url.path.removesuffix(".mp4").strip("/")
            active += 1
            peak = max(peak, active)
            started.append(identifier)
            if len(started) == workers:
                gate.set()
            await gate.wait()
            if identifier == "0" and workers > 1:
                await release_first.wait()
            await asyncio.sleep(0)
            if identifier == str(workers - 1):
                release_first.set()
            active -= 1
            return httpx.Response(
                200,
                headers={"content-type": "video/mp4", "set-cookie": "session=ignore; Path=/"},
                content=b"\x00\x00\x00\x18ftypmp42" + b"x" * 100,
            )

        options = mock_http(handler)
        arguments = {} if workers == 1 else {"workers": workers}
        results = await download_many(
            sample_records(7),
            tmp_path,
            on_result=lambda result: completed.append(result["aweme_id"]),
            **arguments,
        )
        assert peak == workers
        assert options["trust_env"] is False
        assert options["limits"].max_connections == workers
        assert options["limits"].max_keepalive_connections == workers
        assert [result["aweme_id"] for result in results] == list(map(str, range(7)))
        assert all(result["status"] == "downloaded" for result in results)
        assert len(completed) == 7
        assert len(list(tmp_path.glob("*.mp4"))) == 7
        assert not list(tmp_path.glob("*.part"))
        if workers > 1:
            assert completed[0] != "0"

    asyncio.run(asyncio.wait_for(run(), 5))


def test_rate_limit_stops_new_work_and_finishes_inflight(tmp_path, mock_http):
    async def run():
        second_started = asyncio.Event()
        rate_limited = asyncio.Event()
        started = []

        async def handler(request):
            identifier = request.url.path.removesuffix(".mp4").strip("/")
            started.append(identifier)
            if identifier == "0":
                await second_started.wait()
                return httpx.Response(429)
            second_started.set()
            await rate_limited.wait()
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"video")

        def completed(result):
            if result.get("http_status") == 429:
                rate_limited.set()

        mock_http(handler)
        results = await download_many(sample_records(8), tmp_path, workers=2, on_result=completed)
        assert started == ["0", "1"]
        assert [result["aweme_id"] for result in results] == ["0", "1"]
        assert results[0]["http_status"] == 429
        assert results[0]["status"] == "failed"
        assert results[1]["status"] == "downloaded"
        assert len(list(tmp_path.glob("*.mp4"))) == 1

    asyncio.run(asyncio.wait_for(run(), 5))


def test_duplicate_ids_and_existing_files_skip_network(tmp_path, mock_http):
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"video")

    mock_http(handler)
    records = sample_records(2)
    repeated = [records[0], records[0], records[1], records[0], records[1]]
    first = asyncio.run(download_many(repeated, tmp_path, workers=3))
    second = asyncio.run(download_many(repeated, tmp_path, workers=3))
    assert sorted(calls) == ["/0.mp4", "/1.mp4"]
    assert [result["aweme_id"] for result in first] == ["0", "1"]
    assert all(result["status"] == "exists" for result in second)
    assert len(list(tmp_path.glob("*.mp4"))) == 2


def test_cancellation_cleans_only_owned_partial_files(tmp_path, mock_http):
    async def run():
        streaming = asyncio.Event()
        blocked = asyncio.Event()
        started = 0

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal started
                yield b"\x00\x00\x00\x18ftypmp42" + b"x" * 100
                started += 1
                if started == 2:
                    streaming.set()
                await blocked.wait()

        async def handler(request):
            return httpx.Response(200, headers={"content-type": "video/mp4"}, stream=Stream())

        mock_http(handler)
        unrelated = tmp_path / "unrelated.part"
        unrelated.write_bytes(b"keep")
        task = asyncio.create_task(download_many(sample_records(2), tmp_path, workers=2))
        await streaming.wait()
        assert len(list(tmp_path.glob("*.part"))) == 3
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(tmp_path.glob("*.part")) == [unrelated]
        assert unrelated.read_bytes() == b"keep"
        assert not list(tmp_path.glob("*.mp4"))

    asyncio.run(asyncio.wait_for(run(), 5))
