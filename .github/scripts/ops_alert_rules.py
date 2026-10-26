"""What a failed run's log means, for the ops channel alert in ops_alert.py.

Each rule is a regex over the failing step's log, a verdict, and a short note
that says what happened and what to do next. To teach the alert a new kind of
failure, add a Rule here and a fixture log under fixtures/ops_alert/ with a
test in test_ops_alert.py. Every matching rule is listed in the alert; the most
severe verdict among them leads it. A log no rule matches is UNCLEAR.
"""
import dataclasses
import enum
import re


class Verdict(enum.IntEnum):
    # Ordered by severity, so sorting the matches puts the leading verdict first.
    NO_ACTION = 0
    UNCLEAR = 1
    ACTION = 2


VERDICT_LABELS = {
    Verdict.ACTION: "Action needed",
    Verdict.UNCLEAR: "Check the run",
    Verdict.NO_ACTION: "Probably no action needed",
}


@dataclasses.dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern
    verdict: Verdict
    advice: str


RULES = [
    Rule(
        id="dependency-build",
        pattern=re.compile(r"Failed to build `|maturin failed|Failed building wheel"),
        verdict=Verdict.ACTION,
        advice="A dependency failed to build, probably because the runner "
        "image, Python, or a system library changed. It won't fix itself: "
        "update the pin for the package in the log line.",
    ),
    Rule(
        id="data-invalid",
        # The importer workflows print these when format.py or materialize.py
        # reject the imported data; see import_fancons.yml.
        pattern=re.compile(
            r"tools/format\.py failed on the imported data"
            r"|Imported data failed tools/materialize\.py"
        ),
        verdict=Verdict.ACTION,
        advice="The imported data failed the format or schema check, so "
        "nothing was committed and this import is held back. Read the run's "
        "errors to find the bad record.",
    ),
    Rule(
        id="source-blocked",
        pattern=re.compile(r"\b403 Forbidden\b|\b429 Too Many Requests\b"),
        verdict=Verdict.ACTION,
        advice="A source site returned 403 or 429 and may be blocking the "
        "importer. Don't rerun the importer by hand, because more requests "
        "can make a block permanent. Ask the site owner before you retry.",
    ),
    Rule(
        id="source-down",
        pattern=re.compile(
            r"\b5\d\d (?:Server Error|Internal Server Error|Bad Gateway"
            r"|Service Unavailable|Gateway Timeout)\b"
            r"|httpx\.(?:ReadTimeout|ConnectTimeout|ConnectError|RemoteProtocolError)"
        ),
        verdict=Verdict.NO_ACTION,
        advice="A source site timed out or returned a server error. The "
        "importer retries on its schedule. If tomorrow's reminder still shows "
        "this, check whether the site is up.",
    ),
    Rule(
        id="push-race",
        pattern=re.compile(r"\[rejected\].*\(fetch first\)|\(non-fast-forward\)"),
        verdict=Verdict.NO_ACTION,
        advice="Another job pushed to main first, so this run's commit was "
        "rejected. The next run retries.",
    ),
]
