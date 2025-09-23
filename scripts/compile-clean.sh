#!/bin/bash
set -euo pipefail

# Move result
mkdir -p build
mv libplc.so build/
rm *.o