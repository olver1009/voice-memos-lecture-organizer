#!/usr/bin/env python3
"""Read-only lecture title/folder planner; all mutations belong to Voice Memos UI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo


APPLE_UNIX_OFFSET = 978_307_200
SEOUL = ZoneInfo("Asia/Seoul")
START_DATE = date(2026, 9, 1)
END_DATE = date(2026, 12, 18)

VOICE_DB = Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/CloudRecordings.db"
CACHE = Path.home() / "Documents/Codex/.cache/lecture-recording-organizer.json"

SCHEDULE = {
    0: [("09:00", "10:30", "미주지역지리"), ("13:00", "15:00", "지도학및실습"), ("16:30", "18:00", "도시지리학특강")],
    1: [("15:00", "16:30", "응용 지형학"), ("16:30", "18:00", "기후변화와 미래환경")],
    2: [("10:30", "12:00", "미주지역지리"), ("13:00", "15:00", "지도학및실습"), ("15:00", "16:30", "도시지리학특강")],
    3: [("15:00", "16:30", "응용 지형학"), ("16:30", "18:00", "기후변화와 미래환경")],
}


def emit(payload: dict, exit_code: int = 0) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    raise SystemExit(exit_code)


def ro_connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def normalized_title(value: str | None) -> str:
    text = unicodedata.normalize("NFC", value or "").strip()
    text = re.sub(r"\.m4a$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)[:：](?=\d)", "/", text, count=1)
    return re.sub(r"\s+", " ", text).strip()


def canonical_title(value: str) -> str:
    text = normalized_title(value)
    text = re.sub(r"^(\d+)/(\d+) ", r"\1월 \2일 ", text)
    return re.sub(r" (\d+)/(\d+)$", r" \1-\2", text)


def rounded_seconds(value: float | int) -> int:
    return int(float(value) + 0.5)


def parse_duration(value: object) -> int:
    if isinstance(value, (int, float)):
        return rounded_seconds(value)
    text = str(value or "").strip().replace(" ", "")
    if not text:
        raise ValueError("empty duration")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return rounded_seconds(float(text))
    clock = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+)", text)
    if clock:
        hours = int(clock.group(1) or 0)
        return hours * 3600 + int(clock.group(2)) * 60 + int(clock.group(3))
    hours = re.search(r"(\d+)시간", text)
    minutes = re.search(r"(\d+)분", text)
    seconds = re.search(r"(\d+)초", text)
    if not any((hours, minutes, seconds)):
        raise ValueError(f"unsupported duration: {value}")
    return int(hours.group(1) if hours else 0) * 3600 + int(minutes.group(1) if minutes else 0) * 60 + int(seconds.group(1) if seconds else 0)


def parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def classify(started: datetime) -> str | None:
    classes = SCHEDULE.get(started.weekday(), [])
    current = started.time().replace(tzinfo=None)
    core = [course for start, end, course in classes if parse_hhmm(start) <= current < parse_hhmm(end)]
    if len(core) == 1:
        return core[0]
    if core:
        return None
    early = []
    for start, _end, course in classes:
        class_start = datetime.combine(started.date(), parse_hhmm(start), SEOUL)
        if class_start - timedelta(minutes=15) <= started < class_start:
            early.append(course)
    return early[0] if len(early) == 1 else None


def load_voice_rows(path: Path, today: date) -> tuple[list[dict], set[tuple[str, int]]]:
    with ro_connect(path) as db:
        rows = db.execute(
            """
            SELECT Z_PK, ZUNIQUEID, ZCUSTOMLABELFORSORTING, ZPATH, ZDATE, ZDURATION, ZFOLDER
            FROM ZCLOUDRECORDING
            WHERE ZPATH IS NOT NULL
            ORDER BY ZDATE
            """
        ).fetchall()
    return voice_candidates(rows, today)


def voice_candidates(rows: list, today: date) -> tuple[list[dict], set[tuple[str, int]]]:
    result = []
    ignored: set[tuple[str, int]] = set()
    upper = min(today, END_DATE)
    for row in rows:
        title = row["ZCUSTOMLABELFORSORTING"] or ""
        seconds = rounded_seconds(row["ZDURATION"] or 0)
        key = (normalized_title(title), seconds)
        if float(row["ZDURATION"] or 0) <= 600:
            ignored.add(key)
            continue
        started = datetime.fromtimestamp(float(row["ZDATE"]) + APPLE_UNIX_OFFSET, timezone.utc).astimezone(SEOUL)
        if not (START_DATE <= started.date() <= upper):
            ignored.add(key)
            continue
        if not (time(9, 0) <= started.time().replace(tzinfo=None) < time(18, 0)):
            ignored.add(key)
            continue
        result.append(
            {
                "z_pk": int(row["Z_PK"]),
                "folder_pk": row["ZFOLDER"],
                "unique_id": row["ZUNIQUEID"],
                "current_title": title,
                "started": started,
                "seconds": seconds,
                "course": classify(started),
            }
        )
    return result, ignored


def load_folders(path: Path) -> dict[str, list[int]]:
    folders = defaultdict(list)
    with ro_connect(path) as db:
        for row in db.execute("SELECT Z_PK, ZENCRYPTEDNAME FROM ZFOLDER"):
            folders[normalized_title(row["ZENCRYPTEDNAME"])].append(int(row["Z_PK"]))
    return dict(folders)


def load_visible(raw: str) -> tuple[int, list[dict]]:
    payload = json.load(sys.stdin) if raw == "-" else json.loads(raw)
    rows = payload["rows"]
    total = int(payload["total_count"])
    visible = []
    for row in rows:
        title = row.get("title") or row.get("description") or ""
        duration = row.get("duration_seconds", row.get("duration"))
        visible.append(
            {
                "title": title,
                "normalized_title": normalized_title(title),
                "seconds": parse_duration(duration),
            }
        )
    return total, visible


def base_title(row: dict) -> str:
    started = row["started"]
    return f"{started.month}월 {started.day}일 {row['course']}"


def existing_title_slot(current: str, base: str, count: int) -> int | None:
    # Normalize legacy date/part separators for slot matching; assignment writes canonical_title.
    current_norm, base_norm = canonical_title(current), normalized_title(base)
    current_norm, base_norm = current_norm.replace(" ", ""), base_norm.replace(" ", "")
    if current_norm == base_norm:
        return 1
    match = re.fullmatch(re.escape(base_norm) + r"(\d+)-(\d+)", current_norm)
    if not match:
        return None
    position, total = map(int, match.groups())
    if count == 1 and 1 <= position <= total:
        return 1  # Do not renumber an already named historical fragment.
    return position if total == count and 1 <= position <= count else None


def assign_titles(group: list[dict]) -> None:
    count = len(group)
    used: set[int] = set()
    for row in group:
        base = base_title(row)
        slot = existing_title_slot(row["current_title"], base, count)
        if slot is not None and slot not in used:
            row["desired_title"] = canonical_title(row["current_title"])
            used.add(slot)
        else:
            row["desired_title"] = None
    for chronological_index, row in enumerate(group, 1):
        if row["desired_title"] is not None:
            continue
        base = base_title(row)
        if count == 1:
            row["desired_title"] = base
            continue
        position = chronological_index
        if position in used:
            position = next(number for number in range(1, count + 1) if number not in used)
        used.add(position)
        row["desired_title"] = f"{base} {position}-{count}"


def serialize_row(row: dict) -> dict:
    return {
        "z_pk": row["z_pk"],
        "unique_id": row["unique_id"],
        "started_local": row["started"].isoformat(timespec="seconds"),
        "course": row["course"],
        "folder_pk": row["folder_pk"],
        "current_title": row["current_title"],
        "desired_title": row.get("desired_title"),
        "duration_seconds": row["seconds"],
    }


def build_plan(voice_db: Path, today: date, visible_raw: str) -> dict:
    if visible_raw == "-":
        visible_raw = sys.stdin.read()
    total_count, visible = load_visible(visible_raw)
    if total_count != len(visible):
        return {"status": "fatal", "reason": "incomplete_all_recordings_ax", "expected_rows": total_count, "received_rows": len(visible), "actions": []}

    voice_rows, ignored_keys = load_voice_rows(voice_db, today)
    voice_by_key: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in voice_rows:
        voice_by_key[(normalized_title(row["current_title"]), row["seconds"])].append(row)
    visible_counts = Counter((row["normalized_title"], row["seconds"]) for row in visible)

    verified, reports = [], []
    for item in visible:
        key = (item["normalized_title"], item["seconds"])
        if key in ignored_keys:
            continue
        matches = voice_by_key.get(key, [])
        if len(matches) == 1 and visible_counts[key] == 1:
            row = dict(matches[0])
            verified.append(row)
            continue
        reports.append({"type": "list_unverified", "title": item["title"], "duration_seconds": item["seconds"]})

    if reports:
        return {"status": "fatal", "reason": "voice_memos_identity_mismatch", "actions": [], "reports": reports}

    timetable_rows = []
    for row in verified:
        if row["course"] is None:
            reports.append({"type": "timetable_mismatch", **serialize_row(row)})
        else:
            timetable_rows.append(row)

    grouped: dict[tuple[date, str], list[dict]] = defaultdict(list)
    for row in timetable_rows:
        grouped[(row["started"].date(), row["course"])].append(row)
    for group in grouped.values():
        group.sort(key=lambda row: row["started"])
        assign_titles(group)

    folders = load_folders(voice_db)
    payload = json.loads(visible_raw)
    visible_folders = Counter(normalized_title(name) for name in payload["folders"])
    actions, completed = [], 0
    for row in sorted(timetable_rows, key=lambda item: item["started"]):
        course = row["course"]
        ids = folders.get(course, [])
        if len(ids) != 1 or visible_folders[course] != 1:
            reports.append({"type": "folder_missing_or_duplicate", "course": course})
            continue
        rename = normalized_title(row["current_title"]) != normalized_title(row["desired_title"])
        move = row["folder_pk"] != ids[0]
        if rename or move:
            actions.append({
                **serialize_row(row),
                "rename_to": row["desired_title"] if rename else None,
                "move_to": course if move else None,
                "desired_folder_pk": ids[0],
            })
        else:
            completed += 1

    return {
        "status": "ok",
        "all_recordings_count": total_count,
        "verified_candidates": len(verified),
        "actions": actions,
        "reports": reports,
        "summary": {
            "rename": sum(bool(action["rename_to"]) for action in actions),
            "move": sum(bool(action["move_to"]) for action in actions),
            "completed": completed,
            "reports": len(reports),
        },
    }


def verify(voice_db: Path, raw: str) -> dict:
    """Check the same recording identities after UI edits."""
    expected = json.loads(sys.stdin.read() if raw == "-" else raw)
    failures = []
    with ro_connect(voice_db) as db:
        for item in expected:
            row = db.execute(
                "SELECT ZCUSTOMLABELFORSORTING, ZFOLDER, ZDURATION, ZUNIQUEID FROM ZCLOUDRECORDING WHERE Z_PK=?",
                (item["z_pk"],),
            ).fetchone()
            if (row is None
                or row["ZUNIQUEID"] != item["unique_id"]
                or normalized_title(row["ZCUSTOMLABELFORSORTING"]) != normalized_title(item["title"])
                or row["ZFOLDER"] != item["folder_pk"]
                or rounded_seconds(row["ZDURATION"] or 0) != item["duration_seconds"]):
                failures.append(item["z_pk"])
    return {"status": "ok" if not failures else "failed", "checked": len(expected), "failed_ids": failures}


def fingerprint(voice_db: Path, today: date) -> str:
    # Include every persisted recording/folder field, including deletion flags.
    # Do not interpret undocumented flags or store private row contents in the cache.
    with ro_connect(voice_db) as db:
        db.execute("BEGIN")
        state = {
            table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY Z_PK")]
            for table in ("ZCLOUDRECORDING", "ZFOLDER")
        }
    eligible, _ = voice_candidates(state["ZCLOUDRECORDING"], today)
    state["eligible_ids"] = [row["z_pk"] for row in eligible]
    state["database"] = str(voice_db.resolve())
    state["policy"] = [START_DATE.isoformat(), END_DATE.isoformat(), SCHEDULE]
    state["code"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    encoded = json.dumps(state, ensure_ascii=False, default=lambda value: value.hex()).encode()
    return hashlib.sha256(encoded).hexdigest()


def check_cache(voice_db: Path, today: date, cache: Path) -> dict:
    try:
        saved = json.loads(cache.read_text())
    except (OSError, ValueError):
        return {"status": "needs_ui", "reason": "no_verified_snapshot"}
    if not isinstance(saved, dict):
        return {"status": "needs_ui", "reason": "invalid_verified_snapshot"}
    if saved.get("fingerprint") != fingerprint(voice_db, today):
        return {"status": "needs_ui", "reason": "recordings_folders_or_rules_changed"}
    return {"status": "no_changes", "basis": "unchanged_ui_verified_snapshot", "actions": []}


def remember(voice_db: Path, today: date, raw: str, cache: Path) -> dict:
    if raw == "-":
        raw = sys.stdin.read()
    before = fingerprint(voice_db, today)
    result = build_plan(voice_db, today, raw)
    if result["status"] != "ok" or result["actions"] or result["reports"]:
        return {"status": "not_saved", "reason": "not_fully_verified_complete"}
    if fingerprint(voice_db, today) != before:
        return {"status": "not_saved", "reason": "database_changed_during_verification"}
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement: interruption cannot leave a partial success record.
    with tempfile.NamedTemporaryFile(mode="w", dir=cache.parent, delete=False) as handle:
        json.dump({"fingerprint": before}, handle)
        temp_path = handle.name
    os.replace(temp_path, cache)
    return {"status": "ok", "verified_candidates": result["verified_candidates"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--visible-json", help="Complete UI rows, total_count and folder names; - for stdin")
    mode.add_argument("--verify-json", help="Expected z_pk, unique_id, title, folder_pk, duration_seconds; - for stdin")
    mode.add_argument("--check", action="store_true", help="Skip UI only when the last fully verified DB snapshot is unchanged")
    mode.add_argument("--remember-json", help="Fresh complete UI inventory after all work; save only if no actions/reports remain")
    parser.add_argument("--voice-db", type=Path, default=VOICE_DB)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--today", type=date.fromisoformat, default=datetime.now(SEOUL).date())
    args = parser.parse_args()
    try:
        if args.check:
            result = check_cache(args.voice_db, args.today, args.cache)
        elif args.remember_json is not None:
            result = remember(args.voice_db, args.today, args.remember_json, args.cache)
        elif args.verify_json is not None:
            result = verify(args.voice_db, args.verify_json)
        else:
            result = build_plan(args.voice_db, args.today, args.visible_json)
        emit(result, 0 if result["status"] in ("ok", "no_changes", "needs_ui") else 2)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError, json.JSONDecodeError) as error:
        emit({"status": "fatal", "reason": type(error).__name__, "detail": str(error), "actions": []}, 2)


if __name__ == "__main__":
    main()
