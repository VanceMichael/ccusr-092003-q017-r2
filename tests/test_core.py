
import os
import tempfile
import unittest

from app.store import DomainError, Store
from scripts.migrate import migrate


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.db_path)
        migrate(self.db_path)
        self.store = Store(self.db_path)
        # 基础档案
        self.store.create_vehicle({
            "vehicle_ref": "BUS-01", "capacity_lower": 20, "capacity_upper": 15,
        })
        self.store.create_route({"route_ref": "R-1", "name": "宽窄巷子环线"})
        self.store.add_route_revision("R-1", {
            "revision": 1, "stops": ["人民公园", "宽窄巷子", "琴台路"],
        })
        self.store.add_route_revision("R-1", {
            "revision": 2, "stops": ["人民公园", "天府广场", "琴台路"],
            "note": "临时改道",
        })
        self.store.create_trip({
            "trip_ref": "TRIP-1", "vehicle_ref": "BUS-01", "route_ref": "R-1",
            "planned_revision": 1, "planned_departure": "2026-09-26T09:00:00+08:00",
        })
        self.store.create_food_lot({
            "lot_ref": "LOT-A", "name": "茉莉花茶点", "quantity": 50,
        })
        self.store.load_food("TRIP-1", {"lot_ref": "LOT-A", "loaded_qty": 30})

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def board(self, event_id: str, passenger: str, deck: str = "upper",
              at: str = "2026-09-26T08:50:00+08:00", offline: bool = False):
        return self.store.record_passenger_event("TRIP-1", {
            "event_id": event_id, "passenger_ref": passenger, "action": "board",
            "deck": deck, "occurred_at": at, "offline": offline,
        })


class DepartureCheckTest(StoreTestCase):
    """发车前人数、库存、车况缺一不可。"""

    def test_all_three_required(self) -> None:
        self.board("E1", "P-001")
        check = self.store.run_departure_check("TRIP-1", "值班员甲")
        self.assertTrue(check["all_ok"])
        self.store.depart_trip("TRIP-1")

    def test_missing_inventory_blocks_departure(self) -> None:
        self.store.recall_food_lot("LOT-A", "检出异物")
        check = self.store.run_departure_check("TRIP-1")
        self.assertFalse(check["inventory_ok"])
        self.assertFalse(check["all_ok"])
        with self.assertRaises(DomainError) as ctx:
            self.store.depart_trip("TRIP-1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("库存", str(ctx.exception))

    def test_defective_vehicle_blocks_departure(self) -> None:
        self.store.set_vehicle_condition("BUS-01", "defective")
        check = self.store.run_departure_check("TRIP-1")
        self.assertFalse(check["vehicle_ok"])
        with self.assertRaises(DomainError):
            self.store.depart_trip("TRIP-1")

    def test_over_capacity_blocks_departure(self) -> None:
        # 封闭二层后容量只剩下层 20，登车 21 人则人数检查不通过
        self.store.set_deck_zone("TRIP-1", {
            "deck": "upper", "is_open": False, "reason": "检修",
        })
        for i in range(21):
            self.board(f"E{i}", f"P-{i:03d}", deck="lower")
        check = self.store.run_departure_check("TRIP-1")
        self.assertFalse(check["headcount_ok"])
        self.assertEqual(check["open_capacity"], 20)

    def test_depart_without_check_rejected(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.store.depart_trip("TRIP-1")
        self.assertIn("检查", str(ctx.exception))


class OfflineIdempotencyTest(StoreTestCase):
    """断网扫码恢复后不能重复增加乘客或销量。"""

    def test_duplicate_board_event_not_counted_twice(self) -> None:
        _, dup1 = self.board("E-OFF-1", "P-100", offline=True)
        self.assertFalse(dup1)
        # 断网恢复后同一 event_id 重传
        _, dup2 = self.board("E-OFF-1", "P-100", offline=True)
        self.assertTrue(dup2)
        with self.store.connect() as conn:
            onboard = self.store.onboard_passengers(conn, "TRIP-1")
        self.assertEqual(len(onboard), 1)

    def test_conflicting_event_id_rejected(self) -> None:
        self.board("E-X", "P-100")
        with self.assertRaises(DomainError) as ctx:
            self.board("E-X", "P-999")  # 同一 event_id 不同乘客
        self.assertEqual(ctx.exception.status, 409)

    def test_duplicate_sale_not_counted_twice(self) -> None:
        sale = {
            "event_id": "S-1", "lot_ref": "LOT-A", "action": "sold",
            "quantity": 5, "occurred_at": "2026-09-26T10:00:00+08:00",
        }
        _, dup1 = self.store.record_food_event("TRIP-1", sale)
        _, dup2 = self.store.record_food_event("TRIP-1", dict(sale))
        self.assertFalse(dup1)
        self.assertTrue(dup2)
        with self.store.connect() as conn:
            remaining = self.store._lot_remaining(conn, "TRIP-1")
        self.assertEqual(remaining["LOT-A"], 25)  # 只扣一次

    def test_oversell_rejected(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.store.record_food_event("TRIP-1", {
                "event_id": "S-9", "lot_ref": "LOT-A", "action": "sold",
                "quantity": 31, "occurred_at": "2026-09-26T10:00:00+08:00",
            })
        self.assertEqual(ctx.exception.status, 409)


class DeckClosureTest(StoreTestCase):
    """途中封闭二层要列出受影响人员和处置。"""

    def test_close_upper_lists_affected_with_handling(self) -> None:
        self.board("E1", "P-1", deck="upper")
        self.board("E2", "P-2", deck="upper")
        self.board("E3", "P-3", deck="lower")
        result = self.store.close_deck("TRIP-1", {
            "deck": "upper", "reason": "大风预警",
            "handling_map": {"P-2": "refunded"},
        })
        affected = {a["passenger_ref"]: a["handling"] for a in result["affected"]}
        self.assertEqual(affected, {"P-1": "moved_to_lower", "P-2": "refunded"})
        # 复盘里能看到封闭调整与受影响人员
        replay = self.store.replay_trip("TRIP-1")
        closures = [a for a in replay["adjustments"]
                    if a["kind"] == "deck_closure"]
        self.assertEqual(len(closures), 1)
        self.assertEqual(len(closures[0]["affected"]), 2)


class RecallAndRerouteTest(StoreTestCase):
    """食品停用、故障或道路调整只改变相关班次并保留原计划。"""

    def test_recall_only_affects_related_trips_and_keeps_plan(self) -> None:
        # 第二个班次未配载 LOT-A，不应受影响
        self.store.create_trip({
            "trip_ref": "TRIP-2", "vehicle_ref": "BUS-01", "route_ref": "R-1",
            "planned_revision": 1,
        })
        result = self.store.recall_food_lot("LOT-A", "批次检出异物")
        self.assertEqual(
            [t["trip_ref"] for t in result["affected_trips"]], ["TRIP-1"]
        )
        replay = self.store.replay_trip("TRIP-1")
        # 原配载计划保留，停用以事件形式记录
        self.assertEqual(replay["food"][0]["loaded_qty"], 30)
        self.assertEqual(replay["food"][0]["lot_status"], "recalled")
        self.assertEqual(replay["food"][0]["remaining_qty"], 0)
        actions = [e["action"] for e in replay["food"][0]["events"]]
        self.assertIn("recalled", actions)
        # 停用后库存检查不通过
        check = self.store.run_departure_check("TRIP-1")
        self.assertFalse(check["inventory_ok"])

    def test_recall_records_actual_remaining_qty(self) -> None:
        self.store.record_food_event("TRIP-1", {
            "event_id": "S-1", "lot_ref": "LOT-A", "action": "sold",
            "quantity": 5, "occurred_at": "2026-09-26T10:00:00+08:00",
        })
        result = self.store.recall_food_lot("LOT-A")
        self.assertEqual(result["affected_trips"][0]["recalled_qty"], 25)
        replay = self.store.replay_trip("TRIP-1")
        recalled = [e for e in replay["food"][0]["events"]
                    if e["action"] == "recalled"]
        self.assertEqual(recalled[0]["quantity"], 25)

    def test_recall_is_idempotent(self) -> None:
        self.store.recall_food_lot("LOT-A")
        self.store.recall_food_lot("LOT-A")  # 重复停用不产生重复事件
        replay = self.store.replay_trip("TRIP-1")
        recalls = [e for e in replay["food"][0]["events"]
                   if e["action"] == "recalled"]
        self.assertEqual(len(recalls), 1)

    def test_reroute_keeps_planned_revision(self) -> None:
        self.store.reroute_trip("TRIP-1", {
            "to_revision": 2, "reason": "琴台路临时管制",
        })
        trip = self.store.get_trip("TRIP-1")
        self.assertEqual(trip["planned_revision"], 1)  # 原计划保留
        self.assertEqual(trip["active_revision"], 2)
        replay = self.store.replay_trip("TRIP-1")
        self.assertEqual(replay["route"]["planned_stops"][1], "宽窄巷子")
        self.assertEqual(replay["route"]["active_stops"][1], "天府广场")

    def test_breakdown_marks_vehicle_and_records_adjustment(self) -> None:
        self.store.report_breakdown("TRIP-1", {"detail": "发动机故障灯亮"})
        self.assertEqual(
            self.store.get_vehicle("BUS-01")["condition_status"], "defective"
        )
        replay = self.store.replay_trip("TRIP-1")
        kinds = [a["kind"] for a in replay["adjustments"]]
        self.assertIn("breakdown", kinds)


class ReplayTest(StoreTestCase):
    """复盘：采用的路线、各时点在车人数、茶歇处理结果与异常责任链。"""

    def test_replay_full_picture(self) -> None:
        self.board("E1", "P-1", deck="upper",
                   at="2026-09-26T08:50:00+08:00")
        self.board("E2", "P-2", deck="lower",
                   at="2026-09-26T08:55:00+08:00")
        self.store.record_passenger_event("TRIP-1", {
            "event_id": "E3", "passenger_ref": "P-1", "action": "alight",
            "occurred_at": "2026-09-26T09:30:00+08:00",
        })
        self.store.record_food_event("TRIP-1", {
            "event_id": "S-1", "lot_ref": "LOT-A", "action": "sold",
            "quantity": 3, "occurred_at": "2026-09-26T09:20:00+08:00",
        })
        self.store.report_incident("TRIP-1", {
            "category": "food", "description": "乘客反馈点心异味",
            "responsible_ref": "STAFF-07",
            "occurred_at": "2026-09-26T09:25:00+08:00",
        })
        self.store.reroute_trip("TRIP-1", {"to_revision": 2})

        replay = self.store.replay_trip("TRIP-1")
        # 路线
        self.assertEqual(replay["route"]["active_revision"], 2)
        self.assertEqual(replay["route"]["planned_revision"], 1)
        # 各时点在车人数
        counts = [t["onboard_after"] for t in replay["passenger_timeline"]]
        self.assertEqual(counts, [1, 2, 1])
        # 茶歇处理结果
        self.assertEqual(replay["food"][0]["remaining_qty"], 27)
        # 异常责任链
        self.assertEqual(replay["incidents"][0]["responsible_ref"], "STAFF-07")
        self.assertEqual(replay["adjustments"][0]["kind"], "reroute")


if __name__ == "__main__":
    unittest.main()
