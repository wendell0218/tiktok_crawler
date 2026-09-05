import json

from dycrawler.database import database_status, export_jsonl, load_records, recent_events, save_event, save_records


def test_database_round_trip(tmp_path):
    database = tmp_path / "douyin.db"
    output = tmp_path / "metadata.jsonl"
    records = [
        {"aweme_id": "2", "title": "第二条", "variants": [{"variant_id": "b"}]},
        {"aweme_id": "1", "title": "第一条", "variants": [{"variant_id": "a"}, {"variant_id": "c"}]},
    ]
    save_records(database, records, keyword="猫")
    selected = load_records(database, keyword="猫")
    assert {item["aweme_id"] for item in selected} == {"1", "2"}
    assert export_jsonl(database, output, keyword="猫") == 2
    assert [json.loads(line)["aweme_id"] for line in output.read_text().splitlines()] == [
        item["aweme_id"] for item in selected
    ]
    save_event(database, "search", "challenge", "critical", {"status": 429})
    assert recent_events(database, 1)[0]["category"] == "challenge"
    assert database_status(database) == {"videos": 2, "variants": 3, "keywords": 1, "events": 1}


def test_upsert_keeps_keyword_relation(tmp_path):
    database = tmp_path / "douyin.db"
    save_records(database, [{"aweme_id": "1", "title": "旧", "variants": []}], keyword="猫")
    save_records(database, [{"aweme_id": "1", "title": "新", "variants": []}])
    assert load_records(database, keyword="猫")[0]["title"] == "新"
