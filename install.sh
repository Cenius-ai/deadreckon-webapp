#!/usr/bin/env bash
# DeadReckon setup: verify the interpreter, install the optional console
# script, then EXIT.  It never starts anything long-running and never needs
# the network or root privileges.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON is not on PATH; install Python 3.9 or newer" >&2
    exit 1
fi

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "error: DeadReckon needs Python 3.9 or newer (found $("$PYTHON" -V 2>&1))" >&2
    exit 1
fi

# No runtime dependencies: requirements.txt documents that explicitly.
# --no-index keeps this step strictly offline.
"$PYTHON" -m pip install --disable-pip-version-check --no-index \
    -r requirements.txt >/dev/null 2>&1 || {
        echo "warning: could not read requirements.txt with pip (continuing; nothing to install)" >&2
    }

# Optional convenience: a `deadreckon` command on PATH.  The tool itself runs
# fine as `python3 deadreckon.py`, so a failure here is not fatal.
if "$PYTHON" -m pip install --disable-pip-version-check \
        --no-build-isolation --no-deps -e . >/dev/null 2>&1; then
    echo "installed the 'deadreckon' console script"
else
    echo "warning: could not install the 'deadreckon' console script;" >&2
    echo "         run the tool as: $PYTHON deadreckon.py ..." >&2
fi

# Cheap self-check: an incomplete or broken source tree fails here, loudly.
"$PYTHON" -c 'import deadreckon' || {
    echo "error: deadreckon.py failed to import" >&2
    exit 1
}

cat <<'DONE'
Setup complete.

Try it:
  python3 deadreckon.py --help
  python3 deadreckon.py status
  sh demo.sh                 # recorded end-to-end passage in a temp directory
  python3 deadreckon.py legs --store examples/solent-passage.json
DONE
