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
`duckdb` submodule with `--test-dir` registers DuckDB's tests. `scripts/run_duckdb_tests.py` does
that for you:

```bash
make test_duckdb          # the fast tests (.test)
make test_duckdb_slow     # adds .test_slow

scripts/run_duckdb_tests.py --jobs 4          # four workers, one quack port each
scripts/run_duckdb_tests.py test/sql/join     # only tests under that path
```

The script runs the tests as a series of short-lived unittest processes instead of one long one -
see the caveat at the end for why - and gives each process a timeout. A chunk that overruns, dies,
or degrades into `on_init` failures is split in half and retried, so a test that crashes or hangs
the process costs only itself instead of everything queued behind it. To run a single chunk by hand:

```bash
./build/release/test/unittest \
    --test-config test/configs/quack_client_server.json \
    --test-dir duckdb "test/sql/join/*"
```

The config binds a fixed port (9494), so only one run may use it at a time; the script rewrites the
port for each worker when you pass `--jobs`.

### What is skipped, and why

About 1870 of DuckDB's ~5500 tests do not pass over a quack connection today. They are listed in
`skip_tests`, grouped by cause, so the groups shrink as the underlying issues are fixed:

| tests | cause |
| ----- | ----- |
| 398 | client-side `Parser Error: syntax error at or near "."` after `ALTER` + `DROP` + a DDL statement |
| 278 | feature not implemented in the quack storage extension (CHECKPOINT, VACUUM, CREATE INDEX/SEQUENCE/TYPE/FUNCTION, ALTER, DELETE/UPDATE planning, RETURNING) |
| 242 | `EXPLAIN` / plan inspection - the local plan is replaced by a single remote quack scan |
| 226 | only the error *message* crosses the wire, not the exception type, so every remote error arrives as an `Invalid Input Error` |
| 152 | sequences, types, indexes and functions are invisible in the client catalog |
| 119 | a remote result with duplicate column names cannot be bound |
| 107 | remote `Invalid Input Error`, not grouped further yet |
| 94 | catalog/metadata queries (`SHOW`, `duckdb_*`, `information_schema`, comments) describe the quack catalog |
| 93 | transaction semantics differ over the wire |
| 80 | other divergences, not grouped further yet |
| 37 | error text differs, expected error not raised, or the test breaks the config's own on_init / on_cleanup / on_new_connection |
| 11 | aborts or hangs the whole process (see below) |

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

That runs the whole suite with `skip_tests` ignored (`run_duckdb_tests.py --no-skip --report`,
which records the failure block unittest printed for each failing test) and then sorts those tests
into causes (`classify_duckdb_tests.py --write`, which rewrites `skip_tests` in the config). The
rules live in `RULES` in that script, ordered most specific first, so a test that trips over a
known root cause is filed under it rather than under the symptom it happens to show. To see how a
group was arrived at before writing it:

```bash
scripts/classify_duckdb_tests.py duckdb_test_sweep.json                 # the counts
scripts/classify_duckdb_tests.py duckdb_test_sweep.json --show explain  # the SQL and error per test
```

A sweep runs several thousand more tests than a normal run and takes correspondingly longer.

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

A handful of tests also abort or hang the whole process rather than failing on their own:
`COMMENT ON COLUMN` aborts in `RemotePushdownOptimizer::RewriteStatement(AlterStatement&)`, and the
quack client can block forever on a request the server never dispatches (seen from both
`QuackCatalog::DropSchema` and `QuackScanBindCatalogName`, always on the `BEGIN TRANSACTION` that
`QuackTransaction::ForceStart()` sends, with every server worker idle). The ones seen so far are in
the first `skip_tests` group, but the hang is intermittent, which is why the runner has a timeout.
