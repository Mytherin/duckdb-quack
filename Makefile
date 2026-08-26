PROJ_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

# Configuration of extension
EXT_NAME=rpc
EXT_CONFIG=${PROJ_DIR}extension_config.cmake
EXT_FLAGS=-DCMAKE_CXX_STANDARD=17

# Include the Makefile from extension-ci-tools
include extension-ci-tools/makefiles/duckdb_extension.Makefile
include extension-ci-tools/makefiles/vcpkg.Makefile

#### Running the regular DuckDB test suite through a quack client/server connection ####
# scripts/run_duckdb_tests.py drives the unittest binary built here (quack + httpfs are linked in)
# against the tests in the duckdb submodule, using test/configs/quack_client_server.json: it starts
# a quack server inside each test instance, attaches back to it and makes that remote catalog the
# default database, so every statement travels through the quack RPC protocol.
#
# The script runs the tests as a series of short-lived processes because a DuckDB instance that
# runs a quack server is never reclaimed while its server holds connections -- see test/README.md.
test_duckdb: test_duckdb_release
test_duckdb_release:
	python3 scripts/run_duckdb_tests.py --build release
test_duckdb_debug:
	python3 scripts/run_duckdb_tests.py --build debug
test_duckdb_reldebug:
	python3 scripts/run_duckdb_tests.py --build reldebug
test_duckdb_slow:
	python3 scripts/run_duckdb_tests.py --build release --slow

.PHONY: test_duckdb test_duckdb_release test_duckdb_debug test_duckdb_reldebug test_duckdb_slow
