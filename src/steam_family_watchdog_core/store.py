"""SQLite 库存与可靠批次投递；表结构与 JS 版保持兼容。

Store 的操作同步执行，应在同一事件循环线程中调用；无需外部数据库服务。
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .artwork import artwork
from .util import iso_time, json_text


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def valid_client(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ApiError(400, "invalid_clientid", "clientid 必须为 1～128 字符，只能包含字母、数字、下划线、点、冒号或连字符")
    return value


def normalize_apps(apps: object) -> list[dict]:
    if not isinstance(apps, list):
        raise ValueError("Steam 返回的 apps 不是数组；本次不覆盖库存")
    seen = set()
    normalized = []
    for item in apps:
        if (not isinstance(item, dict) or type(item.get("appid")) is not int
                or item["appid"] <= 0 or item["appid"] in seen):
            raise ValueError("Steam 返回无效或重复的 AppID；本次不覆盖库存")
        seen.add(item["appid"])
        reason = item.get("exclude_reason")
        app_type = item.get("app_type")
        if (reason is not None and reason != 0) or (app_type is not None and app_type != 1):
            continue
        if "owner_steamids" in item and not isinstance(item["owner_steamids"], list):
            raise ValueError("Steam 返回无效的 owner_steamids；本次不覆盖库存")
        owners = item.get("owner_steamids", [])
        if any(not isinstance(id_, str) or not re.fullmatch(r"[0-9]{17}", id_) for id_ in owners):
            raise ValueError("SteamID64 必须是字符串，避免整数精度丢失")
        acquired = item.get("rt_time_acquired")
        name = item.get("name")
        normalized.append({
            "appid": item["appid"],
            "name": name.strip() if isinstance(name, str) and name.strip() else f"App {item['appid']}",
            "owners": sorted(set(owners)),
            "rt_time_acquired": acquired if type(acquired) is int and acquired > 0 else None,
            **artwork(item["appid"], item.get("capsule_filename"), item.get("img_icon_hash")),
        })
    return normalized


class Store:
    def __init__(self, file: str | Path = ":memory:", missing_confirmations: int = 2):
        if type(missing_confirmations) is not int or not 1 <= missing_confirmations <= 10:
            raise ValueError("missing_confirmations 必须为 1～10 的整数")
        self.db = sqlite3.connect(str(file), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.missing_confirmations = missing_confirmations
        self.db.executescript("""
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS families (
                id TEXT PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0, last_scan TEXT
            );
            CREATE TABLE IF NOT EXISTS games (
                family_id TEXT NOT NULL, appid INTEGER NOT NULL, name TEXT NOT NULL,
                owners TEXT NOT NULL, acquired INTEGER, missing INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(family_id, appid)
            );
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY, cursor INTEGER NOT NULL, delivery_id TEXT,
                pending_ids TEXT, last_delivery_id TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS member_names (
                steamid TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS game_artwork (
                family_id TEXT NOT NULL, appid INTEGER NOT NULL, details TEXT NOT NULL,
                PRIMARY KEY(family_id, appid)
            );
        """)

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: object) -> None:
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def name(self, steamid: str, aliases: dict | None = None) -> str | None:
        alias = (aliases or {}).get(steamid)
        if isinstance(alias, str) and alias:
            return alias
        row = self.db.execute("SELECT name FROM member_names WHERE steamid=?", (steamid,)).fetchone()
        return (row["name"] or None) if row else None

    def save_names(self, names: dict[str, str], at: str) -> None:
        with self.transaction():
            self.db.executemany("""INSERT INTO member_names VALUES (?,?,?) ON CONFLICT(steamid)
                DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at""",
                ((id_, name, at) for id_, name in names.items()))

    def artwork(self, family_id: str, appid: int) -> dict:
        row = self.db.execute("SELECT details FROM game_artwork WHERE family_id=? AND appid=?", (family_id, appid)).fetchone()
        return json.loads(row["details"]) if row else artwork(appid)

    def event(self, row: sqlite3.Row) -> dict:
        payload = json.loads(row["payload"])
        # Legacy payloads gain optional artwork without altering stored history.
        return {"event_id": row["id"], **self.artwork(payload["family_groupid"], payload["appid"]), **payload}

    def scan(self, family_id: str, raw_apps: object, detected_at: str,
             members: list[str] | None = None, aliases: dict | None = None) -> dict:
        apps = normalize_apps(raw_apps)  # Validate the entire response before writing.
        members = members or []
        with self.transaction():
            family = self.db.execute("SELECT * FROM families WHERE id=?", (family_id,)).fetchone()
            initial = not family or not family["initialized"]
            previous_members = json.loads(self.meta(f"members:{family_id}") or "[]")
            joined = set(members) - set(previous_members)
            old = {row["appid"]: row for row in self.db.execute("SELECT * FROM games WHERE family_id=?", (family_id,))}
            current_ids = {app["appid"] for app in apps}
            count = 0
            for app in apps:
                prior = old.get(app["appid"])
                prior_owners = json.loads(prior["owners"]) if prior else {}
                added_owners = [id_ for id_ in app["owners"] if id_ not in prior_owners]
                owners = dict.fromkeys(app["owners"], 0)
                if not app["owners"] and prior:
                    owners.update(prior_owners)
                else:
                    for id_, misses in prior_owners.items():
                        if id_ not in owners and misses + 1 < self.missing_confirmations:
                            owners[id_] = misses + 1
                images = artwork(app["appid"], app["capsule_filename"], app["img_icon_hash"])
                if not initial and (not prior or added_owners):
                    source_ids = added_owners if prior else app["owners"]
                    payload = {
                        "type": "owner_added" if prior else "game_added",
                        "family_groupid": family_id, "appid": app["appid"], "name": app["name"],
                        "owners": [{"steamid": id_, "name": self.name(id_, aliases)} for id_ in app["owners"]],
                        "added_owners": [{"steamid": id_, "name": self.name(id_, aliases)} for id_ in source_ids],
                        "source_context": "member_joined" if joined.intersection(source_ids) else "library_change",
                        "detected_at": detected_at, "observed_after": family["last_scan"], "observed_until": detected_at,
                        "rt_time_acquired": app["rt_time_acquired"],
                        "steam_acquired_at": iso_time(app["rt_time_acquired"]) if app["rt_time_acquired"] else None,
                        "acquired_time_verified": False, **images,
                    }
                    self.db.execute("INSERT INTO events(payload) VALUES (?)", (json_text(payload),))
                    count += 1
                self.db.execute("""INSERT INTO games VALUES (?,?,?,?,?,0)
                    ON CONFLICT(family_id,appid) DO UPDATE SET name=excluded.name,
                    owners=excluded.owners, acquired=excluded.acquired, missing=0""",
                    (family_id, app["appid"], app["name"], json_text(owners), app["rt_time_acquired"]))
                self.db.execute("""INSERT INTO game_artwork VALUES (?,?,?) ON CONFLICT(family_id,appid)
                    DO UPDATE SET details=excluded.details""", (family_id, app["appid"], json_text(images)))
            for appid, prior in old.items():
                if appid not in current_ids:
                    if prior["missing"] + 1 >= self.missing_confirmations:
                        self.db.execute("DELETE FROM games WHERE family_id=? AND appid=?", (family_id, appid))
                    else:
                        self.db.execute("UPDATE games SET missing=missing+1 WHERE family_id=? AND appid=?", (family_id, appid))
            self.db.execute("""INSERT INTO families VALUES (?,1,?) ON CONFLICT(id)
                DO UPDATE SET initialized=1,last_scan=excluded.last_scan""", (family_id, detected_at))
            self.set_meta("active_family", family_id)
            self.set_meta(f"members:{family_id}", json_text(members))
            self.set_meta("last_success_at", detected_at)
            return {"baseline_created": bool(initial), "event_count": count, "game_count": len(apps)}

    def ready(self) -> bool:
        return self.meta("active_family") is not None

    def latest_id(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]

    def register(self, clientid: str, start: str = "latest") -> dict:
        valid_client(clientid)
        if start not in ("latest", "beginning"):
            raise ApiError(400, "invalid_start", "start 必须为 latest 或 beginning")
        if not self.ready():
            raise ApiError(503, "not_ready", "尚未建立家庭库基线，请先完成 Steam 登录")
        row = self.db.execute("SELECT * FROM clients WHERE id=?", (clientid,)).fetchone()
        if row:
            return {"created": False, "cursor": row["cursor"]}
        cursor = 0 if start == "beginning" else self.latest_id()
        self.db.execute("INSERT INTO clients(id,cursor,created_at) VALUES (?,?,?)", (clientid, cursor, iso_time()))
        return {"created": True, "cursor": cursor}

    def changes(self, clientid: str, limit: int = 10) -> dict:
        valid_client(clientid)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ApiError(400, "invalid_limit", "limit 必须为 1～100 的整数")
        with self.transaction():
            registered = self.register(clientid, "beginning")
            client = self.db.execute("SELECT * FROM clients WHERE id=?", (clientid,)).fetchone()
            delivery_id = client["delivery_id"]
            if delivery_id:
                rows = [self.db.execute("SELECT * FROM events WHERE id=?", (id_,)).fetchone()
                        for id_ in json.loads(client["pending_ids"])]
            else:
                rows = self.db.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (client["cursor"], limit)).fetchall()
                if rows:
                    delivery_id = str(uuid4())
                    self.db.execute("UPDATE clients SET delivery_id=?,pending_ids=? WHERE id=?",
                                    (delivery_id, json_text([row["id"] for row in rows]), clientid))
            return {
                "success": True, "clientid": clientid, "client_created": registered["created"],
                "delivery_id": delivery_id or None, "count": len(rows),
                "new_games": [self.event(row) for row in rows],
                "has_more": self.latest_id() > (rows[-1]["id"] if rows else client["cursor"]),
                "cursor": client["cursor"],
            }

    def ack(self, clientid: str, delivery_id: str) -> dict:
        valid_client(clientid)
        if not isinstance(delivery_id, str) or len(delivery_id) > 64:
            raise ApiError(400, "invalid_delivery_id", "需要有效的 delivery_id")
        with self.transaction():
            row = self.db.execute("SELECT * FROM clients WHERE id=?", (clientid,)).fetchone()
            if not row:
                raise ApiError(404, "unknown_client", "clientid 尚未注册")
            if row["last_delivery_id"] == delivery_id:
                return {"success": True, "already_acked": True, "cursor": row["cursor"]}
            if row["delivery_id"] != delivery_id:
                raise ApiError(409, "delivery_mismatch", "delivery_id 与待确认批次不匹配")
            cursor = max(row["cursor"], *json.loads(row["pending_ids"]))
            self.db.execute("""UPDATE clients SET cursor=?,last_delivery_id=?,delivery_id=NULL,pending_ids=NULL WHERE id=?""",
                            (cursor, delivery_id, clientid))
            return {"success": True, "already_acked": False, "cursor": cursor}

    def games(self) -> list[dict]:
        family_id = self.meta("active_family")
        return [{
            "appid": row["appid"], "name": row["name"],
            "owner_steamids": sorted(json.loads(row["owners"])), "rt_time_acquired": row["acquired"],
            **self.artwork(family_id, row["appid"]),
        } for row in self.db.execute("SELECT * FROM games WHERE family_id=? AND missing=0 ORDER BY name", (family_id,))]

    def status(self) -> dict:
        return {
            "baseline_ready": self.ready(), "family_groupid": self.meta("active_family"),
            "last_success_at": self.meta("last_success_at"),
            "event_count": self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "client_count": self.db.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
        }
