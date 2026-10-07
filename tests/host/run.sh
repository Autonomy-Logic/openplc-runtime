#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# Host tests: plain C++17 executables, no framework, no device. For
# runtime code Ceedling cannot reach (uses std::thread/mutex) and that
# needs no plc_main. Add a test by appending it to TESTS below.

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
  "tests/host/test_rt_mutex.cpp:"
  "tests/host/test_task_policy.cpp:core/src/plc_app/task_policy.c"
  "tests/host/test_image_outputs.cpp:core/src/plc_app/image_tables.cpp:core/src/plc_app/located_globals.c"
)

failures=0
for entry in "${TESTS[@]}"; do
  test_src="${entry%%:*}"
  deps="${entry#*:}"
  name=$(basename "$test_src" .cpp)

  printf '\n=== %s ===\n' "$name"
  # C sources are compiled as C, not C++.
  objs=()
  for dep in ${deps//:/ }; do
    if [[ "$dep" == *.c ]]; then
      obj="$OUT/$(basename "$dep" .c).o"
      # shellcheck disable=SC2086
      ${CC:-cc} -std=gnu11 -Wall -Wextra -g $INCLUDES -c "$dep" -o "$obj" || { objs=(); break; }
      objs+=("$obj")
    else
      objs+=("$dep")
    fi
  done
  # shellcheck disable=SC2086
  if ! $CXX $CXXFLAGS $INCLUDES "$test_src" ${objs[@]+"${objs[@]}"} -o "$OUT/$name" -lpthread; then
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
