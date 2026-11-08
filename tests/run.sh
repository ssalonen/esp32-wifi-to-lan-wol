#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
binary=$(mktemp /tmp/wol-schedule-test.XXXXXX)
trap 'rm -f "$binary"' EXIT HUP INT TERM
c++ -std=c++11 -Wall -Wextra -Werror -Iinclude tests/wake_schedule_test.cpp -o "$binary"
"$binary"
python3 -m unittest discover -s tests -p 'test_*.py' -v
