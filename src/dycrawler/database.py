import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    aweme_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_hits (
    keyword TEXT NOT NULL,
    aweme_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (keyword, aweme_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phase TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect(path):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def save_records(path, records, keyword=""):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(path) as connection:
        for record in records:
            payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            connection.execute(
                "INSERT INTO videos(aweme_id,payload,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(aweme_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                (record["aweme_id"], payload, now),
            )
            if keyword:
                connection.execute(
                    "INSERT INTO search_hits(keyword,aweme_id,first_seen_at,last_seen_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(keyword,aweme_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                    (keyword, record["aweme_id"], now, now),
                )


def load_records(path, keyword="", aweme_ids=None, limit=0):
    aweme_ids = [str(item) for item in (aweme_ids or []) if str(item)]
    parameters = []
    if keyword:
        query = (
            "SELECT v.payload FROM videos v JOIN search_hits h ON h.aweme_id=v.aweme_id "
            "WHERE h.keyword=? ORDER BY h.first_seen_at,v.aweme_id"
        )
        parameters.append(keyword)
    elif aweme_ids:
        placeholders = ",".join("?" for _ in aweme_ids)
        query = f"SELECT payload FROM videos WHERE aweme_id IN ({placeholders}) ORDER BY aweme_id"
        parameters.extend(aweme_ids)
    else:
        query = "SELECT payload FROM videos ORDER BY updated_at,aweme_id"
    if limit > 0:
        query += " LIMIT ?"
        parameters.append(limit)
    with connect(path) as connection:
        return [json.loads(row[0]) for row in connection.execute(query, parameters)]


def save_event(path, phase, category, severity, detail):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    with connect(path) as connection:
        connection.execute(
            "INSERT INTO events(phase,category,severity,detail,created_at) VALUES(?,?,?,?,?)",
            (phase, category, severity, detail[:2000], now),
        )


def export_jsonl(path, output, keyword="", aweme_ids=None, limit=0):
    records = load_records(path, keyword=keyword, aweme_ids=aweme_ids, limit=limit)
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".part")
    with temporary.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
    temporary.replace(output)
    return len(records)


def database_status(path):
    with connect(path) as connection:
        videos = connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        keywords = connection.execute("SELECT COUNT(DISTINCT keyword) FROM search_hits").fetchone()[0]
        events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        variants = 0
        for row in connection.execute("SELECT payload FROM videos"):
            variants += len(json.loads(row[0]).get("variants") or [])
    return {"videos": videos, "variants": variants, "keywords": keywords, "events": events}


def recent_events(path, limit=20):
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT phase,category,severity,detail,created_at FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    return [dict(row) for row in rows]
