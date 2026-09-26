"""HTTP 入口：REST 路由 + JSON 错误处理。启动时自动应用数据库迁移。"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import service
from .db import apply_migrations, connect

_write_lock = threading.Lock()


def _compile(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template) + "$")


ROUTES = [
    ("GET", _compile("/health"), service.health),
    ("POST", _compile("/vehicles"), service.create_vehicle),
    ("GET", _compile("/vehicles"), service.list_vehicles),
    ("POST", _compile("/vehicles/{vehicle_ref}/inspections"), service.add_inspection),
    ("POST", _compile("/vehicles/{vehicle_ref}/breakdown"), service.vehicle_breakdown),
    ("POST", _compile("/routes"), service.create_route),
    ("POST", _compile("/routes/{route_ref}/revisions"), service.add_route_revision),
    ("POST", _compile("/trips"), service.create_trip),
    ("GET", _compile("/trips/{trip_ref}"), service.get_trip),
    ("POST", _compile("/trips/{trip_ref}/stop-windows"), service.add_stop_window),
    ("POST", _compile("/trips/{trip_ref}/deck-zones"), service.set_deck_zone),
    ("POST", _compile("/trips/{trip_ref}/scans"), service.record_scan),
    ("POST", _compile("/trips/{trip_ref}/headcounts"), service.report_headcount),
    ("POST", _compile("/trips/{trip_ref}/inventory"), service.load_inventory),
    ("POST", _compile("/trips/{trip_ref}/sales"), service.record_sale),
    ("POST", _compile("/trips/{trip_ref}/departure-check"), service.departure_check),
    ("POST", _compile("/trips/{trip_ref}/depart"), service.depart),
    ("POST", _compile("/trips/{trip_ref}/deck-closures"), service.close_deck),
    ("POST", _compile("/trips/{trip_ref}/complete"), service.complete_trip),
    ("GET", _compile("/trips/{trip_ref}/replay"), service.replay),
    ("POST", _compile("/food-lots"), service.create_food_lot),
    ("POST", _compile("/food-lots/{food_lot}/suspend"), service.suspend_food_lot),
    ("POST", _compile("/safety-events"), service.add_safety_event),
    ("POST", _compile("/safety-events/{event_ref}/responsibility"), service.add_responsibility_link),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "TeaBusControl/1.0"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        body: dict = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": "invalid_json", "message": "请求体不是合法 JSON"})
                    return
                if not isinstance(body, dict):
                    self._send(400, {"error": "invalid_json", "message": "请求体必须是 JSON 对象"})
                    return
        path = self.path.split("?", 1)[0]
        for route_method, pattern, fn in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            data = {**body, **match.groupdict()}
            database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
            with _write_lock:
                conn = connect(database_path)
                try:
                    with conn:
                        status, payload = fn(conn, data)
                except service.ServiceError as exc:
                    self._send(exc.status, {"error": exc.code, "message": exc.message})
                    return
                finally:
                    conn.close()
            self._send(status, payload)
            return
        self._send(404, {"error": "not_found", "message": f"接口不存在：{method} {path}"})

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
    if database_path != ":memory:":
        os.makedirs(os.path.dirname(database_path) or ".", exist_ok=True)
    conn = connect(database_path)
    apply_migrations(conn)
    conn.close()
    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
