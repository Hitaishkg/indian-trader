#!/bin/bash
cd /home/hitaish/projects/indian-trader
PYTHON=/home/hitaish/projects/indian-trader/.venv/bin/python

$PYTHON _tmp_combo_test.py 40 0.15 > /tmp/c1.json 2>/tmp/c1.err &
P1=$!
$PYTHON _tmp_combo_test.py 55 0.15 > /tmp/c2.json 2>/tmp/c2.err &
P2=$!
$PYTHON _tmp_combo_test.py 40 0.12 > /tmp/c3.json 2>/tmp/c3.err &
P3=$!
$PYTHON _tmp_combo_test.py 55 0.12 > /tmp/c4.json 2>/tmp/c4.err &
P4=$!

echo "PIDs: $P1 $P2 $P3 $P4"
wait $P1 $P2 $P3 $P4
echo "All done"
