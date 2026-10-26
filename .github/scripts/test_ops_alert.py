#!/usr/bin/env python3
"""Tests for the ops channel alert. Run directly:
python3 .github/scripts/test_ops_alert.py"""
import datetime
import io
import json
import pathlib
import re
import subprocess
import unittest
import unittest.mock

import ops_alert
from ops_alert import Kind, Policy, Run, classify, decide
from ops_alert_rules import Rule, Verdict

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ops_alert"
T0 = datetime.datetime(2026, 10, 24, 20, 15, tzinfo=datetime.timezone.utc)


def timeline(pattern, every=datetime.timedelta(hours=1), start=T0):
    """Runs from a string, oldest first: F failed, P passed, C cancelled.
    Returned newest first, the order the API gives."""
    codes = {"F": "failure", "P": "success", "C": "cancelled"}
    runs = [
        Run(i + 1, codes[c], start + i * every, f"https://example.test/runs/{i + 1}")
        for i, c in enumerate(pattern)
    ]
    return runs[::-1]


def events(runs):
    """The event every run would post, oldest first, as (run id, kind)."""
    return [
        (r.id, e.kind) for r in runs[::-1] if (e := decide(runs, r.id)) is not None
    ]


class TestDecide(unittest.TestCase):
    def test_one_off_failure_posts_nothing(self):
        self.assertEqual(events(timeline("PPFPP")), [])

    def test_short_hourly_streak_posts_nothing(self):
        # 6 failures span 5 hours: still under the 6-hour bar.
        self.assertEqual(events(timeline("P" + "F" * 6 + "P")), [])

    def test_hourly_streak_alerts_once_at_six_hours(self):
        got = events(timeline("P" + "F" * 12))
        # Run 2 is the first failure; run 8 is 6 hours later.
        self.assertEqual(got, [(8, Kind.ALERT)])

    def test_six_hourly_workflow_alerts_on_second_failure(self):
        got = events(timeline("PFFF", every=datetime.timedelta(hours=6)))
        self.assertEqual(got, [(3, Kind.ALERT)])

    def test_daily_workflow_alerts_on_second_failure(self):
        got = events(timeline("PFF", every=datetime.timedelta(days=1)))
        self.assertEqual(got, [(3, Kind.ALERT)])

    def test_recovery_only_after_an_alert(self):
        self.assertEqual(events(timeline("PFFP")), [])
        got = events(timeline("P" + "F" * 7 + "P"))
        self.assertEqual(got, [(8, Kind.ALERT), (9, Kind.RECOVERED)])

    def test_cancelled_runs_neither_break_nor_extend_a_streak(self):
        got = events(timeline("P" + "FC" * 7))
        # Failures at hours 1,3,...,13; the alert lands on the 7-hour one.
        alert = [k for _, k in got]
        self.assertEqual(alert, [Kind.ALERT])
        self.assertEqual(events(timeline("PFCCCCCCCCP")), [])

    def test_decision_ignores_newer_runs(self):
        runs = timeline("P" + "F" * 7 + "P")
        # Run 8 alerted when it finished; a later success doesn't change that.
        self.assertEqual(decide(runs, 8).kind, Kind.ALERT)

    def test_reminds_once_a_day_and_not_right_after_the_alert(self):
        # Hourly failures from 2026-10-24 21:15 for three days.
        runs = timeline("P" + "F" * 72)
        got = events(runs)
        kinds = [k for _, k in got]
        self.assertEqual(kinds[0], Kind.ALERT)
        reminders = [runs[::-1][i - 1].created_at for i, k in got if k is Kind.REMIND]
        # Alert at 10-25 03:15. The 10-25 16:15 run is 13h later: first
        # reminder. Then one per day at the first run after 16:00 UTC.
        self.assertEqual(
            [r.strftime("%m-%d %H:%M") for r in reminders],
            ["10-25 16:15", "10-26 16:15", "10-27 16:15"],
        )

    def test_reminder_waits_out_the_quiet_period(self):
        # The streak qualifies at 15:15, so the 16:15 run is an hour after
        # the alert. That day's reminder goes to the first run 12h on.
        start = datetime.datetime(2026, 10, 25, 8, 15, tzinfo=datetime.timezone.utc)
        runs = timeline("P" + "F" * 24, start=start)
        got = [(runs[::-1][i - 1].created_at.strftime("%m-%d %H:%M"), k) for i, k in events(runs)]
        self.assertEqual(
            got, [("10-25 15:15", Kind.ALERT), ("10-26 03:15", Kind.REMIND)]
        )

    def test_reminder_deferred_by_quiet_period_still_goes_out_same_day(self):
        # The real 2026-10-25 import_fancons history: throttled to a run
        # every few hours. Alert at 06:04; 16:12 is too soon after it, so
        # the reminder goes to 19:02 instead of slipping a whole day.
        times = ["10-24 20:45", "10-24 23:09", "10-25 01:07", "10-25 06:04",
                 "10-25 11:21", "10-25 16:12", "10-25 19:02", "10-25 21:44",
                 "10-26 00:19", "10-26 06:09", "10-26 12:55", "10-26 19:15"]
        runs = [
            Run(i + 1, "success" if i == 0 else "failure",
                datetime.datetime.strptime("2026-" + t, "%Y-%m-%d %H:%M").replace(
                    tzinfo=datetime.timezone.utc))
            for i, t in enumerate(times)
        ][::-1]
        got = [(times[i - 1], k) for i, k in events(runs)]
        self.assertEqual(got, [
            ("10-25 06:04", Kind.ALERT),
            ("10-25 19:02", Kind.REMIND),
            ("10-26 19:15", Kind.REMIND),
        ])

    def test_streak_past_fetched_history_does_not_realert(self):
        # Only the newest 50 runs were fetched, all failures.
        runs = timeline("F" * 200)[:50]
        newest = runs[0].id
        event = decide(runs, newest)
        self.assertNotEqual(getattr(event, "kind", None), Kind.ALERT)

    def test_unknown_run_posts_nothing(self):
        self.assertIsNone(decide(timeline("FFFFFFFF"), 999))

    def test_policy_override(self):
        strict = Policy(min_failures=3, min_span=datetime.timedelta(0))
        runs = timeline("PFFF")
        self.assertEqual(decide(runs, 4, strict).kind, Kind.ALERT)
        self.assertIsNone(decide(runs, 3, strict))


class TestClassify(unittest.TestCase):
    def fixture(self, name):
        return (FIXTURES / name).read_text()

    def test_pyicu_build_failure_needs_action(self):
        d = classify(self.fixture("pyicu_build_failure.log"))
        self.assertEqual(d.verdict, Verdict.ACTION)
        self.assertIn("dependency failed to build", d.advice[0])
        self.assertEqual(d.evidence, "error: Failed to build `pyicu==2.15.2`")

    def test_python_315_build_failure_needs_action(self):
        d = classify(self.fixture("python315_build_failure.log"))
        self.assertEqual(d.verdict, Verdict.ACTION)
        self.assertIn("orjson", d.evidence)

    def test_403_outranks_push_race_and_both_are_listed(self):
        d = classify(self.fixture("concat_403_and_push_race.log"))
        self.assertEqual(d.verdict, Verdict.ACTION)
        self.assertIn("403", d.evidence)
        self.assertEqual(len(d.advice), 2)
        self.assertIn("pushed to main first", d.advice[1])

    def test_push_race_alone_needs_no_action(self):
        log = "\n".join(
            l for l in self.fixture("concat_403_and_push_race.log").splitlines()
            if "403" not in l
        )
        self.assertEqual(classify(log).verdict, Verdict.NO_ACTION)

    def test_upstream_timeout_needs_no_action(self):
        d = classify(self.fixture("synthetic_upstream_timeout.log"))
        self.assertEqual(d.verdict, Verdict.NO_ACTION)
        self.assertEqual(d.evidence, "httpx.ReadTimeout: The read operation timed out")

    def test_unknown_failure_is_unclear_with_last_error(self):
        d = classify(self.fixture("concat_api_keyerror.log"))
        self.assertEqual(d.verdict, Verdict.UNCLEAR)
        self.assertEqual(d.evidence, "KeyError: 'conventions'")

    def test_most_severe_verdict_leads_whatever_the_rule_order(self):
        # A rule added later in the file must still lead if it's more severe.
        mild = Rule("mild", re.compile("timeout"), Verdict.NO_ACTION, "mild advice")
        severe = Rule("severe", re.compile("broken"), Verdict.ACTION, "severe advice")
        d = classify("a timeout\nthen broken", rules=[mild, severe])
        self.assertEqual(d.verdict, Verdict.ACTION)
        self.assertEqual(d.advice, ["severe advice", "mild advice"])
        self.assertEqual(d.evidence, "then broken")

    def test_unreadable_log_is_unclear(self):
        d = classify("")
        self.assertEqual(d.verdict, Verdict.UNCLEAR)
        self.assertIn("Couldn't read", d.advice[0])

    def test_long_evidence_is_cut(self):
        d = classify("x\tstep\t2026-10-26T00:00:00Z httpx.ConnectError: " + "a" * 500)
        self.assertLessEqual(len(d.evidence), ops_alert.LOG_LINE_MAX)


class TestMessage(unittest.TestCase):
    def test_alert_message(self):
        runs = timeline("P" + "F" * 7)
        event = decide(runs, 8)
        d = classify((FIXTURES / "pyicu_build_failure.log").read_text())
        text = ops_alert.format_message("import_fancons", event, d)
        self.assertEqual(
            text,
            "import_fancons has failed 7 runs in a row, since 2026-10-24 21:15 UTC.\n"
            "\n"
            "Action needed.\n"
            "- A dependency failed to build. The build environment probably "
            "changed (runner image, Python, or a system library), so this won't "
            "fix itself.\n"
            "\n"
            "Log: error: Failed to build `pyicu==2.15.2`\n"
            "https://example.test/runs/8",
        )

    def test_recovery_message_and_truncated_count(self):
        runs = timeline("P" + "F" * 7 + "P")
        text = ops_alert.format_message("import_rams", decide(runs, 9), truncated=True)
        self.assertEqual(
            text,
            "import_rams is passing again after 7+ failed runs "
            "(2026-10-24 21:15 to 2026-10-25 03:15 UTC).\n"
            "https://example.test/runs/9",
        )


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSend(unittest.TestCase):
    def quiet(self, fn, *a, **kw):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            return fn(*a, **kw), out.getvalue()

    def test_missing_secrets_warn_and_do_not_raise(self):
        ok, out = self.quiet(ops_alert.send_telegram, "hi", "", "chat")
        self.assertFalse(ok)
        self.assertIn("::warning::", out)

    def test_transport_error_never_leaks_the_token(self):
        def boom(url, **kw):
            raise OSError(f"failed to reach {url}")

        ok, out = self.quiet(ops_alert.send_telegram, "hi", "SECRET123", "c", urlopen=boom)
        self.assertFalse(ok)
        self.assertNotIn("SECRET123", out)

    def test_non_200_is_a_warning(self):
        ok, out = self.quiet(
            ops_alert.send_telegram, "hi", "t", "c", urlopen=lambda *a, **k: FakeResponse(502)
        )
        self.assertFalse(ok)
        self.assertIn("HTTP 502", out)

    def test_delivers(self):
        sent = {}

        def capture(url, data, timeout):
            sent["url"], sent["data"] = url, data
            return FakeResponse(200)

        ok, _ = self.quiet(ops_alert.send_telegram, "hello", "tok", "-100", urlopen=capture)
        self.assertTrue(ok)
        self.assertIn("/bottok/sendMessage", sent["url"])
        self.assertIn(b"text=hello", sent["data"])
        self.assertIn(b"chat_id=-100", sent["data"])


def api_run(r):
    return {
        "id": r.id,
        "conclusion": r.conclusion,
        "created_at": r.created_at.isoformat().replace("+00:00", "Z"),
        "html_url": r.url,
    }


class FakeGh:
    """Stands in for the gh CLI, serving one workflow's history."""

    def __init__(self, runs, log="", head_branch="main", log_fails=False):
        self.runs, self.log, self.head_branch, self.log_fails = runs, log, head_branch, log_fails

    def __call__(self, *args):
        if args[0] == "run":
            if self.log_fails:
                raise subprocess.CalledProcessError(1, "gh")
            return self.log
        path = args[1]
        if path == "repos/o/r":
            return json.dumps({"default_branch": "main"})
        if "/runs/" in path and "workflows" not in path:
            run_id = int(path.rsplit("/", 1)[1])
            r = next(r for r in self.runs if r.id == run_id)
            # Same shape as the real API: the embedded repository object
            # has no default_branch.
            return json.dumps({
                "name": "import_fancons", "workflow_id": 42, "conclusion": r.conclusion,
                "head_branch": self.head_branch, "repository": {"full_name": "o/r"},
            })
        page = int(path.rsplit("page=", 1)[1])
        batch = self.runs[(page - 1) * 100 : page * 100]
        return json.dumps({"workflow_runs": [api_run(r) for r in batch]})


class TestMain(unittest.TestCase):
    def run_main(self, gh, run_id, dry_run=False):
        sent = []
        argv = ["--run-id", str(run_id), "--repo", "o/r"] + (["--dry-run"] if dry_run else [])
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            code = ops_alert.main(
                argv, gh=gh, send=lambda text, tok, chat: sent.append(text),
                env={"OPS_TOKEN": "t", "OPS_CHAT": "c"},
            )
        self.assertEqual(code, 0)
        return sent

    def test_end_to_end_alert_with_verdict(self):
        log = (FIXTURES / "pyicu_build_failure.log").read_text()
        sent = self.run_main(FakeGh(timeline("P" + "F" * 7), log), 8)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("import_fancons has failed 7 runs"))
        self.assertIn("Action needed.", sent[0])

    def test_quiet_run_sends_nothing(self):
        self.assertEqual(self.run_main(FakeGh(timeline("PPF")), 3), [])

    def test_dry_run_sends_nothing(self):
        self.assertEqual(self.run_main(FakeGh(timeline("P" + "F" * 7)), 8, dry_run=True), [])

    def test_branch_runs_are_ignored(self):
        self.assertEqual(self.run_main(FakeGh(timeline("P" + "F" * 7), head_branch="x"), 8), [])

    def test_unreadable_log_still_alerts_as_unclear(self):
        sent = self.run_main(FakeGh(timeline("P" + "F" * 7), log_fails=True), 8)
        self.assertIn("Unclear, take a look.", sent[0])

    def test_history_spanning_pages_finds_the_streak_start(self):
        # 150 hourly failures: the streak's start and the success before it
        # are on page 2, so there must be no "+" and no re-alert.
        runs = timeline("P" + "F" * 150)
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            got, truncated = ops_alert.fetch_runs("o/r", 42, "main", runs[0].id, gh=FakeGh(runs))
        self.assertFalse(truncated)
        self.assertEqual(len(got), 151)

    def test_fetch_stops_at_the_first_page_with_a_success_past_the_streak(self):
        runs = timeline("P" * 300 + "F" * 20)
        calls = []
        gh = FakeGh(runs)
        got, _ = ops_alert.fetch_runs(
            "o/r", 42, "main", runs[0].id, gh=lambda *a: calls.append(a) or gh(*a)
        )
        self.assertEqual(len(calls), 1)

    def test_streak_longer_than_every_page_is_truncated(self):
        runs = timeline("F" * (ops_alert.MAX_PAGES * 100 + 5))
        got, truncated = ops_alert.fetch_runs("o/r", 42, "main", runs[0].id, gh=FakeGh(runs))
        self.assertTrue(truncated)
        self.assertEqual(len(got), ops_alert.MAX_PAGES * 100)


if __name__ == "__main__":
    unittest.main()
