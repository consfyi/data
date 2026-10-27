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
import urllib.error

import ops_alert
from ops_alert import Kind, Policy, Run, classify, decide
from ops_alert_rules import Rule, Verdict

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ops_alert"
WORKFLOWS = pathlib.Path(__file__).parent.parent / "workflows"
T0 = datetime.datetime(2026, 10, 24, 20, 15, tzinfo=datetime.timezone.utc)


def timeline(pattern, every=datetime.timedelta(hours=1), start=T0):
    """Runs from a string, oldest first: F failed, T timed out, S startup
    failure, P passed, C cancelled. Returned newest first, the order the API
    gives."""
    codes = {
        "F": "failure", "T": "timed_out", "S": "startup_failure",
        "P": "success", "C": "cancelled",
    }
    runs = [
        Run(i + 1, codes[c], start + i * every, f"https://example.test/runs/{i + 1}")
        for i, c in enumerate(pattern)
    ]
    return runs[::-1]


def log_lines(*lines):
    """A log in `gh run view --log-failed`'s shape, one line per argument."""
    return "".join(
        f"import\tUNKNOWN STEP\t2026-10-26T00:00:{i:02d}.0000000Z {l}\n"
        for i, l in enumerate(lines)
    )


def events(runs):
    """The event every run would post, oldest first, as (run id, kind)."""
    return [
        (r.id, e.kind) for r in runs[::-1] if (e := decide(runs, r.id)) is not None
    ]


class TestDecide(unittest.TestCase):
    def test_one_off_failure_posts_nothing(self):
        self.assertEqual(events(timeline("PPFPP")), [])

    def test_short_hourly_streak_posts_nothing(self):
        # 5 failures span 4 hours: still under the 5-hour bar.
        self.assertEqual(events(timeline("P" + "F" * 5 + "P")), [])

    def test_hourly_streak_alerts_once_at_five_hours(self):
        got = events(timeline("P" + "F" * 12))
        # Run 2 is the first failure; run 7 is 5 hours later.
        self.assertEqual(got, [(7, Kind.ALERT)])

    def test_six_hourly_workflow_alerts_on_second_failure(self):
        got = events(timeline("PFFF", every=datetime.timedelta(hours=6)))
        self.assertEqual(got, [(3, Kind.ALERT)])

    def test_jittered_six_hourly_runs_alert_on_second_failure(self):
        # Scheduled runs start late by varying amounts: these two failures
        # are 5h40m apart, not 6h.
        times = ["10-25 18:25", "10-26 00:40", "10-26 06:20"]
        runs = [
            Run(i + 1, "success" if i == 0 else "failure",
                datetime.datetime.strptime("2026-" + t, "%Y-%m-%d %H:%M").replace(
                    tzinfo=datetime.timezone.utc))
            for i, t in enumerate(times)
        ][::-1]
        self.assertEqual(events(runs), [(3, Kind.ALERT)])

    def test_timed_out_runs_count_as_failures(self):
        self.assertEqual(events(timeline("P" + "T" * 7)), [(7, Kind.ALERT)])

    def test_mixed_failure_kinds_alert_once(self):
        got = events(timeline("P" + "FTS" * 3))
        self.assertEqual([k for _, k in got], [Kind.ALERT])

    def test_daily_workflow_alerts_on_second_failure(self):
        got = events(timeline("PFF", every=datetime.timedelta(days=1)))
        self.assertEqual(got, [(3, Kind.ALERT)])

    def test_recovery_only_after_an_alert(self):
        self.assertEqual(events(timeline("PFFP")), [])
        got = events(timeline("P" + "F" * 7 + "P"))
        self.assertEqual(got, [(7, Kind.ALERT), (9, Kind.RECOVERED)])

    def test_cancelled_runs_neither_break_nor_extend_a_streak(self):
        got = events(timeline("P" + "FC" * 7))
        # Failures at hours 1,3,...,13; the alert lands on the 7-hour one.
        alert = [k for _, k in got]
        self.assertEqual(alert, [Kind.ALERT])
        self.assertEqual(events(timeline("PFCCCCCCCCP")), [])

    def test_decision_ignores_newer_runs(self):
        runs = timeline("P" + "F" * 7 + "P")
        # Run 7 alerted when it finished; a later success doesn't change that.
        self.assertEqual(decide(runs, 7).kind, Kind.ALERT)

    def test_reminds_once_a_day_and_not_right_after_the_alert(self):
        # Hourly failures from 2026-10-24 21:15 for three days.
        runs = timeline("P" + "F" * 72)
        got = events(runs)
        kinds = [k for _, k in got]
        self.assertEqual(kinds[0], Kind.ALERT)
        reminders = [runs[::-1][i - 1].created_at for i, k in got if k is Kind.REMIND]
        # Alert at 10-25 02:15. The 10-25 16:15 run is 14h later: first
        # reminder. Then one per day at the first run after 16:00 UTC.
        self.assertEqual(
            [r.strftime("%m-%d %H:%M") for r in reminders],
            ["10-25 16:15", "10-26 16:15", "10-27 16:15"],
        )

    def test_reminder_waits_out_the_quiet_period(self):
        # The streak qualifies at 14:15, so the 16:15 run is two hours after
        # the alert. That day's reminder goes to the first run 12h on.
        start = datetime.datetime(2026, 10, 25, 8, 15, tzinfo=datetime.timezone.utc)
        runs = timeline("P" + "F" * 24, start=start)
        got = [(runs[::-1][i - 1].created_at.strftime("%m-%d %H:%M"), k) for i, k in events(runs)]
        self.assertEqual(
            got, [("10-25 14:15", Kind.ALERT), ("10-26 02:15", Kind.REMIND)]
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

    def test_startup_failures_count_as_failures(self):
        self.assertEqual(events(timeline("P" + "S" * 7)), [(7, Kind.ALERT)])

    def test_rerun_attempts_never_alert_or_remind(self):
        # Runs 7 and 21 are each the newest run when they're re-run.
        runs = timeline("P" + "F" * 72)
        self.assertEqual(decide(runs[-7:], 7).kind, Kind.ALERT)
        self.assertIsNone(decide(runs[-7:], 7, previous_attempt="failure"))
        self.assertEqual(decide(runs[-21:], 21).kind, Kind.REMIND)
        self.assertIsNone(decide(runs[-21:], 21, previous_attempt="failure"))
        self.assertIsNone(decide(runs[-7:], 7, previous_attempt="success"))

    def test_failed_rerun_after_only_cancelled_attempts_decides_like_a_first(self):
        # Run 7's earlier attempts were cancelled and posted nothing, so its
        # failed re-run is the first to count it.
        runs = timeline("P" + "F" * 72)
        for previous in ("", "cancelled"):
            with self.subTest(previous=previous):
                event = decide(runs[-7:], 7, previous_attempt=previous)
                self.assertEqual(event.kind, Kind.ALERT)
                self.assertEqual(len(event.streak), 6)
                self.assertEqual(
                    decide(runs[-21:], 21, previous_attempt=previous).kind, Kind.REMIND)

    def test_failed_rerun_after_only_cancelled_attempts_of_an_older_run_posts_nothing(self):
        # Run 8 failed while run 7 counted as cancelled, so run 8 alerted.
        runs = timeline("P" + "F" * 5 + "CF")
        self.assertEqual(decide(runs, 8).kind, Kind.ALERT)
        runs = timeline("P" + "F" * 7)
        self.assertIsNone(decide(runs, 7, previous_attempt=""))

    def test_failed_rerun_is_not_silenced_by_a_newer_cancelled_run(self):
        # Run 8 was cancelled, so it posted nothing and run 7 still alerts.
        event = decide(timeline("P" + "F" * 6 + "C"), 7, previous_attempt="cancelled")
        self.assertEqual(event.kind, Kind.ALERT)
        self.assertEqual(len(event.streak), 6)

    def test_rerun_that_passes_recovers_the_streak_it_ended(self):
        # Run 7's first attempt failed, which made the streak 5 hours long
        # and alerted. Its re-run passed, so the streak is over.
        runs = timeline("P" + "F" * 5 + "P")
        self.assertIsNone(decide(runs, 7))
        event = decide(runs, 7, previous_attempt="failure")
        self.assertEqual(event.kind, Kind.RECOVERED)
        self.assertEqual(len(event.streak), 6)
        # A re-run that passes after a streak too short to alert stays quiet.
        self.assertIsNone(decide(timeline("PFP"), 3, previous_attempt="failure"))

    def test_rerun_of_an_older_run_posts_nothing(self):
        # Run 7 is re-run and passes, but run 8 has failed since: the
        # streak is still open, so there is nothing to recover.
        runs = timeline("P" + "F" * 5 + "PF")
        self.assertIsNone(decide(runs, 7, previous_attempt="failure"))

    def test_rerun_inside_an_already_recovered_streak_posts_nothing(self):
        # Run 7 failed, then run 8 passed and posted the recovery. Re-running
        # run 7 into a pass must not post it again.
        self.assertEqual(decide(timeline("P" + "F" * 6 + "P"), 8).kind, Kind.RECOVERED)
        runs = timeline("P" + "F" * 5 + "PP")
        self.assertIsNone(decide(runs, 7, previous_attempt="failure"))

    def test_rerun_whose_first_attempt_passed_posts_nothing(self):
        # Runs 2 to 7 qualify, so only the passed first attempt keeps run 8
        # quiet: that attempt already posted the recovery.
        runs = timeline("P" + "F" * 6 + "P")
        self.assertEqual(decide(runs, 8).kind, Kind.RECOVERED)
        self.assertIsNone(decide(runs, 8, previous_attempt="success"))

    def test_rerun_that_passes_after_a_cancelled_attempt_recovers(self):
        # Runs 2 to 7 failed and alerted. Run 8's first attempt was cancelled,
        # which posted nothing, so its passing re-run ends the streak.
        runs = timeline("P" + "F" * 6 + "P")
        for previous in ("cancelled", ""):
            with self.subTest(previous=previous):
                event = decide(runs, 8, previous_attempt=previous)
                self.assertEqual(event.kind, Kind.RECOVERED)
                self.assertEqual([r.id for r in event.streak], [7, 6, 5, 4, 3, 2])


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

    def test_403_in_an_earlier_step_that_passed_is_ignored(self):
        # The real 2026-10-11 import_concat run: import_concat_all.sh hit a
        # 403 and skipped that source, then format.py failed the job.
        d = classify(self.fixture("concat_403_then_format_failure.log"))
        self.assertEqual(d.verdict, Verdict.ACTION)
        self.assertIn("data-invalid", d.rule_ids)
        self.assertNotIn("source-blocked", d.rule_ids)
        self.assertFalse(any("403" in a for a in d.advice))
        self.assertNotIn("403", d.evidence)

    def test_push_race_in_the_failing_step_outweighs_an_earlier_403(self):
        d = classify(self.fixture("concat_403_then_push_race_sectioned.log"))
        self.assertEqual(d.verdict, Verdict.NO_ACTION)
        self.assertEqual(d.rule_ids, ("push-race",))

    def test_403_in_the_failing_step_needs_action(self):
        lines = self.fixture("concat_403_then_push_race_sectioned.log").splitlines()
        # Fail the import step itself: keep it and its exit-code line.
        d = classify("\n".join(lines[:4] + lines[-1:]))
        self.assertEqual(d.verdict, Verdict.ACTION)
        self.assertEqual(d.rule_ids, ("source-blocked",))

    def test_step_markers_without_an_exit_code_line_use_the_whole_log(self):
        log = log_lines("##[group]Run ./import.sh", "Client error '403 Forbidden'",
                        "##[group]Run ./commit.sh")
        self.assertEqual(classify(log).rule_ids, ("source-blocked",))

    def test_rate_limit_needs_action(self):
        d = classify(log_lines("httpx.HTTPStatusError: Client error '429 Too Many Requests'"))
        self.assertEqual(d.verdict, Verdict.ACTION)
        self.assertEqual(d.rule_ids, ("source-blocked",))

    def test_service_unavailable_needs_no_action(self):
        d = classify(log_lines("httpx.HTTPStatusError: Server error '503 Service Unavailable'"))
        self.assertEqual(d.verdict, Verdict.NO_ACTION)
        self.assertEqual(d.rule_ids, ("source-down",))

    def test_data_invalid_matches_the_workflows_own_messages(self):
        # Built from import_fancons.yml's echo lines, so a reworded message
        # that the rule no longer matches fails here.
        text = (WORKFLOWS / "import_fancons.yml").read_text()
        messages = re.findall(r'echo "::error::(.*)"', text)
        self.assertEqual(len(messages), 2)
        for msg in messages:
            with self.subTest(msg=msg[:40]):
                d = classify(log_lines(
                    "##[group]Run uv run tools/format.py *.json",
                    "Error: invalid record",
                    "##[error]Process completed with exit code 1.",
                    f'##[group]Run echo "::error::{msg}"',
                    f'^[[36;1mecho "::error::{msg}"^[[0m',
                    "##[endgroup]",
                    f"##[error]{msg}",
                ))
                self.assertEqual(d.verdict, Verdict.ACTION)
                self.assertEqual(d.rule_ids, ("data-invalid",))
                # The step's printed message, not its echo command.
                self.assertTrue(d.evidence.startswith(msg[:100]), d.evidence)

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
        # The step's exit-code line says nothing, and the post-job cleanup
        # after it isn't the failure.
        d = classify(self.fixture("concat_api_keyerror.log"))
        self.assertEqual(d.verdict, Verdict.UNCLEAR)
        self.assertEqual(
            d.advice,
            ["No known failure pattern matched. Open the run and read the "
             "failing step's errors."],
        )
        self.assertEqual(d.evidence, "KeyError: 'conventions'")

    def test_last_error_line_in_the_failing_step_is_the_evidence(self):
        # Plain output after the error line isn't the failure.
        log = log_lines(
            "##[group]Run ./import.sh",
            "##[error]Real cause",
            "Cleaning up temp files",
            "##[error]Process completed with exit code 1.",
        )
        self.assertEqual(classify(log).evidence, "Real cause")

    def test_cancelled_step_without_a_timeout_line_is_the_failing_step(self):
        # The 403 was in an earlier step that passed.
        log = log_lines(
            "##[group]Run ./tools/data-importers/import_concat_all.sh",
            "httpx.HTTPStatusError: Client error '403 Forbidden'",
            "##[group]Run uv run tools/format.py *.json",
            "Formatting 250 files",
            "##[error]The operation was canceled.",
            "Post job cleanup.",
        )
        d = classify(log)
        self.assertNotIn("source-blocked", d.rule_ids)
        self.assertEqual(d.evidence, "Formatting 250 files")

    def test_timed_out_step_is_the_failing_step(self):
        # No exit-code line: the job timed out mid-step. The 403 was in an
        # earlier step that passed.
        log = log_lines(
            "##[group]Run ./tools/data-importers/import_concat_all.sh",
            "httpx.HTTPStatusError: Client error '403 Forbidden' for url "
            "'https://reg.example.test/api'",
            "##[group]Run uv run tools/format.py *.json",
            "Formatting 250 files",
            "##[error]The job running on runner GitHub Actions 1000 has exceeded "
            "the maximum execution time of 30 minutes.",
            "##[error]The operation was canceled.",
            "Post job cleanup.",
            "Cleaning up orphan processes",
        )
        d = classify(log)
        self.assertNotIn("source-blocked", d.rule_ids)
        self.assertEqual(d.verdict, Verdict.UNCLEAR)
        self.assertEqual(d.evidence, "Formatting 250 files")

    def test_each_pattern_alternative_matches_its_rule(self):
        cases = [
            ("Failed building wheel for x", "dependency-build"),
            ("💥 maturin failed", "dependency-build"),
            (" ! [rejected]        main -> main (non-fast-forward)", "push-race"),
            ("httpx.HTTPStatusError: Server error '502 Bad Gateway'", "source-down"),
            ("httpx.HTTPStatusError: Server error '500 Internal Server Error'",
             "source-down"),
            ("httpx.HTTPStatusError: Server error '504 Gateway Timeout'", "source-down"),
            ("httpx.ConnectError: [Errno -3] Temporary failure in name resolution",
             "source-down"),
            ("httpx.ConnectTimeout: timed out", "source-down"),
            ("httpx.RemoteProtocolError: Server disconnected without sending a "
             "response.", "source-down"),
        ]
        for line, rule_id in cases:
            with self.subTest(line=line):
                self.assertEqual(classify(log_lines(line)).rule_ids, (rule_id,))

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
        self.assertEqual(
            d.advice,
            ["Couldn't read the run's log. Open the run to see which step failed."],
        )

    def test_long_evidence_is_cut(self):
        d = classify("x\tstep\t2026-10-26T00:00:00Z httpx.ConnectError: " + "a" * 500)
        self.assertLessEqual(len(d.evidence), ops_alert.LOG_LINE_MAX)


class TestMessage(unittest.TestCase):
    def test_alert_message(self):
        runs = timeline("P" + "F" * 6)
        event = decide(runs, 7)
        d = classify((FIXTURES / "pyicu_build_failure.log").read_text())
        text = ops_alert.format_message("import_fancons", event, d)
        self.assertEqual(
            text,
            "Action needed: import_fancons has failed 6 runs in a row since "
            "2026-10-24 21:15 UTC.\n"
            "\n"
            "- A dependency failed to build, probably because the runner image, "
            "Python, or a system library changed. It won't fix itself: update "
            "the pin for the package in the log line.\n"
            "\n"
            "Log line (may include text from source sites): "
            "error: Failed to build `pyicu==2.15.2`\n"
            "Run: https://example.test/runs/7",
        )

    def test_reminder_message(self):
        runs = timeline("P" + "F" * 72)
        event = decide(runs, 21)
        d = classify((FIXTURES / "synthetic_upstream_timeout.log").read_text())
        text = ops_alert.format_message("import_fancons", event, d)
        self.assertEqual(
            text,
            "Probably no action needed: import_fancons is still failing, 20 runs "
            "in a row since 2026-10-24 21:15 UTC.\n"
            "\n"
            "- A source site timed out or returned a server error. The importer "
            "retries on its schedule. If tomorrow's reminder still shows this, "
            "check whether the site is up.\n"
            "\n"
            "Log line (may include text from source sites): "
            "httpx.ReadTimeout: The read operation timed out\n"
            "Run: https://example.test/runs/21",
        )

    def test_recovery_message_and_truncated_count(self):
        runs = timeline("P" + "F" * 7 + "P")
        text = ops_alert.format_message("import_rams", decide(runs, 9), truncated=True)
        self.assertEqual(
            text,
            "No action needed: import_rams is passing again after 7+ failed runs "
            "(2026-10-24 21:15 to 2026-10-25 03:15 UTC).\n"
            "Run: https://example.test/runs/9",
        )

    def test_one_run_is_singular(self):
        run = timeline("F")[0]
        d = classify("")
        alert = ops_alert.format_message("import_rams", ops_alert.Event(Kind.ALERT, [run], run), d)
        self.assertTrue(alert.startswith(
            "Check the run: import_rams has failed 1 run in a row since"), alert)
        recovered = ops_alert.format_message(
            "import_rams", ops_alert.Event(Kind.RECOVERED, [run], run))
        self.assertIn("after 1 failed run (", recovered)


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

    def test_telegram_error_reports_its_status_without_the_token(self):
        def reject(url, **kw):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        ok, out = self.quiet(ops_alert.send_telegram, "hi", "SECRET123", "c", urlopen=reject)
        self.assertFalse(ok)
        self.assertIn("HTTP 401", out)
        self.assertNotIn("SECRET123", out)

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
        self.assertIn(b"disable_web_page_preview=true", sent["data"])


def api_run(r, event="schedule", head_repo="o/r", attempts=None):
    """`attempts` maps a run in progress's earlier attempts to their
    conclusions; its current attempt is the one after them."""
    if attempts is not None:
        return {**api_run(r, event, head_repo), "conclusion": None,
                "status": "in_progress", "run_attempt": len(attempts) + 1}
    return {
        "id": r.id,
        "conclusion": r.conclusion,
        "status": "completed",
        "run_attempt": 1,
        "created_at": r.created_at.isoformat().replace("+00:00", "Z"),
        "html_url": r.url,
        "event": event,
        "head_repository": {"full_name": head_repo},
    }


class FakeGh:
    """Stands in for the gh CLI, serving one workflow's history."""

    def __init__(self, runs, log="", head_branch="main", log_fails=False,
                 event="schedule", head_repo="o/r", attempt=1, listed=None,
                 attempts=None, fork_ids=(), in_progress=None, logs=None,
                 reruns=None):
        self.runs, self.log, self.head_branch, self.log_fails = runs, log, head_branch, log_fails
        # Failed-step logs by attempt number, where they differ from `log`.
        self.logs = logs or {}
        self.event, self.head_repo, self.attempt = event, head_repo, attempt
        # What the runs listing returns, if it lags behind self.runs.
        self.listed = runs if listed is None else listed
        # Earlier attempts' conclusions by attempt number, served when
        # attempt > 1, and the attempt numbers asked for, in order.
        self.attempts, self.requested = attempts or {}, []
        # Runs the listing shows as a fork's pull request runs.
        self.fork_ids = set(fork_ids)
        # Runs in progress, mapped to their earlier attempts' conclusions,
        # where None is an attempt that hasn't completed.
        self.in_progress = in_progress or {}
        # Completed runs, mapped to their earlier attempts' conclusions; the
        # latest attempt's is the run's own.
        self.reruns = reruns or {}

    def latest(self, run_id):
        """The run's latest attempt number."""
        attempts = self.in_progress.get(run_id, self.reruns.get(run_id))
        return self.attempt if attempts is None else len(attempts) + 1

    def __call__(self, *args):
        if args[0] == "run":
            if self.log_fails:
                raise subprocess.CalledProcessError(1, "gh")
            # Without --attempt, gh shows the run's latest attempt.
            run_id = int(args[2])
            n = self.latest(run_id)
            if "--attempt" in args:
                n = int(args[args.index("--attempt") + 1])
            return self.logs.get(n, self.log)
        path = args[1]
        if path == "repos/o/r":
            return json.dumps({"default_branch": "main"})
        if "/attempts/" in path:
            run_id, n = map(int, re.fullmatch(
                r"repos/o/r/actions/runs/(\d+)/attempts/(\d+)", path).groups())
            attempts = self.in_progress.get(run_id, self.reruns.get(run_id))
            if attempts is None:
                attempts, latest = self.attempts, self.attempt
                self.requested.append(n)
            else:
                latest = len(attempts) + 1
            # Only attempts up to the latest exist to be asked for.
            assert 1 <= n <= latest, path
            conclusion = attempts.get(n)
            return json.dumps({"id": run_id, "run_attempt": n, "conclusion": conclusion,
                               "status": "in_progress" if conclusion is None else "completed"})
        if "/runs/" in path and "workflows" not in path:
            run_id = int(path.rsplit("/", 1)[1])
            r = next(r for r in self.runs if r.id == run_id)
            # Same shape as the real API: the embedded repository object
            # has no default_branch.
            return json.dumps({
                **api_run(r, attempts=self.in_progress.get(run_id)),
                "name": "import_fancons", "workflow_id": 42, "event": self.event,
                "head_branch": self.head_branch, "run_attempt": self.latest(run_id),
                "head_repository": {"full_name": self.head_repo},
                "repository": {"full_name": "o/r"},
            })
        page = int(path.rsplit("page=", 1)[1])
        listed = self.listed
        if "status=completed" in path:
            listed = [r for r in listed if r.id not in self.in_progress]
        batch = listed[(page - 1) * 100 : page * 100]
        return json.dumps({"workflow_runs": [
            api_run(r, "pull_request", "someone/r") if r.id in self.fork_ids
            else {**api_run(r), "run_attempt": self.latest(r.id)} if r.id in self.reruns
            else api_run(r, attempts=self.in_progress.get(r.id))
            for r in batch
        ]})


class TestMain(unittest.TestCase):
    ENV = {"OPS_TOKEN": "tok-123", "OPS_CHAT": "chat-456"}

    def run_main(self, gh, run_id, dry_run=False, run_attempt=None):
        """Returns the (text, token, chat) of each send, and stdout."""
        sent = []
        argv = ["--run-id", str(run_id), "--repo", "o/r"] + (["--dry-run"] if dry_run else [])
        if run_attempt is not None:
            argv += ["--run-attempt", str(run_attempt)]
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = ops_alert.main(
                argv, gh=gh, send=lambda text, tok, chat: sent.append((text, tok, chat)),
                env=self.ENV,
            )
        self.assertEqual(code, 0)
        return sent, out.getvalue()

    def texts(self, gh, run_id, run_attempt=None):
        return [text for text, _, _ in self.run_main(gh, run_id, run_attempt=run_attempt)[0]]

    def test_end_to_end_alert_with_verdict(self):
        log = (FIXTURES / "pyicu_build_failure.log").read_text()
        sent, out = self.run_main(FakeGh(timeline("P" + "F" * 6), log), 7)
        self.assertEqual(len(sent), 1)
        text, tok, chat = sent[0]
        self.assertTrue(text.startswith("Action needed: import_fancons has failed 6 runs"))
        self.assertEqual((tok, chat), ("tok-123", "chat-456"))
        # The matched rule ids go to the job log only, not to Telegram.
        self.assertIn("--- alert [dependency-build] ---", out)
        self.assertNotIn("dependency-build", text)

    def test_quiet_run_sends_nothing(self):
        self.assertEqual(self.texts(FakeGh(timeline("PPF")), 3), [])

    def test_dry_run_sends_nothing(self):
        sent, out = self.run_main(FakeGh(timeline("P" + "F" * 6)), 7, dry_run=True)
        self.assertEqual(sent, [])
        self.assertIn("--- alert", out)

    def test_branch_runs_are_ignored(self):
        self.assertEqual(self.texts(FakeGh(timeline("P" + "F" * 6), head_branch="x"), 7), [])

    # Run 3 of a 6-hourly workflow's second failure, which would alert.
    ALERTING = timeline("PFF", every=datetime.timedelta(hours=6))

    def test_fork_pull_request_on_a_branch_named_main_is_ignored(self):
        gh = FakeGh(self.ALERTING, event="pull_request", head_repo="someone/r")
        self.assertEqual(self.texts(gh, 3), [])
        # A fork's own scheduled run doesn't count either.
        gh = FakeGh(self.ALERTING, event="schedule", head_repo="someone/r")
        self.assertEqual(self.texts(gh, 3), [])
        # Same repo, but not a scheduled or dispatched run.
        self.assertEqual(self.texts(FakeGh(self.ALERTING, event="push"), 3), [])

    def test_scheduled_and_dispatched_runs_from_this_repo_alert(self):
        for event in ("schedule", "workflow_dispatch"):
            with self.subTest(event=event):
                self.assertEqual(len(self.texts(FakeGh(self.ALERTING, event=event), 3)), 1)

    def test_rerun_attempt_does_not_realert(self):
        gh = FakeGh(self.ALERTING, attempt=2, attempts={1: "failure"})
        self.assertEqual(self.texts(gh, 3), [])

    def test_rerun_that_passes_posts_recovery(self):
        gh = FakeGh(timeline("P" + "F" * 5 + "P"), attempt=2, attempts={1: "failure"})
        texts = self.texts(gh, 7)
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("No action needed: import_fancons is passing again"))

    def test_failed_rerun_after_a_cancelled_attempt_alerts(self):
        # Attempt 1 posted nothing, so attempt 2's failure is the first to
        # count run 3.
        for previous in ("cancelled", None):
            with self.subTest(previous=previous):
                gh = FakeGh(self.ALERTING, attempt=2, attempts={1: previous})
                texts = self.texts(gh, 3)
                self.assertEqual(len(texts), 1)
                self.assertIn("has failed 2 runs in a row", texts[0])

    def test_failed_rerun_after_a_cancelled_attempt_of_an_older_run_posts_nothing(self):
        gh = FakeGh(timeline("P" + "F" * 7), attempt=2, attempts={1: "cancelled"})
        self.assertEqual(self.texts(gh, 7), [])

    def test_rerun_that_passes_after_a_cancelled_attempt_posts_recovery(self):
        for previous in ("cancelled", None):
            with self.subTest(previous=previous):
                gh = FakeGh(timeline("P" + "F" * 6 + "P"), attempt=2, attempts={1: previous})
                texts = self.texts(gh, 8)
                self.assertEqual(len(texts), 1)
                self.assertIn("is passing again after 6 failed runs (", texts[0])

    def test_rerun_whose_previous_attempt_passed_posts_nothing(self):
        # Runs 2 to 7 qualify, so only the passed first attempt keeps it quiet.
        gh = FakeGh(timeline("P" + "F" * 6 + "P"), attempt=2, attempts={1: "success"})
        self.assertEqual(self.texts(gh, 8), [])

    # Run 3 of a 6-hourly workflow passed on its third attempt. Only its
    # first attempt's failure makes the streak long enough to have alerted.
    RERUN_PASSED = timeline("PFP", every=datetime.timedelta(hours=6))

    def test_rerun_after_a_failed_then_cancelled_attempt_posts_recovery(self):
        gh = FakeGh(self.RERUN_PASSED, attempt=3, attempts={1: "failure", 2: "cancelled"})
        texts = self.texts(gh, 3)
        self.assertEqual(len(texts), 1)
        self.assertIn("is passing again after 2 failed runs (", texts[0])

    def test_rerun_after_a_passed_then_cancelled_attempt_posts_nothing(self):
        # Runs 2 to 7 qualify, so only the passed first attempt keeps it quiet.
        gh = FakeGh(timeline("P" + "F" * 6 + "P"), attempt=3,
                    attempts={1: "success", 2: "cancelled"})
        self.assertEqual(self.texts(gh, 8), [])

    def test_rerun_after_a_passed_then_failed_attempt_posts_nothing(self):
        # Attempt 1 passed and already posted any recovery, so attempt 2's
        # failure doesn't let attempt 3 post a second one.
        gh = FakeGh(timeline("P" + "F" * 6 + "P"), attempt=3,
                    attempts={1: "success", 2: "failure"})
        self.assertEqual(self.texts(gh, 8), [])

    def test_rerun_reads_every_earlier_attempt(self):
        gh = FakeGh(self.RERUN_PASSED, attempt=4,
                    attempts={1: "cancelled", 2: "cancelled", 3: "failure"})
        self.texts(gh, 3)
        self.assertEqual(sorted(gh.requested), [1, 2, 3])

    def test_rerun_of_an_older_run_posts_nothing(self):
        gh = FakeGh(timeline("P" + "F" * 5 + "PF"), attempt=2, attempts={1: "failure"})
        self.assertEqual(self.texts(gh, 7), [])

    # Runs 2 to 7 failed hourly and run 7 alerted. Run 7 is being re-run when
    # run 8 completes: the listing shows run 7 in progress on attempt 2.
    RACING = timeline("P" + "F" * 6 + "P")

    def test_newer_run_that_passes_during_a_rerun_posts_recovery(self):
        gh = FakeGh(self.RACING, in_progress={7: {1: "failure"}})
        texts = self.texts(gh, 8)
        self.assertEqual(len(texts), 1)
        self.assertIn("is passing again after 6 failed runs (", texts[0])

    def test_newer_run_that_fails_during_a_rerun_does_not_realert(self):
        runs = timeline("P" + "F" * 7)
        self.assertEqual(self.texts(FakeGh(runs, in_progress={7: {1: "failure"}}), 8), [])

    def test_rerun_in_progress_counts_its_newest_completed_attempt(self):
        # Attempt 3 is running and attempt 2 never completed, so attempt 1's
        # failure is run 7's result.
        gh = FakeGh(self.RACING, in_progress={7: {1: "failure", 2: None}})
        self.assertEqual(len(self.texts(gh, 8)), 1)

    def test_rerun_in_progress_after_a_cancelled_attempt_counts_the_failure(self):
        # Attempt 2 was cancelled, so attempt 1's failure is run 7's result.
        gh = FakeGh(self.RACING, in_progress={7: {1: "failure", 2: "cancelled"}})
        texts = self.texts(gh, 8)
        self.assertEqual(len(texts), 1)
        self.assertIn("is passing again after 6 failed runs (", texts[0])

    def test_run_whose_latest_attempt_was_cancelled_counts_its_failure(self):
        # Run 7's attempt 1 failed and alerted; its re-run was cancelled.
        # Counted as cancelled, run 7 would let run 8 alert a second time.
        runs = timeline("P" + "F" * 5 + "CF")
        self.assertEqual(len(self.texts(FakeGh(runs), 8)), 1)
        self.assertEqual(self.texts(FakeGh(runs, reruns={7: {1: "failure"}}), 8), [])

    def test_first_attempt_in_progress_is_ignored(self):
        # Run 150 is still on its first attempt, so it has no result yet. It
        # neither ends the streak nor stops the paging as a success would.
        runs = timeline("P" + "F" * 150)
        gh = FakeGh(runs, in_progress={150: {}})
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            got, truncated = ops_alert.fetch_runs("o/r", 42, "main", runs[0], gh=gh)
        self.assertFalse(truncated)
        self.assertEqual(len(got), 150)
        self.assertNotIn(150, [r.id for r in got])
        self.assertEqual(got[-1].conclusion, "success")

    def test_triggering_attempt_decides_while_a_newer_attempt_runs(self):
        # Run 7's attempt 1 failed, ending a 5-hour streak, and attempt 2
        # passed. Attempt 2's job runs only after attempt 3 has started.
        runs = timeline("P" + "F" * 5 + "P")
        gh = FakeGh(runs, in_progress={7: {1: "failure", 2: "success"}})
        texts = self.texts(gh, 7, run_attempt=2)
        self.assertEqual(len(texts), 1)
        self.assertIn("is passing again after 6 failed runs (", texts[0])

    def test_triggering_attempt_decides_after_a_newer_attempt_passed(self):
        # Run 7's attempt 1 failed and is due to alert, but its job runs
        # only after attempt 2 passed, so the run and the listing show a pass.
        runs = timeline("P" + "F" * 5 + "P")
        gh = FakeGh(runs, attempt=2, attempts={1: "failure"})
        texts = self.texts(gh, 7, run_attempt=1)
        self.assertEqual(len(texts), 1)
        self.assertIn("has failed 6 runs in a row", texts[0])

    def test_triggering_attempt_log_and_link_are_its_own(self):
        # Run 7's attempt 1 failed to build a dependency and is due to alert.
        # By the time its job runs, attempt 2 failed on a 403 and attempt 3
        # has started, so gh's default log is a newer attempt's.
        runs = timeline("P" + "F" * 6)
        blocked = log_lines("##[error]HTTP Error 403 Forbidden")
        gh = FakeGh(runs, in_progress={7: {1: "failure", 2: "failure"}}, log=blocked,
                    logs={1: (FIXTURES / "pyicu_build_failure.log").read_text()})
        sent, out = self.run_main(gh, 7, run_attempt=1)
        self.assertEqual(len(sent), 1)
        self.assertIn("--- alert [dependency-build] ---", out)
        self.assertIn("error: Failed to build `pyicu==2.15.2`", sent[0][0])
        self.assertNotIn("403", sent[0][0])
        self.assertTrue(sent[0][0].endswith("Run: https://example.test/runs/7/attempts/1"))

    def test_fork_runs_in_the_history_do_not_extend_a_streak(self):
        # Runs 2 to 6 are a fork's failing pull requests from its own main;
        # counted, they would make run 7 the end of a 5-hour streak.
        runs = timeline("P" + "F" * 6)
        self.assertEqual(len(self.texts(FakeGh(runs), 7)), 1)
        self.assertEqual(self.texts(FakeGh(runs, fork_ids=range(2, 7)), 7), [])

    def test_run_missing_from_the_listing_is_added(self):
        runs = self.ALERTING
        sent, out = self.run_main(FakeGh(runs, listed=runs[1:]), 3)
        self.assertEqual(len(sent), 1)
        self.assertIn("not in the run listing yet", out)

    def test_run_missing_from_a_long_listing_is_found_on_the_first_page(self):
        # The newest of 6 hourly failures isn't listed yet, and 1,500
        # passes come before them: one page answers it, with no "+".
        runs = timeline("P" * 1500 + "F" * 6)
        calls = []
        gh = FakeGh(runs, listed=runs[1:])

        def counting(*a):
            if "workflows/" in a[-1]:
                calls.append(a)
            return gh(*a)

        sent, _ = self.run_main(counting, runs[0].id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("has failed 6 runs in a row", sent[0][0])
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            _, truncated = ops_alert.fetch_runs("o/r", 42, "main", runs[0], gh=gh)
        self.assertFalse(truncated)

    def test_unreadable_log_still_alerts_as_unclear(self):
        texts = self.texts(FakeGh(timeline("P" + "F" * 6), log_fails=True), 7)
        self.assertTrue(texts[0].startswith("Check the run: "))

    def test_streak_past_every_page_shows_a_plus(self):
        # Run 1029 is at 16:15 UTC, so it is a reminder day's first due run.
        runs = timeline("F" * 1029)
        texts = self.texts(FakeGh(runs), 1029)
        self.assertEqual(len(texts), 1)
        self.assertIn(f"still failing, {ops_alert.MAX_PAGES * 100}+ runs in a row", texts[0])

    def test_failing_gh_api_call_fails_this_run(self):
        # A red ops_alert run is the visible sign that alerting is broken.
        def gh(*args):
            raise subprocess.CalledProcessError(1, "gh")

        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(subprocess.CalledProcessError):
                ops_alert.main(["--run-id", "7", "--repo", "o/r"], gh=gh, env=self.ENV)

    def test_history_spanning_pages_finds_the_streak_start(self):
        # 150 hourly failures: the streak's start and the success before it
        # are on page 2, so there must be no "+" and no re-alert.
        runs = timeline("P" + "F" * 150)
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            got, truncated = ops_alert.fetch_runs("o/r", 42, "main", runs[0], gh=FakeGh(runs))
        self.assertFalse(truncated)
        self.assertEqual(len(got), 151)

    def test_recovery_after_a_streak_spanning_pages_counts_it_all(self):
        runs = timeline("P" + "F" * 150 + "P")
        calls = []
        gh = FakeGh(runs)

        def counting(*a):
            if "workflows/" in a[-1]:
                calls.append(a)
            return gh(*a)

        texts = self.texts(counting, runs[0].id)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(texts), 1)
        self.assertIn("is passing again after 150 failed runs (", texts[0])

    def test_short_history_takes_one_call(self):
        runs = timeline("F" * 30)
        calls = []
        gh = FakeGh(runs)
        got, truncated = ops_alert.fetch_runs(
            "o/r", 42, "main", runs[0], gh=lambda *a: calls.append(a) or gh(*a)
        )
        self.assertEqual(len(calls), 1)
        self.assertFalse(truncated)
        self.assertEqual(len(got), 30)

    def test_fetch_stops_at_the_first_page_with_a_success_past_the_streak(self):
        runs = timeline("P" * 300 + "F" * 20)
        calls = []
        gh = FakeGh(runs)
        got, _ = ops_alert.fetch_runs(
            "o/r", 42, "main", runs[0], gh=lambda *a: calls.append(a) or gh(*a)
        )
        self.assertEqual(len(calls), 1)

    def test_streak_longer_than_every_page_is_truncated(self):
        runs = timeline("F" * (ops_alert.MAX_PAGES * 100 + 5))
        got, truncated = ops_alert.fetch_runs("o/r", 42, "main", runs[0], gh=FakeGh(runs))
        self.assertTrue(truncated)
        self.assertEqual(len(got), ops_alert.MAX_PAGES * 100)


class TestWorkflow(unittest.TestCase):
    # A line-based read, so this runs with plain python3 and no PyYAML.
    text = (WORKFLOWS / "ops_alert.yml").read_text()

    def test_watched_names_match_real_workflows(self):
        watched = re.search(r"^    workflows:\n((?:      - .+\n)+)", self.text, re.M)
        self.assertIsNotNone(watched)
        watched = re.findall(r"- (\S+)", watched.group(1))
        names = set()
        for path in WORKFLOWS.glob("*.yml"):
            m = re.search(r"^name:\s*(\S+)\s*$", path.read_text(), re.M)
            if m:
                names.add(m.group(1))
        self.assertTrue(watched)
        for name in watched:
            self.assertIn(name, names)

    def test_every_scheduled_importer_is_watched(self):
        watched = re.search(r"^    workflows:\n((?:      - .+\n)+)", self.text, re.M)
        watched = set(re.findall(r"- (\S+)", watched.group(1)))
        importers = set()
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text()
            m = re.search(r"^name:\s*(import_\S+)\s*$", text, re.M)
            if m and re.search(r"^\s+schedule:", text, re.M):
                importers.add(m.group(1))
        self.assertTrue(importers)
        self.assertLessEqual(importers, watched)


if __name__ == "__main__":
    unittest.main()
