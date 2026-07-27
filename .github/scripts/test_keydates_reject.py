#!/usr/bin/env python3
"""Unit tests for the /reject comment parser and record loop. Run directly:
python3 .github/scripts/test_keydates_reject.py"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import types
import unittest
import unittest.mock

import keydates_reject
from keydates_reject import parse


class TestParse(unittest.TestCase):
    def test_valid_comment(self):
        fields, err = parse(
            "/reject anthrocon-2026 registration.closes 2026-06-26 — that's the pre-reg deadline"
        )
        self.assertIsNone(err)
        self.assertEqual(
            fields,
            ("anthrocon-2026", "registration", "closes", "2026-06-26",
             "that's the pre-reg deadline"),
        )

    def test_valid_without_reason(self):
        fields, err = parse("/reject anthrocon-2026 hotel.opens 2026-01-05")
        self.assertIsNone(err)
        self.assertEqual(fields[3], "2026-01-05")

    def test_overlong_day_is_rejected(self):
        # regression: 2026-07-125 used to parse as 2026-07-12 with the
        # stray "5" swallowed into the reason
        fields, err = parse(
            "/reject biggest-little-fur-con-2026 performances.opens 2026-07-125 "
            "— this date is already in place"
        )
        self.assertIsNone(fields)
        self.assertEqual(err, "parse-failure")

    def test_impossible_month(self):
        fields, err = parse("/reject anthrocon-2026 hotel.opens 2026-13-01 — nope")
        self.assertIsNone(fields)
        self.assertEqual(err, "invalid-date")

    def test_impossible_day(self):
        fields, err = parse("/reject anthrocon-2026 hotel.opens 2026-02-30 — nope")
        self.assertIsNone(fields)
        self.assertEqual(err, "invalid-date")

    def test_unknown_category(self):
        fields, err = parse("/reject anthrocon-2026 parking.opens 2026-06-26 — x")
        self.assertEqual(err, "parse-failure")

    def test_garbage(self):
        fields, err = parse("/reject please")
        self.assertEqual(err, "parse-failure")


class FakeGit:
    """Stands in for `subprocess.run(["git", ...])`. Models origin/main as an
    in-memory list: `reset --hard origin/main` rewrites the working file to it
    (optionally growing it first, to mimic another run landing between
    attempts); a successful `push` promotes the working file to origin/main."""

    def __init__(self, tmpfile, remote, push_rc, grow_on_reset=(), raise_first=None,
                 corrupt=False):
        self.tmpfile = tmpfile
        self.remote = list(remote)
        self.push_rc = list(push_rc)
        self.grow = list(grow_on_reset)
        self.reset_count = 0
        self.calls = []
        self.raise_first = raise_first   # subcommand to raise on, once
        self._raised = False
        self.corrupt = corrupt           # reset writes invalid JSON (bad file on main)

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        sub = argv[1] if len(argv) > 1 else ""
        if sub == self.raise_first and not self._raised:
            self._raised = True          # simulate one transient git failure
            raise subprocess.CalledProcessError(1, argv)
        rc = 0
        if sub == "reset":
            self.reset_count += 1
            if self.reset_count >= 2 and self.grow:   # a rival run landed
                self.remote.append(self.grow.pop(0))
            with open(self.tmpfile, "w") as f:
                if self.corrupt:
                    f.write("{ not valid json")
                else:
                    json.dump(self.remote, f)
        elif sub == "push":
            rc = self.push_rc.pop(0) if self.push_rc else 0
            if rc == 0:
                with open(self.tmpfile) as f:
                    self.remote = json.load(f)   # our append is now on main
        return types.SimpleNamespace(returncode=rc)

    def subs(self):
        return [c[1] for c in self.calls if len(c) > 1]


class TestRecord(unittest.TestCase):
    ENV = {
        "COMMENT_BODY": "/reject anthrocon-2026 registration.closes 2026-06-26 — pre-reg deadline",
        "COMMENT_USER": "sparkyfen",
        "COMMENT_CREATED_AT": "2026-07-27T00:00:00Z",
    }

    def _run(self, remote, push_rc, grow_on_reset=(), raise_first=None, corrupt=False):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as f:
            json.dump(remote, f)
        self.addCleanup(os.unlink, path)
        fake = FakeGit(path, remote, push_rc, grow_on_reset, raise_first, corrupt)
        out = io.StringIO()
        with unittest.mock.patch.object(keydates_reject, "REJECTIONS_FILE", path), \
             unittest.mock.patch.object(keydates_reject.subprocess, "run", fake), \
             unittest.mock.patch.object(keydates_reject.time, "sleep", lambda *_: None), \
             unittest.mock.patch.dict(os.environ, self.ENV, clear=True), \
             contextlib.redirect_stdout(out):
            rc = keydates_reject.main()
        with open(path) as f:
            try:
                final = json.load(f)       # None if the run left it corrupt
            except json.JSONDecodeError:
                final = None
        return rc, out.getvalue(), final, fake

    def test_happy_path_appends_and_pushes(self):
        rc, sentinel, final, fake = self._run(remote=[], push_rc=[0])
        self.assertEqual((rc, sentinel), (0, "ok"))
        self.assertEqual([(r["event_id"], r["category"], r["kind"], r["date"]) for r in final],
                         [("anthrocon-2026", "registration", "closes", "2026-06-26")])
        self.assertIn("push", fake.subs())

    def test_push_race_recomputes_against_fresh_main(self):
        # first push loses the race; a rival reject ("other") landed meanwhile.
        # The retry must re-read origin/main and re-append OURS on top of it —
        # not clobber the rival, not duplicate ourselves.
        other = {"event_id": "x-2026", "category": "hotel", "kind": "opens",
                 "date": "2026-01-01", "reason": "r", "by": "u", "at": "t"}
        rc, sentinel, final, fake = self._run(
            remote=[], push_rc=[1, 0], grow_on_reset=[other])
        self.assertEqual((rc, sentinel), (0, "ok"))
        keys = [(r["event_id"], r["category"], r["kind"], r["date"]) for r in final]
        self.assertIn(("x-2026", "hotel", "opens", "2026-01-01"), keys)          # rival preserved
        self.assertEqual(keys.count(("anthrocon-2026", "registration", "closes", "2026-06-26")), 1)  # ours once
        self.assertEqual(fake.subs().count("push"), 2)                            # retried

    def test_duplicate_after_resync_does_not_push(self):
        dup = {"event_id": "anthrocon-2026", "category": "registration", "kind": "closes",
               "date": "2026-06-26", "reason": "already", "by": "someone", "at": "earlier"}
        rc, sentinel, final, fake = self._run(remote=[dup], push_rc=[0])
        self.assertEqual((rc, sentinel), (0, "duplicate"))
        self.assertEqual(len(final), 1)                 # not re-appended
        self.assertNotIn("push", fake.subs())           # nothing pushed

    def test_retry_exhaustion_emits_sentinel_and_exits_zero(self):
        # every push loses the race: still return the sentinel + exit 0 so the
        # React step runs and posts feedback (exit 1 would silently drop it).
        rc, sentinel, final, fake = self._run(remote=[], push_rc=[1] * 25)
        self.assertEqual((rc, sentinel), (0, "push-failure"))
        self.assertEqual(fake.subs().count("push"), 25)

    def test_corrupt_rejections_file_fails_soft_not_silent(self):
        # a corrupt keydates_rejections.json on main makes json.load raise every
        # attempt; the loop must NOT crash silently — it emits push-failure and
        # exits 0 so the React step still posts a 👎 (the whole point of the fix).
        rc, sentinel, final, fake = self._run(remote=[], push_rc=[0], corrupt=True)
        self.assertEqual((rc, sentinel), (0, "push-failure"))
        self.assertNotIn("push", fake.subs())   # never got far enough to push

    def test_transient_git_error_recovers_on_later_attempt(self):
        # the first `fetch` blows up with a CalledProcessError; the loop must
        # catch it, back off, and still record the reject on a later attempt.
        rc, sentinel, final, fake = self._run(
            remote=[], push_rc=[0], raise_first="fetch")
        self.assertEqual((rc, sentinel), (0, "ok"))
        keys = [(r["event_id"], r["category"], r["kind"], r["date"]) for r in final]
        self.assertIn(("anthrocon-2026", "registration", "closes", "2026-06-26"), keys)


if __name__ == "__main__":
    unittest.main()
