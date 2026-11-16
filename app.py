#!/usr/bin/env python3
"""
Local web UI for the 25Live room availability tool.

    python app.py            # then open http://127.0.0.1:8000
    python app.py --port 9000 --open

Endpoints
    GET /                      the single-page frontend (web/index.html)
    GET /api/rooms?q=CASE      room search (name contains q)
    GET /api/availability      rooms=A,B&start=YYYY-MM-DD&end=YYYY-MM-DD
                               &hours=07:00-22:00&show=free|busy|all&min=0
    GET /api/availability.csv  same parameters, returned as a CSV download

Standard library only. Binds to localhost by default.
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import threading
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import room_availability as ra

WEB_DIR = Path(__file__).parent / "web"
MAX_ROOMS = 60
MAX_DAYS = 120


class BadRequest(Exception):
    pass


def parse_query(qs: dict):
    """Validate availability query params -> kwargs for ra.collect_rows."""
    raw = qs.get("rooms", [""])[0]
    rooms, seen = [], set()
    for part in raw.replace("\n", ",").split(","):
        part = part.strip()
        if part and ra.norm(part) not in seen:
            seen.add(ra.norm(part))
            rooms.append(part)
    if not rooms:
        raise BadRequest("Add at least one room.")
    if len(rooms) > MAX_ROOMS:
        raise BadRequest(f"Too many rooms (max {MAX_ROOMS}).")

    try:
        start = date.fromisoformat(qs.get("start", [""])[0] or date.today().isoformat())
        end = date.fromisoformat(qs.get("end", [""])[0] or (start + timedelta(days=6)).isoformat())
    except ValueError:
        raise BadRequest("Dates must look like YYYY-MM-DD.")
    if end < start:
        raise BadRequest("End date is before the start date.")
    if (end - start).days + 1 > MAX_DAYS:
        raise BadRequest(f"Date range too long (max {MAX_DAYS} days).")

    try:
        open_t, close_t = ra.parse_hours(qs.get("hours", ["07:00-22:00"])[0])
    except Exception:
        raise BadRequest("Hours must look like 07:00-22:00.")
    if close_t <= open_t:
        raise BadRequest("Closing time must be after opening time.")

    show = qs.get("show", ["free"])[0]
    if show not in ("free", "busy", "all"):
        raise BadRequest("show must be free, busy or all.")
    try:
        min_minutes = max(0, int(qs.get("min", ["0"])[0] or 0))
    except ValueError:
        raise BadRequest("min must be a number of minutes.")

    return dict(rooms=rooms, start=start, end=end, open_t=open_t, close_t=close_t,
                show=show, min_minutes=min_minutes)


class Handler(BaseHTTPRequestHandler):
    server_version = "RoomAvailability/1.0"

    # -- helpers ----------------------------------------------------------- #
    def send_bytes(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload):
        self.send_bytes(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

    # -- routing ----------------------------------------------------------- #
    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path == "/api/rooms":
                return self.api_rooms(qs)
            if url.path == "/api/availability":
                return self.api_availability(qs, as_csv=False)
            if url.path == "/api/availability.csv":
                return self.api_availability(qs, as_csv=True)
            return self.serve_static(url.path)
        except BadRequest as e:
            self.send_json(400, {"error": str(e)})
        except ra.ApiError as e:
            self.send_json(502, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # keep the server alive on unexpected bugs
            self.log_error("unhandled error: %r", e)
            self.send_json(500, {"error": "Something went wrong on the server."})

    def api_rooms(self, qs):
        q = qs.get("q", [""])[0].strip()
        if len(q) < 2:
            return self.send_json(200, {"rooms": []})
        spaces = ra.search_spaces(q)
        rooms = [{"name": s["space_name"], "capacity": s.get("max_capacity", ""),
                  "description": s.get("formal_name", "")} for s in spaces]
        rooms.sort(key=lambda r: r["name"])
        self.send_json(200, {"rooms": rooms})

    def api_availability(self, qs, as_csv: bool):
        params = parse_query(qs)
        rows, missing = ra.collect_rows(**params)
        if as_csv:
            buf = io.StringIO(newline="")
            ra.write_csv(rows, buf)
            body = b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")  # BOM for Excel
            name = f"availability_{params['start']}_{params['end']}.csv"
            return self.send_bytes(200, body, "text/csv; charset=utf-8",
                                   {"Content-Disposition": f'attachment; filename="{name}"'})
        self.send_json(200, {"rows": rows, "missing": missing,
                             "start": params["start"].isoformat(), "end": params["end"].isoformat(),
                             "open_min": params["open_t"].hour * 60 + params["open_t"].minute,
                             "close_min": params["close_t"].hour * 60 + params["close_t"].minute})

    def serve_static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            return self.send_json(404, {"error": "Not found"})
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_bytes(200, target.read_bytes(), ctype)

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" in (args[0] if args else ""):
            print(f"{self.address_string()} {fmt % args}")


def main():
    p = argparse.ArgumentParser(description="Web UI for 25Live room availability")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--open", action="store_true", help="open the browser on start")
    args = p.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Room availability UI running at {url}  (Ctrl+C to stop)")
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
