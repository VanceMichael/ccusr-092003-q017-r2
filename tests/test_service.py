"""service 层核心业务规则测试。"""

import os
import tempfile
import unittest

from app import service
from app.db import apply_migrations, connect

T0 = "2026-09-26T08:00:00+08:00"
T1 = "2026-09-26T08:05:00+08:00"
T2 = "2026-09-26T08:10:00+08:00"
T3 = "2026-09-26T09:00:00+08:00"
T4 = "2026-09-26T10:00:00+08:00"


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.conn = connect(self.db_path)
        apply_migrations(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.db_path)

    def make_trip(self, trip_ref: str = "TRIP-1", vehicle_ref: str = "BUS-01") -> None:
        for call, payload in (
            (service.create_vehicle, {
                "vehicle_ref": vehicle_ref, "name": "茶歇巴士一号",
                "capacity_lower": 30, "capacity_upper": 20,
            }),
            (service.create_route, {
                "route_ref": "ROUTE-1", "name": "宽窄巷子环线",
                "stops": ["宽窄巷子", "人民公园", "天府广场"],
            }),
        ):
            try:
                call(self.conn, payload)
            except service.ServiceError as exc:
                if exc.status != 409:
                    raise
        service.create_trip(self.conn, {
            "trip_ref": trip_ref, "vehicle_ref": vehicle_ref, "route_ref": "ROUTE-1",
            "route_revision": 1, "service_date": "2026-09-26",
        })

    def board(self, trip_ref: str, event_id: str, passenger: str, deck: str = "lower", at: str = T1):
        return service.record_scan(self.conn, {
            "trip_ref": trip_ref, "event_id": event_id, "passenger_ref": passenger,
            "passenger_event": "boarded", "deck": deck, "occurred_at": at,
        })

    def prepare_departable(self, trip_ref: str = "TRIP-1", vehicle_ref: str = "BUS-01") -> None:
        service.add_inspection(self.conn, {
            "vehicle_ref": vehicle_ref, "result": "pass", "inspected_at": T0,
        })
        service.create_food_lot(self.conn, {
            "food_lot": "LOT-1", "item_name": "茉莉花茶点心拼盘", "quantity": 50,
        })
        service.load_inventory(self.conn, {"trip_ref": trip_ref, "food_lot": "LOT-1", "quantity": 40})
        self.board(trip_ref, "EVT-B1", "PAX-001")
        service.report_headcount(self.conn, {
            "trip_ref": trip_ref, "reported_count": 1, "reported_by": "STAFF-01",
        })


class DepartureGateTest(ServiceTestBase):
    def test_depart_blocked_when_all_missing(self) -> None:
        self.make_trip()
        with self.assertRaises(service.ServiceError) as ctx:
            service.depart(self.conn, {"trip_ref": "TRIP-1"})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "departure_blocked")

        status, payload = service.departure_check(self.conn, {"trip_ref": "TRIP-1"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["missing"], ["headcount", "inventory", "vehicle"])

    def test_depart_blocked_when_any_one_missing(self) -> None:
        self.make_trip()
        # 只有车况和库存，缺人数
        service.add_inspection(self.conn, {"vehicle_ref": "BUS-01", "result": "pass", "inspected_at": T0})
        service.create_food_lot(self.conn, {"food_lot": "LOT-1", "item_name": "点心", "quantity": 10})
        service.load_inventory(self.conn, {"trip_ref": "TRIP-1", "food_lot": "LOT-1", "quantity": 10})
        with self.assertRaises(service.ServiceError):
            service.depart(self.conn, {"trip_ref": "TRIP-1"})

    def test_headcount_must_match_scans(self) -> None:
        self.make_trip()
        service.add_inspection(self.conn, {"vehicle_ref": "BUS-01", "result": "pass", "inspected_at": T0})
        service.create_food_lot(self.conn, {"food_lot": "LOT-1", "item_name": "点心", "quantity": 10})
        service.load_inventory(self.conn, {"trip_ref": "TRIP-1", "food_lot": "LOT-1", "quantity": 10})
        self.board("TRIP-1", "EVT-B1", "PAX-001")
        self.board("TRIP-1", "EVT-B2", "PAX-002")
        # 站点仍按旧时刻表上报 1 人，与扫码实际 2 人不符 → 人数闸门不通过
        service.report_headcount(self.conn, {"trip_ref": "TRIP-1", "reported_count": 1})
        status, payload = service.departure_check(self.conn, {"trip_ref": "TRIP-1"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["missing"], ["headcount"])
        # 重新上报正确人数后放行
        service.report_headcount(self.conn, {"trip_ref": "TRIP-1", "reported_count": 2})
        status, payload = service.depart(self.conn, {"trip_ref": "TRIP-1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "departed")

    def test_failed_inspection_blocks_departure(self) -> None:
        self.make_trip()
        self.prepare_departable()
        service.add_inspection(self.conn, {"vehicle_ref": "BUS-01", "result": "fail", "inspected_at": T2})
        with self.assertRaises(service.ServiceError):
            service.depart(self.conn, {"trip_ref": "TRIP-1"})

    def test_depart_success_when_all_ready(self) -> None:
        self.make_trip()
        self.prepare_departable()
        status, payload = service.depart(self.conn, {"trip_ref": "TRIP-1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["onboard_count"], 1)


class IdempotencyTest(ServiceTestBase):
    def test_offline_scan_replay_does_not_double_count(self) -> None:
        self.make_trip()
        status1, first = self.board("TRIP-1", "EVT-OFFLINE-1", "PAX-001")
        self.assertEqual(status1, 201)
        self.assertFalse(first["deduplicated"])
        # 断网恢复后同一 event_id 重传
        status2, second = self.board("TRIP-1", "EVT-OFFLINE-1", "PAX-001")
        self.assertEqual(status2, 200)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["onboard_count"], 1)

    def test_same_event_id_with_different_payload_conflicts(self) -> None:
        self.make_trip()
        self.board("TRIP-1", "EVT-1", "PAX-001")
        with self.assertRaises(service.ServiceError) as ctx:
            self.board("TRIP-1", "EVT-1", "PAX-999")
        self.assertEqual(ctx.exception.code, "event_id_conflict")

    def test_offline_sale_replay_does_not_double_sell(self) -> None:
        self.make_trip()
        service.create_food_lot(self.conn, {"food_lot": "LOT-1", "item_name": "点心", "quantity": 10})
        service.load_inventory(self.conn, {"trip_ref": "TRIP-1", "food_lot": "LOT-1", "quantity": 5})
        sale = {"trip_ref": "TRIP-1", "event_id": "SALE-1", "food_lot": "LOT-1",
                "quantity": 2, "occurred_at": T1}
        status1, first = service.record_sale(self.conn, dict(sale))
        self.assertEqual(status1, 201)
        self.assertEqual(first["inventory"]["sold"], 2)
        status2, second = service.record_sale(self.conn, dict(sale))
        self.assertEqual(status2, 200)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["inventory"]["sold"], 2)
        self.assertEqual(second["inventory"]["remaining"], 3)

    def test_sale_exceeding_stock_rejected(self) -> None:
        self.make_trip()
        service.create_food_lot(self.conn, {"food_lot": "LOT-1", "item_name": "点心", "quantity": 10})
        service.load_inventory(self.conn, {"trip_ref": "TRIP-1", "food_lot": "LOT-1", "quantity": 2})
        with self.assertRaises(service.ServiceError) as ctx:
            service.record_sale(self.conn, {
                "trip_ref": "TRIP-1", "event_id": "SALE-X", "food_lot": "LOT-1",
                "quantity": 3, "occurred_at": T1,
            })
        self.assertEqual(ctx.exception.code, "insufficient_stock")


class DeckClosureTest(ServiceTestBase):
    def test_closure_lists_only_currently_onboard_upper_passengers(self) -> None:
        self.make_trip()
        self.board("TRIP-1", "EVT-1", "PAX-U1", deck="upper", at=T1)
        self.board("TRIP-1", "EVT-2", "PAX-U2", deck="upper", at=T1)
        self.board("TRIP-1", "EVT-3", "PAX-L1", deck="lower", at=T1)
        # PAX-U2 已下车，不应列入受影响
        service.record_scan(self.conn, {
            "trip_ref": "TRIP-1", "event_id": "EVT-4", "passenger_ref": "PAX-U2",
            "passenger_event": "alighted", "occurred_at": T2,
        })
        status, payload = service.close_deck(self.conn, {
            "trip_ref": "TRIP-1", "deck": "upper", "reason": "二层空调故障",
            "occurred_at": T3,
            "dispositions": [{"passenger_ref": "PAX-U1", "disposition": "moved_lower", "note": "引导至下层"}],
        })
        self.assertEqual(status, 201)
        affected = {a["passenger_ref"]: a["disposition"] for a in payload["affected"]}
        self.assertEqual(affected, {"PAX-U1": "moved_lower"})
        # 复盘中可见封闭记录与处置
        _, replay = service.replay(self.conn, {"trip_ref": "TRIP-1"})
        self.assertEqual(len(replay["deck_closures"]), 1)
        self.assertEqual(replay["deck_closures"][0]["affected"][0]["passenger_ref"], "PAX-U1")
        categories = [e["category"] for e in replay["safety_events"]]
        self.assertIn("deck_closure", categories)


class FoodSuspensionTest(ServiceTestBase):
    def test_suspension_only_affects_active_trips(self) -> None:
        self.make_trip("TRIP-ACTIVE", "BUS-01")
        self.make_trip("TRIP-DONE", "BUS-02")
        service.create_food_lot(self.conn, {"food_lot": "LOT-TEA", "item_name": "桂花糕", "quantity": 100})
        service.load_inventory(self.conn, {"trip_ref": "TRIP-ACTIVE", "food_lot": "LOT-TEA", "quantity": 30})
        service.load_inventory(self.conn, {"trip_ref": "TRIP-DONE", "food_lot": "LOT-TEA", "quantity": 20})
        service.record_sale(self.conn, {
            "trip_ref": "TRIP-ACTIVE", "event_id": "SALE-1", "food_lot": "LOT-TEA",
            "quantity": 5, "occurred_at": T1,
        })
        service.complete_trip(self.conn, {"trip_ref": "TRIP-DONE"})

        status, payload = service.suspend_food_lot(self.conn, {
            "food_lot": "LOT-TEA", "reason": "检出过敏原未标注", "occurred_at": T3,
            "responsibility": [{"actor_ref": "QA-01", "role": "食品安全员", "action": "下达停用指令"}],
        })
        self.assertEqual(status, 200)
        self.assertEqual([t["trip_ref"] for t in payload["affected_trips"]], ["TRIP-ACTIVE"])
        self.assertEqual(payload["affected_trips"][0]["suspended_qty"], 25)  # 30 - 5 已售
        self.assertEqual(payload["untouched_trips"], ["TRIP-DONE"])

        # 已完结班次的原始库存记录保留
        _, done_trip = service.get_trip(self.conn, {"trip_ref": "TRIP-DONE"})
        self.assertEqual(done_trip["inventory"][0]["suspended"], 0)
        self.assertEqual(done_trip["inventory"][0]["remaining"], 20)

        # 进行中班次复盘可见食品停用事件与责任链
        _, replay = service.replay(self.conn, {"trip_ref": "TRIP-ACTIVE"})
        events = {e["category"]: e for e in replay["safety_events"]}
        self.assertIn("food_suspension", events)
        chain = events["food_suspension"]["responsibility_chain"]
        self.assertEqual(chain[0]["actor_ref"], "QA-01")
        self.assertEqual(replay["tea_service"]["inventory"][0]["suspended"], 25)

    def test_suspended_lot_cannot_be_loaded(self) -> None:
        self.make_trip()
        service.create_food_lot(self.conn, {"food_lot": "LOT-1", "item_name": "点心", "quantity": 10})
        service.suspend_food_lot(self.conn, {
            "food_lot": "LOT-1", "reason": "抽检不合格", "occurred_at": T1,
        })
        with self.assertRaises(service.ServiceError) as ctx:
            service.load_inventory(self.conn, {"trip_ref": "TRIP-1", "food_lot": "LOT-1", "quantity": 5})
        self.assertEqual(ctx.exception.code, "lot_not_active")


class RouteRevisionTest(ServiceTestBase):
    def test_revision_keeps_original_plan(self) -> None:
        self.make_trip("TRIP-1", "BUS-01")
        self.make_trip("TRIP-2", "BUS-02")
        status, payload = service.add_route_revision(self.conn, {
            "route_ref": "ROUTE-1",
            "stops": ["宽窄巷子", "锦里", "天府广场"],
            "reason": "人民公园路段临时管制",
            "occurred_at": T3,
            "apply_to_trips": ["TRIP-1"],
            "responsibility": [{"actor_ref": "DISP-01", "role": "调度员", "action": "批准改道"}],
        })
        self.assertEqual(status, 201)
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(len(payload["affected_trips"]), 1)

        # TRIP-1 采用新版，原计划保留；TRIP-2 不受影响
        _, trip1 = service.get_trip(self.conn, {"trip_ref": "TRIP-1"})
        self.assertEqual(trip1["planned_revision"], 1)
        self.assertEqual(trip1["actual_revision"], 2)
        _, trip2 = service.get_trip(self.conn, {"trip_ref": "TRIP-2"})
        self.assertEqual(trip2["actual_revision"], 1)

        _, replay = service.replay(self.conn, {"trip_ref": "TRIP-1"})
        self.assertEqual(replay["route"]["planned_stops"], ["宽窄巷子", "人民公园", "天府广场"])
        self.assertEqual(replay["route"]["adopted_stops"], ["宽窄巷子", "锦里", "天府广场"])
        categories = [e["category"] for e in replay["safety_events"]]
        self.assertIn("road_adjustment", categories)


class BreakdownTest(ServiceTestBase):
    def test_breakdown_marks_only_active_trips(self) -> None:
        self.make_trip("TRIP-1", "BUS-01")
        self.make_trip("TRIP-2", "BUS-01")
        service.complete_trip(self.conn, {"trip_ref": "TRIP-2"})
        status, payload = service.vehicle_breakdown(self.conn, {
            "vehicle_ref": "BUS-01", "description": "发动机故障", "occurred_at": T3,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["affected_trips"], ["TRIP-1"])
        _, trip1 = service.get_trip(self.conn, {"trip_ref": "TRIP-1"})
        self.assertEqual(trip1["status"], "disrupted")
        _, trip2 = service.get_trip(self.conn, {"trip_ref": "TRIP-2"})
        self.assertEqual(trip2["status"], "completed")


class ReplayTest(ServiceTestBase):
    def test_replay_shows_timeline_counts_and_chain(self) -> None:
        self.make_trip()
        self.prepare_departable()
        service.record_sale(self.conn, {
            "trip_ref": "TRIP-1", "event_id": "SALE-1", "food_lot": "LOT-1",
            "quantity": 3, "occurred_at": T2,
        })
        service.depart(self.conn, {"trip_ref": "TRIP-1"})
        self.board("TRIP-1", "EVT-B2", "PAX-002", deck="upper", at=T3)
        service.record_scan(self.conn, {
            "trip_ref": "TRIP-1", "event_id": "EVT-A1", "passenger_ref": "PAX-001",
            "passenger_event": "alighted", "occurred_at": T4,
        })
        service.add_safety_event(self.conn, {
            "event_ref": "SE-MANUAL-1", "trip_ref": "TRIP-1", "category": "passenger",
            "description": "乘客投诉二层晃动", "occurred_at": T4,
            "responsibility": [
                {"actor_ref": "DRV-01", "role": "驾驶员", "action": "减速并上报", "at": T4},
                {"actor_ref": "OPS-01", "role": "值班员", "action": "记录并跟进", "at": T4},
            ],
        })
        status, replay = service.replay(self.conn, {"trip_ref": "TRIP-1"})
        self.assertEqual(status, 200)
        # 各时点在车人数
        counts = [(e["passenger_ref"], e["onboard_after"]) for e in replay["passenger_timeline"]]
        self.assertEqual(counts, [("PAX-001", 1), ("PAX-002", 2), ("PAX-001", 1)])
        # 采用的路线
        self.assertEqual(replay["route"]["adopted_revision"], 1)
        # 茶歇处理结果
        self.assertEqual(replay["tea_service"]["inventory"][0]["sold"], 3)
        self.assertEqual(len(replay["tea_service"]["sales"]), 1)
        # 异常责任链
        manual = next(e for e in replay["safety_events"] if e["event_ref"] == "SE-MANUAL-1")
        self.assertEqual([l["actor_ref"] for l in manual["responsibility_chain"]], ["DRV-01", "OPS-01"])
        # 发车核查留痕
        self.assertTrue(any(c["missing"] == [] for c in replay["departure_checks"]))


class ValidationTest(ServiceTestBase):
    def test_time_must_have_offset(self) -> None:
        self.make_trip()
        with self.assertRaises(service.ServiceError) as ctx:
            self.board("TRIP-1", "EVT-1", "PAX-001", at="2026-09-26 08:00:00")
        self.assertEqual(ctx.exception.code, "invalid_time")

    def test_unknown_trip_404(self) -> None:
        with self.assertRaises(service.ServiceError) as ctx:
            service.get_trip(self.conn, {"trip_ref": "NOPE"})
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
