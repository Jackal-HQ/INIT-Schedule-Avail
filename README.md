# FIU 25Live Room Availability

Give it a list of rooms and a date range, get back a CSV of when each room is free (or busy).

It reads the same public JSON endpoints the [25Live Availability Weekly page](https://25live.collegenet.com/pro/fiu#!/home/search/location/availabilityWeekly/&spaces_name=CASE) uses (discovered with the Playwright MCP), so no browser, login, or third-party packages are needed.

## Requirements

- Python 3.9+ (standard library only)

## Usage

```
python room_availability.py CASE132 CASE138
python room_availability.py -f rooms.txt --start 2026-11-16 --days 14 -o avail.csv
python room_availability.py CASE134 --show all --hours 08:00-20:00 --min-minutes 60
```

### Options

| Option | Description | Default |
| --- | --- | --- |
| `rooms` | Room names as positional args, e.g. `CASE132 CASE138` | – |
| `-f, --file` | Text file of room names (one per line or comma-separated, `#` comments allowed) | – |
| `--start` | First date, `YYYY-MM-DD` | today |
| `--days` | Number of days to cover | `7` |
| `--end` | Last date (inclusive); overrides `--days` | – |
| `--hours` | Daily window to report, `HH:MM-HH:MM` | `07:00-22:00` |
| `--show` | `free`, `busy`, or `all` | `free` |
| `--min-minutes` | Drop free gaps shorter than this | `0` |
| `-o, --output` | Output CSV path | `availability.csv` |

### Rooms file example

```
CASE132, CASE135
# a comment
CASE136
```

## Output

CSV (UTF-8 with BOM so Excel opens it cleanly) with columns:

`room, capacity, date, weekday, start, end, status, event, event_state`

- `status` is `free` or `busy`.
- `event` and `event_state` are filled only for `busy` rows.

```
room,capacity,date,weekday,start,end,status,event,event_state
CASE134,37,2026-11-16,Mon,7:00 AM,9:00 AM,free,,
CASE134,37,2026-11-16,Mon,9:00 AM,9:50 AM,busy,ENC 1101 U29M 83227 1268,Tentative
```

## How it works

1. `spaces.json?name=<room>` resolves a room name to its `space_id` and capacity (exact name match, case-insensitive).
2. `rm_reservations.json?space_id=…&start_dt=…&end_dt=…` returns the bookings for that room.
3. Bookings are merged and clipped to the daily window; the gaps are the free times.

## Notes

- Unknown room names print suggestions and the script exits with code 1; other rooms are still written.
- Tentative and pending reservations count as busy, matching the site's default view.
- Results reflect only what 25Live lists; a room that looks free may still be unavailable for other reasons.
- This relies on undocumented endpoints, which could change without notice.
