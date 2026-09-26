
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.store import DomainError, Store
from scripts.migrate import migrate

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/app.sqlite3")
store = Store(DATABASE_PATH)

# (method, 路径模式, 处理函数)；路径参数以 {name} 表示
ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"^/health$"), "health"),
    ("POST", re.compile(r"^/vehicles$"), "create_vehicle"),
    ("GET", re.compile(r"^/vehicles/(?P<vehicle_ref>[^/]+)$"), "get_vehicle"),
    ("POST", re.compile(r"^/vehicles/(?P<vehicle_ref>[^/]+)/condition$"),
     "set_vehicle_condition"),
    ("POST", re.compile(r"^/routes$"), "create_route"),
    ("POST", re.compile(r"^/routes/(?P<route_ref>[^/]+)/revisions$"),
     "add_route_revision"),
    ("GET", re.compile(
        r"^/routes/(?P<route_ref>[^/]+)/revisions/(?P<revision>\d+)$"),
     "get_route_revision"),
    ("POST", re.compile(r"^/trips$"), "create_trip"),
    ("GET", re.compile(r"^/trips/(?P<trip_ref>[^/]+)$"), "get_trip"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/stop-windows$"),
     "add_stop_window"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/deck-zones$"),
     "set_deck_zone"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/passenger-events$"),
     "record_passenger_event"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/departure-check$"),
     "run_departure_check"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/depart$"), "depart_trip"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/load-food$"), "load_food"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/food-events$"),
     "record_food_event"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/reroute$"), "reroute_trip"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/breakdown$"),
     "report_breakdown"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/deck-closures$"),
     "close_deck"),
    ("POST", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/incidents$"),
     "report_incident"),
    ("GET", re.compile(r"^/trips/(?P<trip_ref>[^/]+)/replay$"), "replay_trip"),
    ("POST", re.compile(r"^/food-lots$"), "create_food_lot"),
    ("GET", re.compile(r"^/food-lots/(?P<lot_ref>[^/]+)$"), "get_food_lot"),
    ("POST", re.compile(r"^/food-lots/(?P<lot_ref>[^/]+)/recall$"),
     "recall_food_lot"),
]


def dispatch(name: str, params: dict[str, str], body: dict) -> tuple[int, dict]:
    if name == "health":
        return 200, {"status": "ok"}
    if name == "create_vehicle":
        return 201, store.create_vehicle(body)
    if name == "get_vehicle":
        return 200, store.get_vehicle(params["vehicle_ref"])
    if name == "set_vehicle_condition":
        return 200, store.set_vehicle_condition(
            params["vehicle_ref"], body.get("status", ""))
    if name == "create_route":
        return 201, store.create_route(body)
    if name == "add_route_revision":
        return 201, store.add_route_revision(params["route_ref"], body)
    if name == "get_route_revision":
        return 200, store.get_route_revision(
            params["route_ref"], int(params["revision"]))
    if name == "create_trip":
        return 201, store.create_trip(body)
    if name == "get_trip":
        return 200, store.get_trip(params["trip_ref"])
    if name == "add_stop_window":
        return 201, store.add_stop_window(params["trip_ref"], body)
    if name == "set_deck_zone":
        return 200, store.set_deck_zone(params["trip_ref"], body)
    if name == "record_passenger_event":
        record, duplicated = store.record_passenger_event(
            params["trip_ref"], body)
        return 200, {"record": record, "duplicated": duplicated}
    if name == "run_departure_check":
        return 200, store.run_departure_check(
            params["trip_ref"], body.get("checked_by", ""))
    if name == "depart_trip":
        return 200, store.depart_trip(params["trip_ref"])
    if name == "load_food":
        return 201, store.load_food(params["trip_ref"], body)
    if name == "record_food_event":
        record, duplicated = store.record_food_event(params["trip_ref"], body)
        return 200, {"record": record, "duplicated": duplicated}
    if name == "reroute_trip":
        return 200, store.reroute_trip(params["trip_ref"], body)
    if name == "report_breakdown":
        return 200, store.report_breakdown(params["trip_ref"], body)
    if name == "close_deck":
        return 200, store.close_deck(params["trip_ref"], body)
    if name == "report_incident":
        return 201, store.report_incident(params["trip_ref"], body)
    if name == "replay_trip":
        return 200, store.replay_trip(params["trip_ref"])
    if name == "create_food_lot":
        return 201, store.create_food_lot(body)
    if name == "get_food_lot":
        return 200, store.get_food_lot(params["lot_ref"])
    if name == "recall_food_lot":
        return 200, store.recall_food_lot(
            params["lot_ref"], body.get("reason", ""))
    raise DomainError("未知接口", 404)


class Handler(BaseHTTPRequestHandler):
    def _handle(self) -> None:
        body: dict = {}
        if self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": "请求体不是合法的 JSON"})
                    return
                if not isinstance(body, dict):
                    self._send(400, {"error": "请求体必须是 JSON 对象"})
                    return
        path = self.path.split("?", 1)[0]
        for method, pattern, name in ROUTES:
            if method != self.command:
                continue
            match = pattern.match(path)
            if match:
                try:
                    status, payload = dispatch(name, match.groupdict(), body)
                except DomainError as exc:
                    self._send(exc.status, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001 - 兜底返回 500
                    self._send(500, {"error": f"内部错误：{exc}"})
                else:
                    self._send(status, payload)
                return
        self._send(404, {"error": "接口不存在"})

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    migrate(DATABASE_PATH)  # 启动时确保数据结构就绪（幂等）
    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
