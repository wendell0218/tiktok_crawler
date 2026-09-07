import pytest

from dycrawler.normalize import PayloadError, extract_awemes, extract_co_creators, normalize_aweme, select_variant


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


@pytest.mark.parametrize("status", [1, "1"])
def test_accepted_co_creators_preserve_primary_author_and_public_fields(status):
    aweme = sample_aweme()
    aweme["aweme_id"] = "7619622418637720878"
    aweme["author"] = {
        "uid": "21617530639355",
        "sec_uid": "MS4wLjABAAAAssihLDGWRZQW6LPBR9aTi5UTO-vgXikwTObIvrMCz_Q",
        "nickname": "亿点点不一样",
    }
    member = {
        "uid": "105525949232",
        "sec_uid": "MS4wLjABAAAAaCcBHb3Rhc4zxF8YkBOfHfLh6k-IWEK2l3Ne9xOXPnQ",
        "nickname": "影视飓风",
        "role_title": "出镜",
        "invite_status": status,
        "avatar_thumb": {"url_list": ["https://example.com/avatar"]},
        "follower_count": 15968092,
        "extra": "not exported",
    }
    aweme["cooperation_info"] = {"co_creators": [member]}
    expected = {key: member[key] for key in ("uid", "sec_uid", "nickname", "role_title")} | {"invite_status": 1}
    record = normalize_aweme(aweme)
    assert record["aweme_id"] == "7619622418637720878"
    assert record["co_creators"] == extract_co_creators(aweme) == [expected]
    assert record["author"] == aweme["author"]
    assert record["author"]["sec_uid"] != record["co_creators"][0]["sec_uid"]
    assert member["invite_status"] == status and "extra" in member


@pytest.mark.parametrize("info", [
    None, {}, [], "invalid", False, 1,
    {"co_creators": None}, {"co_creators": {}}, {"co_creators": "invalid"},
    {"co_creators": [None, [], "invalid", 1, True, {}]},
])
def test_missing_or_malformed_cooperation_info_is_empty(info):
    aweme = sample_aweme()
    aweme["cooperation_info"] = info
    assert extract_co_creators(aweme) == []
    assert normalize_aweme(aweme)["co_creators"] == []


def test_ordinary_video_without_cooperation_field_has_no_co_creators():
    aweme = sample_aweme()
    assert "cooperation_info" not in aweme
    assert extract_co_creators(aweme) == []
    assert normalize_aweme(aweme)["co_creators"] == []


def test_unrelated_co_creator_flags_do_not_establish_membership():
    aweme = sample_aweme()
    aweme["co_creators"] = [{"sec_uid": "MS4wLjMember", "invite_status": 1}]
    aweme["cooperation_info"] = {"co_creator_nums": 1, "accepted_nums": 1, "extra": '{"is_cooperation": 1}'}
    assert extract_co_creators(aweme) == []
    assert normalize_aweme(aweme)["co_creators"] == []


@pytest.mark.parametrize("status", [None, True, False, 0, 2, -1, 1.0, 1.5, "0", "2", "01", "1.0", " 1 ", "", [], {}])
def test_co_creator_invitation_requires_exact_accepted_status(status):
    aweme = sample_aweme()
    aweme["cooperation_info"] = {"co_creators": [{"sec_uid": "MS4wLjMember", "invite_status": status}]}
    assert extract_co_creators(aweme) == []
    assert normalize_aweme(aweme)["co_creators"] == []


def test_missing_co_creator_invitation_is_not_accepted():
    aweme = sample_aweme()
    aweme["cooperation_info"] = {"co_creators": [{"sec_uid": "MS4wLjMember"}]}
    assert extract_co_creators(aweme) == []


@pytest.mark.parametrize("sec_uid", [None, "", "  ", 123, True, False, [], {}])
def test_co_creator_identity_requires_a_nonempty_string(sec_uid):
    aweme = sample_aweme()
    aweme["cooperation_info"] = {"co_creators": [{"sec_uid": sec_uid, "invite_status": 1}]}
    assert extract_co_creators(aweme) == []


def test_co_creators_deduplicate_by_identity_after_filtering():
    aweme = sample_aweme()
    aweme["cooperation_info"] = {"co_creators": [
        {"sec_uid": "MS4wLjFirst", "invite_status": 0},
        {"sec_uid": "MS4wLjFirst", "invite_status": "1", "nickname": "同名用户"},
        {"sec_uid": "MS4wLjFirst", "invite_status": 1, "nickname": "重复用户"},
        {"sec_uid": "MS4wLjSecond", "invite_status": 1, "nickname": "同名用户"},
    ]}
    expected = [
        {"uid": "", "sec_uid": "MS4wLjFirst", "nickname": "同名用户", "role_title": "", "invite_status": 1},
        {"uid": "", "sec_uid": "MS4wLjSecond", "nickname": "同名用户", "role_title": "", "invite_status": 1},
    ]
    assert extract_co_creators(aweme) == expected
    assert normalize_aweme(aweme)["co_creators"] == expected
