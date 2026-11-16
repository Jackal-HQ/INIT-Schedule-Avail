#!/usr/bin/env python3
"""
Room availability from FIU 25Live -> CSV.

Give it a list of rooms (e.g. CASE132 CASE138) and a date range and it writes
a CSV of when each room is free (or busy, or both).

It uses the same public JSON endpoints that the 25Live "Availability Weekly"
page calls in the browser (found with the Playwright MCP), so no browser or
login is needed:

    spaces.json?name=<room>                  -> room name -> space_id, capacity
    rm_reservations.json?space_id=..&start_dt=..&end_dt=..   -> bookings

Examples
    python room_availability.py CASE132 CASE138
    python room_availability.py -f rooms.txt --start 2026-11-16 --days 14 -o avail.csv
    python room_availability.py CASE134 --show busy
    python room_availability.py CASE134 --show all --hours 08:00-20:00

The same logic powers the web UI (see app.py). Only the standard library is used.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta

BASE = "https://25live.collegenet.com/25live/data/fiu/run"
INCLUDE = "closed blackouts pending related empty"  # same flags the website sends
HEADERS = {"User-Agent": "room-availability-script/1.0", "Accept": "application/json"}
CSV_COLUMNS = ["room", "capacity", "date", "weekday", "start", "end", "status", "event", "event_state"]


class ApiError(Exception):
    """25Live could not be reached or returned an error."""


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def get_json(endpoint: str, params: dict) -> dict:
    url = f"{BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise ApiError(f"25Live returned HTTP {e.code}")
    except urllib.error.URLError as e:
        raise ApiError(f"Could not reach 25Live ({e.reason})")


def as_list(x) -> list:
    """25Live returns a dict for one item and a list for many."""
    if x is None or x == "":
        return []
    return x if isinstance(x, list) else [x]


def norm(name: str) -> str:
    return re.sub(r"[\s_-]+", "", name).upper()


def search_spaces(query: str) -> list[dict]:
    """All spaces whose name contains `query` (25Live does a 'contains' match)."""
    data = get_json("spaces.json", {"name": query, "caller": "room-availability"})
    return as_list(data.get("spaces", {}).get("space"))


def find_space(room: str) -> tuple[dict | None, list[str]]:
    """Resolve 'CASE132' to its space record (exact match). Returns (space, suggestions)."""
    spaces = search_spaces(room)
    for s in spaces:
        if norm(s["space_name"]) == norm(room):
            return s, []
    return None, [s["space_name"] for s in spaces[:8]]


def get_bookings(space_id: int, start: date, end: date) -> list[tuple[datetime, datetime, dict]]:
    """Return (start, end, event-info) for every booking, as naive local datetimes."""
    data = get_json("rm_reservations.json", {
        "space_id": space_id,
        "start_dt": f"{start.isoformat()}T00:00:00",
        "end_dt": f"{end.isoformat()}T23:59:00",
        "include": INCLUDE,
        "caller": "room-availability",
    })
    out = []
    for r in as_list(data.get("space_reservations", {}).get("space_reservation")):
        # Timestamps carry an offset (e.g. -05:00); wall-clock time is what we want.
        s = datetime.fromisoformat(r["reservation_start_dt"]).replace(tzinfo=None)
        e = datetime.fromisoformat(r["reservation_end_dt"]).replace(tzinfo=None)
        ev = r.get("event", {}) or {}
        out.append((s, e, {
            "event": ev.get("event_name") or ev.get("event_title") or "",
            "state": ev.get("state_name", ""),
        }))
    return out


# --------------------------------------------------------------------------- #
# Interval logic
# --------------------------------------------------------------------------- #
def merge(intervals):
    """Merge overlapping/touching (start, end) intervals."""
    merged = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def free_windows(day_start: datetime, day_end: datetime, busy):
    """Gaps inside [day_start, day_end] not covered by the busy intervals."""
    clipped = [(max(s, day_start), min(e, day_end)) for s, e in busy if e > day_start and s < day_end]
    free, cursor = [], day_start
    for s, e in merge(clipped):
        if s > cursor:
            free.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < day_end:
        free.append((cursor, day_end))
    return free


def fmt(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def minutes(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


# --------------------------------------------------------------------------- #
# Core: rooms + dates -> rows
# --------------------------------------------------------------------------- #
def rows_for_room(space: dict, bookings, start: date, end: date,
                  open_t: time, close_t: time, show: str, min_minutes: int) -> list[dict]:
    rows = []
    cap = space.get("max_capacity", "")
    busy = [(s, e) for s, e, _ in bookings]
    day = start
    while day <= end:
        d_start = datetime.combine(day, open_t)
        d_end = datetime.combine(day, close_t)
        base = {"room": space["space_name"], "capacity": cap,
                "date": day.isoformat(), "weekday": day.strftime("%a")}

        if show in ("free", "all"):
            for s, e in free_windows(d_start, d_end, busy):
                if (e - s) >= timedelta(minutes=max(min_minutes, 1)):
                    rows.append({**base, "start": fmt(s), "end": fmt(e), "status": "free",
                                 "event": "", "event_state": "",
                                 "start_min": minutes(s), "end_min": minutes(e)})
        if show in ("busy", "all"):
            for s, e, info in bookings:
                cs, ce = max(s, d_start), min(e, d_end)
                if ce > cs:
                    rows.append({**base, "start": fmt(cs), "end": fmt(ce), "status": "busy",
                                 "event": info["event"], "event_state": info["state"],
                                 "start_min": minutes(cs), "end_min": minutes(ce)})
        day += timedelta(days=1)
    return rows


def collect_rows(rooms: list[str], start: date, end: date,
                 open_t: time = time(7, 0), close_t: time = time(22, 0),
                 show: str = "free", min_minutes: int = 0):
    """
    Look up every room and build availability rows.

    Returns (rows, missing) where `missing` is a list of {"room", "suggestions"}.
    Raises ApiError if 25Live can't be reached.
    """
    def work(room):
        space, suggestions = find_space(room)
        if not space:
            return room, None, [], suggestions
        return room, space, get_bookings(space["space_id"], start, end), []

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(work, rooms))  # keeps input order, re-raises ApiError

    rows, missing = [], []
    for room, space, bookings, suggestions in results:
        if space is None:
            missing.append({"room": room, "suggestions": suggestions})
            continue
        rows += rows_for_room(space, bookings, start, end, open_t, close_t, show, min_minutes)

    rows.sort(key=lambda r: (r["room"], r["date"], r["start_min"], r["status"]))
    return rows, missing


def write_csv(rows: list[dict], fileobj) -> None:
    w = csv.DictWriter(fileobj, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_hours(text: str) -> tuple[time, time]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", text)
    if not m:
        raise argparse.ArgumentTypeError("use HH:MM-HH:MM, e.g. 07:00-22:00")
    h1, m1, h2, m2 = map(int, m.groups())
    if (h2, m2) == (24, 0):
        return time(h1, m1), time(23, 59)
    try:
        return time(h1, m1), time(h2, m2)
    except ValueError:
        raise argparse.ArgumentTypeError("hours out of range")


def read_rooms(args) -> list[str]:
    rooms = list(args.rooms)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0]
                rooms += [p.strip() for p in line.replace(",", " ").split() if p.strip()]
    seen, unique = set(), []
    for r in rooms:
        if norm(r) not in seen:
            seen.add(norm(r))
            unique.append(r)
    return unique


def main():
    p = argparse.ArgumentParser(description="FIU 25Live room availability -> CSV")
    p.add_argument("rooms", nargs="*", help="room names, e.g. CASE132 CASE138")
    p.add_argument("-f", "--file", help="text file with room names (one per line or comma-separated)")
    p.add_argument("--start", type=date.fromisoformat, default=date.today(),
                   help="first date, YYYY-MM-DD (default: today)")
    p.add_argument("--days", type=int, default=7, help="number of days (default: 7)")
    p.add_argument("--end", type=date.fromisoformat, help="last date, inclusive (overrides --days)")
    p.add_argument("--hours", type=parse_hours, default=parse_hours("07:00-22:00"),
                   help="daily window to report, HH:MM-HH:MM (default: 07:00-22:00)")
    p.add_argument("--show", choices=["free", "busy", "all"], default="free",
                   help="rows to output (default: free)")
    p.add_argument("--min-minutes", type=int, default=0,
                   help="drop free gaps shorter than this many minutes")
    p.add_argument("-o", "--output", default="availability.csv", help="CSV path (default: availability.csv)")
    args = p.parse_args()

    rooms = read_rooms(args)
    if not rooms:
        p.error("give at least one room (positional args or --file)")
    end = args.end or (args.start + timedelta(days=args.days - 1))
    if end < args.start:
        p.error("end date is before start date")
    open_t, close_t = args.hours

    try:
        rows, missing = collect_rows(rooms, args.start, end, open_t, close_t,
                                     args.show, args.min_minutes)
    except ApiError as e:
        raise SystemExit(str(e))

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
        write_csv(rows, f)

    print(f"Wrote {len(rows)} rows for {len(rooms) - len(missing)} room(s) to {args.output}")
    for m in missing:
        hint = f" Did you mean: {', '.join(m['suggestions'])}?" if m["suggestions"] else ""
        print(f"  ! '{m['room']}' not found.{hint}", file=sys.stderr)
    if missing:
        sys.exit(1)


if __name__ == "__main__":
    main()
