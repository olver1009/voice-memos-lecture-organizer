from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import unicodedata
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import plan


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "voice.sqlite"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE ZFOLDER (Z_PK INTEGER PRIMARY KEY, ZENCRYPTEDNAME TEXT);
            CREATE TABLE ZCLOUDRECORDING (
                Z_PK INTEGER PRIMARY KEY, ZUNIQUEID TEXT, ZCUSTOMLABELFORSORTING TEXT,
                ZPATH TEXT, ZDATE REAL, ZDURATION REAL, ZFOLDER INTEGER);
        """)
        self.folders = ["미주지역지리", "지도학및실습", "도시지리학특강",
                        "응용 지형학", "기후변화와 미래환경"]
        self.db.executemany("INSERT INTO ZFOLDER VALUES (?,?)", enumerate(self.folders, 1))
        self.rows = []

    def add(self, title="새로운 녹음", started="2026-09-22T15:02:00+09:00",
            duration=4000, folder=None, visible=True):
        pk = self.db.execute("SELECT COUNT(*)+1 FROM ZCLOUDRECORDING").fetchone()[0]
        timestamp = datetime.fromisoformat(started).timestamp() - plan.APPLE_UNIX_OFFSET
        self.db.execute("INSERT INTO ZCLOUDRECORDING VALUES (?,?,?,?,?,?,?)",
                        (pk, f"id-{pk}", title, f"{pk}.m4a", timestamp, duration, folder))
        self.db.commit()
        if visible:
            self.rows.append({"title": title, "duration_seconds": plan.rounded_seconds(duration)})
        return pk

    def run_plan(self, **overrides):
        payload = {"total_count": len(self.rows), "rows": self.rows, "folders": self.folders}
        payload.update(overrides)
        return plan.build_plan(self.path, date(2026, 10, 3), json.dumps(payload))

    def test_rename_and_move_together(self):
        self.add()
        action = self.run_plan()["actions"][0]
        self.assertEqual(action["rename_to"], "9월 22일 응용 지형학")
        self.assertEqual(action["move_to"], "응용 지형학")
        self.assertEqual(action["desired_folder_pk"], 4)

    def test_completed_and_legacy_names_are_noops(self):
        for title in ["9/22 응용지형학", "9월 22일 응용 지형학", "9/22 응용 지형학 2/2"]:
            with self.subTest(title=title):
                self.db.execute("DELETE FROM ZCLOUDRECORDING")
                self.rows.clear()
                self.add(unicodedata.normalize("NFD", title), folder=4)
                self.assertEqual(self.run_plan()["actions"], [])

    def test_move_only_and_rename_only(self):
        self.add("9월 22일 응용 지형학", folder=1)
        self.assertIsNone(self.run_plan()["actions"][0]["rename_to"])
        self.assertEqual(self.run_plan()["actions"][0]["move_to"], "응용 지형학")
        self.db.execute("UPDATE ZCLOUDRECORDING SET ZFOLDER=4, ZCUSTOMLABELFORSORTING='새로운 녹음'")
        self.db.commit()
        self.rows[0]["title"] = "새로운 녹음"
        action = self.run_plan()["actions"][0]
        self.assertIsNotNone(action["rename_to"])
        self.assertIsNone(action["move_to"])

    def test_rerun_after_apply_has_zero_actions(self):
        self.add()
        action = self.run_plan()["actions"][0]
        self.db.execute("UPDATE ZCLOUDRECORDING SET ZCUSTOMLABELFORSORTING=?,ZFOLDER=? WHERE Z_PK=?",
                        (action["desired_title"], action["desired_folder_pk"], action["z_pk"]))
        self.db.commit()
        self.rows[0]["title"] = action["desired_title"]
        self.assertEqual(self.run_plan()["actions"], [])

    def test_partial_list_and_identity_mismatch_stop_all_edits(self):
        self.add()
        self.assertEqual(self.run_plan(total_count=2)["status"], "fatal")
        self.rows[0]["title"] = "wrong title"
        result = self.run_plan()
        self.assertEqual(result["status"], "fatal")
        self.assertEqual(result["actions"], [])

    def test_missing_or_duplicate_folder_skips(self):
        self.add()
        self.assertEqual(self.run_plan(folders=self.folders[:3])["actions"], [])
        self.db.execute("INSERT INTO ZFOLDER VALUES (6,'응용 지형학')")
        self.db.commit()
        self.assertEqual(self.run_plan()["actions"], [])

    def test_deleted_db_rows_do_not_enter_split_group(self):
        self.add("deleted fragment", visible=False)
        self.add("visible fragment", started="2026-09-22T15:20:00+09:00")
        action = self.run_plan()["actions"][0]
        self.assertEqual(action["desired_title"], "9월 22일 응용 지형학")

    def test_excludes_short_outside_hours_and_semester(self):
        self.add(duration=600)
        self.add("evening", started="2026-09-22T18:00:00+09:00")
        self.add("old", started="2026-08-31T09:00:00+09:00")
        self.assertEqual(self.run_plan()["actions"], [])

    def test_timetable_and_early_boundary(self):
        for clock, expected in [("12:44", None), ("12:45", "지도학및실습"),
                                ("15:00", "도시지리학특강"), ("16:30", None)]:
            started = datetime.fromisoformat(f"2026-09-23T{clock}:00+09:00")
            self.assertEqual(plan.classify(started), expected)
        self.add(started="2026-09-25T15:00:00+09:00")
        self.assertEqual(self.run_plan()["reports"][0]["type"], "timetable_mismatch")

    def test_split_titles_are_unique_and_stable(self):
        self.add("first")
        self.add("second", started="2026-09-22T15:20:00+09:00", duration=2000)
        actions = self.run_plan()["actions"]
        self.assertEqual([a["desired_title"] for a in actions],
                         ["9월 22일 응용 지형학 1-2", "9월 22일 응용 지형학 2-2"])

    def test_verification_detects_partial_failure_or_changed_identity(self):
        pk = self.add(folder=4)
        expected = [{"z_pk": pk, "unique_id": f"id-{pk}", "title": "새로운 녹음",
                     "folder_pk": 4, "duration_seconds": 4000}]
        self.assertEqual(plan.verify(self.path, json.dumps(expected))["status"], "ok")
        for column, value in [("ZFOLDER", 3), ("ZCUSTOMLABELFORSORTING", "different"),
                              ("ZDURATION", 4002), ("ZUNIQUEID", "replaced")]:
            with self.subTest(column=column):
                self.db.execute(f"UPDATE ZCLOUDRECORDING SET {column}=? WHERE Z_PK=?", (value, pk))
                self.db.commit()
                self.assertEqual(plan.verify(self.path, json.dumps(expected))["status"], "failed")
                self.db.execute("UPDATE ZCLOUDRECORDING SET ZFOLDER=4,ZCUSTOMLABELFORSORTING='새로운 녹음',ZDURATION=4000,ZUNIQUEID=? WHERE Z_PK=?",
                                (f"id-{pk}", pk))
                self.db.commit()

    def test_duration_formats(self):
        for value in [4259, "1:10:59", "1시간, 10분, 59초"]:
            self.assertEqual(plan.parse_duration(value), 4259)

    def test_cache_requires_complete_ui_verification(self):
        cache = Path(self.tmp.name) / "state.json"
        self.add()
        payload = json.dumps({"total_count": 1, "folders": self.folders, "rows": self.rows})
        self.assertEqual(plan.check_cache(self.path, date(2026, 10, 3), cache)["status"], "needs_ui")
        self.assertEqual(plan.remember(self.path, date(2026, 10, 3), payload, cache)["status"], "not_saved")
        self.assertFalse(cache.exists())
        self.db.execute("UPDATE ZCLOUDRECORDING SET ZCUSTOMLABELFORSORTING='9월 22일 응용 지형학', ZFOLDER=4")
        self.db.commit()
        self.rows[0]["title"] = "9월 22일 응용 지형학"
        payload = json.dumps({"total_count": 1, "folders": self.folders, "rows": self.rows})
        self.assertEqual(plan.remember(self.path, date(2026, 10, 3), payload, cache)["status"], "ok")
        self.assertEqual(plan.check_cache(self.path, date(2026, 10, 3), cache)["status"], "no_changes")
        saved = json.loads(cache.read_text())
        self.assertEqual(list(saved), ["fingerprint"])

    def test_cache_invalidates_every_recording_and_folder_change(self):
        self.add("9월 22일 응용 지형학", folder=4)
        initial = plan.fingerprint(self.path, date(2026, 10, 3))
        for column, value in [("ZCUSTOMLABELFORSORTING", "changed"), ("ZFOLDER", 1),
                              ("ZDURATION", 4100), ("ZPATH", None),
                              ("ZUNIQUEID", "replacement"), ("ZDATE", 0)]:
            with self.subTest(column=column):
                previous = self.db.execute(f"SELECT {column} FROM ZCLOUDRECORDING").fetchone()[0]
                self.db.execute(f"UPDATE ZCLOUDRECORDING SET {column}=?", (value,))
                self.db.commit()
                try:
                    self.assertNotEqual(plan.fingerprint(self.path, date(2026, 10, 3)), initial)
                finally:
                    self.db.execute(f"UPDATE ZCLOUDRECORDING SET {column}=?", (previous,))
                    self.db.commit()
        self.db.execute("UPDATE ZFOLDER SET ZENCRYPTEDNAME='renamed folder' WHERE Z_PK=4")
        self.db.commit()
        self.assertNotEqual(plan.fingerprint(self.path, date(2026, 10, 3)), initial)

    def test_future_recording_eligibility_invalidates_cache(self):
        self.add("future", started="2026-10-05T09:00:00+09:00")
        self.assertNotEqual(plan.fingerprint(self.path, date(2026, 10, 3)),
                            plan.fingerprint(self.path, date(2026, 10, 5)))

    def test_cache_cannot_remember_partial_or_ambiguous_inventory(self):
        self.add("9월 22일 응용 지형학", folder=4)
        cache = Path(self.tmp.name) / "state.json"
        for payload in [
            {"total_count": 2, "folders": self.folders, "rows": self.rows},
            {"total_count": 1, "folders": [], "rows": self.rows},
        ]:
            self.assertEqual(plan.remember(self.path, date(2026, 10, 3),
                                          json.dumps(payload), cache)["status"], "not_saved")
            self.assertFalse(cache.exists())

    def test_corrupt_cache_falls_back_to_ui(self):
        cache = Path(self.tmp.name) / "state.json"
        for value in ["not-json", "[]", "null", "123", "{}"]:
            cache.write_text(value)
            self.assertEqual(plan.check_cache(self.path, date(2026, 10, 3), cache)["status"], "needs_ui")


if __name__ == "__main__":
    unittest.main()
