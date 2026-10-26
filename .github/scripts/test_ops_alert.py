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

    def test_rerun_attempts_never_alert_or_remind(self):
        runs = timeline("P" + "F" * 72)
        self.assertEqual(decide(runs, 7).kind, Kind.ALERT)
        self.assertIsNone(decide(runs, 7, attempt=2))
        self.assertEqual(decide(runs, 21).kind, Kind.REMIND)
        self.assertIsNone(decide(runs, 21, attempt=2))

    def test_rerun_that_passes_recovers_the_streak_it_ended(self):
        # Run 7's first attempt failed, which made the streak 5 hours long
        # and alerted. Its re-run passed, so the streak is over.
        runs = timeline("P" + "F" * 5 + "P")
        self.assertIsNone(decide(runs, 7))
        event = decide(runs, 7, attempt=2)
        self.assertEqual(event.kind, Kind.RECOVERED)
        self.assertEqual(len(event.streak), 6)
        # A re-run that passes after a streak too short to alert stays quiet.
        self.assertIsNone(decide(timeline("PFP"), 3, attempt=2))


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
        # The log ends with the step's exit-code line, which says nothing.
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
            "Log (quoted from the run, may include text from source sites): "
            "error: Failed to build `pyicu==2.15.2`\n"
            "https://example.test/runs/7",
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
            "Log (quoted from the run, may include text from source sites): "
            "httpx.ReadTimeout: The read operation timed out\n"
            "https://example.test/runs/21",
        )

    def test_recovery_message_and_truncated_count(self):
        runs = timeline("P" + "F" * 7 + "P")
        text = ops_alert.format_message("import_rams", decide(runs, 9), truncated=True)
        self.assertEqual(
            text,
            "No action needed: import_rams is passing again after 7+ failed runs "
            "(2026-10-24 21:15 to 2026-10-25 03:15 UTC).\n"
            "https://example.test/runs/9",
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

    def __init__(self, runs, log="", head_branch="main", log_fails=False,
                 event="schedule", head_repo="o/r", attempt=1, listed=None):
        self.runs, self.log, self.head_branch, self.log_fails = runs, log, head_branch, log_fails
        self.event, self.head_repo, self.attempt = event, head_repo, attempt
        # What the runs listing returns, if it lags behind self.runs.
        self.listed = runs if listed is None else listed

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
                **api_run(r),
                "name": "import_fancons", "workflow_id": 42, "event": self.event,
                "head_branch": self.head_branch, "run_attempt": self.attempt,
                "head_repository": {"full_name": self.head_repo},
                "repository": {"full_name": "o/r"},
            })
        page = int(path.rsplit("page=", 1)[1])
        batch = self.listed[(page - 1) * 100 : page * 100]
        return json.dumps({"workflow_runs": [api_run(r) for r in batch]})


class TestMain(unittest.TestCase):
    ENV = {"OPS_TOKEN": "tok-123", "OPS_CHAT": "chat-456"}

    def run_main(self, gh, run_id, dry_run=False):
        """Returns the (text, token, chat) of each send, and stdout."""
        sent = []
        argv = ["--run-id", str(run_id), "--repo", "o/r"] + (["--dry-run"] if dry_run else [])
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = ops_alert.main(
                argv, gh=gh, send=lambda text, tok, chat: sent.append((text, tok, chat)),
                env=self.ENV,
            )
        self.assertEqual(code, 0)
        return sent, out.getvalue()

    def texts(self, gh, run_id):
        return [text for text, _, _ in self.run_main(gh, run_id)[0]]

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
        # Same repo, but not a scheduled or dispatched run.
        self.assertEqual(self.texts(FakeGh(self.ALERTING, event="push"), 3), [])

    def test_scheduled_and_dispatched_runs_from_this_repo_alert(self):
        for event in ("schedule", "workflow_dispatch"):
            with self.subTest(event=event):
                self.assertEqual(len(self.texts(FakeGh(self.ALERTING, event=event), 3)), 1)

    def test_rerun_attempt_does_not_realert(self):
        self.assertEqual(self.texts(FakeGh(self.ALERTING, attempt=2), 3), [])

    def test_rerun_that_passes_posts_recovery(self):
        texts = self.texts(FakeGh(timeline("P" + "F" * 5 + "P"), attempt=2), 7)
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("No action needed: import_fancons is passing again"))

    def test_run_missing_from_the_listing_is_added(self):
        runs = self.ALERTING
        sent, out = self.run_main(FakeGh(runs, listed=runs[1:]), 3)
        self.assertEqual(len(sent), 1)
        self.assertIn("not in the run listing yet", out)

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

    def test_step_env_has_what_the_script_reads(self):
        for var in ("OPS_TOKEN", "OPS_CHAT", "RUN_ID", "DRY_RUN"):
            self.assertRegex(self.text, rf"(?m)^ +{var}: \S")


if __name__ == "__main__":
    unittest.main()
