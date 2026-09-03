#!/usr/bin/env python3
"""Turn a sweep report into the ``skip_tests`` groups of the quack test config.

``run_duckdb_tests.py --no-skip --report sweep.json`` records, for every DuckDB test that
fails over a quack connection, the failure block unittest printed for it. This script sorts
those tests into causes and rewrites ``test/configs/quack_client_server.json`` accordingly,
so the skip list stays a description of what is broken today rather than a frozen snapshot.

Usage:
    scripts/run_duckdb_tests.py --no-skip --report sweep.json
    scripts/classify_duckdb_tests.py sweep.json            # show the grouping
    scripts/classify_duckdb_tests.py sweep.json --write    # and write it to the config
    scripts/classify_duckdb_tests.py sweep.json --show unclassified   # inspect one group
"""

import argparse
import collections
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "test", "configs", "quack_client_server.json")

SEPARATOR = "=" * 80
HEADER = re.compile(r"^\d+\. test/\S+?:\d+$")
# "Binder Error: ...", "Invalid Input Error: ...", "Conversion Error: ..."
ERROR = re.compile(r"^((?:\w+ )*?\w*Error): (.*)$", re.M)


def parse(block):
    """(headline, sql, actual) from one unittest failure block."""
    lines = block.split("\n")
    if not lines or not HEADER.match(lines[0]):
        return "", "", block  # a crashed/hung chunk, recorded whole
    sections = []
    current = []
    for line in lines[1:]:
        if line == SEPARATOR:
            sections.append("\n".join(current).strip())
            current = []
        else:
            current.append(line)
    sections.append("\n".join(current).strip())
    # The header line is immediately followed by a separator, so the first section is empty.
    while sections and not sections[0]:
        sections.pop(0)
    headline = sections[0] if sections else ""
    sql = sections[1] if len(sections) > 1 else ""
    # A result mismatch labels the two results; a statement that simply failed prints only the
    # error, as the last section after the SQL.
    actual = sections[-1] if len(sections) > 2 else ""
    for index, section in enumerate(sections):
        if section.startswith("Actual result:") and index + 1 < len(sections):
            actual = sections[index + 1]
    return headline, sql, actual


MISMATCH = "Query failed, but error message did not match expected error message: "
REGEX_PREFIX = "<REGEX>:"
ERROR_TYPE = re.compile(r"((?:\w+ )*?\w*Error)")


def error_of(text):
    """(exception type, message) if the text starts with a DuckDB error, else (None, text)."""
    match = ERROR.match(text or "")
    return (match.group(1), match.group(2)) if match else (None, text or "")


def expected_error(headline):
    """The error text the test asserted on, from a message-mismatch headline."""
    if not headline.startswith(MISMATCH):
        return None
    # unittest appends " (<test file>:<line>)!" to the headline.
    return re.sub(r"\s*\(test/\S+?:\d+\)!?$", "", headline[len(MISMATCH):]).strip()


def only_the_type_differs(headline, actual):
    """True when the test would have passed had the exception type crossed the wire.

    That is the whole of the wire's error handling today: ErrorResponse carries
    error.RawMessage() and nothing else, so the client rebuilds every remote error as
    INVALID_INPUT. Tests that name an exception type - most name only the type, as in
    "statement error / Binder Error" - fail on a message that is otherwise exactly right.

    So rather than compare the two texts, put the type the test asked for back onto the
    error that came out and re-apply the test's own assertion: a regex search for a
    <REGEX>: expectation, a substring match otherwise. If that passes, the type is all
    that was wrong.
    """
    expected = expected_error(headline)
    actual_type, message = error_of(actual)
    if not expected or not actual_type:
        return False
    is_regex = expected.startswith(REGEX_PREFIX)
    if is_regex:
        expected = expected[len(REGEX_PREFIX):]
    type_match = ERROR_TYPE.search(expected)
    if not type_match or type_match.group(1) == actual_type:
        return False
    repaired = f"{type_match.group(1)}: {message}"
    if is_regex:
        try:
            return bool(re.search(expected, repaired, re.DOTALL))
        except re.error:
            return False
    return expected in repaired


def says(pattern):
    return lambda headline, sql, actual: bool(re.search(pattern, actual))


# Ordered most specific first; the first rule whose predicate matches owns the test. Rules that
# key on the error a statement actually raised come before rules that key on the shape of the
# statement, so a test is filed under the thing that broke rather than the thing it was doing:
# an EXPLAIN that dies on the qualified-name parser bug belongs in the parser group. Each entry
# is (key, the reason written into the config, predicate over the parsed failure block).
RULES = [
    ("on_init",
     "The test changes the instance in a way the config's own on_init cannot survive (memory "
     "limit, threads, extension settings), so the startup queries fail rather than the test",
     says(r"Startup queries provided via on_init failed")),

    ("on_cleanup",
     "The test leaves the connection somewhere the config's on_cleanup cannot run from, so the "
     "clean-up routine fails rather than the test",
     says(r"Error while running clean-up routine")),

    ("crash_or_hang",
     "Aborts or hangs the unittest process, so it takes the whole run down rather than failing "
     "on its own. Order-dependent: these pass when run alone",
     lambda headline, sql, actual: not headline),

    ("error_type_lost",
     "Only the error message crosses the wire, not the exception type (ErrorResponse serializes "
     "error.RawMessage() only), so the right error arrives as an 'Invalid Input Error'",
     lambda headline, sql, actual: only_the_type_differs(headline, actual)),

    ("parser_error_dot",
     "Client-side 'syntax error at or near \".\"' - a qualified name is rendered with an empty "
     "component during the catalog reload that follows ALTER + DROP. The most common case of "
     "the lossy deparse below, and counted separately because the trigger is understood",
     says(r'syntax error at or near "\."')),

    ("query_roundtrip",
     "The client re-serializes the parsed statement back to SQL text before sending it, and the "
     "deparse is lossy: lambdas come out as the deprecated -> arrow, NULL::TYPE loses its cast, "
     "and literals of extension types are emitted unquoted, so the server rejects the text",
     says(r"Deprecated lambda arrow|syntax error at or near"
          r"|ORDER BY non-integer literal has no effect"
          r"|Struct remap can only remap nested types"
          r"|Could not choose a best candidate function")),

    ("duplicate_columns",
     "A remote query whose result has duplicate column names cannot be bound: "
     'table "quack_query_by_name" has duplicate column name',
     says(r"has duplicate column name")),

    ("attach_bind_failure",
     "ATTACH fails outright: a remote table cannot be bound while the client builds the catalog "
     "(Failed to bind remote table while attaching quack catalog)",
     says(r"Failed to bind remote table while attaching")),

    ("prepared_parameters",
     "Prepared statement parameters do not reach the server: 'Values were not provided for the "
     "following parameters'",
     says(r"Values were not provided for the following parameters")),

    ("superseded",
     "The client abandons an in-flight request: 'superseded by a new query'",
     says(r"superseded by a new query")),

    ("transactions",
     "Transaction semantics differ over the wire - most of these are 'cannot start a transaction "
     "within a transaction', the rest aborted-transaction state and COMMIT/ROLLBACK with no "
     "active transaction",
     says(r"(?i)\b(cannot (start|commit|rollback)|transaction is aborted|no transaction is "
          r"active|transaction within a transaction)\b")),

    ("constraints",
     "Constraints behave differently over the wire: duplicate key / PRIMARY KEY / UNIQUE / "
     "foreign key violations that the local catalog does not raise",
     says(r"(?i)(violates (primary key|unique) constraint|PRIMARY KEY or UNIQUE constraint "
          r"violation|can have only one primary key|Failed to create foreign key|Conflict target "
          r"has to be provided)")),

    ("unimplemented",
     "Feature not implemented in the quack storage extension yet",
     says(r"not (implemented|supported)( yet| for this table type)?"
          r"|is only implemented for DuckDB tables")),

    ("catalog_objects_invisible",
     "The quack client catalog only exposes tables and views: sequences, types, indexes, macros "
     "and functions created through the connection are not visible to the client binder",
     says(r"(?i)(sequence|type|index|macro|(scalar|table|aggregate) function) with name "
          r"\S+ (does not exist|already exists)")),

    ("config_conflict",
     "The test collides with the config's own on_init rather than with quack itself: it changes "
     "secret manager settings, or re-runs on_init against a server that is already serving",
     says(r"Changing Secret Manager settings|Server already exists for quack:"
          r"|Schema with name \"main\" already exists")),

    ("unexpected_success",
     "A statement expected to fail succeeds over the quack connection",
     lambda headline, sql, actual: headline.startswith("Query unexpectedly succeeded")),

    ("error_text_differs",
     "The remote error text differs from what the test expects, beyond the exception type",
     lambda headline, sql, actual: headline.startswith(MISMATCH)),

    ("remote_error",
     "A statement that should succeed fails with a remote error not yet grouped further",
     lambda headline, sql, actual: error_of(actual)[0] is not None),

    # From here on the statement ran; only the result is wrong, so the shape of the query is
    # what identifies the cause.
    ("explain",
     "EXPLAIN / plan inspection: every base table lives behind a remote quack scan, so the local "
     "plan is replaced by a single 'Quack Query By Name' node and the expected operators never "
     "appear",
     lambda headline, sql, actual: bool(re.match(r"(?is)\s*EXPLAIN\b", sql))
     or "QUACK_QUERY_BY_NAME" in actual or "Quack Query By Name" in actual),

    ("catalog_metadata",
     "Result differs: catalog and metadata queries (SHOW, duckdb_* tables, information_schema, "
     "pg_catalog, comments) describe the quack catalog rather than a local DuckDB one",
     lambda headline, sql, actual: bool(re.search(
         r"(?i)\b(duckdb_\w+|information_schema|pg_\w+|sqlite_\w+|pragma_\w+"
         r"|current_(database|schema|schemas))\b", sql)
         or re.match(r"(?is)\s*(PRAGMA|SHOW|DESCRIBE|SUMMARIZE|CALL)\b", sql))),

    ("wrong_result",
     "Returns a different result over a quack connection, for a reason not yet grouped",
     lambda headline, sql, actual: True),
]


def classify(report):
    """{rule key: sorted test paths} for every test in the report."""
    groups = collections.defaultdict(list)
    for test, block in report.items():
        headline, sql, actual = parse(block)
        for key, _, matches in RULES:
            if matches(headline, sql, actual):
                groups[key].append(test)
                break
    return {key: sorted(tests) for key, tests in groups.items()}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", help="JSON written by run_duckdb_tests.py --report")
    parser.add_argument("--write", action="store_true",
                        help="rewrite skip_tests in the config with the grouping")
    parser.add_argument("--show", help="print the failing SQL and error of every test in a group")
    args = parser.parse_args()

    with open(args.report) as f:
        report = json.load(f)
    groups = classify(report)

    if args.show:
        if args.show not in groups:
            sys.exit(f"no group {args.show!r}; have {', '.join(sorted(groups))}")
        for test in groups[args.show]:
            headline, sql, actual = parse(report[test])
            print(f"--- {test}\n    {headline.splitlines()[0] if headline else '(process died)'}"
                  f"\n    sql:    {sql.splitlines()[0][:160] if sql else ''}"
                  f"\n    actual: {actual.splitlines()[0][:160] if actual else ''}")
        return 0

    order = [key for key, _, _ in RULES if key in groups]
    for key in order:
        print(f"{len(groups[key]):5d}  {key}")
    print(f"{sum(len(groups[key]) for key in order):5d}  total")

    if args.write:
        with open(CONFIG) as f:
            config = json.load(f)
        reasons = {key: reason for key, reason, _ in RULES}
        config["skip_tests"] = [{"reason": reasons[key], "paths": groups[key]} for key in order]
        with open(CONFIG, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        print(f"wrote {len(config['skip_tests'])} groups to {CONFIG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
