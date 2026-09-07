import pytest

from dycrawler.normalize import PayloadError, extract_awemes, normalize_aweme, select_variant


def sample_aweme():
    return {
        "aweme_id": 7420000000000000001,
        "aweme_type": 4,
        "desc": "示例视频",
        "create_time": 1720000000,
        "author": {"uid": "1", "sec_uid": "sec", "nickname": "作者"},
        "statistics": {"digg_count": 12},
        "video": {
            "duration": 15000,
            "width": 1080,
            "height": 1920,
            "origin_cover": {"url_list": ["https://cover.example.com/a.jpg"]},
            "bit_rate": [
                {
                    "gear_name": "normal_720_1",
                    "bit_rate": 1000000,
                    "FPS": 30,
                    "codec_type": "h264",
                    "play_addr": {
                        "uri": "video-720",
                        "width": 720,
                        "height": 1280,
                        "data_size": 100,
                        "url_list": [
                            "https://cdn.example.com/720-a.mp4",
                            "https://cdn.example.com/720-b.mp4",
                        ],
                    },
                },
                {
                    "gear_name": "normal_1080_1",
                    "bit_rate": 2000000,
                    "codec_type": "h265",
                    "play_addr_265": {
                        "uri": "video-1080",
                        "width": 1080,
                        "height": 1920,
                        "data_size": 200,
                        "url_list": ["https://cdn.example.com/1080.mp4"],
                    },
                },
            ],
        },
    }


def test_extract_and_normalize_search_payload():
    aweme = sample_aweme()
    payload = {
        "status_code": 0,
        "cursor": 15,
        "has_more": 1,
        "data": [{"type": 1, "aweme_info": aweme}, {"type": 99}],
    }
    parsed = extract_awemes(payload)
    record = normalize_aweme(parsed["awemes"][0], keyword="测试", user_agent="browser")
    assert parsed["received"] == 2
    assert parsed["cursor"] == 15
    assert record["aweme_id"] == "7420000000000000001"
    assert record["source_keyword"] == "测试"
    assert len(record["variants"]) == 2
    assert record["variants"][0]["quality"] == 1080
    assert record["play_url"] == "https://cdn.example.com/1080.mp4"


def test_mix_item_and_business_error():
    aweme = sample_aweme()
    parsed = extract_awemes({"data": [{"aweme_mix_info": {"mix_items": [aweme]}}]})
    assert parsed["awemes"][0]["aweme_id"] == aweme["aweme_id"]
    with pytest.raises(PayloadError):
        extract_awemes({"status_code": 2483, "data": []})


def test_variant_selection():
    record = normalize_aweme(sample_aweme())
    assert select_variant(record, quality="best")["quality"] == 1080
    assert select_variant(record, quality="720p", codec="h264")["quality"] == 720
    assert select_variant(record, quality="900", fallback="lower")["quality"] == 720
    with pytest.raises(LookupError):
        select_variant(record, quality="480")


def test_normalize_collection_membership():
    aweme = sample_aweme()
    aweme["mix_info"] = {
        "mix_id": 7420000000000000002,
        "mix_name": "  摄影教程  ",
        "extra": "not exported",
    }
    record = normalize_aweme(aweme)
    assert record["collection"] == {"id": "7420000000000000002", "title": "摄影教程"}
    assert aweme["mix_info"]["mix_id"] == 7420000000000000002


@pytest.mark.parametrize("mix_info", [None, {}, [], "invalid", {"mix_name": "Only a title"}])
def test_collection_without_membership_id_is_unknown(mix_info):
    aweme = sample_aweme()
    aweme["mix_info"] = mix_info
    assert normalize_aweme(aweme)["collection"] is None


@pytest.mark.parametrize("mix_id", [None, "", "  ", "$undefined", False, True, {}, []])
def test_invalid_collection_id_is_unknown(mix_id):
    aweme = sample_aweme()
    aweme["mix_info"] = {"mix_id": mix_id, "mix_name": "Example"}
    assert normalize_aweme(aweme)["collection"] is None


def test_collection_title_is_optional():
    aweme = sample_aweme()
    aweme["mix_info"] = {"mix_id": "7420000000000000002"}
    assert normalize_aweme(aweme)["collection"] == {"id": "7420000000000000002", "title": ""}


def test_series_is_not_reported_as_collection():
    aweme = sample_aweme()
    assert normalize_aweme(aweme)["collection"] is None
    aweme["series_info"] = {"series_id": "123", "series_name": "A paid series"}
    assert normalize_aweme(aweme)["collection"] is None
