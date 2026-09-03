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


# Ordered most specific first; the first rule whose test matches owns the test. Each entry is
# (key, reason written into the config, predicate over the parsed failure block).
RULES = [
    ("crash_or_hang",
     "Aborts or hangs the unittest process, so it takes the whole run down rather than failing "
     "on its own",
     lambda headline, sql, actual: not headline),

    ("parser_error_dot",
     'Client-side \'Parser Error: syntax error at or near "."\' - a qualified name is rendered '
     "with an empty component during the catalog reload that follows ALTER + DROP",
     lambda headline, sql, actual: 'syntax error at or near "."' in actual),

    ("duplicate_columns",
     "A remote query whose result has duplicate column names cannot be bound: "
     'table "quack_query_by_name" has duplicate column name',
     lambda headline, sql, actual: "has duplicate column name" in actual),

    ("unimplemented",
     "Feature not implemented in the quack storage extension yet",
     lambda headline, sql, actual: re.search(
         r"not (implemented|supported)( yet)?|is only implemented for DuckDB tables", actual)),

    ("explain",
     "EXPLAIN / plan inspection: every base table lives behind a remote quack scan, so the local "
     "plan is replaced by a single 'Quack Query By Name' node and the expected operators never "
     "appear",
     lambda headline, sql, actual: re.match(r"(?is)\s*EXPLAIN\b", sql)
     or "QUACK_QUERY_BY_NAME" in actual or "Quack Query By Name" in actual),

    ("error_type_lost",
     "Only the error message crosses the wire, not the exception type (ErrorResponse serializes "
     "error.RawMessage() only), so remote errors arrive as 'Invalid Input Error'",
     lambda headline, sql, actual: headline.startswith("Query failed, but error message did not "
                                                       "match")
     and actual.startswith("Invalid Input Error")),

    ("unexpected_success",
     "A statement expected to fail succeeds over the quack connection",
     lambda headline, sql, actual: headline.startswith("Query unexpectedly succeeded")),

    ("transactions",
     "Transaction semantics differ over the wire (nested BEGIN, aborted-transaction state and "
     "ROLLBACK without an active transaction behave differently on the remote side)",
     lambda headline, sql, actual: re.search(
         r"(?i)\btransaction\b", actual) or re.match(
         r"(?is)\s*(BEGIN|COMMIT|ROLLBACK|START TRANSACTION|ABORT)\b", sql)),

    ("catalog_metadata",
     "Result differs: catalog/metadata queries (SHOW, duckdb_* tables, information_schema, "
     "comments) describe the quack catalog rather than a local DuckDB catalog",
     lambda headline, sql, actual: re.search(
         r"(?i)\b(show|describe|summarize|duckdb_\w+|information_schema|pg_catalog|"
         r"current_(database|schema|schemas)|pragma_\w+)\b", sql)
     or re.match(r"(?is)\s*(PRAGMA|SHOW|DESCRIBE|SUMMARIZE|CALL)\b", sql)),

    ("catalog_objects_invisible",
     "The quack client catalog only exposes tables and views: sequences, types, indexes and "
     "user-defined functions created through the connection are not visible to the client binder",
     lambda headline, sql, actual: re.search(
         r"(?i)(sequence|type|index|macro|function) with name \S+ does not exist", actual)
     or re.match(r"(?is)\s*(CREATE|DROP|ALTER)\s+(OR REPLACE\s+)?"
                 r"(TEMP\w*\s+)?(UNIQUE\s+)?(SEQUENCE|TYPE|INDEX|MACRO|FUNCTION)\b", sql)),

    ("error_text_differs",
     "The remote error text differs from what the test expects",
     lambda headline, sql, actual: headline.startswith("Query failed, but error message did not "
                                                       "match")),

    ("remote_error",
     "Fails over a quack connection with a remote error not yet grouped further",
     lambda headline, sql, actual: bool(ERROR.match(actual))),

    ("wrong_result",
     "Fails over a quack connection with a wrong result, for a reason not yet grouped",
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
