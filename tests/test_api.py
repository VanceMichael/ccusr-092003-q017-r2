"""HTTP 层冒烟测试：真实启动服务进程内实例，验证路由与 JSON 交互。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from app.db import apply_migrations, connect
from app.main import Handler


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fd, cls.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        conn = connect(cls.db_path)
        apply_migrations(conn)
        conn.close()
        cls._old_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = cls.db_path
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        if cls._old_env is None:
            del os.environ["DATABASE_PATH"]
        else:
            os.environ["DATABASE_PATH"] = cls._old_env
        os.unlink(cls.db_path)

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self) -> None:
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_unknown_route_404(self) -> None:
        status, body = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_full_flow_over_http(self) -> None:
        self.assertEqual(self.call("POST", "/vehicles", {
            "vehicle_ref": "BUS-HTTP", "capacity_lower": 30, "capacity_upper": 20,
        })[0], 201)
        self.assertEqual(self.call("POST", "/routes", {
            "route_ref": "ROUTE-HTTP", "stops": ["春熙路", "太古里"],
        })[0], 201)
        self.assertEqual(self.call("POST", "/trips", {
            "trip_ref": "TRIP-HTTP", "vehicle_ref": "BUS-HTTP", "route_ref": "ROUTE-HTTP",
            "route_revision": 1, "service_date": "2026-09-26",
        })[0], 201)
        # 未满足三要素，发车被拒
        status, body = self.call("POST", "/trips/TRIP-HTTP/depart", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "departure_blocked")
        # 补齐三要素
        self.call("POST", "/vehicles/BUS-HTTP/inspections", {
            "result": "pass", "inspected_at": "2026-09-26T08:00:00+08:00",
        })
        self.call("POST", "/food-lots", {"food_lot": "LOT-HTTP", "item_name": "盖碗茶", "quantity": 20})
        self.call("POST", "/trips/TRIP-HTTP/inventory", {"food_lot": "LOT-HTTP", "quantity": 20})
        self.call("POST", "/trips/TRIP-HTTP/scans", {
            "event_id": "EVT-H1", "passenger_ref": "PAX-H1",
            "passenger_event": "boarded", "occurred_at": "2026-09-26T08:05:00+08:00",
        })
        self.call("POST", "/trips/TRIP-HTTP/headcounts", {"reported_count": 1})
        status, _ = self.call("POST", "/trips/TRIP-HTTP/depart", {})
        self.assertEqual(status, 200)
        # 复盘可访问
        status, replay = self.call("GET", "/trips/TRIP-HTTP/replay")
        self.assertEqual(status, 200)
        self.assertEqual(replay["route"]["adopted_revision"], 1)
        self.assertEqual(len(replay["passenger_timeline"]), 1)
        # 食品停用：路径中的批次编号正确生效
        status, body = self.call("POST", "/food-lots/LOT-HTTP/suspend", {
            "reason": "抽检不合格", "occurred_at": "2026-09-26T10:00:00+08:00",
        })
        self.assertEqual(status, 200)
        self.assertEqual([t["trip_ref"] for t in body["affected_trips"]], ["TRIP-HTTP"])

    def test_invalid_json_400(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/vehicles", data=b"{not-json", method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
