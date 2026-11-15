#!/usr/bin/env bash
# DeadReckon end-to-end demo: a full Solent passage, non-interactive.
#
#   sh demo.sh                     # records a real passage into a temp store
#   DEADRECKON_DEMO_DIR=/tmp/x sh demo.sh   # keep the store afterwards
#
# Everything happens in a throwaway directory, so your own
# deadreckon-store.json is never touched.  Exits non-zero on any real error.
set -eu
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
DEMO_DIR="${DEADRECKON_DEMO_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/deadreckon-demo.XXXXXX")}"
export DEADRECKON_STORE="$DEMO_DIR/passage.json"

cleanup() {
    if [ -z "${DEADRECKON_DEMO_DIR:-}" ]; then
        rm -rf "$DEMO_DIR"
    fi
}
trap cleanup EXIT

# run <args...>   print the command, then run it (any failure aborts the demo)
run() {
    printf '\n$ deadreckon %s\n' "$*"
    "$PYTHON" deadreckon.py "$@"
}

# run_shown <echo-text> <args...>  print a pre-wrapped command line, then run it
run_shown() {
    printf '\n$ %s\n' "$1"
    shift
    "$PYTHON" deadreckon.py "$@"
}

# run_expected_failure <args...>   print the command, keep going, show the code
run_expected_failure() {
    printf '\n$ deadreckon %s\n' "$*"
    status=0
    "$PYTHON" deadreckon.py "$@" || status=$?
    printf '(refused as expected, exit %s)\n' "$status"
}

printf 'DeadReckon demo - store: %s\n' "$DEADRECKON_STORE"
printf 'No network, no accounts: one JSON file in a local directory.\n'

run --version

printf '\n== 1. A cold store knows it is empty ==\n'
run status

printf '\n== 2. Lay out the leg table from Cowes, Isle of Wight ==\n'
run legs origin 50.7640 -1.2970
run legs add 093 6.2
run legs add 045 3.4
run legs add 130 2.1
run legs

printf '\n== 3. Enter the current: setting 075 true, drifting 1.4 kn ==\n'
run drift 075 1.4
run drift

printf '\n== 4. Record two fixes as the passage runs ==\n'
run_shown 'deadreckon fix 50.7640 -1.2970 --course 093 --speed 6.4 \
      --time 2024-06-01T09:00:00Z' \
    fix 50.7640 -1.2970 --course 093 --speed 6.4 --time 2024-06-01T09:00:00Z
run_shown 'deadreckon fix 50.7712 -1.1620 --course 045 --speed 5.8 \
      --time 2024-06-01T10:00:00Z' \
    fix 50.7712 -1.1620 --course 045 --speed 5.8 --time 2024-06-01T10:00:00Z
run fixes

printf '\n== 5. Advance the DR position with the current vector-summed in ==\n'
run dr 1.5

printf '\n== 6. Save that DR position into the log ==\n'
run dr 1.5 --save
run fixes last

printf '\n== 7. The whole state at a glance ==\n'
run status

printf '\n== 8. Machine-readable output for scripting ==\n'
run dr 1 --json

printf '\n== 9. Clear the offset: advances now use vessel motion only ==\n'
run drift clear
run dr 1

printf '\n== 10. Bad input never half-writes the log ==\n'
run_expected_failure fix 95.0 -0.1 --course 090 --speed 6
run_expected_failure legs add 361 10
run_expected_failure dr 0
run_expected_failure legs clear
printf '\n$ deadreckon fixes\n'
"$PYTHON" deadreckon.py fixes

printf '\n== 11. Undo the newest fix, then clear the table ==\n'
run fixes undo
run legs clear --yes
run status

printf '\n== 12. The store on disk: one indented, diffable JSON document ==\n'
printf '%s\n' "--- $DEADRECKON_STORE ---"
cat "$DEADRECKON_STORE"

printf '\nDemo finished cleanly.\n'
if [ -n "${DEADRECKON_DEMO_DIR:-}" ]; then
    printf 'Store kept at %s\n' "$DEADRECKON_STORE"
fi
