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

Only the standard library is used.
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
from datetime import date, datetime, time, timedelta

BASE = "https://25live.collegenet.com/25live/data/fiu/run"
INCLUDE = "closed blackouts pending related empty"  # same flags the website sends
HEADERS = {"User-Agent": "room-availability-script/1.0", "Accept": "application/json"}


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
        raise SystemExit(f"HTTP {e.code} from 25Live for {url}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Could not reach 25Live ({e.reason})")


def as_list(x) -> list:
    """25Live returns a dict for one item and a list for many."""
    if x is None or x == "":
        return []
    return x if isinstance(x, list) else [x]


def norm(name: str) -> str:
    return re.sub(r"[\s_-]+", "", name).upper()


def find_space(room: str) -> dict | None:
    """Resolve a room name like 'CASE132' to its 25Live space record (exact match)."""
    data = get_json("spaces.json", {"name": room, "caller": "room-availability"})
    spaces = as_list(data.get("spaces", {}).get("space"))
    for s in spaces:
        if norm(s["space_name"]) == norm(room):
            return s
    close = ", ".join(s["space_name"] for s in spaces[:8])
    print(f"  ! '{room}' not found." + (f" Did you mean: {close}?" if close else ""),
          file=sys.stderr)
    return None


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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_hours(text: str) -> tuple[time, time]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", text)
    if not m:
        raise argparse.ArgumentTypeError("use HH:MM-HH:MM, e.g. 07:00-22:00")
    h1, m1, h2, m2 = map(int, m.groups())
    if h2 == 24 and m2 == 0:
        return time(h1, m1), time.max.replace(microsecond=0)
    return time(h1, m1), time(h2, m2)


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


def fmt(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


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

    rows, missing = [], []
    for room in rooms:
        print(f"{room}: ", end="", flush=True)
        space = find_space(room)
        if not space:
            missing.append(room)
            continue
        bookings = get_bookings(space["space_id"], args.start, end)
        print(f"{len(bookings)} bookings")
        cap = space.get("max_capacity", "")

        day = args.start
        while day <= end:
            d_start = datetime.combine(day, open_t)
            d_end = datetime.combine(day, close_t)
            base = {"room": space["space_name"], "capacity": cap,
                    "date": day.isoformat(), "weekday": day.strftime("%a")}
            busy = [(s, e) for s, e, _ in bookings]

            if args.show in ("free", "all"):
                for s, e in free_windows(d_start, d_end, busy):
                    if (e - s) >= timedelta(minutes=max(args.min_minutes, 1)):
                        rows.append({**base, "start": fmt(s), "end": fmt(e), "status": "free",
                                     "event": "", "event_state": "",
                                     "_sort": (space["space_name"], s)})
            if args.show in ("busy", "all"):
                for s, e, info in bookings:
                    cs, ce = max(s, d_start), min(e, d_end)
                    if ce > cs:
                        rows.append({**base, "start": fmt(cs), "end": fmt(ce), "status": "busy",
                                     "event": info["event"], "event_state": info["state"],
                                     "_sort": (space["space_name"], cs)})
            day += timedelta(days=1)

    rows.sort(key=lambda r: (r["room"], r["date"], r["_sort"][1]))
    cols = ["room", "capacity", "date", "weekday", "start", "end", "status", "event", "event_state"]
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"\nWrote {len(rows)} rows to {args.output}")
    if missing:
        print(f"Rooms not found: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
