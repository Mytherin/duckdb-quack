#!/usr/bin/env python3
"""Run DuckDB's own test suite over a quack client/server connection.

Drives ``test/unittest`` with ``test/configs/quack_client_server.json`` against the
tests in the ``duckdb`` submodule.

The tests are run as a series of short-lived unittest processes rather than one long
one. That is a workaround, not a preference: a DuckDB instance that runs a quack
server is kept alive by its own server-side connections (the server holds a
``duckdb::Connection``, which holds a ``shared_ptr<DatabaseInstance>``), so an
instance abandoned by ``restart``/``load`` is never reclaimed. Over a few thousand
tests the accumulated instances first eat the process thread budget and then leave
the server unable to answer at all ("Server returned nothing"). Chunking bounds
that; a single process reliably gets through a few hundred tests.

Usage:
    scripts/run_duckdb_tests.py                 # fast tests, release build
    scripts/run_duckdb_tests.py --slow          # include .test_slow
    scripts/run_duckdb_tests.py --jobs 4        # parallel workers (see --help caveat)
    scripts/run_duckdb_tests.py --build debug
    scripts/run_duckdb_tests.py test/sql/join   # only tests under that path
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "test", "configs", "quack_client_server.json")
BASE_PORT = 9494

SUMMARY = re.compile(r"^test cases:\s+(\d+)\s+\|(.*)$", re.M)
COUNT = re.compile(r"(\d+) (passed|failed|skipped)")
# Catch prints this instead of the table when nothing failed.
ALL_PASSED = re.compile(
    r"^All tests passed \((?:(\d+) skipped tests?, )?\d+ assertions? in (\d+) test cases?\)$", re.M)

print_lock = threading.Lock()


def collect_tests(include_slow):
    """All sqllogictest files under duckdb/test, as paths relative to duckdb/."""
    tests = []
    base = os.path.join(ROOT, "duckdb")
    for dirpath, _, filenames in os.walk(os.path.join(base, "test")):
        for name in filenames:
            if name.endswith(".test") or (include_slow and name.endswith(".test_slow")):
                tests.append(os.path.relpath(os.path.join(dirpath, name), base))
    return sorted(tests)


def worker_config(port, tmpdir):
    """The shared config with the quack port rewritten, so workers do not collide."""
    if port == BASE_PORT:
        return CONFIG
    with open(CONFIG) as f:
        config = json.load(f)
    for key in ("on_init", "on_cleanup"):
        config[key] = config[key].replace(str(BASE_PORT), str(port))
    path = os.path.join(tmpdir, f"quack_client_server_{port}.json")
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    return path


def tally(output):
    """(cases, passed, failed, skipped) from unittest's own summary lines."""
    cases = passed = failed = skipped = 0
    for match in ALL_PASSED.finditer(output):
        cases += int(match.group(2))
        passed += int(match.group(2))
        skipped += int(match.group(1) or 0)
    for match in SUMMARY.finditer(output):
        cases += int(match.group(1))
        for value, kind in COUNT.findall(match.group(2)):
            if kind == "passed":
                passed += int(value)
            elif kind == "failed":
                failed += int(value)
            else:
                skipped += int(value)
    return cases, passed, failed, skipped


# The quack server inside a long-lived process eventually stops answering (see the module
# docstring); when that happens every remaining test in the chunk fails in on_init with this.
DEGRADED = "Startup queries provided via on_init failed: IO Error"


def run_chunk(worker, tests, unittest, config, tmpdir, env, label, per_test):
    """Run one chunk in a fresh process; returns (counts, output, died)."""
    listing = os.path.join(tmpdir, f"chunk_{worker}_{label}.txt")
    with open(listing, "w") as f:
        f.write("\n".join(tests) + "\n")
    # A quack client can block forever on a request the server never dispatches, so no chunk is
    # allowed to run unbounded. The budget scales with the chunk so a hang isolated down to a
    # single test costs ~2 minutes rather than the full chunk budget.
    timeout = 120 + per_test * len(tests)
    try:
        process = subprocess.run(
            [unittest, "--test-config", config, "--test-dir", "duckdb", "-f", listing],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        output = (expired.stdout or "") + (expired.stderr or "")
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return tally(output), output + f"\n*** chunk timed out after {timeout}s ***\n", True
    output = process.stdout + process.stderr
    return tally(output), output, process.returncode not in (0, 1)


def run_worker(worker, tests, unittest, config, tmpdir, chunk_size, per_test, totals,
               failures):
    """Run one worker's tests, chunk_size at a time, in fresh processes.

    A chunk whose process dies, or which degrades into on_init failures, is split in
    half and retried, down to a single test - so one bad test costs only itself
    instead of everything queued behind it.
    """
    env = dict(os.environ)
    # Keep workers off each other's scratch space.
    env["DUCKDB_TEST_TEMP_DIR_ROOT"] = f"duckdb_unittest_tempdir/quack_w{worker}"

    def run(chunk, first, label):
        counts, output, died = run_chunk(worker, chunk, unittest, config, tmpdir, env,
                                         label, per_test)
        if (died or DEGRADED in output) and len(chunk) > 1:
            half = len(chunk) // 2
            run(chunk[:half], first, label + "a")
            run(chunk[half:], first + half, label + "b")
            return
        with print_lock:
            for i in range(4):
                totals[i] += counts[i]
            if counts[2] or died:
                failures.append((worker, first, output))
            span = f"{first}" if len(chunk) == 1 else f"{first}-{first + len(chunk) - 1}"
            print(f"worker {worker}: tests {span}: "
                  f"{counts[1]} passed, {counts[2]} failed, {counts[3]} skipped"
                  + ("  (process died - see below)" if died else ""), flush=True)

    for index in range(0, len(tests), chunk_size):
        run(tests[index:index + chunk_size], index, str(index))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build", default="release", help="build directory under build/")
    parser.add_argument("--slow", action="store_true", help="also run .test_slow files")
    parser.add_argument("--chunk-size", type=int, default=200,
                        help="tests per unittest process (default 200)")
    parser.add_argument("--jobs", type=int, default=1,
                        help="parallel workers, each with its own quack port. Faster, but all "
                             "workers share the duckdb working directory, so tests that write "
                             "next to their input data can collide and report false failures - "
                             "re-check anything that fails with --jobs 1")
    parser.add_argument("--timeout-per-test", type=int, default=None,
                        help="seconds of chunk timeout budget per test (default 4, or 30 with "
                             "--slow); a chunk that overruns is split and retried")
    parser.add_argument("filter", nargs="?",
                        help="only run tests whose path contains this substring")
    args = parser.parse_args()

    unittest = os.path.join(ROOT, "build", args.build, "test", "unittest")
    if not os.path.exists(unittest):
        sys.exit(f"{unittest} not found - run `make {args.build}` first")

    per_test = args.timeout_per_test if args.timeout_per_test else (30 if args.slow else 4)
    tests = collect_tests(args.slow)
    if args.filter:
        tests = [t for t in tests if args.filter in t]
    if not tests:
        sys.exit("no tests matched")
    print(f"running {len(tests)} DuckDB tests over quack: "
          f"{args.chunk_size} per process, {args.jobs} worker(s)", flush=True)

    totals = [0, 0, 0, 0]
    failures = []
    with tempfile.TemporaryDirectory() as tmpdir:
        threads = []
        for worker in range(args.jobs):
            # Round-robin so every worker gets a mix of fast and slow areas.
            thread = threading.Thread(
                target=run_worker,
                args=(worker, tests[worker::args.jobs], unittest,
                      worker_config(BASE_PORT + worker, tmpdir), tmpdir,
                      args.chunk_size, per_test, totals, failures))
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()

    print(f"\ntotal: {totals[0]} cases, {totals[1]} passed, "
          f"{totals[2]} failed, {totals[3]} skipped")
    for worker, index, output in sorted(failures):
        print(f"\n===== worker {worker}, tests from {index} =====")
        print(output[-8000:])
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
