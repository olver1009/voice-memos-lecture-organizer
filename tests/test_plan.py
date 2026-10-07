from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from unittest.mock import patch
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

    def test_legacy_separators_are_normalized_and_standard_names_are_noops(self):
        cases = [
            ("9/22 응용지형학", "9월 22일 응용지형학"),
            ("9월 22일 응용 지형학", None),
            ("9/22 응용 지형학 2/2", "9월 22일 응용 지형학 2-2"),
            ("9월 22일 응용 지형학 1/2", "9월 22일 응용 지형학 1-2"),
        ]
        for title, expected in cases:
            with self.subTest(title=title):
                self.db.execute("DELETE FROM ZCLOUDRECORDING")
                self.rows.clear()
                self.add(unicodedata.normalize("NFD", title), folder=4)
                actions = self.run_plan()["actions"]
                if expected is None:
                    self.assertEqual(actions, [])
                else:
                    self.assertEqual(len(actions), 1)
                    self.assertEqual(actions[0]["rename_to"], expected)

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
        result = self.run_plan()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["reports"], [])

    def test_excluded_and_eligible_identity_collisions_stop_edits_and_cache(self):
        cases = [
            ("2026-08-31T09:00:00+09:00", 4000, 4000),
            ("2026-09-22T18:00:00+09:00", 4000, 4000),
            ("2026-09-22T15:00:00+09:00", 600, 600.4),
        ]
        cache = Path(self.tmp.name) / "state.json"
        for started, excluded_duration, eligible_duration in cases:
            for excluded_visible in (False, True):
                with self.subTest(started=started, excluded_visible=excluded_visible):
                    self.db.execute("DELETE FROM ZCLOUDRECORDING")
                    self.rows.clear()
                    self.add(started=started, duration=excluded_duration,
                             visible=excluded_visible)
                    self.add(duration=eligible_duration)
                    result = self.run_plan()
                    self.assertEqual(result["status"], "fatal")
                    self.assertEqual(result["reason"], "voice_memos_identity_mismatch")
                    self.assertEqual(result["actions"], [])
                    payload = json.dumps({"total_count": len(self.rows),
                                          "folders": self.folders, "rows": self.rows})
                    self.assertEqual(plan.remember(self.path, date(2026, 10, 3),
                                                  payload, cache)["status"], "not_saved")
                    self.assertFalse(cache.exists())
                    self.assertEqual(plan.check_cache(self.path, date(2026, 10, 3),
                                                     cache)["status"], "needs_ui")

    def test_duplicate_excluded_identities_stop_edits_and_cache(self):
        cache = Path(self.tmp.name) / "state.json"
        for first_visible in (False, True):
            with self.subTest(first_visible=first_visible):
                self.db.execute("DELETE FROM ZCLOUDRECORDING")
                self.rows.clear()
                self.add(duration=600, visible=first_visible)
                self.add(duration=600)
                self.assertEqual(self.run_plan()["status"], "fatal")
                payload = json.dumps({"total_count": len(self.rows),
                                      "folders": self.folders, "rows": self.rows})
                self.assertEqual(plan.remember(self.path, date(2026, 10, 3),
                                              payload, cache)["status"], "not_saved")
                self.assertFalse(cache.exists())

    def test_timetable_and_early_boundary(self):
        for clock, expected in [("12:44", None), ("12:45", "지도학및실습"),
                                ("15:00", "도시지리학특강"), ("16:30", None)]:
            started = datetime.fromisoformat(f"2026-09-23T{clock}:00+09:00")
            self.assertEqual(plan.classify(started), expected)
        self.add(started="2026-09-25T15:00:00+09:00")
        self.assertEqual(self.run_plan()["reports"][0]["type"], "timetable_mismatch")

    def test_adjacent_classes_use_recording_overlap_not_only_start(self):
        cases = [
            ("2026-10-07T14:59:21+09:00", 4602, "도시지리학특강"),
            ("2026-10-07T14:45:00+09:00", 1000, "지도학및실습"),
            ("2026-10-07T14:45:00+09:00", 4500, "도시지리학특강"),
            ("2026-10-06T16:29:00+09:00", 4000, "기후변화와 미래환경"),
            ("2026-10-08T16:20:00+09:00", 601, "응용 지형학"),
            ("2026-10-07T14:45:00+09:00", 1800, None),
            ("2026-10-07T14:59:21+09:00", None, None),
        ]
        for started, seconds, expected in cases:
            with self.subTest(started=started, seconds=seconds):
                self.assertEqual(plan.classify(datetime.fromisoformat(started), seconds), expected)

    def test_real_oct7_misclassification_is_repaired_without_false_fragments(self):
        self.add("10월 7일 미주지역지리", started="2026-10-07T10:31:12+09:00", duration=4497, folder=1)
        map_pk = self.add("10월 7일 지도학및실습 1-2", started="2026-10-07T13:00:57+09:00", duration=5378, folder=2)
        city_pk = self.add("10월 7일 지도학및실습 2-2", started="2026-10-07T14:59:21+09:00", duration=4602, folder=2)
        result = plan.build_plan(self.path, date(2026, 10, 7), self.inventory())
        self.assertEqual(result["reports"], [])
        self.assertEqual([(a["z_pk"], a["desired_title"], a["desired_folder_pk"]) for a in result["actions"]],
                         [(map_pk, "10월 7일 지도학및실습", 2), (city_pk, "10월 7일 도시지리학특강", 3)])
        self.assertEqual(result["actions"][0]["move_to"], None)
        for action in result["actions"]:
            self.db.execute("UPDATE ZCLOUDRECORDING SET ZCUSTOMLABELFORSORTING=?,ZFOLDER=? WHERE Z_PK=?",
                            (action["desired_title"], action["desired_folder_pk"], action["z_pk"]))
            self.rows[action["z_pk"]-1]["title"] = action["desired_title"]
        self.db.commit()
        self.assertEqual(plan.build_plan(self.path, date(2026, 10, 7), self.inventory())["actions"], [])

    def test_tomorrow_new_recordings_after_today_cache_are_classified_and_saved(self):
        self.add("10월 7일 지도학및실습", started="2026-10-07T13:00:57+09:00", duration=5378, folder=2)
        self.add("10월 7일 도시지리학특강", started="2026-10-07T14:59:21+09:00", duration=4602, folder=3)
        cache = Path(self.tmp.name) / "cache.json"
        today, tomorrow = date(2026, 10, 7), date(2026, 10, 8)
        saved = plan.plan_inventory(self.path, today, self.inventory(), cache, plan.fingerprint(self.path, today))
        self.assertTrue(saved["cache_saved"])
        self.add("새로운 녹음", started="2026-10-08T14:59:55+09:00", duration=4400)
        self.add("새로운 녹음 2", started="2026-10-08T16:29:10+09:00", duration=4300)
        checked = plan.check_cache(self.path, tomorrow, cache)
        self.assertEqual(checked["status"], "needs_ui")
        result = plan.plan_inventory(self.path, tomorrow, self.inventory(), cache, checked["snapshot_fingerprint"])
        self.assertEqual(result["reports"], [])
        self.assertEqual([a["desired_title"] for a in result["actions"]],
                         ["10월 8일 응용 지형학", "10월 8일 기후변화와 미래환경"])
        for action in result["actions"]:
            self.db.execute("UPDATE ZCLOUDRECORDING SET ZCUSTOMLABELFORSORTING=?,ZFOLDER=? WHERE Z_PK=?",
                            (action["desired_title"], action["desired_folder_pk"], action["z_pk"]))
            self.rows[action["z_pk"]-1]["title"] = action["desired_title"]
        self.db.commit()
        saved = plan.plan_inventory(self.path, tomorrow, self.inventory(), cache, plan.fingerprint(self.path, tomorrow))
        self.assertTrue(saved["cache_saved"])
        self.assertEqual(plan.check_cache(self.path, tomorrow, cache)["status"], "no_changes")

    def test_same_course_real_fragments_still_receive_part_numbers(self):
        self.add("first", started="2026-10-07T13:00:00+09:00", duration=2000)
        self.add("second", started="2026-10-07T13:40:00+09:00", duration=1800)
        result = plan.build_plan(self.path, date(2026, 10, 7), self.inventory())
        self.assertEqual([a["desired_title"] for a in result["actions"]],
                         ["10월 7일 지도학및실습 1-2", "10월 7일 지도학및실습 2-2"])

    def test_today_single_standard_title_keeps_its_spacing(self):
        self.add("10월 8일 응용지형학", started="2026-10-08T15:01:00+09:00", folder=4)
        self.assertEqual(plan.build_plan(self.path, date(2026, 10, 8), self.inventory())["actions"], [])

    def test_default_cache_is_in_temp_and_independent_of_working_directory(self):
        import os
        from tempfile import gettempdir
        self.assertTrue(plan.CACHE.is_relative_to(Path(gettempdir())))
        outputs=[]
        for cwd in (self.tmp.name, str(Path(plan.__file__).parent)):
            command=[sys.executable, "-B", "-c", "import plan; print(plan.CACHE)"]
            outputs.append(subprocess.check_output(command, cwd=cwd, text=True,
                env={**os.environ, "PYTHONPATH":str(Path(plan.__file__).parent)}).strip())
        self.assertEqual(outputs, [str(plan.CACHE)]*2)

    def test_split_titles_are_unique_and_stable(self):
        self.add("first")
        self.add("second", started="2026-09-22T15:20:00+09:00", duration=2000)
        actions = self.run_plan()["actions"]
        self.assertEqual([a["desired_title"] for a in actions],
                         ["9월 22일 응용 지형학 1-2", "9월 22일 응용 지형학 2-2"])

    def test_three_initial_fragments_receive_stable_three_part_titles(self):
        self.add("first")
        self.add("second", started="2026-09-22T15:20:00+09:00", duration=2000)
        self.add("third", started="2026-09-22T15:40:00+09:00", duration=1000)
        actions = self.run_plan()["actions"]
        self.assertEqual([a["desired_title"] for a in actions],
                         [f"9월 22일 응용 지형학 {n}-3" for n in (1, 2, 3)])
        for action, visible_row in zip(actions, self.rows):
            self.db.execute("UPDATE ZCLOUDRECORDING SET ZCUSTOMLABELFORSORTING=?,ZFOLDER=? WHERE Z_PK=?",
                            (action["desired_title"], action["desired_folder_pk"], action["z_pk"]))
            visible_row["title"] = action["desired_title"]
        self.db.commit()
        self.assertEqual(self.run_plan()["actions"], [])

    def test_cache_remembers_an_initially_completed_inventory(self):
        self.add("9월 22일 응용 지형학", folder=4)
        cache = Path(self.tmp.name) / "state.json"
        result = self.run_plan()
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["reports"], [])
        payload = json.dumps({"total_count": len(self.rows),
                              "folders": self.folders, "rows": self.rows})
        self.assertEqual(plan.remember(self.path, date(2026, 10, 3), payload, cache)["status"], "ok")
        self.assertEqual(plan.check_cache(self.path, date(2026, 10, 3), cache)["status"], "no_changes")

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

    def inventory(self):
        return json.dumps({"total_count": len(self.rows), "rows": self.rows, "folders": self.folders})

    def test_unseen_eligible_recording_never_enters_completed_cache(self):
        self.add("9월 22일 응용 지형학", folder=4)
        missing = self.add("sync pending", started="2026-10-06T15:02:00+09:00", visible=False)
        cache = Path(self.tmp.name) / "cache.json"
        today = date(2026, 10, 6)
        result = plan.plan_inventory(self.path, today, self.inventory(), cache, plan.fingerprint(self.path, today))
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["reports"][0]["type"], "db_candidate_not_visible")
        self.assertEqual(result["reports"][0]["z_pk"], missing)
        self.assertFalse(cache.exists())
        self.assertEqual(plan.remember(self.path, today, self.inventory(), cache)["status"], "not_saved")
        self.assertEqual(plan.check_cache(self.path, today, cache)["status"], "needs_ui")
        # Merely becoming visible must lead to work, even with unchanged DB.
        self.rows.append({"title": "sync pending", "duration_seconds": 4000})
        result = plan.plan_inventory(self.path, today, self.inventory(), cache, plan.fingerprint(self.path, today))
        self.assertEqual([a["z_pk"] for a in result["actions"]], [missing])

    def test_file_plan_preflight_and_saved_identity_verification(self):
        self.add()
        today = date(2026, 10, 6)
        result = {**self.run_plan(), "snapshot_fingerprint": plan.fingerprint(self.path, today)}
        self.assertEqual(plan.verify_plan(self.path, today, result, before=True)["status"], "ok")
        self.assertEqual(plan.verify_plan(self.path, today, result)["status"], "failed")
        action = result["actions"][0]
        self.db.execute("UPDATE ZCLOUDRECORDING SET ZCUSTOMLABELFORSORTING=?,ZFOLDER=? WHERE Z_PK=?",
                        (action["desired_title"], action["desired_folder_pk"], action["z_pk"]))
        self.db.commit()
        self.assertEqual(plan.verify_plan(self.path, today, result)["status"], "ok")
        self.assertEqual(plan.verify_plan(self.path, today, result, before=True)["reason"], "database_changed_before_apply")

    def test_playback_and_bookkeeping_changes_keep_verified_cache(self):
        self.add("9월 22일 응용 지형학", folder=4)
        for table, columns in plan.IGNORED_CACHE_FIELDS.items():
            for column in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} REAL")
        self.db.commit()
        cache = Path(self.tmp.name) / "cache.json"
        today = date(2026, 10, 3)
        self.assertEqual(plan.remember(self.path, today, self.inventory(), cache)["status"], "ok")
        for table, columns in plan.IGNORED_CACHE_FIELDS.items():
            for column in columns:
                with self.subTest(table=table, column=column):
                    self.db.execute(f"UPDATE {table} SET {column}=123")
                    self.db.commit()
                    self.assertEqual(plan.check_cache(self.path, today, cache)["status"], "no_changes")

    def test_deletion_flags_and_unknown_fields_still_invalidate(self):
        self.add("9월 22일 응용 지형학", folder=4)
        for column in ("ZFLAGS", "ZSHAREDFLAGS", "ZNEWSTATE"):
            self.db.execute(f"ALTER TABLE ZCLOUDRECORDING ADD COLUMN {column} INTEGER DEFAULT 0")
        self.db.commit()
        cache = Path(self.tmp.name) / "cache.json"
        today = date(2026, 10, 3)
        for column in ("ZFLAGS", "ZSHAREDFLAGS", "ZNEWSTATE"):
            plan.remember(self.path, today, self.inventory(), cache)
            self.db.execute(f"UPDATE ZCLOUDRECORDING SET {column}=1")
            self.db.commit()
            self.assertEqual(plan.check_cache(self.path, today, cache)["status"], "needs_ui")

    def test_new_daily_and_late_synced_recordings_are_not_skipped(self):
        self.add("9월 22일 응용 지형학", folder=4)
        cache = Path(self.tmp.name) / "cache.json"
        today = date(2026, 10, 6)
        plan.remember(self.path, today, self.inventory(), cache)
        self.add("새로운 녹음", started="2026-10-06T15:02:00+09:00")
        self.add("늦게 동기화된 녹음", started="2026-10-05T09:02:00+09:00", duration=4200)
        checked = plan.check_cache(self.path, today, cache)
        self.assertEqual(checked["status"], "needs_ui")
        result = plan.plan_inventory(self.path, today, self.inventory(), cache, checked["snapshot_fingerprint"])
        self.assertEqual({a["desired_title"] for a in result["actions"]},
                         {"10월 6일 응용 지형학", "10월 5일 미주지역지리"})
        self.assertNotIn("cache_saved", result)
        self.assertEqual(plan.check_cache(self.path, today, cache)["status"], "needs_ui")
        for action in result["actions"]:
            self.db.execute("UPDATE ZCLOUDRECORDING SET ZCUSTOMLABELFORSORTING=?,ZFOLDER=? WHERE Z_PK=?",
                            (action["desired_title"], action["desired_folder_pk"], action["z_pk"]))
            self.rows[action["z_pk"]-1]["title"] = action["desired_title"]
        self.db.commit()
        checked = plan.check_cache(self.path, today, cache)
        result = plan.plan_inventory(self.path, today, self.inventory(), cache, checked["snapshot_fingerprint"])
        self.assertEqual(result["actions"], [])
        self.assertTrue(result["cache_saved"])
        self.assertEqual(plan.check_cache(self.path, today, cache)["status"], "no_changes")

    def test_next_day_without_new_recordings_keeps_cache(self):
        self.add("9월 22일 응용 지형학", folder=4)
        cache = Path(self.tmp.name) / "cache.json"
        plan.remember(self.path, date(2026, 10, 5), self.inventory(), cache)
        self.assertEqual(plan.check_cache(self.path, date(2026, 10, 6), cache)["status"], "no_changes")

    def test_change_during_collection_stops_plan_and_cache(self):
        self.add("9월 22일 응용 지형학", folder=4)
        cache = Path(self.tmp.name) / "cache.json"
        today = date(2026, 10, 6)
        checked = plan.check_cache(self.path, today, cache)
        collected = self.inventory()
        self.add(started="2026-10-06T15:02:00+09:00")
        result = plan.plan_inventory(self.path, today, collected, cache, checked["snapshot_fingerprint"])
        self.assertEqual(result["status"], "fatal")
        self.assertEqual(result["actions"], [])
        self.assertFalse(cache.exists())

    def test_change_during_verification_stops_plan_and_cache(self):
        self.add("9월 22일 응용 지형학", folder=4)
        cache = Path(self.tmp.name) / "cache.json"
        today = date(2026, 10, 6)
        token = plan.fingerprint(self.path, today)
        original = plan.build_plan
        def interrupted(*args):
            result = original(*args)
            self.db.execute("UPDATE ZCLOUDRECORDING SET ZFOLDER=1")
            self.db.commit()
            return result
        with patch.object(plan, "build_plan", side_effect=interrupted):
            result = plan.plan_inventory(self.path, today, self.inventory(), cache, token)
        self.assertEqual(result["status"], "fatal")
        self.assertEqual(result["actions"], [])
        self.assertFalse(cache.exists())

    def test_cache_save_failure_preserves_verified_noop(self):
        self.add("9월 22일 응용 지형학", folder=4)
        cache = Path(self.tmp.name) / "cache.json"
        today = date(2026, 10, 6)
        with patch.object(plan, "save_snapshot", side_effect=OSError("read-only cache directory")):
            result = plan.plan_inventory(self.path, today, self.inventory(), cache, plan.fingerprint(self.path, today))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["actions"], [])
        self.assertFalse(result["cache_saved"])
        self.assertFalse(cache.exists())

    def test_bad_inventory_cannot_save_cache_in_one_pass(self):
        self.add("9월 22일 응용 지형학", folder=4)
        cache = Path(self.tmp.name) / "cache.json"
        today = date(2026, 10, 6)
        token = plan.fingerprint(self.path, today)
        for payload in [
            {"total_count": 2, "folders": self.folders, "rows": self.rows},
            {"total_count": 1, "folders": [], "rows": self.rows},
        ]:
            result = plan.plan_inventory(self.path, today, json.dumps(payload), cache, token)
            self.assertFalse(result.get("cache_saved", False))
            self.assertFalse(cache.exists())


    def test_inventory_file_cli_requires_token_and_saves_verified_noop(self):
        self.add("9월 22일 응용 지형학", folder=4)
        cache = Path(self.tmp.name) / "cache.json"
        inventory = Path(self.tmp.name) / "inventory.json"
        inventory.write_text(self.inventory())
        command = [sys.executable, "-B", str(Path(plan.__file__)), "--voice-db", str(self.path),
                   "--cache", str(cache), "--today", "2026-10-06"]
        checked = subprocess.run(command + ["--check"], capture_output=True, text=True, check=True)
        token = json.loads(checked.stdout)["snapshot_fingerprint"]
        rejected = subprocess.run(command + ["--inventory-file", str(inventory)], capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 2)
        self.assertFalse(cache.exists())
        accepted = subprocess.run(command + ["--inventory-file", str(inventory), "--expected-fingerprint", token],
                                  capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(accepted.stdout)["cache_saved"])
        checked = subprocess.run(command + ["--check"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(checked.stdout)["status"], "no_changes")

    def test_plan_file_cli_passes_actions_without_reprinting_and_verifies_saved_changes(self):
        self.add()
        directory = Path(self.tmp.name)
        cache, inventory, actions = [directory / name for name in ("cache.json", "inventory.json", "actions.json")]
        inventory.write_text(self.inventory())
        command = [sys.executable, "-B", str(Path(plan.__file__)), "--voice-db", str(self.path),
                   "--cache", str(cache), "--today", "2026-10-06"]
        checked = subprocess.run(command + ["--check"], capture_output=True, text=True, check=True)
        token = json.loads(checked.stdout)["snapshot_fingerprint"]
        result = subprocess.run(command + ["--inventory-file", str(inventory), "--plan-file", str(actions),
                                 "--expected-fingerprint", token], capture_output=True, text=True, check=True)
        self.assertNotIn("actions", json.loads(result.stdout))
        self.assertEqual(actions.stat().st_mode & 0o777, 0o600)
        self.assertFalse(cache.exists())
        subprocess.run(command + ["--validate-plan-file", str(actions)], capture_output=True, check=True)
        failed = subprocess.run(command + ["--verify-plan-file", str(actions)], capture_output=True)
        self.assertEqual(failed.returncode, 2)
        action = json.loads(actions.read_text())["actions"][0]
        self.db.execute("UPDATE ZCLOUDRECORDING SET ZCUSTOMLABELFORSORTING=?,ZFOLDER=? WHERE Z_PK=?",
                        (action["desired_title"], action["desired_folder_pk"], action["z_pk"]))
        self.db.commit()
        result = subprocess.run(command + ["--verify-plan-file", str(actions)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"status": "ok", "checked": 1, "failed_ids": []})
        stale = subprocess.run(command + ["--validate-plan-file", str(actions)], capture_output=True)
        self.assertEqual(stale.returncode, 2)


if __name__ == "__main__":
    unittest.main()
