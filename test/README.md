# Testing this extension
This directory contains all the tests for this extension. The `sql` directory holds tests that are written as [SQLLogicTests](https://duckdb.org/dev/sqllogictest/intro.html). DuckDB aims to have most its tests in this format as SQL statements, so for the quack extension, this should probably be the goal too.

The root makefile contains targets to build and run all of these tests. To run the SQLLogicTests:
```bash
make test
```
or 
```bash
make test_debug
```
## Running the regular DuckDB test suite over quack

`test/configs/quack_client_server.json` runs DuckDB's own sqllogictests through a quack
client/server connection instead of against a local DuckDB catalog:

* `on_init` starts a quack server inside the test instance (`quack:localhost:9494`), creates a
  secret for it and attaches back to it over TCP as `quack_remote`,
* `on_new_connection` does `USE quack_remote`, so every statement of every test is planned against
  the quack catalog and travels over the quack RPC protocol,
* `on_cleanup` stops the server again (see the note on the leak below).

The `unittest` binary built here has quack, httpfs and json linked in, so pointing it at the
`duckdb` submodule with `--test-dir` registers DuckDB's 4723 tests. The run itself is driven by
DuckDB's own runner, `duckdb/scripts/ci/run_tests.py`, which already does batching, timeouts,
retries and crash handling:

```bash
make test_duckdb

python3 duckdb/scripts/ci/run_tests.py build/release/test/unittest \
    --test-config test/configs/quack_client_server.json \
    --test-flags "--test-dir duckdb" --workers 1 --batch-size 1 "test/sql/join/*"
```

Two of those flags are not optional here:

* **`--workers 1`** - the config binds a fixed port (9494), so two unittest processes cannot run
  it at the same time.
* **`--batch-size 1`** - `run_tests.py` reports a failure per *batch*, not per test. At the default
  batch size ten individually failing tests come back as "9 passed, 1 failed", which is fine for
  gating CI but useless for deciding which tests to skip. A fresh process per test also keeps the
  instance leak described at the end of this file bounded.

To run one test the way the runner would, take the `reproduce:` line it prints on failure.

### What is skipped, and why

1437 of DuckDB's 4723 fast tests do not pass over a quack connection today. A sweep saw 3287
passing; one more test is nondeterministic over the wire and fails only sometimes. The failing
ones are listed in `skip_tests`, grouped by cause, so a fix shows up as a group that shrinks:

| tests | cause |
| ----- | ----- |
| 345 | client-side `Parser Error: syntax error at or near "."` - a qualified name deparsed with an empty component, after `ALTER` + `DROP` + a DDL statement |
| 239 | only the error *message* crosses the wire, not the exception type, so a test that asserts `Binder Error` gets the right message as an `Invalid Input Error` |
| 158 | `EXPLAIN` / plan inspection - the local plan is replaced by a single remote quack scan |
| 138 | a remote result with duplicate column names cannot be bound |
| 118 | sequences, types, indexes, macros and functions are invisible in the client catalog |
| 72 | transaction semantics differ over the wire, mostly `cannot start a transaction within a transaction` |
| 53 | `ATTACH` fails outright: a remote table cannot be bound while the client builds the catalog |
| 51 | catalog/metadata queries (`SHOW`, `duckdb_*`, `information_schema`, `pg_catalog`) describe the quack catalog |
| 45 | remote error, not grouped further yet |
| 38 | the statement is re-serialized to SQL text lossily - lambdas come back as `->`, `NULL::TYPE` loses its cast, extension-type literals are emitted unquoted |
| 33 | different result, not grouped further yet |
| 25 | prepared statement parameters do not reach the server |
| 24 | the test collides with the config's own `on_init` (secret manager settings, or a server that is already serving) |
| 22 | the client abandons an in-flight request: `superseded by a new query` |
| 21 | feature not implemented in the quack storage extension |
| 18 | a statement expected to fail succeeds over the connection |
| 16 | the test leaves the connection somewhere `on_cleanup` cannot run from |
| 15 | the remote error text differs beyond the exception type |
| 4 | the test changes the instance so `on_init` cannot survive it (memory limit, threads) |
| 2 | aborts the process (see below) |

The two lossy-deparse groups (345 and 38) are the same root cause; the first is kept separate
because its trigger is understood and it is by far the largest single win available.

`skip_error_messages` additionally skips any test that trips over a "not implemented yet" /
"not supported yet" error from the quack storage extension, so a newly added test that hits an
unimplemented feature skips instead of failing.

`settings: async_threads=2` is not about semantics: it keeps the per-instance thread count small,
which matters because of the leak described next.

### Regenerating the skip list

The groups are derived from a run, not maintained by hand. After fixing something the list blames,
re-derive them:

```bash
make test_duckdb_reclassify
```

That runs the whole suite with `skip_tests` ignored, saving the runner's output to
`duckdb_test_sweep.log`, then sorts the failures into causes and rewrites `skip_tests`
(`scripts/classify_duckdb_tests.py --write`). The rules live in `RULES` in that script, ordered
most specific first, so a test that trips over a known root cause is filed under it rather than
under the symptom it happens to show. To see how a group was arrived at before writing it:

```bash
scripts/classify_duckdb_tests.py duckdb_test_sweep.log                 # the counts
scripts/classify_duckdb_tests.py duckdb_test_sweep.log --show explain  # the SQL and error per test
```

To see what a change actually moved, diff the old and new skip lists by reason with DuckDB's own
`duckdb/scripts/test_config_compare.py old.json new.json`.

One wrinkle the classifier has to work around: `run_tests.py` renders a sqllogictest failure block
in full, but drops the block for a failure Catch raised itself - an abort, or a failing `on_init` /
`on_cleanup` - because its `iter_stdout_failure_blocks` skips the fatal-error and explicit-message
forms. Those arrive in the log as a bare `assertions:` line with no reason at all, so the
classifier re-runs just those few tests directly to find out why they failed.

A sweep takes about an hour, since `--batch-size 1` means a process per test.

A sweep also sees one run, so a test that is nondeterministic over the wire - a `UNION ALL` with no
`ORDER BY`, say, whose branches race - can pass during the sweep and fail afterwards. Re-run the
sweep, or add the test to the group it belongs in by hand.

### Known caveat: the run can exhaust the process thread limit

Server-side connections hold a strong reference to the `DatabaseInstance` that owns the server, so
an instance is only reclaimed once its server is stopped. `on_cleanup` does that after every test,
but a test that uses `restart`/`load` abandons its previous instance without one, and teardown of
those is asynchronous. In restart-heavy areas (`test/sql/storage/**`) the backlog can reach the
per-process thread limit, at which point `on_init` starts failing with
"thread constructor failed: Resource temporarily unavailable".

The config keeps the per-instance cost down with `async_threads=2` (the async pool is 40 threads per
instance by default and dominates the backlog), but that only moves the ceiling: a single process
that runs the whole fast suite gets to roughly test 2200 before the quack server stops answering at
all ("Server returned nothing (no headers, no data)") and every remaining test fails in `on_init`.
That is why `scripts/run_duckdb_tests.py` chunks the run - a fresh process every few hundred tests
keeps the accumulation bounded. Fixing the reference cycle would remove the need for the script.

A few tests still abort or hang the whole process rather than failing on their own, which is why
the runner bounds every chunk with a timeout and splits and retries a chunk that dies.
`COMMENT ON COLUMN` still aborts in `RemotePushdownOptimizer::RewriteStatement(AlterStatement&)`:
it calls `info.GetCatalogType()` on a `SetColumnCommentInfo` whose `catalog_entry_type` is still
`INVALID`, because whether the target is a table or a view is only resolved during binding. The
quack client can also still block on a request the server never dispatches (seen from both
`QuackCatalog::DropSchema` and `QuackScanBindCatalogName`, on the `BEGIN TRANSACTION` that
`QuackTransaction::ForceStart()` sends, with every server worker idle). Those tests are in the
`crash_or_hang` group.

Note that Catch traps the abort and still prints its summary, so a crash does not look like a dead
process from the outside - everything queued behind the aborting test simply never runs. The runner
watches for Catch's "due to a fatal error condition" instead, and reports a failure that Catch
raised itself (an abort, or a failing `on_init` / `on_cleanup`) under the banner Catch prints on
stdout, since those never produce a sqllogictest failure block on stderr.
