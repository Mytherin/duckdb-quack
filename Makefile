PROJ_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

# Configuration of extension
EXT_NAME=rpc
EXT_CONFIG=${PROJ_DIR}extension_config.cmake
EXT_FLAGS=-DCMAKE_CXX_STANDARD=17

# Include the Makefile from extension-ci-tools
include extension-ci-tools/makefiles/duckdb_extension.Makefile
include extension-ci-tools/makefiles/vcpkg.Makefile

#### Running the regular DuckDB test suite through a quack client/server connection ####
# DuckDB's own runner drives the unittest binary built here (quack + httpfs are linked in) against
# the tests in the duckdb submodule, using test/configs/quack_client_server.json: it starts a quack
# server inside each test instance, attaches back to it and makes that remote catalog the default
# database, so every statement travels through the quack RPC protocol.
#
# --workers 1 because the config binds a fixed port (9494), so two unittest processes cannot run
# it at once. --batch-size 1 because run_tests.py counts a failure per batch rather than per test,
# and a fresh process per test also keeps the instance leak described in test/README.md bounded.
# --retry 2 (what DuckDB's own CI uses) so the handful of concurrency tests that fail
# intermittently over the wire do not have to be skipped outright; the same flag on the sweep
# below keeps the skip list to tests that fail every time.
DUCKDB_TESTS = python3 duckdb/scripts/ci/run_tests.py --test-flags "--test-dir duckdb" \
	--workers 1 --batch-size 1 --retry 2

test_duckdb: test_duckdb_release
test_duckdb_release:
	$(DUCKDB_TESTS) build/release/test/unittest --test-config test/configs/quack_client_server.json
test_duckdb_debug:
	$(DUCKDB_TESTS) build/debug/test/unittest --test-config test/configs/quack_client_server.json
test_duckdb_reldebug:
	$(DUCKDB_TESTS) build/reldebug/test/unittest --test-config test/configs/quack_client_server.json

# Re-derive test/configs/quack_client_server.json's skip_tests from what actually fails today:
# run everything with the skip list disabled, then group the failures by cause. Do this after
# fixing something the skip list blames, so the groups shrink instead of going stale.
test_duckdb_reclassify:
	python3 -c "import json; c = json.load(open('test/configs/quack_client_server.json')); \
		c['skip_tests'] = []; json.dump(c, open('build/quack_no_skip.json', 'w'), indent=2)"
	-$(DUCKDB_TESTS) build/release/test/unittest --test-config build/quack_no_skip.json \
		> duckdb_test_sweep.log 2>&1
	python3 scripts/classify_duckdb_tests.py duckdb_test_sweep.log --config build/quack_no_skip.json --write

.PHONY: test_duckdb test_duckdb_release test_duckdb_debug test_duckdb_reldebug
.PHONY: test_duckdb_reclassify
