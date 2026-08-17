#!/usr/bin/env python3
"""Integration tests for materialize.py's event date-order check. Runs the
real script via uv in a temp directory, so it needs uv on PATH (present in CI
via astral-sh/setup-uv). Run directly:
python3 .github/scripts/test_materialize_dates.py"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MATERIALIZE = REPO_ROOT / "tools" / "materialize.py"


def make_series_events(events):
    """Build a schema-valid series dict from (id, startDate, endDate) tuples.

    Events are listed newest first, matching the layout of the repo's series
    files."""
    return {
        "name": "Testcon",
        "events": [
            {
                "id": event_id,
                "name": f"Testcon ({event_id})",
                "url": "https://example.com",
                "startDate": start_date,
                "endDate": end_date,
                "venue": "Test Hall",
                "locale": "en-US",
            }
            for event_id, start_date, end_date in events
        ],
    }


def make_series(start_date, end_date):
    """Build a one-event series with the given start and end dates."""
    return make_series_events([("testcon-2027", start_date, end_date)])


def run_materialize(series):
    """Write ``series`` as a fixture and run materialize.py against it via uv.

    Returns the CompletedProcess. materialize only globs *.json in its cwd, so
    out/ can live alongside the fixture without being picked up as a series
    file."""
    with tempfile.TemporaryDirectory() as data_dir:
        with open(os.path.join(data_dir, "testcon.json"), "w") as f:
            json.dump(series, f)
        out_dir = os.path.join(data_dir, "out")
        os.mkdir(out_dir)
        return subprocess.run(
            ["uv", "run", "--script", str(MATERIALIZE), out_dir],
            cwd=data_dir,
            capture_output=True,
            text=True,
        )


@unittest.skipUnless(
    shutil.which("uv"), "these tests run materialize.py via uv, which is not on PATH"
)
class TestDateOrder(unittest.TestCase):
    """End-to-end checks of the endDate >= startDate rule in materialize.py."""

    def test_end_after_start_passes(self):
        """A multi-day event with endDate after startDate validates."""
        result = run_materialize(make_series("2027-04-02", "2027-04-04"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_single_day_event_passes(self):
        """A single-day event (endDate == startDate) validates."""
        result = run_materialize(make_series("2027-04-02", "2027-04-02"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_end_before_start_fails(self):
        """An event whose endDate precedes startDate fails with a clear message.

        Regression: ainmhicon-2027 was submitted with endDate 2026-04-04
        against startDate 2027-04-02 and passed validation, because the
        schema's formatMinimum/$data keyword is an ajv extension that
        python-jsonschema ignores."""
        result = run_materialize(make_series("2027-04-02", "2026-04-04"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("endDate 2026-04-04 is before startDate 2027-04-02", result.stderr)

    def test_later_event_in_series_fails(self):
        """The check runs for every event in a series, not just the first.

        Series files list events newest first, so an older event is the
        likely place for a bad date to hide."""
        result = run_materialize(
            make_series_events(
                [
                    ("testcon-2027", "2027-04-02", "2027-04-04"),
                    ("testcon-2026", "2026-04-03", "2026-04-01"),
                ]
            )
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("testcon-2026", result.stderr)
        self.assertIn(
            "endDate 2026-04-01 is before startDate 2026-04-03", result.stderr
        )

    def test_schema_invalid_file_still_fails(self):
        """A schema-invalid file is reported and skipped, not crashed on.

        Regression for the has_errors fix: without it the file falls through
        into the event loop and exits 1 via an uncaught KeyError, so the
        traceback assertion is what discriminates."""
        series = make_series("2027-04-02", "2027-04-04")
        del series["events"][0]["locale"]
        result = run_materialize(series)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("required property", result.stderr)
        self.assertIn("locale", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
