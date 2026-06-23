#!/usr/bin/env bash
# run_demo.sh -- small, fast demonstration across the benchmark suite.
# For results closest to the paper, raise --vectors to 10000 and -n to 20+.
set -e

PY=${PYTHON:-python3}

echo "############ Combinational ISCAS-85 ############"
$PY main.py netlists/c2670.v -o output/c2670 -q 8 -n 5 --vectors 1000 --detect 3000 --mero --seed 1
$PY main.py netlists/c3540.v -o output/c3540 -q 8 -n 5 --vectors 1000 --detect 3000 --seed 1

echo "############ Sequential ISCAS-89 (scan) ############"
$PY main.py netlists/s1423scan.v -o output/s1423 -q 8 -n 5 --vectors 1000 --detect 3000 --seed 2

echo
echo "All demo runs complete. See output/<name>/summary.json for metrics."
