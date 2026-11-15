#!/usr/bin/env python3
"""DeadReckon - an offline dead-reckoning log for small-craft navigators.

Course-and-distance leg table, DR fix log and current drift offset in one
JSON store.  Standard library only; no network, no accounts, no GUI.

Units are fixed: nautical miles, knots, decimal degrees, degrees true and
UTC ISO-8601 timestamps.  Spherical model with the mean Earth radius
3440.065 nm (so one degree of arc is about 60.04 nm).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, Sequence

APP_NAME = "DeadReckon"
VERSION = "1.0.0"

STORE_VERSION = 1
DEFAULT_STORE = "deadreckon-store.json"
STORE_ENV_VAR = "DEADRECKON_STORE"

# Mean Earth radius in nautical miles (1 nm = 1 arcminute of latitude).
EARTH_RADIUS_NM = 3440.065
# Half the great-circle circumference; a longer single leg has no meaning here.
MAX_LEG_NM = 10800.0

EXIT_OK = 0
EXIT_USER_ERROR = 2
EXIT_STORE_ERROR = 3

TIMESTAMP_EXAMPLE = "2024-06-01T12:00:00Z"

LEG_FIELDS = (
    "id",
    "course_deg",
    "distance_nm",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
    "cumulative_nm",
    "created_utc",
)

FIX_FIELDS = (
    "id",
    "recorded_utc",
    "lat",
    "lon",
    "course_deg",
    "speed_kn",
    "kind",
    "offset_set_deg",
    "offset_drift_kn",
)

DRIFT_FIELDS = ("set_deg", "rate_kn", "updated_utc")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class UserError(Exception):
    """Bad input from the user: exit 2, never touches the store."""


class StoreError(Exception):
    """The store file is missing-but-expected, corrupt or unwritable: exit 3."""


# --------------------------------------------------------------------------
# Spherical navigation math
# --------------------------------------------------------------------------


def _rad(degrees: float) -> float:
    return math.radians(degrees)


def _deg(radians: float) -> float:
    return math.degrees(radians)


def quantize(value: float, places: int = 6) -> float:
    """Round a stored number so the JSON stays stable and human-diffable."""
    rounded = round(value, places)
    return 0.0 if rounded == 0 else rounded


def normalize_lon(lon_deg: float) -> float:
    """Fold a longitude into [-180, 180)."""
    wrapped = math.fmod(lon_deg + 180.0, 360.0)
    if wrapped < 0:
        wrapped += 360.0
    return wrapped - 180.0


def destination_point(
    lat_deg: float, lon_deg: float, course_deg: float, distance_nm: float
) -> tuple[float, float]:
    """Great-circle arrival after running `distance_nm` on `course_deg`."""
    angular = distance_nm / EARTH_RADIUS_NM
    lat1 = _rad(lat_deg)
    lon1 = _rad(lon_deg)
    bearing = _rad(course_deg)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular)
        + math.cos(lat1) * math.sin(angular) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular) * math.cos(lat1),
        math.cos(angular) - math.sin(lat1) * math.sin(lat2),
    )
    return (_deg(lat2), normalize_lon(_deg(lon2)))


def inverse(
    lat1_deg: float, lon1_deg: float, lat2_deg: float, lon2_deg: float
) -> tuple[float, float]:
    """Initial course (degrees true) and great-circle distance in nm."""
    lat1 = _rad(lat1_deg)
    lat2 = _rad(lat2_deg)
    delta_lon = _rad(normalize_lon(lon2_deg - lon1_deg))
    cos_central = (
        math.sin(lat1) * math.sin(lat2)
        + math.cos(lat1) * math.cos(lat2) * math.cos(delta_lon)
    )
    central = math.acos(max(-1.0, min(1.0, cos_central)))
    distance_nm = central * EARTH_RADIUS_NM
    if distance_nm < 1e-9:
        return (0.0, 0.0)
    course = _deg(
        math.atan2(
            math.sin(delta_lon) * math.cos(lat2),
            math.cos(lat1) * math.sin(lat2)
            - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon),
        )
    )
    return (course % 360.0, distance_nm)


# --------------------------------------------------------------------------
# Value parsing and range validation (all user errors, nothing written)
# --------------------------------------------------------------------------


def parse_number(raw: Any, label: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise UserError(f"{label} must be a number (got {raw!r})") from None


def check_lat(raw: Any) -> float:
    value = parse_number(raw, "latitude")
    if not math.isfinite(value) or not -90.0 <= value <= 90.0:
        raise UserError(f"latitude must be between -90 and 90 degrees (got {raw})")
    return value


def check_lon(raw: Any) -> float:
    value = parse_number(raw, "longitude")
    if not math.isfinite(value) or not -180.0 <= value <= 180.0:
        raise UserError(f"longitude must be between -180 and 180 degrees (got {raw})")
    return value


def check_course(raw: Any, label: str = "course") -> float:
    value = parse_number(raw, label)
    if not math.isfinite(value) or not 0.0 <= value <= 360.0:
        raise UserError(
            f"{label} must be between 0 and 360 degrees true (got {raw})"
        )
    return value


def check_distance(raw: Any) -> float:
    value = parse_number(raw, "distance")
    if not math.isfinite(value) or value <= 0.0 or value > MAX_LEG_NM:
        raise UserError(
            f"distance must be greater than 0 and at most {MAX_LEG_NM:.0f} nm "
            f"(got {raw})"
        )
    return value


def check_speed(raw: Any, label: str = "speed") -> float:
    value = parse_number(raw, label)
    if not math.isfinite(value) or value < 0.0:
        raise UserError(f"{label} must be 0 knots or more (got {raw})")
    return value


def check_hours(raw: Any) -> float:
    value = parse_number(raw, "hours")
    if not math.isfinite(value) or value <= 0.0:
        raise UserError(f"hours must be greater than 0 (got {raw})")
    return value


def parse_timestamp(raw: str) -> str:
    """Accept ISO-8601 with or without an offset; return canonical UTC 'Z'."""
    text = raw.strip()
    if not text:
        raise UserError(
            f"time must be an ISO-8601 UTC timestamp like {TIMESTAMP_EXAMPLE}"
        )
    candidate = text[:-1] + "+00:00" if text[-1] in "Zz" else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        raise UserError(
            f"time must be an ISO-8601 UTC timestamp like {TIMESTAMP_EXAMPLE} "
            f"(got {raw!r})"
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Store: load (never creates), validate shape, atomic save
# --------------------------------------------------------------------------


def empty_store() -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "origin_lat": None,
        "origin_lon": None,
        "legs": [],
        "fixes": [],
        "drift": None,
    }


def resolve_store_path(args: argparse.Namespace) -> Path:
    raw = getattr(args, "store", None) or os.environ.get(STORE_ENV_VAR) or DEFAULT_STORE
    return Path(os.path.expanduser(str(raw)))


def _ensure_mapping(entry: Any, where: str) -> None:
    if not isinstance(entry, dict):
        raise StoreError(f"{where} is not a JSON object")


def _require_fields(entry: dict[str, Any], fields: Sequence[str], where: str) -> None:
    missing = [name for name in fields if name not in entry]
    if missing:
        raise StoreError(f"{where} is missing {', '.join(missing)}")


def validate_store_shape(data: Any) -> dict[str, Any]:
    """Structural check so a hand-edited store fails loudly, never half-renders."""
    if not isinstance(data, dict):
        raise StoreError("top level is not a JSON object")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise StoreError("version is missing or not an integer")
    if version != STORE_VERSION:
        raise StoreError(
            f"unsupported store version {version} (this build reads {STORE_VERSION})"
        )
    for key in ("origin_lat", "origin_lon"):
        value = data.get(key)
        if value is not None and not isinstance(value, (int, float)):
            raise StoreError(f"{key} is not a number or null")
    for key in ("legs", "fixes"):
        if not isinstance(data.get(key), list):
            raise StoreError(f"{key} is not a list")
        for index, entry in enumerate(data[key]):
            _ensure_mapping(entry, f"{key}[{index}]")
            _require_fields(
                entry, LEG_FIELDS if key == "legs" else FIX_FIELDS,
                f"{key}[{index}]",
            )
    drift = data.get("drift")
    if drift is not None:
        _ensure_mapping(drift, "drift")
        _require_fields(drift, DRIFT_FIELDS, "drift")
    data.setdefault("origin_lat", None)
    data.setdefault("origin_lon", None)
    return data


def load_store(path: Path) -> dict[str, Any]:
    """Read the store.  A missing file is simply an empty passage."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_store()
    except IsADirectoryError:
        raise StoreError(f"cannot read store {path}: it is a directory") from None
    except OSError as exc:
        raise StoreError(
            f"cannot read store {path}: {exc.strerror or exc}"
        ) from None
    if not text.strip():
        raise StoreError(f"cannot read store {path}: the file is empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StoreError(
            f"cannot read store {path}: invalid JSON at line {exc.lineno} "
            f"column {exc.colno}"
        ) from None
    except UnicodeDecodeError:
        raise StoreError(f"cannot read store {path}: not valid UTF-8 text") from None
    try:
        return validate_store_shape(data)
    except StoreError as exc:
        raise StoreError(f"cannot read store {path}: {exc}") from None


def save_store(path: Path, store: dict[str, Any]) -> None:
    """Write via a temp file in the same directory, then os.replace."""
    store["version"] = STORE_VERSION
    directory = path.parent if str(path.parent) else Path(".")
    temp_path: str | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(directory)
        )
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(store, stream, indent=2, sort_keys=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
    except OSError as exc:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise StoreError(
            f"cannot write store {path}: {exc.strerror or exc}"
        ) from None


# --------------------------------------------------------------------------
# Passage helpers
# --------------------------------------------------------------------------


def origin_of(store: dict[str, Any]) -> tuple[float, float] | None:
    lat, lon = store["origin_lat"], store["origin_lon"]
    if lat is None or lon is None:
        return None
    return (float(lat), float(lon))


def chain_start(store: dict[str, Any]) -> tuple[float, float]:
    """Where a new leg begins: the last arrival, else the origin, else 0,0."""
    legs = store["legs"]
    if legs:
        return (float(legs[-1]["end_lat"]), float(legs[-1]["end_lon"]))
    origin = origin_of(store)
    return origin if origin is not None else (0.0, 0.0)


def recompute_chain(store: dict[str, Any]) -> None:
    """Re-derive every arrival and cumulative distance from the origin."""
    lat, lon = origin_of(store) or (0.0, 0.0)
    cumulative = 0.0
    for leg in store["legs"]:
        leg["start_lat"] = quantize(lat)
        leg["start_lon"] = quantize(lon)
        end_lat, end_lon = destination_point(
            lat, lon, float(leg["course_deg"]), float(leg["distance_nm"])
        )
        leg["end_lat"] = quantize(end_lat)
        leg["end_lon"] = quantize(end_lon)
        cumulative += float(leg["distance_nm"])
        leg["cumulative_nm"] = quantize(cumulative)
        lat, lon = end_lat, end_lon


def next_fix_id(store: dict[str, Any]) -> int:
    return max((int(fix["id"]) for fix in store["fixes"]), default=0) + 1


def drift_snapshot(store: dict[str, Any]) -> tuple[float | None, float | None]:
    drift = store["drift"]
    if drift is None:
        return (None, None)
    return (float(drift["set_deg"]), float(drift["rate_kn"]))


@dataclass(frozen=True)
class Advance:
    """A resolved DR advance with the current offset vector-summed in."""

    from_lat: float
    from_lon: float
    vessel_course: float
    vessel_speed: float
    hours: float
    drift_set: float | None
    drift_rate: float | None
    ground_course: float
    ground_speed: float
    distance_nm: float
    lat: float
    lon: float


def compute_advance(
    lat: float,
    lon: float,
    course: float,
    speed: float,
    hours: float,
    drift: dict[str, Any] | None,
) -> Advance:
    """Sum the vessel and current velocity vectors, then advance the position."""
    north = speed * math.cos(_rad(course))
    east = speed * math.sin(_rad(course))
    drift_set = drift_rate = None
    if drift is not None:
        drift_set = float(drift["set_deg"])
        drift_rate = float(drift["rate_kn"])
        north += drift_rate * math.cos(_rad(drift_set))
        east += drift_rate * math.sin(_rad(drift_set))
    ground_speed = math.hypot(east, north)
    ground_course = (
        _deg(math.atan2(east, north)) % 360.0 if ground_speed > 0 else course
    )
    distance_nm = ground_speed * hours
    if distance_nm > 0:
        new_lat, new_lon = destination_point(lat, lon, ground_course, distance_nm)
    else:
        new_lat, new_lon = lat, lon
    return Advance(
        from_lat=lat,
        from_lon=lon,
        vessel_course=course,
        vessel_speed=speed,
        hours=hours,
        drift_set=drift_set,
        drift_rate=drift_rate,
        ground_course=ground_course,
        ground_speed=ground_speed,
        distance_nm=distance_nm,
        lat=quantize(new_lat),
        lon=quantize(new_lon),
    )


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def fmt_lat(value: float) -> str:
    return f"{0.0 if abs(value) < 0.0005 else value:.3f}"


def fmt_lon(value: float) -> str:
    return f"{0.0 if abs(value) < 0.0005 else value:.3f}"


def fmt_pos(lat: float, lon: float) -> str:
    return f"{fmt_lat(lat)},{fmt_lon(lon)}"


def fmt_trim(value: float, places: int = 2) -> str:
    return f"{value:.{places}f}"


def fmt_course(value: float) -> str:
    return f"{int(round(value)) % 360:03d}T"


def snapshot_offset(fix: dict[str, Any]) -> dict[str, Any] | None:
    """The current offset stored on a fix, or None when none was active."""
    if fix["offset_set_deg"] is None or fix["offset_drift_kn"] is None:
        return None
    return {"set_deg": fix["offset_set_deg"], "rate_kn": fix["offset_drift_kn"]}


def fmt_offset(drift: dict[str, Any] | None) -> str:
    if drift is None:
        return "-"
    return f"{fmt_course(float(drift['set_deg']))}/{float(drift['rate_kn']):.1f}"


def fmt_offset_words(drift: dict[str, Any] | None) -> str:
    if drift is None:
        return "none"
    course = fmt_course(float(drift["set_deg"]))
    return f"set {course} / {float(drift['rate_kn']):.2f} kn"


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    right_aligned: Sequence[bool],
) -> list[str]:
    """Fixed-width ASCII columns; two-space gutters; trailing blanks trimmed."""
    widths = [
        max([len(headers[index])] + [len(row[index]) for row in rows])
        for index in range(len(headers))
    ]
    lines = []
    for row_index, row in enumerate([list(headers)] + [list(row) for row in rows]):
        if row_index == 0:
            cells = [
                cell.ljust(widths[index]) for index, cell in enumerate(row)
            ]
        else:
            cells = [
                cell.rjust(widths[index]) if right_aligned[index]
                else cell.ljust(widths[index])
                for index, cell in enumerate(row)
            ]
        lines.append("  ".join(cells).rstrip())
    return lines


def render_legs(store: dict[str, Any]) -> str:
    origin = origin_of(store)
    if origin is None:
        head = "Leg table  origin not set (the first leg starts at 0.000,0.000)"
    else:
        head = f"Leg table  origin {fmt_pos(*origin)}"
    legs = store["legs"]
    if not legs:
        return "\n".join(
            [
                head,
                "",
                "No legs yet.  Start a table with:",
                "  deadreckon legs origin <lat> <lon>",
                "  deadreckon legs add <course> <nm>",
            ]
        )
    rows = [
        [
            str(leg["id"]),
            fmt_course(float(leg["course_deg"])),
            fmt_trim(float(leg["distance_nm"])),
            fmt_lat(float(leg["start_lat"])),
            fmt_lat(float(leg["end_lat"])),
            fmt_lon(float(leg["end_lon"])),
            fmt_trim(float(leg["cumulative_nm"])),
        ]
        for leg in legs
    ]
    headers = ("Leg", "Course", "Dist nm", "From Lat", "Arr Lat", "Arr Lon", "Cum nm")
    aligns = (True, True, True, True, True, True, True)
    total = float(legs[-1]["cumulative_nm"])
    lines = [head, ""] + render_table(headers, rows, aligns)
    lines.append("")
    lines.append(
        f"Total {fmt_trim(total)} nm over {len(legs)} leg"
        f"{'' if len(legs) == 1 else 's'}   final position "
        f"{fmt_pos(float(legs[-1]['end_lat']), float(legs[-1]['end_lon']))}"
    )
    return "\n".join(lines)


def render_fixes(store: dict[str, Any]) -> str:
    fixes = store["fixes"]
    if not fixes:
        return "\n".join(
            [
                "Fix log  empty",
                "",
                "No fixes recorded yet.  Record one with:",
                "  deadreckon fix <lat> <lon> --course <deg> --speed <kn>",
            ]
        )
    rows = [
        [
            str(fix["id"]),
            str(fix["recorded_utc"]),
            fmt_lat(float(fix["lat"])),
            fmt_lon(float(fix["lon"])),
            fmt_course(float(fix["course_deg"])),
            fmt_trim(float(fix["speed_kn"])),
            "dr" if fix["kind"] == "dr" else "obs",
            fmt_offset(snapshot_offset(fix)),
        ]
        for fix in fixes
    ]
    headers = ("Id", "Recorded UTC", "Lat", "Lon", "Crs", "Spd kn", "Kind", "Offset")
    aligns = (True, True, True, True, True, True, True, True)
    lines = [f"Fix log  {len(fixes)} fix{'' if len(fixes) == 1 else 'es'}", ""]
    lines += render_table(headers, rows, aligns)
    lines.append("")
    lines.append("Offset shows the current's set/rate snapshot taken at record time.")
    return "\n".join(lines)


def render_fix_result(store: dict[str, Any], fix: dict[str, Any],
                      previous: dict[str, Any] | None) -> str:
    label = "dr" if fix["kind"] == "dr" else "observed"
    lines = [
        f"Recorded fix {fix['id']} ({label})  {fix['recorded_utc']}",
        f"Position {fmt_pos(float(fix['lat']), float(fix['lon']))}"
        f"   course {fmt_course(float(fix['course_deg']))}"
        f"   speed {fmt_trim(float(fix['speed_kn']))} kn",
        "Offset snapshot " + fmt_offset_words(snapshot_offset(fix)),
    ]
    if previous is None:
        lines.append("This is the first fix in the log.")
    else:
        lines.append(
            f"Previous fix {previous['id']} at {previous['recorded_utc']} "
            f"{fmt_pos(float(previous['lat']), float(previous['lon']))}"
        )
    lines.append(f"The log now holds {len(store['fixes'])} fix"
                 f"{'' if len(store['fixes']) == 1 else 'es'}.")
    return "\n".join(lines)


def render_drift(store: dict[str, Any]) -> str:
    drift = store["drift"]
    if drift is None:
        return "\n".join(
            [
                "No current offset set.",
                "",
                "Set one with:  deadreckon drift <set-deg> <rate-kn>",
                "Clear it with: deadreckon drift clear",
            ]
        )
    return (
        f"Current offset  set {fmt_course(float(drift['set_deg']))}"
        f"  drift {float(drift['rate_kn']):.2f} kn"
        f"  (updated {drift['updated_utc']})"
    )


def render_advance(store: dict[str, Any], advance: Advance, source: str,
                   saved_fix: dict[str, Any] | None) -> str:
    drift = store["drift"]
    drift_text = (
        "none"
        if drift is None
        else f"set {fmt_course(float(drift['set_deg']))} / "
        f"{float(drift['rate_kn']):.2f} kn"
    )
    lines = [
        f"DR advance from {source} {fmt_pos(advance.from_lat, advance.from_lon)}"
        f"  course {fmt_course(advance.vessel_course)}"
        f"  speed {fmt_trim(advance.vessel_speed)} kn",
        f"Current offset  {drift_text}",
        f"Ground course {fmt_course(advance.ground_course)}"
        f"   ground speed {fmt_trim(advance.ground_speed)} kn",
        f"Distance run {fmt_trim(advance.distance_nm)} nm over "
        f"{advance.hours:.3f} h",
        f"New DR position {fmt_pos(advance.lat, advance.lon)}",
    ]
    if saved_fix is not None:
        lines.append(
            f"Saved as dr fix {saved_fix['id']} at {saved_fix['recorded_utc']}"
        )
    return "\n".join(lines)


def render_status(store: dict[str, Any], path: Path) -> str:
    exists = "exists" if path.exists() else "not created yet"
    origin = origin_of(store)
    legs = store["legs"]
    fixes = store["fixes"]
    lines = [f"Store       {path}  ({exists})"]
    lines.append(
        f"Origin      {fmt_pos(*origin)}"
        if origin is not None
        else "Origin      not set"
    )
    if legs:
        total = float(legs[-1]["cumulative_nm"])
        lines.append(
            f"Leg table   {len(legs)} leg{'' if len(legs) == 1 else 's'}, "
            f"{fmt_trim(total)} nm total"
        )
    else:
        lines.append("Leg table   no legs recorded")
    if fixes:
        latest = fixes[-1]
        lines.append(
            f"Fix log     {len(fixes)} fix{'' if len(fixes) == 1 else 'es'}, "
            f"latest #{latest['id']} at {latest['recorded_utc']}"
        )
        lines.append(
            "Latest fix  "
            f"{fmt_pos(float(latest['lat']), float(latest['lon']))}"
            f"  course {fmt_course(float(latest['course_deg']))}"
            f"  speed {fmt_trim(float(latest['speed_kn']))} kn"
        )
        if origin is not None:
            made_course, made_nm = inverse(
                origin[0],
                origin[1],
                float(latest["lat"]),
                float(latest["lon"]),
            )
            lines.append(
                f"Made good   {fmt_trim(made_nm)} nm on "
                f"{fmt_course(made_course)} from the origin"
            )
    else:
        lines.append("Fix log     no fixes recorded")
    lines.append(f"Offset      {fmt_offset_words(store['drift'])}")
    if not legs and not fixes and store["drift"] is None:
        lines.append("")
        lines.append("No legs, fixes or current offset recorded yet.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# JSON payloads (field names mirror the store)
# --------------------------------------------------------------------------


def legs_payload(store: dict[str, Any], path: Path) -> dict[str, Any]:
    legs = store["legs"]
    return {
        "store": str(path),
        "origin": (
            None
            if origin_of(store) is None
            else {"lat": store["origin_lat"], "lon": store["origin_lon"]}
        ),
        "legs": legs,
        "leg_count": len(legs),
        "total_nm": float(legs[-1]["cumulative_nm"]) if legs else 0.0,
    }


def fixes_payload(store: dict[str, Any], path: Path) -> dict[str, Any]:
    return {"store": str(path), "fixes": store["fixes"], "count": len(store["fixes"])}


def drift_payload(store: dict[str, Any], path: Path) -> dict[str, Any]:
    return {"store": str(path), "drift": store["drift"]}


def advance_payload(
    advance: Advance, saved_fix: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "advance": {
            "from": {
                "lat": quantize(advance.from_lat),
                "lon": quantize(advance.from_lon),
            },
            "vessel_course_deg": quantize(advance.vessel_course),
            "vessel_speed_kn": quantize(advance.vessel_speed),
            "hours": quantize(advance.hours),
            "offset_set_deg": (
                None if advance.drift_set is None else quantize(advance.drift_set)
            ),
            "offset_drift_kn": (
                None if advance.drift_rate is None else quantize(advance.drift_rate)
            ),
            "ground_course_deg": quantize(advance.ground_course),
            "ground_speed_kn": quantize(advance.ground_speed),
            "distance_nm": quantize(advance.distance_nm),
            "position": {"lat": advance.lat, "lon": advance.lon},
        },
        "saved_fix": saved_fix,
    }


def status_payload(store: dict[str, Any], path: Path) -> dict[str, Any]:
    legs = store["legs"]
    fixes = store["fixes"]
    return {
        "store": str(path),
        "store_exists": path.exists(),
        "origin": (
            None
            if origin_of(store) is None
            else {"lat": store["origin_lat"], "lon": store["origin_lon"]}
        ),
        "leg_count": len(legs),
        "total_nm": float(legs[-1]["cumulative_nm"]) if legs else 0.0,
        "fix_count": len(fixes),
        "latest_fix": fixes[-1] if fixes else None,
        "drift": store["drift"],
    }


def emit(as_json: bool, payload: dict[str, Any], human: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(human)


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------


def cmd_legs_list(args: argparse.Namespace, path: Path) -> int:
    store = load_store(path)
    emit(args.json, legs_payload(store, path), render_legs(store))
    return EXIT_OK


def cmd_legs_add(args: argparse.Namespace, path: Path) -> int:
    course = check_course(args.course)
    distance = check_distance(args.distance)
    store = load_store(path)
    start_lat, start_lon = chain_start(store)
    end_lat, end_lon = destination_point(start_lat, start_lon, course, distance)
    previous_cumulative = (
        float(store["legs"][-1]["cumulative_nm"]) if store["legs"] else 0.0
    )
    leg = {
        "id": len(store["legs"]) + 1,
        "course_deg": quantize(course),
        "distance_nm": quantize(distance),
        "start_lat": quantize(start_lat),
        "start_lon": quantize(start_lon),
        "end_lat": quantize(end_lat),
        "end_lon": quantize(end_lon),
        "cumulative_nm": quantize(previous_cumulative + distance),
        "created_utc": now_utc(),
    }
    store["legs"].append(leg)
    save_store(path, store)
    if len(store["legs"]) == 1 and origin_of(store) is None:
        from_text = (
            f"Started the table at 0.000,0.000 (no origin set; "
            "use 'deadreckon legs origin <lat> <lon>' to move it)."
        )
    else:
        from_text = f"Started from {fmt_pos(start_lat, start_lon)}."
    human = "\n".join(
        [
            f"Added leg {leg['id']}: {fmt_course(course)} "
            f"{fmt_trim(distance)} nm to {fmt_pos(end_lat, end_lon)}.",
            from_text,
            "",
            render_legs(store),
        ]
    )
    payload = legs_payload(store, path)
    payload["added_leg"] = leg
    emit(args.json, payload, human)
    return EXIT_OK


def cmd_legs_origin(args: argparse.Namespace, path: Path) -> int:
    lat = check_lat(args.lat)
    lon = check_lon(args.lon)
    store = load_store(path)
    store["origin_lat"] = quantize(lat)
    store["origin_lon"] = quantize(lon)
    recompute_chain(store)
    save_store(path, store)
    moved = len(store["legs"])
    human = "\n".join(
        [
            f"Origin set to {fmt_pos(lat, lon)}.",
            f"Recomputed {moved} leg{'' if moved == 1 else 's'} from the new origin.",
            "",
            render_legs(store),
        ]
    )
    payload = legs_payload(store, path)
    payload["moved_from_origin"] = moved
    emit(args.json, payload, human)
    return EXIT_OK


def cmd_legs_clear(args: argparse.Namespace, path: Path) -> int:
    store = load_store(path)
    count = len(store["legs"])
    if count == 0:
        human = "The leg table is already empty."
        emit(args.json, {"store": str(path), "cleared": 0, **legs_payload(store, path)},
             human)
        return EXIT_OK
    if not args.yes:
        raise UserError(
            f"refusing to clear {count} leg{'' if count == 1 else 's'} without "
            "confirmation; re-run with --yes"
        )
    store["legs"] = []
    save_store(path, store)
    human = (
        f"Cleared {count} leg{'' if count == 1 else 's'}; the table is now empty.\n"
        "The origin is unchanged."
    )
    payload = {"store": str(path), "cleared": count}
    payload.update(legs_payload(store, path))
    emit(args.json, payload, human)
    return EXIT_OK


def cmd_fix(args: argparse.Namespace, path: Path) -> int:
    lat = check_lat(args.lat)
    lon = check_lon(args.lon)
    course = check_course(args.course)
    speed = check_speed(args.speed)
    recorded = parse_timestamp(args.time) if args.time else now_utc()
    store = load_store(path)
    offset_set, offset_rate = drift_snapshot(store)
    previous = store["fixes"][-1] if store["fixes"] else None
    fix = {
        "id": next_fix_id(store),
        "recorded_utc": recorded,
        "lat": quantize(lat),
        "lon": quantize(lon),
        "course_deg": quantize(course),
        "speed_kn": quantize(speed),
        "kind": "observed",
        "offset_set_deg": offset_set,
        "offset_drift_kn": offset_rate,
    }
    store["fixes"].append(fix)
    save_store(path, store)
    payload = {
        "store": str(path),
        "fix": fix,
        "previous_fix": previous,
        "fix_count": len(store["fixes"]),
    }
    emit(args.json, payload, render_fix_result(store, fix, previous))
    return EXIT_OK


def cmd_fixes_list(args: argparse.Namespace, path: Path) -> int:
    store = load_store(path)
    emit(args.json, fixes_payload(store, path), render_fixes(store))
    return EXIT_OK


def cmd_fixes_last(args: argparse.Namespace, path: Path) -> int:
    store = load_store(path)
    latest = store["fixes"][-1] if store["fixes"] else None
    payload = {
        "store": str(path),
        "fix": latest,
        "count": len(store["fixes"]),
    }
    if latest is None:
        human = "\n".join(
            [
                "No fixes recorded yet.",
                "",
                "Record one with:",
                "  deadreckon fix <lat> <lon> --course <deg> --speed <kn>",
            ]
        )
    else:
        human = "\n".join(
            [
                f"Latest fix {latest['id']} "
                f"({'dr' if latest['kind'] == 'dr' else 'observed'})"
                f"  {latest['recorded_utc']}",
                f"Position {fmt_pos(float(latest['lat']), float(latest['lon']))}"
                f"   course {fmt_course(float(latest['course_deg']))}"
                f"   speed {fmt_trim(float(latest['speed_kn']))} kn",
                "Offset snapshot " + fmt_offset_words(snapshot_offset(latest)),
                f"The log holds {len(store['fixes'])} fix"
                f"{'' if len(store['fixes']) == 1 else 'es'}.",
            ]
        )
    emit(args.json, payload, human)
    return EXIT_OK


def cmd_fixes_undo(args: argparse.Namespace, path: Path) -> int:
    store = load_store(path)
    if not store["fixes"]:
        raise UserError("nothing to undo: the fix log is empty")
    removed = store["fixes"].pop()
    save_store(path, store)
    remaining = len(store["fixes"])
    human = (
        f"Undid fix {removed['id']} ({removed['recorded_utc']}); "
        f"the log now holds {remaining} fix{'' if remaining == 1 else 'es'}."
    )
    emit(args.json,
         {"store": str(path), "removed": removed, "count": remaining},
         human)
    return EXIT_OK


def cmd_drift(args: argparse.Namespace, path: Path) -> int:
    values = list(args.values)
    if not values:
        store = load_store(path)
        emit(args.json, drift_payload(store, path), render_drift(store))
        return EXIT_OK
    if len(values) == 1 and values[0].lower() == "clear":
        store = load_store(path)
        had = store["drift"] is not None
        store["drift"] = None
        save_store(path, store)
        human = (
            "Current offset cleared; advances now use vessel motion only."
            if had
            else "No current offset was set."
        )
        emit(args.json, {"store": str(path), "had_drift": had, "drift": None}, human)
        return EXIT_OK
    if len(values) != 2:
        raise UserError(
            "drift expects no arguments (show), 'clear', or "
            "<set-deg> <rate-kn> (set)"
        )
    set_deg = check_course(values[0], "set direction")
    rate = check_speed(values[1], "drift rate")
    store = load_store(path)
    store["drift"] = {
        "set_deg": quantize(set_deg),
        "rate_kn": quantize(rate),
        "updated_utc": now_utc(),
    }
    save_store(path, store)
    payload = drift_payload(store, path)
    emit(args.json, payload, render_drift(store))
    return EXIT_OK


def cmd_dr(args: argparse.Namespace, path: Path) -> int:
    hours = check_hours(args.hours)
    store = load_store(path)
    if args.from_pos is not None:
        if args.course is None or args.speed is None:
            raise UserError("--from requires --course <deg> and --speed <kn>")
        lat = check_lat(args.from_pos[0])
        lon = check_lon(args.from_pos[1])
        course = check_course(args.course)
        speed = check_speed(args.speed)
        source = "explicit position"
    else:
        if args.course is not None or args.speed is not None:
            raise UserError(
                "--course and --speed can only be used together with "
                "--from <lat> <lon>"
            )
        if not store["fixes"]:
            raise UserError(
                "no fix recorded yet; record one with 'deadreckon fix <lat> <lon> "
                "--course <deg> --speed <kn>' or pass --from <lat> <lon> "
                "--course <deg> --speed <kn>"
            )
        latest = store["fixes"][-1]
        lat = float(latest["lat"])
        lon = float(latest["lon"])
        course = float(latest["course_deg"])
        speed = float(latest["speed_kn"])
        source = f"fix {latest['id']}"
    advance = compute_advance(lat, lon, course, speed, hours, store["drift"])
    saved_fix = None
    if args.save:
        offset_set, offset_rate = drift_snapshot(store)
        saved_fix = {
            "id": next_fix_id(store),
            "recorded_utc": now_utc(),
            "lat": advance.lat,
            "lon": advance.lon,
            "course_deg": quantize(advance.vessel_course),
            "speed_kn": quantize(advance.vessel_speed),
            "kind": "dr",
            "offset_set_deg": offset_set,
            "offset_drift_kn": offset_rate,
        }
        store["fixes"].append(saved_fix)
        save_store(path, store)
    emit(args.json,
         {**advance_payload(advance, saved_fix), "store": str(path)},
         render_advance(store, advance, source, saved_fix))
    return EXIT_OK


def cmd_status(args: argparse.Namespace, path: Path) -> int:
    store = load_store(path)
    emit(args.json, status_payload(store, path), render_status(store, path))
    return EXIT_OK


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class CliParser(argparse.ArgumentParser):
    """Argparse that reports problems as one-line errors on stderr (exit 2)."""

    def error(self, message: str) -> NoReturn:
        print(f"deadreckon: error: {_rewrite_unknown_command(message)}",
              file=sys.stderr)
        self.exit(EXIT_USER_ERROR)


def _rewrite_unknown_command(message: str) -> str:
    """Turn argparse's 'invalid choice' wall into one actionable line."""
    match = re.search(r"invalid choice: '([^']*)'", message)
    if match:
        return (
            f"unknown command '{match.group(1)}'; run 'deadreckon --help' "
            "for the command list"
        )
    return message


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--store",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"store file to use (default: ${STORE_ENV_VAR} or {DEFAULT_STORE})",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print one JSON object on stdout instead of the text table",
    )

    parser = CliParser(
        prog="deadreckon",
        description=(
            f"{APP_NAME} {VERSION} - offline dead reckoning: leg table, "
            "fix log, drift offset."
        ),
        epilog=(
            "examples:\n"
            "  deadreckon legs origin 50.800 -1.100\n"
            "  deadreckon legs add 090 12.5\n"
            "  deadreckon fix 50.800 -1.100 --course 090 --speed 6.5\n"
            "  deadreckon drift 045 1.2\n"
            "  deadreckon dr 2.5 --save\n"
            "  deadreckon fixes --json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"deadreckon {VERSION}")
    parser.add_argument(
        "--store",
        metavar="PATH",
        default=None,
        help=f"store file to use (default: ${STORE_ENV_VAR} or {DEFAULT_STORE})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="print one JSON object on stdout instead of the text table",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    legs = commands.add_parser(
        "legs", parents=[common],
        help="list the course-and-distance leg table",
        description="List the leg table (no action) or edit it.",
    )
    legs.set_defaults(handler=cmd_legs_list)
    leg_actions = legs.add_subparsers(dest="action", metavar="<action>")
    add = leg_actions.add_parser(
        "add", parents=[common], help="append a leg and reprint the table",
        description="Append a leg from the end of the table and reprint it.",
    )
    add.add_argument("course", metavar="COURSE",
                     help="course in degrees true (0-360)")
    add.add_argument("distance", metavar="DISTANCE",
                     help="distance run in nautical miles (> 0)")
    add.set_defaults(handler=cmd_legs_add)
    origin = leg_actions.add_parser(
        "origin", parents=[common], help="set or move the table origin",
        description="Set the origin and recompute every arrival from it.",
    )
    origin.add_argument("lat", metavar="LAT", help="origin latitude, decimal degrees")
    origin.add_argument("lon", metavar="LON", help="origin longitude, decimal degrees")
    origin.set_defaults(handler=cmd_legs_origin)
    clear = leg_actions.add_parser(
        "clear", parents=[common], help="clear the whole leg table",
        description="Clear the leg table (requires --yes). The origin is kept.",
    )
    clear.add_argument("--yes", action="store_true", help="confirm the clear")
    clear.set_defaults(handler=cmd_legs_clear)

    fix = commands.add_parser(
        "fix", parents=[common],
        help="record one DR fix in the log",
        description=(
            "Record one fix with its UTC time, position, course and speed, "
            "stamping the active current offset."
        ),
    )
    fix.add_argument("lat", metavar="LAT", help="latitude, decimal degrees")
    fix.add_argument("lon", metavar="LON", help="longitude, decimal degrees")
    fix.add_argument("--course", required=True, metavar="DEG",
                     help="course in degrees true (0-360)")
    fix.add_argument("--speed", required=True, metavar="KN",
                     help="speed through the water in knots (>= 0)")
    fix.add_argument("--time", metavar="ISO8601",
                     help=f"UTC timestamp (default: now, e.g. {TIMESTAMP_EXAMPLE})")
    fix.set_defaults(handler=cmd_fix)

    fixes = commands.add_parser(
        "fixes", parents=[common],
        help="list the fix log, or show/undo the latest fix",
        description="List the DR fix log (no action), show or undo the latest fix.",
    )
    fixes.set_defaults(handler=cmd_fixes_list)
    fix_actions = fixes.add_subparsers(dest="action", metavar="<action>")
    last = fix_actions.add_parser(
        "last", parents=[common], help="show the most recent fix",
        description="Show the most recent fix in the log.",
    )
    last.set_defaults(handler=cmd_fixes_last)
    undo = fix_actions.add_parser(
        "undo", parents=[common], help="remove the most recent fix",
        description="Remove the most recent fix from the log.",
    )
    undo.set_defaults(handler=cmd_fixes_undo)

    drift = commands.add_parser(
        "drift", parents=[common],
        help="show, set or clear the current drift offset",
        description=(
            "Show the active current offset (no arguments), set it with "
            "'drift <set-deg> <rate-kn>', or clear it with 'drift clear'."
        ),
    )
    drift.add_argument(
        "values", nargs="*", metavar="ARG",
        help="nothing to show, 'clear' to remove, or '<set-deg> <rate-kn>' to set",
    )
    drift.set_defaults(handler=cmd_drift)

    dr = commands.add_parser(
        "dr", parents=[common],
        help="advance a DR position with the current offset applied",
        description=(
            "Advance from the latest fix (or --from) over an elapsed time in "
            "hours with the current offset vector-summed in."
        ),
    )
    dr.add_argument("hours", metavar="HOURS", help="elapsed time in hours (> 0)")
    dr.add_argument("--from", dest="from_pos", nargs=2, metavar=("LAT", "LON"),
                    help="start from this position instead of the latest fix")
    dr.add_argument("--course", metavar="DEG",
                    help="vessel course in degrees true (with --from)")
    dr.add_argument("--speed", metavar="KN",
                    help="vessel speed in knots (with --from)")
    dr.add_argument("--save", action="store_true",
                    help="append the result to the fix log as a dr fix")
    dr.set_defaults(handler=cmd_dr)

    status = commands.add_parser(
        "status", parents=[common],
        help="show the store path, origin, counts, offset and latest fix",
        description="Report exactly what state the tool is in.",
    )
    status.set_defaults(handler=cmd_status)

    return parser


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    handler = getattr(args, "handler", None)
    if handler is None:
        print(
            "deadreckon: error: no command given; run 'deadreckon --help' "
            "for the command list",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    path = resolve_store_path(args)
    return handler(args, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except UserError as exc:
        print(f"deadreckon: error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    except StoreError as exc:
        print(f"deadreckon: store error: {exc}", file=sys.stderr)
        return EXIT_STORE_ERROR
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. `| head`); exit quietly.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
