#!/usr/bin/env python3
"""Integration tests for materialize.py's event date-order check. Runs the
real script via uv in a temp directory, so it needs uv on PATH (present in CI
via astral-sh/setup-uv). Run directly:
python3 .github/scripts/test_materialize_dates.py"""
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MATERIALIZE = REPO_ROOT / "tools" / "materialize.py"


def make_series(start_date, end_date):
    return {
        "name": "Testcon",
        "events": [
            {
                "id": "testcon-2027",
                "name": "Testcon 2027",
                "url": "https://example.com",
                "startDate": start_date,
                "endDate": end_date,
                "venue": "Test Hall",
                "locale": "en-US",
            }
        ],
    }


def run_materialize(series):
    # materialize only globs *.json in its cwd, so out/ can live alongside
    # the fixture without being picked up as a series file
    with tempfile.TemporaryDirectory() as data_dir:
        with open(os.path.join(data_dir, "testcon.json"), "w") as f:
            json.dump(series, f)
        out_dir = os.path.join(data_dir, "out")
        os.mkdir(out_dir)
        return subprocess.run(
            ["uv", "run", str(MATERIALIZE), out_dir],
            cwd=data_dir,
            capture_output=True,
            text=True,
        )


class TestDateOrder(unittest.TestCase):
    def test_end_after_start_passes(self):
        result = run_materialize(make_series("2027-04-02", "2027-04-04"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_single_day_event_passes(self):
        result = run_materialize(make_series("2027-04-02", "2027-04-02"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_end_before_start_fails(self):
        # regression: ainmhicon-2027 was submitted with endDate 2026-04-04
        # against startDate 2027-04-02 and passed validation, because the
        # schema's formatMinimum/$data keyword is an ajv extension that
        # python-jsonschema ignores
        result = run_materialize(make_series("2027-04-02", "2026-04-04"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("endDate 2026-04-04 is before startDate 2027-04-02", result.stderr)

    def test_schema_invalid_file_still_fails(self):
        # regression for the has_errors fix: a file that fails schema
        # validation must be skipped and reported, not fall through into the
        # event loop. Without the fix it also exits 1, but via an uncaught
        # KeyError, so the traceback assertion is what discriminates.
        series = make_series("2027-04-02", "2027-04-04")
        del series["events"][0]["locale"]
        result = run_materialize(series)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("'locale' is a required property", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
