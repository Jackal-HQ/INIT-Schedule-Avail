# FIU 25Live Room Availability

Pick rooms and a date range, see when each room is free (or busy), and export it as a CSV. Use it from a **web UI** or the **command line**.

It reads the same public JSON endpoints the [25Live Availability Weekly page](https://25live.collegenet.com/pro/fiu#!/home/search/location/availabilityWeekly/&spaces_name=CASE) uses (discovered with the Playwright MCP), so no browser, login, or third-party packages are needed.

## Requirements

- Python 3.9+ (standard library only)

## Web UI

```
python app.py --open
```

Then use the page at <http://127.0.0.1:8000> (`--port` to change it, `--open` launches your browser).

- **Rooms:** search a building (`CASE`) and click a room, or "Add all" matches. Type an exact room and press Enter, or paste a comma-separated list. Your last rooms and hours are remembered.
- **Dates / hours / minimum free gap:** date presets (Today, This week, Next 7/14 days), the daily window to report, and a minimum gap to hide short breaks between classes (default 30 min).
- **Results:** a summary, then a sortable/filterable **Table** or a day-by-day **Timeline**. Toggle Free / Busy / All.
- **Download CSV:** exports the rows for the current Free / Busy / All choice.
- Rooms that can't be found show a "did you mean" banner; click a suggestion to swap it in.

The server listens on `127.0.0.1` only. It is meant for local use; don't expose it to a network.

## Command line

```
python room_availability.py CASE132 CASE138
python room_availability.py -f rooms.txt --start 2026-11-16 --days 14 -o avail.csv
python room_availability.py CASE134 --show all --hours 08:00-20:00 --min-minutes 60
```

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

Rooms file example:

```
CASE132, CASE135
# a comment
CASE136
```

## CSV format

UTF-8 with BOM (so Excel opens it cleanly), columns:

`room, capacity, date, weekday, start, end, status, event, event_state`

- `status` is `free` or `busy`.
- `event` and `event_state` are filled only for `busy` rows.

```
room,capacity,date,weekday,start,end,status,event,event_state
CASE134,37,2026-11-16,Mon,7:00 AM,9:00 AM,free,,
CASE134,37,2026-11-16,Mon,9:00 AM,9:50 AM,busy,ENC 1101 U29M 83227 1268,Tentative
```

## Project layout

| File | Purpose |
| --- | --- |
| `room_availability.py` | Core logic + CLI (fetches from 25Live, computes free windows, writes CSV) |
| `app.py` | Local web server: serves the UI and a small JSON/CSV API |
| `web/index.html` | The single-page frontend (no build step, no CDN) |

### API (used by the UI)

| Endpoint | Description |
| --- | --- |
| `GET /api/rooms?q=CASE` | Rooms whose name contains `q` |
| `GET /api/availability` | `rooms`, `start`, `end`, `hours`, `show`, `min` → JSON rows |
| `GET /api/availability.csv` | Same parameters, returned as a CSV download |

Limits: 60 rooms and 120 days per request.

## How it works

1. `spaces.json?name=<room>` resolves a room name to its `space_id` and capacity (exact name match, case-insensitive).
2. `rm_reservations.json?space_id=…&start_dt=…&end_dt=…` returns the bookings for that room (rooms are fetched in parallel).
3. Bookings are merged and clipped to the daily window; the gaps are the free times.

## Notes

- Tentative and pending reservations count as busy, matching the site's default view.
- Results reflect only what 25Live lists; a room that looks free may still be unavailable for other reasons.
- This relies on undocumented endpoints, which could change without notice.
