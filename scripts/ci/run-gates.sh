#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/veripipe-pycache}"
export PYTHONPATH="${ROOT}/shared-skills/pipeline-v2/scripts${PYTHONPATH:+:${PYTHONPATH}}"

run_gate() {
  local name="$1"
  shift
  printf 'CI gate START: %s\n' "$name"
  "$@"
  printf 'CI gate PASS: %s\n' "$name"
}

check_bash_syntax() {
  local path
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    bash -n "$ROOT/$path"
  done < <(git -C "$ROOT" ls-files '*.sh')
}

check_python_syntax() {
  local path
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    python3 -m py_compile "$ROOT/$path"
  done < <(git -C "$ROOT" ls-files '*.py')
}

run_contracts() {
  local test_script
  for test_script in "$ROOT"/tests/contract/*.sh; do
    bash "$test_script"
  done
}

run_units() {
  # Run every directory that holds test_*.py. `unittest discover` does not
  # recurse into non-package subdirectories, so discovering each test dir
  # explicitly keeps nested suites (e.g. tests/unit/pipeline_v2) from being
  # silently skipped.
  local dir
  for dir in $(find "$ROOT/tests/unit" -name 'test_*.py' -exec dirname {} \; | sort -u); do
    python3 -m unittest discover -s "$dir" -p 'test_*.py'
  done
}

cd "$ROOT"
run_gate "bash-syntax" check_bash_syntax
run_gate "python-syntax" check_python_syntax
run_gate "secret-scan" python3 "$ROOT/scripts/ci/check-secrets.py"
run_gate "contract" run_contracts
run_gate "unit" run_units
printf 'CI gates ALL PASS\n'
