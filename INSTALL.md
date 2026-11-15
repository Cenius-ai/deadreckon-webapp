# Installing DeadReckon

DeadReckon is a single Python file with **no third-party dependencies**. Setup
is one command that installs and exits; there is no service to start, no
database to migrate and nothing to configure.

## 1. Prerequisites (system)

| Requirement | Notes |
|---|---|
| Python **3.9 or newer** | CPython from python.org, your distro, or Homebrew. Check with `python3 -V`. |
| A POSIX shell | `sh`/`bash` on Linux and macOS; Git Bash or WSL on Windows. |
| A terminal that is at least 80 columns wide | Output never wraps past 80 columns, so narrower terminals are fine too. |

Nothing else is needed. No compilers, no `libsqlite3-dev`, no credits, no
network access at runtime. DeadReckon never opens a socket.

The setup script is unprivileged by design: it does **not** use `sudo`,
`apt-get`, `brew`, `apk` or any other system package manager, and it does not
install or start system services.

## 2. Install (one command, then it exits)

```sh
sh install.sh
```

What it does, in order:

1. Checks that `python3` exists and is 3.9+ (exits non-zero with a clear
   message otherwise).
2. Runs `pip install --no-index -r requirements.txt`. `requirements.txt`
   declares no packages — the flag combination means the step can never reach
   the network.
3. Tries to install the optional `deadreckon` console script
   (`pip install --no-build-isolation --no-deps -e .`). If pip cannot do this
   on your machine, it warns and continues: the tool already runs as
   `python3 deadreckon.py`.
4. Imports `deadreckon` once as a self-check, so a broken tree fails here
   rather than at first use.
5. Prints the next commands and **exits**. It never starts anything
   long-running.

It is safe to re-run.

Package manager: **pip** (the committed `pyproject.toml` is the manifest,
`requirements.txt` the dependency list). There is no lockfile to keep because
there are no dependencies to lock.

## 3. First run (no seed step — there is no data to seed)

DeadReckon has no server and no seed stage. The store file is created by the
first command that writes something:

```sh
cd /path/to/a/directory/you/can/write
python3 /path/to/deadreckon/deadreckon.py legs origin 50.7640 -1.2970
python3 /path/to/deadreckon/deadreckon.py legs add 093 6.2
python3 /path/to/deadreckon/deadreckon.py legs
```

`deadreckon-store.json` appears next to you. Pick a different file with
`--store PATH` or the `DEADRECKON_STORE` environment variable (see
`.env.example`).

To look at a ready-made passage instead of starting from empty:

```sh
python3 deadreckon.py legs  --store examples/solent-passage.json
python3 deadreckon.py fixes --store examples/solent-passage.json
python3 deadreckon.py status --store examples/solent-passage.json
```

## 4. Run the recorded demo

```sh
sh demo.sh
```

It writes a whole passage (legs, current, fixes, an advance, a saved DR fix,
a validation refusal, an undo, a clear) into a throwaway temp directory and
prints the resulting JSON store. Non-interactive, exits 0 on success.

Keep the store afterwards with:

```sh
DEADRECKON_DEMO_DIR=/tmp/deadreckon-demo sh demo.sh
```

## 5. Run the tests

```sh
python3 -m unittest discover -s tests   # stdlib only, nothing to install
python3 -m pytest -q                    # same suite, if pytest is already present
```

56 tests, standard library `unittest` (pytest collects the same cases), no
network, no fixtures to fetch.

## 6. Uninstall

Nothing was installed into the system beyond an editable link for the optional
console script:

```sh
python3 -m pip uninstall deadreckon
```

Then delete the directory. Your passage logs (`*.json`) are yours to keep.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `deadreckon: error: unknown command 'legs add 090 10'` | The shell passed everything as one word — quote-free single arguments are required: `deadreckon legs add 090 10`. |
| `deadreckon: store error: cannot read store … invalid JSON …` | The store file was edited by hand or truncated. It is left untouched; repair the JSON or point `--store` at another file. |
| `deadreckon: error: refusing to clear 3 legs without confirmation` | Destructive commands need `--yes`. |
| `warning: could not install the 'deadreckon' console script` | pip could not build the editable install on this machine. Nothing is wrong with the tool: run `python3 deadreckon.py …`. |
| Exit code 3 with `cannot write store` | The directory is not writable, or the `--store` path is a directory. Choose a writable path. |
