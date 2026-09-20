#!/usr/bin/env bash
# Recorded by asciinema to produce examples/demo.cast:
#   asciinema rec -c 'bash examples/record-demo.sh' examples/demo.cast
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
printf '$ python3 examples/demo.py\n'
python3 examples/demo.py
