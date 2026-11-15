# Using DeadReckon

Worked walkthroughs of the real commands. All examples use the default store
(`deadreckon-store.json` in the current directory); add `--store PATH` to any
command to use another file. `deadreckon` below is shorthand for
`python3 deadreckon.py` (or the console script if `install.sh` managed to
install it).

## 1. Lay out a leg table

Start from a known position — Cowes, Isle of Wight.

```sh
$ deadreckon legs origin 50.7640 -1.2970
Origin set to 50.764,-1.297.
Recomputed 0 legs from the new origin.

Leg table  origin 50.764,-1.297

No legs yet.  Start a table with:
  deadreckon legs origin <lat> <lon>
  deadreckon legs add <course> <nm>
```

Add legs as course (degrees true) and distance run (nautical miles). Each new
leg starts where the previous one arrived.

```sh
$ deadreckon legs add 093 6.2
Added leg 1: 093T 6.20 nm to 50.758,-1.134.
Started from 50.764,-1.297.

Leg  Course  Dist nm  From Lat  Arr Lat  Arr Lon  Cum nm
  1    093T     6.20    50.764   50.758   -1.134    6.20

Total 6.20 nm over 1 leg   final position 50.758,-1.134
```

```sh
$ deadreckon legs add 045 3.4
Added leg 2: 045T 3.40 nm to 50.799,-1.071.
Started from 50.758,-1.134.
```

`deadreckon legs` reprints the whole table at any time: per-leg course,
distance, both ends, cumulative distance, and the total.

### Move the origin

```sh
$ deadreckon legs origin 51.500 -0.100
Origin set to 51.500,-0.100.
Recomputed 4 legs from the new origin.
```

Every arrival is recomputed from the new origin — the courses and distances you
entered are kept, the positions are re-derived.

### Clear the table

```sh
$ deadreckon legs clear
deadreckon: error: refusing to clear 4 legs without confirmation; re-run with --yes
$ deadreckon legs clear --yes
Cleared 4 legs; the table is now empty.
The origin is unchanged.
```

## 2. Enter the current

The offset is the direction the current sets **toward** (degrees true) and how
fast it runs (knots).

```sh
$ deadreckon drift 075 1.4
Current offset  set 075T  drift 1.40 kn  (updated 2024-06-01T08:35:00Z)

$ deadreckon drift
Current offset  set 075T  drift 1.40 kn  (updated 2024-06-01T08:35:00Z)

$ deadreckon drift clear
Current offset cleared; advances now use vessel motion only.
```

## 3. Keep the DR fix log

A fix is a position with the course and speed being made good at that moment.
`--time` is optional; omit it and the current UTC time is stamped.

```sh
$ deadreckon fix 50.7640 -1.2970 --course 093 --speed 6.4 --time 2024-06-01T09:00:00Z
Recorded fix 1 (observed)  2024-06-01T09:00:00Z
Position 50.764,-1.297   course 093T   speed 6.40 kn
Offset snapshot set 075T / 1.40 kn
This is the first fix in the log.
The log now holds 1 fix.
```

Each fix snapshots the offset that was active when it was recorded, so the log
still explains itself after the current is changed.

```sh
$ deadreckon fixes
Fix log  4 fixes

Id  Recorded UTC          Lat     Lon     Crs   Spd kn  Kind  Offset
 1  2024-06-01T09:00:00Z  50.764  -1.297  093T    6.40   obs  075T/1.4
 2  2024-06-01T10:00:00Z  50.771  -1.162  045T    5.80   obs  075T/1.4
 3  2024-06-01T11:00:00Z  50.834  -1.062  045T    5.80   obs  075T/1.4
 4  2024-06-01T12:30:00Z  50.945  -0.846  045T    5.80    dr  075T/1.4

Offset shows the current's set/rate snapshot taken at record time.
```

`deadreckon fixes last` shows the newest fix on its own. Recorded the wrong
thing? Undo it and record again — there is no in-place editing.

```sh
$ deadreckon fixes undo
Undid fix 4 (2024-06-01T12:30:00Z); the log now holds 3 fixes.
$ deadreckon fixes undo
deadreckon: error: nothing to undo: the fix log is empty
```

## 4. Advance the DR position

`dr HOURS` starts from the latest fix, adds the vessel velocity to the current
velocity, and reports the ground course and speed, the distance run and the new
position.

```sh
$ deadreckon dr 1.5
DR advance from fix 3 50.834,-1.062  course 045T  speed 5.80 kn
Current offset  set 075T / 1.40 kn
Ground course 051T   ground speed 7.05 kn
Distance run 10.57 nm over 1.500 h
New DR position 50.945,-0.846
```

With no offset set, the advance is vessel motion only:

```sh
$ deadreckon drift clear
$ deadreckon dr 2
DR advance from fix 3 50.834,-1.062  course 045T  speed 5.80 kn
Current offset  none
Ground course 045T   ground speed 5.80 kn
Distance run 11.60 nm over 2.000 h
New DR position 50.970,-0.845
```

Add `--save` to append the computed position to the log as a `dr` fix (it
records the vessel course and speed used, so chaining further advances applies
the current exactly once each time):

```sh
$ deadreckon dr 1.5 --save
...
Saved as dr fix 4 at 2024-06-01T13:05:00Z
```

Advance from a position that is not in the log with `--from`:

```sh
$ deadreckon dr 1 --from 0 0 --course 090 --speed 6
DR advance from explicit position 0.000,0.000  course 090T  speed 6.00 kn
Current offset  none
Ground course 090T   ground speed 6.00 kn
Distance run 6.00 nm over 1.000 h
New DR position 0.000,0.100
```

## 5. Check the state

```sh
$ deadreckon status
Store       /home/nav/passage/deadreckon-store.json  (exists)
Origin      50.764,-1.297
Leg table   4 legs, 13.50 nm total
Fix log     4 fixes, latest #4 at 2024-06-01T12:30:00Z
Latest fix  50.945,-0.846  course 045T  speed 5.80 kn
Made good   20.27 nm on 057T from the origin
Offset      set 075T / 1.40 kn
```

`Made good` is the great-circle course and distance from the table origin to
the latest fix — the inverse of the leg arithmetic, handy for spotting a leg
that does not match the log.

## 6. Script it

`--json` prints exactly one JSON object on stdout for `legs`, `fixes`,
`fixes last`, `drift`, `dr` and `status`. Errors still go to stderr with a
non-zero exit code, so a pipeline stays parseable.

```sh
$ deadreckon legs --json
{
  "store": "/home/nav/passage/deadreckon-store.json",
  "origin": {"lat": 50.764, "lon": -1.297},
  "legs": [ ... ],
  "leg_count": 4,
  "total_nm": 13.5
}

$ deadreckon dr 1 --json | python3 -c 'import json,sys; a=json.load(sys.stdin)["advance"]; print(a["ground_speed_kn"], a["position"])'
7.047287 {'lat': 50.908154, 'lon': -0.917954}
```

## 7. Every failure is one line, and nothing is half-written

```sh
$ deadreckon fix 95.0 -0.1 --course 090 --speed 6
deadreckon: error: latitude must be between -90 and 90 degrees (got 95.0)
$ deadreckon legs add 361 10
deadreckon: error: course must be between 0 and 360 degrees true (got 361)
$ deadreckon dr 0
deadreckon: error: hours must be greater than 0 (got 0)
$ deadreckon fix 51.5 -0.1 --course 090 --speed 6 --time not-a-time
deadreckon: error: time must be an ISO-8601 UTC timestamp like 2024-06-01T12:00:00Z (got 'not-a-time')
```

Exit codes: `0` success, `2` user error, `3` store error. Validation runs
before anything is opened for writing, so a rejected command leaves the store
byte-for-byte unchanged.

## Ranges

| Input | Accepted |
|---|---|
| latitude | −90 … 90 decimal degrees |
| longitude | −180 … 180 decimal degrees |
| course, current set | 0 … 360 degrees true (360 is treated as due north when computed) |
| distance | greater than 0, at most 10800 nm (half the great-circle circumference) |
| speed, drift rate | 0 knots or more |
| elapsed hours | greater than 0 |
| time | ISO-8601, e.g. `2024-06-01T12:00:00Z` or `2024-06-01T14:00:00+02:00` (normalised to UTC) |
