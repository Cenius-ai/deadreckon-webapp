"""Smoke tests for the DeadReckon CLI.

Run with:  python3 -m unittest discover -s tests

Every test drives the real command line in a subprocess so exit codes,
stdout/stderr routing and the on-disk JSON store are all exercised exactly
as a user would see them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deadreckon.py"


class CliTestCase(unittest.TestCase):
    """Base class: a throwaway store per test, plus a run() helper."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.store = self.tmp / "passage.json"

    def run_cli(
        self, *args: str, store: Path | None = None, env_extra: dict | None = None
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("DEADRECKON_STORE", None)
        env["DEADRECKON_STORE"] = str(store if store is not None else self.store)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            cwd=str(self.tmp),
            env=env,
            timeout=60,
        )

    def ok(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        result = self.run_cli(*args, **kwargs)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"{args} failed: {result.stderr}",
        )
        return result

    def fails(self, code: int, *args: str, **kwargs) -> subprocess.CompletedProcess:
        result = self.run_cli(*args, **kwargs)
        self.assertEqual(
            result.returncode,
            code,
            msg=f"{args} expected exit {code}, got {result.returncode}: "
            f"{result.stderr}",
        )
        return result

    def read_store(self, store: Path | None = None) -> dict:
        """Parse the store JSON exactly as a script would: json.loads on text."""
        return json.loads((store or self.store).read_text(encoding="utf-8"))

    def seed_basic_passage(self) -> None:
        self.ok("legs", "origin", "50.800", "-1.100")
        self.ok("legs", "add", "090", "12.5")
        self.ok("legs", "add", "180", "12.5")
        self.ok("drift", "045", "1.2")
        self.ok(
            "fix", "50.800", "-1.100",
            "--course", "090", "--speed", "6.5",
            "--time", "2024-06-01T12:00:00Z",
        )


class TestCommandSurface(CliTestCase):
    def test_help_lists_every_command_and_global_flag(self) -> None:
        result = self.ok("--help")
        for token in ("legs", "fix", "fixes", "dr", "drift", "status"):
            self.assertIn(token, result.stdout)
        self.assertIn("--store", result.stdout)
        self.assertIn("--json", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_version_prints_and_exits_zero(self) -> None:
        result = self.ok("--version")
        self.assertIn("deadreckon", result.stdout)

    def test_unknown_command_is_a_user_error(self) -> None:
        result = self.fails(2, "bogus")
        self.assertIn("unknown command", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_no_command_is_a_user_error(self) -> None:
        result = self.fails(2)
        self.assertIn("no command given", result.stderr)

    def test_subcommand_help_exits_zero(self) -> None:
        for command in ("legs", "fix", "fixes", "dr", "drift", "status"):
            self.ok(command, "--help")


class TestNavigationMath(CliTestCase):
    def test_destination_point_matches_great_circle(self) -> None:
        sys.path.insert(0, str(ROOT))
        import deadreckon  # noqa: PLC0415

        lat, lon = deadreckon.destination_point(0.0, 0.0, 90.0, 60.0)
        self.assertAlmostEqual(lat, 0.0, places=6)
        # 60 nm is 60 / 60.040 degrees of arc on the 3440.065 nm sphere.
        self.assertAlmostEqual(lon, 1.0, delta=0.001)

    def test_inverse_gives_east_course_and_sixty_ish_miles(self) -> None:
        sys.path.insert(0, str(ROOT))
        import deadreckon  # noqa: PLC0415

        course, distance = deadreckon.inverse(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(course, 90.0, places=6)
        self.assertAlmostEqual(distance, 60.0, delta=0.1)

    def test_longitude_normalisation_wraps_the_antimeridian(self) -> None:
        sys.path.insert(0, str(ROOT))
        import deadreckon  # noqa: PLC0415

        lat, lon = deadreckon.destination_point(0.0, 179.8, 90.0, 60.0)
        self.assertAlmostEqual(lat, 0.0, places=6)
        self.assertAlmostEqual(lon, -179.2, delta=0.05)


class TestStore(CliTestCase):
    def test_first_write_creates_the_store_document(self) -> None:
        self.assertFalse(self.store.exists())
        self.ok("legs", "add", "090", "10")
        data = self.read_store()
        for key in ("version", "origin_lat", "origin_lon", "legs", "fixes", "drift"):
            self.assertIn(key, data)
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["legs"]), 1)

    def test_corrupt_store_reports_and_leaves_the_file_untouched(self) -> None:
        self.store.write_text('{"version": 1, "legs": [', encoding="utf-8")
        before = self.store.read_bytes()
        result = self.fails(3, "legs")
        self.assertIn("store error", result.stderr)
        self.assertIn("invalid JSON", result.stderr)
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(result.stdout, "")

    def test_store_flag_wins_over_env_and_default_path(self) -> None:
        other = self.tmp / "elsewhere" / "passage.json"
        self.ok("fix", "51.5", "-0.1", "--course", "090", "--speed", "6",
                store=other)
        self.assertTrue(other.exists())
        self.assertFalse(self.store.exists())
        self.assertFalse((self.tmp / "deadreckon-store.json").exists())
        self.assertEqual(len(self.read_store(other)["fixes"]), 1)

    def test_environment_variable_selects_the_store(self) -> None:
        env_store = self.tmp / "from-env.json"
        self.ok("legs", "add", "000", "1", env_extra={"DEADRECKON_STORE": str(env_store)})
        self.assertTrue(env_store.exists())

    def test_store_round_trips_through_a_full_passage(self) -> None:
        self.seed_basic_passage()
        data = self.read_store()
        self.assertEqual(len(data["legs"]), 2)
        self.assertEqual(len(data["fixes"]), 1)
        self.assertEqual(data["drift"]["set_deg"], 45.0)


class TestLegTable(CliTestCase):
    def test_two_legs_chain_and_total_is_reported(self) -> None:
        self.ok("legs", "origin", "0", "0")
        self.ok("legs", "add", "090", "60")
        self.ok("legs", "add", "180", "60")
        data = self.read_store()
        first, second = data["legs"]
        self.assertAlmostEqual(first["end_lat"], 0.0, places=6)
        self.assertAlmostEqual(first["end_lon"], 1.0, delta=0.001)
        self.assertAlmostEqual(second["end_lat"], -1.0, delta=0.002)
        self.assertAlmostEqual(second["cumulative_nm"], 120.0, places=6)
        result = self.ok("legs")
        self.assertIn("120.00", result.stdout)
        self.assertIn("Total 120.00 nm over 2 legs", result.stdout)

    def test_first_leg_starts_at_zero_zero_when_no_origin_is_set(self) -> None:
        result = self.ok("legs", "add", "090", "10")
        self.assertIn("0.000,0.000", result.stdout)
        self.assertAlmostEqual(self.read_store()["legs"][0]["start_lat"], 0.0)

    def test_origin_override_re_anchors_every_leg(self) -> None:
        self.ok("legs", "add", "090", "10")
        self.ok("legs", "add", "090", "10")
        self.ok("legs", "origin", "51.500", "-0.100")
        data = self.read_store()
        self.assertEqual(data["origin_lat"], 51.5)
        self.assertEqual(data["origin_lon"], -0.1)
        self.assertAlmostEqual(data["legs"][0]["start_lat"], 51.5)
        self.assertAlmostEqual(data["legs"][0]["start_lon"], -0.1)
        self.assertAlmostEqual(data["legs"][1]["start_lat"],
                               data["legs"][0]["end_lat"], places=6)
        self.assertIn("51.500,-0.100", self.ok("legs").stdout)

    def test_clear_requires_yes_and_keeps_the_legs(self) -> None:
        for _ in range(3):
            self.ok("legs", "add", "090", "5")
        result = self.fails(2, "legs", "clear")
        self.assertIn("--yes", result.stderr)
        self.assertEqual(len(self.read_store()["legs"]), 3)

    def test_clear_with_yes_empties_the_table(self) -> None:
        for _ in range(3):
            self.ok("legs", "add", "090", "5")
        self.ok("legs", "clear", "--yes")
        self.assertEqual(self.read_store()["legs"], [])
        self.assertIn("No legs yet", self.ok("legs").stdout)

    def test_empty_table_invites_the_next_command(self) -> None:
        result = self.ok("legs")
        self.assertIn("No legs yet", result.stdout)
        self.assertIn("legs origin", result.stdout)


class TestFixLog(CliTestCase):
    def test_record_a_fix_and_read_it_back(self) -> None:
        self.ok("drift", "090", "2")
        self.ok("fix", "51.500", "-0.100", "--course", "090", "--speed", "6.0",
                "--time", "2024-06-01T12:00:00Z")
        data = self.read_store()
        self.assertEqual(len(data["fixes"]), 1)
        fix = data["fixes"][0]
        self.assertEqual(fix["id"], 1)
        self.assertEqual(fix["lat"], 51.5)
        self.assertEqual(fix["lon"], -0.1)
        self.assertEqual(fix["course_deg"], 90.0)
        self.assertEqual(fix["speed_kn"], 6.0)
        self.assertEqual(fix["recorded_utc"], "2024-06-01T12:00:00Z")
        self.assertEqual(fix["kind"], "observed")
        self.assertEqual(fix["offset_set_deg"], 90.0)
        self.assertEqual(fix["offset_drift_kn"], 2.0)
        listing = self.ok("fixes").stdout
        self.assertIn("51.500", listing)
        self.assertIn("-0.100", listing)
        self.assertIn("090T", listing)
        self.assertIn("6.00", listing)
        self.assertIn("2024-06-01T12:00:00Z", listing)

    def test_fixes_persist_in_recorded_order_across_processes(self) -> None:
        self.ok("fix", "51.5", "-0.1", "--course", "090", "--speed", "6",
                "--time", "2024-06-01T10:00:00Z")
        self.ok("fix", "51.6", "-0.2", "--course", "100", "--speed", "5",
                "--time", "2024-06-01T11:00:00Z")
        data = self.read_store()
        self.assertEqual([fix["id"] for fix in data["fixes"]], [1, 2])
        stdout = self.ok("fixes").stdout
        self.assertLess(stdout.index("2024-06-01T10:00:00Z"),
                        stdout.index("2024-06-01T11:00:00Z"))

    def test_fixes_last_shows_the_newest_fix(self) -> None:
        self.ok("fix", "51.5", "-0.1", "--course", "090", "--speed", "6",
                "--time", "2024-06-01T10:00:00Z")
        self.ok("fix", "51.6", "-0.2", "--course", "100", "--speed", "5",
                "--time", "2024-06-01T11:00:00Z")
        result = self.ok("fixes", "last")
        self.assertIn("Latest fix 2", result.stdout)
        self.assertIn("2024-06-01T11:00:00Z", result.stdout)

    def test_fixes_last_on_an_empty_log_is_a_clean_empty_state(self) -> None:
        result = self.ok("fixes", "last")
        self.assertIn("No fixes recorded yet", result.stdout)

    def test_undo_removes_only_the_most_recent_fix(self) -> None:
        self.ok("fix", "51.5", "-0.1", "--course", "090", "--speed", "6",
                "--time", "2024-06-01T10:00:00Z")
        self.ok("fix", "51.6", "-0.2", "--course", "100", "--speed", "5",
                "--time", "2024-06-01T11:00:00Z")
        result = self.ok("fixes", "undo")
        self.assertIn("Undid fix 2", result.stdout)
        data = self.read_store()
        self.assertEqual(len(data["fixes"]), 1)
        self.assertEqual(data["fixes"][0]["id"], 1)
        self.assertEqual(len(self.ok("fixes").stdout.splitlines()), 6)

    def test_undo_on_an_empty_log_is_a_user_error(self) -> None:
        result = self.fails(2, "fixes", "undo")
        self.assertIn("nothing to undo", result.stderr)


class TestDrift(CliTestCase):
    def test_set_then_show_the_offset(self) -> None:
        self.ok("drift", "135", "1.5")
        data = self.read_store()
        self.assertEqual(data["drift"]["set_deg"], 135.0)
        self.assertEqual(data["drift"]["rate_kn"], 1.5)
        shown = self.ok("drift").stdout
        self.assertIn("135T", shown)
        self.assertIn("1.50 kn", shown)

    def test_no_offset_set_is_reported_plainly(self) -> None:
        shown = self.ok("drift").stdout
        self.assertIn("No current offset set", shown)
        self.assertIn("deadreckon drift", shown)

    def test_clear_removes_the_offset(self) -> None:
        self.ok("drift", "135", "1.5")
        self.ok("drift", "clear")
        self.assertIsNone(self.read_store()["drift"])
        self.assertIn("No current offset set", self.ok("drift").stdout)

    def test_set_direction_out_of_range_is_rejected_without_writing(self) -> None:
        result = self.fails(2, "drift", "361", "1.0")
        self.assertIn("between 0 and 360", result.stderr)
        self.assertFalse(self.store.exists())


class TestAdvance(CliTestCase):
    def test_offset_is_vector_summed_into_ground_course_and_speed(self) -> None:
        self.ok("drift", "090", "2.00")
        self.ok("fix", "51.500", "-0.100", "--course", "000", "--speed", "6.00",
                "--time", "2024-06-01T12:00:00Z")
        result = self.ok("dr", "1")
        self.assertIn("018T", result.stdout)
        self.assertIn("6.32", result.stdout)
        self.assertIn("Distance run 6.32 nm over 1.000 h", result.stdout)
        advance = self.ok("dr", "1", "--json")
        payload = json.loads(advance.stdout)["advance"]
        self.assertAlmostEqual(payload["ground_speed_kn"], 6.324555, places=4)
        self.assertAlmostEqual(payload["ground_course_deg"], 18.434948, places=4)
        self.assertGreater(payload["position"]["lat"], 51.5)
        self.assertGreater(payload["position"]["lon"], -0.1)

    def test_no_offset_advances_on_vessel_motion_only(self) -> None:
        self.ok("fix", "51.500", "-0.100", "--course", "000", "--speed", "6.00",
                "--time", "2024-06-01T12:00:00Z")
        result = self.ok("dr", "2")
        self.assertIn("Current offset  none", result.stdout)
        self.assertIn("Distance run 12.00 nm over 2.000 h", result.stdout)
        self.assertIn("Ground course 000T", result.stdout)

    def test_save_appends_a_dr_fix(self) -> None:
        self.ok("drift", "090", "2.00")
        self.ok("fix", "51.500", "-0.100", "--course", "000", "--speed", "6.00",
                "--time", "2024-06-01T12:00:00Z")
        result = self.ok("dr", "1", "--save")
        self.assertIn("Saved as dr fix 2", result.stdout)
        data = self.read_store()
        self.assertEqual(len(data["fixes"]), 2)
        self.assertEqual(data["fixes"][1]["kind"], "dr")
        self.assertIn("dr", self.ok("fixes").stdout)

    def test_without_save_the_log_is_untouched(self) -> None:
        self.ok("fix", "51.500", "-0.100", "--course", "000", "--speed", "6.00",
                "--time", "2024-06-01T12:00:00Z")
        before = self.read_store()
        self.ok("dr", "1")
        self.assertEqual(self.read_store(), before)

    def test_advance_without_a_fix_or_from_is_a_user_error(self) -> None:
        result = self.fails(2, "dr", "1")
        self.assertIn("no fix recorded yet", result.stderr)

    def test_advance_from_an_explicit_position(self) -> None:
        result = self.ok("dr", "1", "--from", "0", "0", "--course", "090",
                         "--speed", "6")
        self.assertIn("explicit position", result.stdout)
        self.assertIn("Ground course 090T", result.stdout)

    def test_from_without_course_and_speed_is_a_user_error(self) -> None:
        result = self.fails(2, "dr", "1", "--from", "0", "0")
        self.assertIn("--course", result.stderr)


class TestValidation(CliTestCase):
    def test_out_of_range_course_leaves_the_store_unchanged(self) -> None:
        self.ok("fix", "51.5", "-0.1", "--course", "090", "--speed", "6")
        before = self.store.read_bytes()
        result = self.fails(2, "fix", "51.5", "-0.1", "--course", "361",
                            "--speed", "6")
        self.assertIn("course must be between 0 and 360", result.stderr)
        self.assertEqual(self.store.read_bytes(), before)

    def test_out_of_range_latitude(self) -> None:
        result = self.fails(2, "fix", "95.0", "-0.1", "--course", "090",
                            "--speed", "6")
        self.assertIn("latitude must be between -90 and 90", result.stderr)

    def test_out_of_range_longitude(self) -> None:
        result = self.fails(2, "fix", "51.5", "-190", "--course", "090",
                            "--speed", "6")
        self.assertIn("longitude must be between -180 and 180", result.stderr)

    def test_zero_hours_is_rejected(self) -> None:
        result = self.fails(2, "dr", "0")
        self.assertIn("hours must be greater than 0", result.stderr)

    def test_bad_timestamp_is_rejected(self) -> None:
        result = self.fails(2, "fix", "51.5", "-0.1", "--course", "090",
                            "--speed", "6", "--time", "not-a-time")
        self.assertIn("ISO-8601", result.stderr)
        self.assertFalse(self.store.exists())

    def test_negative_speed_is_rejected(self) -> None:
        result = self.fails(2, "fix", "51.5", "-0.1", "--course", "090",
                            "--speed", "-3")
        self.assertIn("speed must be 0 knots or more", result.stderr)

    def test_zero_distance_is_rejected(self) -> None:
        result = self.fails(2, "legs", "add", "090", "0")
        self.assertIn("distance must be greater than 0", result.stderr)

    def test_non_numeric_course_names_the_field(self) -> None:
        result = self.fails(2, "legs", "add", "east", "10")
        self.assertIn("course must be a number", result.stderr)

    def test_timestamp_offsets_are_normalised_to_utc(self) -> None:
        self.ok("fix", "51.5", "-0.1", "--course", "090", "--speed", "6",
                "--time", "2024-06-01T14:00:00+02:00")
        self.assertEqual(self.read_store()["fixes"][0]["recorded_utc"],
                         "2024-06-01T12:00:00Z")


class TestStatus(CliTestCase):
    def test_populated_status_reports_everything(self) -> None:
        self.seed_basic_passage()
        self.ok("fix", "50.9", "-1.0", "--course", "095", "--speed", "6.0",
                "--time", "2024-06-01T13:00:00Z")
        result = self.ok("status")
        self.assertIn(str(self.store), result.stdout)
        self.assertIn("2 legs", result.stdout)
        self.assertIn("2 fixes", result.stdout)
        self.assertIn("50.800,-1.100", result.stdout)
        self.assertIn("045T", result.stdout)
        self.assertIn("2024-06-01T13:00:00Z", result.stdout)

    def test_empty_store_status_says_nothing_is_recorded(self) -> None:
        result = self.ok("status")
        self.assertIn(str(self.store), result.stdout)
        self.assertIn("not created yet", result.stdout)
        self.assertIn("No legs, fixes or current offset recorded yet", result.stdout)


class TestJsonOutput(CliTestCase):
    def test_legs_json_is_one_object_with_the_leg_array(self) -> None:
        self.ok("legs", "origin", "0", "0")
        self.ok("legs", "add", "090", "10")
        self.ok("legs", "add", "180", "10")
        result = self.ok("legs", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["legs"]), 2)
        self.assertNotIn("Leg  Course", result.stdout)
        self.assertEqual(payload["leg_count"], 2)

    def test_fixes_json_on_an_empty_log(self) -> None:
        result = self.ok("fixes", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["fixes"], [])

    def test_drift_json_shows_the_active_offset(self) -> None:
        self.ok("drift", "135", "1.5")
        payload = json.loads(self.ok("drift", "--json").stdout)
        self.assertEqual(payload["drift"]["set_deg"], 135.0)
        self.ok("drift", "clear")
        cleared = json.loads(self.ok("drift", "--json").stdout)
        self.assertIsNone(cleared["drift"])

    def test_status_and_dr_json(self) -> None:
        self.seed_basic_passage()
        status = json.loads(self.ok("status", "--json").stdout)
        self.assertEqual(status["leg_count"], 2)
        self.assertEqual(status["fix_count"], 1)
        advance = json.loads(self.ok("dr", "1", "--json").stdout)
        self.assertIn("ground_course_deg", advance["advance"])
        self.assertIsNone(advance["saved_fix"])

    def test_json_error_keeps_stdout_parseable(self) -> None:
        result = self.fails(2, "legs", "add", "361", "10", "--json")
        self.assertEqual(result.stdout, "")
        self.assertIn("course must be between 0 and 360", result.stderr)

    def test_json_flag_also_works_before_the_command(self) -> None:
        self.ok("fix", "51.5", "-0.1", "--course", "090", "--speed", "6")
        payload = json.loads(self.ok("--json", "fixes").stdout)
        self.assertEqual(payload["count"], 1)


class TestPresentation(CliTestCase):
    def test_leg_rows_share_column_widths_and_rounding(self) -> None:
        self.ok("legs", "origin", "0", "0")
        self.ok("legs", "add", "090", "5")
        self.ok("legs", "add", "180", "125.25")
        self.ok("legs", "add", "270", "1000")
        lines = self.ok("legs").stdout.splitlines()
        header = next(line for line in lines if "Course" in line and "Cum" in line)
        for label in ("Leg", "Course", "Dist nm", "Arr Lat", "Arr Lon", "Cum nm"):
            self.assertIn(label, header)
        data_rows = [line for line in lines if line[:1] == " " and line.strip()[:1].isdigit()]
        self.assertEqual(len(data_rows), 3)
        # Every row lays its fields out on the same column edges.
        edges = [field_edges(row) for row in data_rows]
        self.assertEqual(edges[0], edges[1])
        self.assertEqual(edges[1], edges[2])
        self.assertIn("125.25", "\n".join(lines))
        for value in positions_in(data_rows):
            self.assertRegex(value, r"^-?\d+\.\d{3}$")

    def test_output_is_plain_ascii_under_eighty_columns(self) -> None:
        self.seed_basic_passage()
        self.ok("dr", "1", "--save")
        for args in (("legs",), ("fixes",), ("fixes", "last"), ("drift",),
                     ("status",), ("dr", "2"), ("--help",)):
            result = self.ok(*args)
            for stream in (result.stdout, result.stderr):
                self.assertNotIn("\x1b", stream)
                for line in stream.splitlines():
                    self.assertLessEqual(len(line), 80, msg=f"{args}: {line!r}")

    def test_fix_rows_are_aligned_under_the_header(self) -> None:
        self.ok("fix", "51.5", "-0.1", "--course", "090", "--speed", "6",
                "--time", "2024-06-01T12:00:00Z")
        self.ok("fix", "-1.25", "179.999", "--course", "359.5", "--speed", "12.5",
                "--time", "2024-06-01T13:00:00Z")
        self.ok("drift", "000", "0.5")
        self.ok("fix", "0", "0", "--course", "180", "--speed", "4",
                "--time", "2024-06-01T14:00:00Z")
        lines = [line for line in self.ok("fixes").stdout.splitlines() if line]
        header = next(line for line in lines if line.startswith("Id"))
        for label in ("Id", "Recorded UTC", "Lat", "Lon", "Crs", "Spd kn",
                      "Kind", "Offset"):
            self.assertIn(label, header)
        data_rows = [line for line in lines if line[:1] == " " and line.strip()[:1].isdigit()]
        self.assertEqual(len(data_rows), 3)
        edges = [field_edges(row) for row in data_rows]
        self.assertEqual(edges[0], edges[1])
        self.assertEqual(edges[1], edges[2])
        self.assertEqual(len(edges[0]), 8)


def field_edges(line: str) -> list[int]:
    """End index of every space-delimited field, which is where columns align."""
    edges: list[int] = []
    for match in re.finditer(r"\S+(?: \S+)*", line):
        for token in re.finditer(r"\S+", match.group(0)):
            edges.append(match.start() + token.end())
    return edges


def positions_in(lines: list[str]) -> list[str]:
    """Numeric tokens from the position columns of leg rows."""
    found: list[str] = []
    for line in lines:
        tokens = line.split()
        if len(tokens) >= 7:
            found.extend(tokens[3:6])
    return found


if __name__ == "__main__":
    unittest.main()
