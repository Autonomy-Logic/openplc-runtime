#!/usr/bin/env bash
#
# Host tests: plain executables, no framework, no device.
#
# For runtime C++ that Ceedling cannot reach (it is configured for C, and these
# translation units use std::thread / std::mutex) and that does not need the
# lifecycle harness's real `plc_main`. One command, runs anywhere with a C++17
# compiler — including macOS, which the lifecycle suite cannot do.
#
#   ./tests/host/run.sh
#
# Add a test by dropping a `test_*.cpp` here that compiles against the sources
# it needs; list it in TESTS below with those sources.

set -euo pipefail

cd "$(dirname "$0")/../.."

CXX="${CXX:-c++}"
CXXFLAGS="-std=c++17 -Wall -Wextra -Wno-unused-parameter -g"
INCLUDES="-Icore/src/plc_app -Icore/src"

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

# test source : extra sources it links
TESTS=(
  "tests/host/test_plc_retain_file_store.cpp:core/src/plc_app/plc_retain_file_store.cpp"
)

failures=0
for entry in "${TESTS[@]}"; do
  test_src="${entry%%:*}"
  deps="${entry#*:}"
  name=$(basename "$test_src" .cpp)

  printf '\n=== %s ===\n' "$name"
  # shellcheck disable=SC2086
  if ! $CXX $CXXFLAGS $INCLUDES "$test_src" ${deps//:/ } -o "$OUT/$name" -lpthread; then
    echo "  FAIL  $name did not compile"
    failures=$((failures + 1))
    continue
  fi

  if ! "$OUT/$name"; then
    failures=$((failures + 1))
  fi
done

printf '\n'
if [ "$failures" -eq 0 ]; then
  echo "host tests: all passed"
else
  echo "host tests: $failures failed"
  exit 1
fi
