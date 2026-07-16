#!/usr/bin/env python3
"""Unit tests for the /reject comment parser. Run directly:
python3 .github/scripts/test_keydates_reject.py"""
import unittest

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


if __name__ == "__main__":
    unittest.main()
