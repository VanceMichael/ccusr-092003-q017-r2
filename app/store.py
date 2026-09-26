"""茶歇巴士移动服务联控的领域逻辑层。

所有记录按实际时序保存（occurred_at 为业务发生时间，received_at 为服务端接收时间）。
登离车与茶饮事件以 event_id 作为幂等键：断网扫码恢复后重传不会重复增加乘客或销量。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

ACTIVE_TRIP_STATUSES = ("scheduled", "boarding", "departed")


class DomainError(Exception):
    """业务规则错误，status 为对应的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(data: dict[str, Any], *fields: str) -> None:
    missing = [f for f in fields if data.get(f) in (None, "")]
    if missing:
        raise DomainError(f"缺少必填字段：{', '.join(missing)}")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class Store:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------------
    # 基础档案：车辆 / 路线 / 路线版本
    # ------------------------------------------------------------------

    def create_vehicle(self, data: dict[str, Any]) -> dict[str, Any]:
        _require(data, "vehicle_ref")
        with self.connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO vehicles(vehicle_ref, name, capacity_lower, "
                    "capacity_upper, condition_status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        data["vehicle_ref"],
                        data.get("name", ""),
                        int(data.get("capacity_lower", 0)),
                        int(data.get("capacity_upper", 0)),
                        data.get("condition_status", "serviceable"),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError(f"车辆已存在：{data['vehicle_ref']}", 409)
        return self.get_vehicle(data["vehicle_ref"])

    def get_vehicle(self, vehicle_ref: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM vehicles WHERE vehicle_ref = ?", (vehicle_ref,)
            ).fetchone()
        if row is None:
            raise DomainError(f"车辆不存在：{vehicle_ref}", 404)
        return _row_to_dict(row)

    def set_vehicle_condition(self, vehicle_ref: str, status: str) -> dict[str, Any]:
        if status not in ("serviceable", "defective"):
            raise DomainError("车况取值仅支持 serviceable / defective")
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE vehicles SET condition_status = ? WHERE vehicle_ref = ?",
                (status, vehicle_ref),
            )
            if cur.rowcount == 0:
                raise DomainError(f"车辆不存在：{vehicle_ref}", 404)
        return self.get_vehicle(vehicle_ref)

    def create_route(self, data: dict[str, Any]) -> dict[str, Any]:
        _require(data, "route_ref")
        with self.connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO routes(route_ref, name, created_at) VALUES (?, ?, ?)",
                    (data["route_ref"], data.get("name", ""), now_iso()),
                )
            except sqlite3.IntegrityError:
                raise DomainError(f"路线已存在：{data['route_ref']}", 409)
        return {"route_ref": data["route_ref"], "name": data.get("name", "")}

    def add_route_revision(self, route_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        _require(data, "revision", "stops")
        stops = data["stops"]
        if not isinstance(stops, list) or not stops:
            raise DomainError("stops 必须是非空数组")
        import json

        with self.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM routes WHERE route_ref = ?", (route_ref,)
            ).fetchone() is None:
                raise DomainError(f"路线不存在：{route_ref}", 404)
            try:
                conn.execute(
                    "INSERT INTO route_revisions(route_ref, revision, stops_json, "
                    "note, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        route_ref,
                        int(data["revision"]),
                        json.dumps(stops, ensure_ascii=False),
                        data.get("note", ""),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError(
                    f"路线 {route_ref} 的版本 {data['revision']} 已存在", 409
                )
        return self.get_route_revision(route_ref, int(data["revision"]))

    def get_route_revision(self, route_ref: str, revision: int) -> dict[str, Any]:
        import json

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM route_revisions WHERE route_ref = ? AND revision = ?",
                (route_ref, revision),
            ).fetchone()
        if row is None:
            raise DomainError(f"路线 {route_ref} 的版本 {revision} 不存在", 404)
        result = _row_to_dict(row)
        result["stops"] = json.loads(result.pop("stops_json"))
        return result

    # ------------------------------------------------------------------
    # 班次与停靠窗口、甲板区域
    # ------------------------------------------------------------------

    def create_trip(self, data: dict[str, Any]) -> dict[str, Any]:
        _require(data, "trip_ref", "vehicle_ref", "route_ref", "planned_revision")
        revision = int(data["planned_revision"])
        with self.connect() as conn:
            self._must_exist(conn, "vehicles", "vehicle_ref", data["vehicle_ref"])
            self._must_exist(conn, "routes", "route_ref", data["route_ref"])
            if conn.execute(
                "SELECT 1 FROM route_revisions WHERE route_ref = ? AND revision = ?",
                (data["route_ref"], revision),
            ).fetchone() is None:
                raise DomainError(
                    f"路线 {data['route_ref']} 的版本 {revision} 不存在", 404
                )
            try:
                conn.execute(
                    "INSERT INTO trips(trip_ref, vehicle_ref, route_ref, "
                    "planned_revision, active_revision, planned_departure, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        data["trip_ref"],
                        data["vehicle_ref"],
                        data["route_ref"],
                        revision,
                        revision,
                        data.get("planned_departure"),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise DomainError(f"班次已存在：{data['trip_ref']}", 409)
            # 默认上下层均开放
            for deck in ("lower", "upper"):
                conn.execute(
                    "INSERT INTO deck_zones(trip_ref, deck, is_open, reason, "
                    "changed_at) VALUES (?, ?, 1, '初始开放', ?)",
                    (data["trip_ref"], deck, now_iso()),
                )
        return self.get_trip(data["trip_ref"])

    @staticmethod
    def _must_exist(
        conn: sqlite3.Connection, table: str, column: str, value: str
    ) -> None:
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ?", (value,)
        ).fetchone() is None:
            raise DomainError(f"{table} 不存在：{value}", 404)

    def get_trip(self, trip_ref: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM trips WHERE trip_ref = ?", (trip_ref,)
            ).fetchone()
        if row is None:
            raise DomainError(f"班次不存在：{trip_ref}", 404)
        return _row_to_dict(row)

    def add_stop_window(self, trip_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        _require(data, "stop_name", "opens_at", "closes_at")
        with self.connect() as conn:
            self._must_exist(conn, "trips", "trip_ref", trip_ref)
            cur = conn.execute(
                "INSERT INTO stop_windows(trip_ref, stop_name, opens_at, closes_at, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (trip_ref, data["stop_name"], data["opens_at"], data["closes_at"],
                 now_iso()),
            )
            row = conn.execute(
                "SELECT * FROM stop_windows WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _row_to_dict(row)

    def set_deck_zone(self, trip_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        _require(data, "deck", "is_open")
        deck = data["deck"]
        if deck not in ("lower", "upper"):
            raise DomainError("deck 仅支持 lower / upper")
        with self.connect() as conn:
            self._must_exist(conn, "trips", "trip_ref", trip_ref)
            conn.execute(
                "INSERT INTO deck_zones(trip_ref, deck, is_open, reason, changed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (trip_ref, deck, 1 if data["is_open"] else 0,
                 data.get("reason", ""), now_iso()),
            )
        return {"trip_ref": trip_ref, "deck": deck,
                "is_open": bool(data["is_open"])}

    def _open_decks(self, conn: sqlite3.Connection, trip_ref: str) -> set[str]:
        rows = conn.execute(
            "SELECT deck, is_open FROM deck_zones WHERE trip_ref = ? "
            "ORDER BY id ASC",
            (trip_ref,),
        ).fetchall()
        state: dict[str, bool] = {}
        for row in rows:
            state[row["deck"]] = bool(row["is_open"])
        return {deck for deck, is_open in state.items() if is_open}

    # ------------------------------------------------------------------
    # 登离车记录（幂等）
    # ------------------------------------------------------------------

    def record_passenger_event(
        self, trip_ref: str, data: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """返回 (记录, 是否重复)。重复时返回已存在的记录，不重复计数。"""
        _require(data, "event_id", "passenger_ref", "action", "occurred_at")
        if data["action"] not in ("board", "alight"):
            raise DomainError("action 仅支持 board / alight")
        with self.connect() as conn:
            self._must_exist(conn, "trips", "trip_ref", trip_ref)
            existing = conn.execute(
                "SELECT * FROM passenger_events WHERE event_id = ?",
                (data["event_id"],),
            ).fetchone()
            if existing is not None:
                self._check_event_consistency(
                    existing, trip_ref, data["passenger_ref"], data["action"]
                )
                return _row_to_dict(existing), True
            conn.execute(
                "INSERT INTO passenger_events(event_id, trip_ref, passenger_ref, "
                "action, deck, stop_name, offline, occurred_at, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data["event_id"],
                    trip_ref,
                    data["passenger_ref"],
                    data["action"],
                    data.get("deck"),
                    data.get("stop_name", ""),
                    1 if data.get("offline") else 0,
                    data["occurred_at"],
                    now_iso(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM passenger_events WHERE event_id = ?",
                (data["event_id"],),
            ).fetchone()
            return _row_to_dict(row), False

    @staticmethod
    def _check_event_consistency(
        existing: sqlite3.Row, trip_ref: str, passenger_ref: str, action: str
    ) -> None:
        if (
            existing["trip_ref"] != trip_ref
            or existing["passenger_ref"] != passenger_ref
            or existing["action"] != action
        ):
            raise DomainError(
                f"event_id {existing['event_id']} 已被不同内容的事件占用", 409
            )

    def onboard_passengers(
        self, conn: sqlite3.Connection, trip_ref: str
    ) -> dict[str, str | None]:
        """当前在车乘客及其所在层（取最近一次登车记录的 deck）。"""
        rows = conn.execute(
            "SELECT passenger_ref, action, deck FROM passenger_events "
            "WHERE trip_ref = ? ORDER BY occurred_at ASC, id ASC",
            (trip_ref,),
        ).fetchall()
        onboard: dict[str, str | None] = {}
        for row in rows:
            if row["action"] == "board":
                onboard[row["passenger_ref"]] = row["deck"]
            else:
                onboard.pop(row["passenger_ref"], None)
        return onboard

    # ------------------------------------------------------------------
    # 发车前检查：人数、库存、车况缺一不可
    # ------------------------------------------------------------------

    def run_departure_check(
        self, trip_ref: str, checked_by: str = ""
    ) -> dict[str, Any]:
        with self.connect() as conn:
            trip = self._get_trip_row(conn, trip_ref)
            vehicle = conn.execute(
                "SELECT * FROM vehicles WHERE vehicle_ref = ?",
                (trip["vehicle_ref"],),
            ).fetchone()

            onboard = self.onboard_passengers(conn, trip_ref)
            open_decks = self._open_decks(conn, trip_ref)
            capacity = 0
            if "lower" in open_decks:
                capacity += vehicle["capacity_lower"]
            if "upper" in open_decks:
                capacity += vehicle["capacity_upper"]
            headcount_ok = len(onboard) <= capacity

            remaining = self._lot_remaining(conn, trip_ref)
            inventory_ok = any(qty > 0 for qty in remaining.values())

            vehicle_ok = vehicle["condition_status"] == "serviceable"

            conn.execute(
                "INSERT INTO departure_checks(trip_ref, headcount_ok, inventory_ok, "
                "vehicle_ok, checked_by, checked_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(trip_ref) DO UPDATE SET headcount_ok = excluded.headcount_ok, "
                "inventory_ok = excluded.inventory_ok, vehicle_ok = excluded.vehicle_ok, "
                "checked_by = excluded.checked_by, checked_at = excluded.checked_at",
                (trip_ref, int(headcount_ok), int(inventory_ok), int(vehicle_ok),
                 checked_by, now_iso()),
            )
        return {
            "trip_ref": trip_ref,
            "headcount_ok": headcount_ok,
            "inventory_ok": inventory_ok,
            "vehicle_ok": vehicle_ok,
            "onboard": len(onboard),
            "open_capacity": capacity,
            "all_ok": headcount_ok and inventory_ok and vehicle_ok,
        }

    def depart_trip(self, trip_ref: str) -> dict[str, Any]:
        with self.connect() as conn:
            trip = self._get_trip_row(conn, trip_ref)
            if trip["status"] not in ("scheduled", "boarding"):
                raise DomainError(f"班次状态为 {trip['status']}，不能发车", 409)
            check = conn.execute(
                "SELECT * FROM departure_checks WHERE trip_ref = ?", (trip_ref,)
            ).fetchone()
            if check is None:
                raise DomainError("尚未完成发车前检查，不能发车", 409)
            missing = [
                name
                for name, ok in (
                    ("人数", check["headcount_ok"]),
                    ("库存", check["inventory_ok"]),
                    ("车况", check["vehicle_ok"]),
                )
                if not ok
            ]
            if missing:
                raise DomainError(
                    f"发车前检查未通过（{'、'.join(missing)}），不能发车", 409
                )
            conn.execute(
                "UPDATE trips SET status = 'departed', departed_at = ? "
                "WHERE trip_ref = ?",
                (now_iso(), trip_ref),
            )
        return self.get_trip(trip_ref)

    def _get_trip_row(self, conn: sqlite3.Connection, trip_ref: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM trips WHERE trip_ref = ?", (trip_ref,)
        ).fetchone()
        if row is None:
            raise DomainError(f"班次不存在：{trip_ref}", 404)
        return row

    # ------------------------------------------------------------------
    # 茶饮批次与事件（幂等）
    # ------------------------------------------------------------------

    def create_food_lot(self, data: dict[str, Any]) -> dict[str, Any]:
        _require(data, "lot_ref")
        with self.connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO food_lots(lot_ref, name, quantity, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (data["lot_ref"], data.get("name", ""),
                     int(data.get("quantity", 0)), now_iso()),
                )
            except sqlite3.IntegrityError:
                raise DomainError(f"批次已存在：{data['lot_ref']}", 409)
        return self.get_food_lot(data["lot_ref"])

    def get_food_lot(self, lot_ref: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM food_lots WHERE lot_ref = ?", (lot_ref,)
            ).fetchone()
        if row is None:
            raise DomainError(f"批次不存在：{lot_ref}", 404)
        return _row_to_dict(row)

    def load_food(self, trip_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        _require(data, "lot_ref", "loaded_qty")
        with self.connect() as conn:
            self._must_exist(conn, "trips", "trip_ref", trip_ref)
            self._must_exist(conn, "food_lots", "lot_ref", data["lot_ref"])
            try:
                conn.execute(
                    "INSERT INTO trip_food_lots(trip_ref, lot_ref, loaded_qty, "
                    "created_at) VALUES (?, ?, ?, ?)",
                    (trip_ref, data["lot_ref"], int(data["loaded_qty"]), now_iso()),
                )
            except sqlite3.IntegrityError:
                raise DomainError(
                    f"班次 {trip_ref} 已配载批次 {data['lot_ref']}", 409
                )
        return {"trip_ref": trip_ref, "lot_ref": data["lot_ref"],
                "loaded_qty": int(data["loaded_qty"])}

    def _lot_remaining(
        self, conn: sqlite3.Connection, trip_ref: str
    ) -> dict[str, int]:
        """班次上各批次的剩余量（仅统计仍在 active 状态的批次）。"""
        rows = conn.execute(
            "SELECT tfl.lot_ref, tfl.loaded_qty, fl.status, "
            "COALESCE(SUM(CASE WHEN fe.action IN ('sold', 'served', 'recalled', "
            "'disposed') THEN fe.quantity ELSE 0 END), 0) AS consumed "
            "FROM trip_food_lots tfl "
            "JOIN food_lots fl ON fl.lot_ref = tfl.lot_ref "
            "LEFT JOIN food_events fe ON fe.trip_ref = tfl.trip_ref "
            "AND fe.lot_ref = tfl.lot_ref "
            "WHERE tfl.trip_ref = ? "
            "GROUP BY tfl.lot_ref",
            (trip_ref,),
        ).fetchall()
        return {
            row["lot_ref"]: (row["loaded_qty"] - row["consumed"])
            if row["status"] == "active"
            else 0
            for row in rows
        }

    def record_food_event(
        self, trip_ref: str, data: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """返回 (记录, 是否重复)。断网恢复重传不会重复增加销量。"""
        _require(data, "event_id", "lot_ref", "action", "quantity", "occurred_at")
        if data["action"] not in ("sold", "served", "disposed"):
            raise DomainError("action 仅支持 sold / served / disposed")
        quantity = int(data["quantity"])
        if quantity <= 0:
            raise DomainError("quantity 必须为正整数")
        with self.connect() as conn:
            self._must_exist(conn, "trips", "trip_ref", trip_ref)
            existing = conn.execute(
                "SELECT * FROM food_events WHERE event_id = ?", (data["event_id"],)
            ).fetchone()
            if existing is not None:
                if (
                    existing["trip_ref"] != trip_ref
                    or existing["lot_ref"] != data["lot_ref"]
                    or existing["action"] != data["action"]
                    or existing["quantity"] != quantity
                ):
                    raise DomainError(
                        f"event_id {data['event_id']} 已被不同内容的事件占用", 409
                    )
                return _row_to_dict(existing), True
            remaining = self._lot_remaining(conn, trip_ref)
            if data["lot_ref"] not in remaining:
                raise DomainError(
                    f"班次 {trip_ref} 未配载批次 {data['lot_ref']}", 404
                )
            if remaining[data["lot_ref"]] < quantity:
                raise DomainError(
                    f"批次 {data['lot_ref']} 剩余不足（剩余 "
                    f"{remaining[data['lot_ref']]}，需要 {quantity}）", 409
                )
            conn.execute(
                "INSERT INTO food_events(event_id, trip_ref, lot_ref, action, "
                "quantity, occurred_at, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (data["event_id"], trip_ref, data["lot_ref"], data["action"],
                 quantity, data["occurred_at"], now_iso()),
            )
            row = conn.execute(
                "SELECT * FROM food_events WHERE event_id = ?",
                (data["event_id"],),
            ).fetchone()
            return _row_to_dict(row), False

    def recall_food_lot(self, lot_ref: str, reason: str = "") -> dict[str, Any]:
        """停用批次：只影响配载了该批次且未结束的班次，原配载计划保留。"""
        with self.connect() as conn:
            self._must_exist(conn, "food_lots", "lot_ref", lot_ref)
            trips = conn.execute(
                "SELECT tfl.trip_ref, tfl.loaded_qty FROM trip_food_lots tfl "
                "JOIN trips t ON t.trip_ref = tfl.trip_ref "
                "WHERE tfl.lot_ref = ? AND t.status IN "
                f"({','.join('?' * len(ACTIVE_TRIP_STATUSES))})",
                (lot_ref, *ACTIVE_TRIP_STATUSES),
            ).fetchall()
            # 先记录各班次剩余量，再停用批次（停用后剩余量查询会归零）
            remaining_by_trip = {
                trip["trip_ref"]: self._lot_remaining(conn, trip["trip_ref"]).get(
                    lot_ref, 0
                )
                for trip in trips
            }
            conn.execute(
                "UPDATE food_lots SET status = 'recalled' WHERE lot_ref = ?",
                (lot_ref,),
            )
            affected = []
            for trip in trips:
                remaining = remaining_by_trip[trip["trip_ref"]]
                event_id = f"recall:{lot_ref}:{trip['trip_ref']}"
                conn.execute(
                    "INSERT OR IGNORE INTO food_events(event_id, trip_ref, lot_ref, "
                    "action, quantity, occurred_at, received_at) "
                    "VALUES (?, ?, ?, 'recalled', ?, ?, ?)",
                    (event_id, trip["trip_ref"], lot_ref, remaining,
                     now_iso(), now_iso()),
                )
                conn.execute(
                    "INSERT INTO trip_adjustments(trip_ref, kind, detail, "
                    "created_at) VALUES (?, 'food_recall', ?, ?)",
                    (trip["trip_ref"],
                     f"批次 {lot_ref} 停用：{reason}" if reason
                     else f"批次 {lot_ref} 停用",
                     now_iso()),
                )
                affected.append({"trip_ref": trip["trip_ref"],
                                 "recalled_qty": remaining})
        return {"lot_ref": lot_ref, "status": "recalled",
                "affected_trips": affected}

    # ------------------------------------------------------------------
    # 途中调整：改道 / 甲板封闭 / 故障
    # ------------------------------------------------------------------

    def reroute_trip(self, trip_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        """改道：切换到新的路线版本，原计划版本保留不变。"""
        _require(data, "to_revision")
        to_revision = int(data["to_revision"])
        with self.connect() as conn:
            trip = self._get_trip_row(conn, trip_ref)
            if conn.execute(
                "SELECT 1 FROM route_revisions WHERE route_ref = ? AND revision = ?",
                (trip["route_ref"], to_revision),
            ).fetchone() is None:
                raise DomainError(
                    f"路线 {trip['route_ref']} 的版本 {to_revision} 不存在", 404
                )
            conn.execute(
                "UPDATE trips SET active_revision = ? WHERE trip_ref = ?",
                (to_revision, trip_ref),
            )
            conn.execute(
                "INSERT INTO trip_adjustments(trip_ref, kind, detail, "
                "from_revision, to_revision, created_at) "
                "VALUES (?, 'reroute', ?, ?, ?, ?)",
                (trip_ref, data.get("reason", ""),
                 trip["active_revision"], to_revision, now_iso()),
            )
        return self.get_trip(trip_ref)

    def report_breakdown(self, trip_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        """车辆故障：标记车况并仅在该班次上记录调整。"""
        with self.connect() as conn:
            trip = self._get_trip_row(conn, trip_ref)
            conn.execute(
                "UPDATE vehicles SET condition_status = 'defective' "
                "WHERE vehicle_ref = ?",
                (trip["vehicle_ref"],),
            )
            conn.execute(
                "INSERT INTO trip_adjustments(trip_ref, kind, detail, created_at) "
                "VALUES (?, 'breakdown', ?, ?)",
                (trip_ref, data.get("detail", ""), now_iso()),
            )
        return self.get_trip(trip_ref)

    def close_deck(self, trip_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        """途中封闭甲板（如二层）：列出受影响乘客并记录逐人处置。

        handling_map: {passenger_ref: handling}，未指定的默认 moved_to_lower
        （封闭下层时默认 alighted）。
        """
        deck = data.get("deck", "upper")
        if deck not in ("lower", "upper"):
            raise DomainError("deck 仅支持 lower / upper")
        reason = data.get("reason", "")
        handling_map: dict[str, str] = data.get("handling_map", {}) or {}
        default_handling = "moved_to_lower" if deck == "upper" else "alighted"

        with self.connect() as conn:
            self._must_exist(conn, "trips", "trip_ref", trip_ref)
            conn.execute(
                "INSERT INTO deck_zones(trip_ref, deck, is_open, reason, "
                "changed_at) VALUES (?, ?, 0, ?, ?)",
                (trip_ref, deck, reason, now_iso()),
            )
            cur = conn.execute(
                "INSERT INTO trip_adjustments(trip_ref, kind, detail, created_at) "
                "VALUES (?, 'deck_closure', ?, ?)",
                (trip_ref, f"封闭{deck}：{reason}" if reason else f"封闭{deck}",
                 now_iso()),
            )
            adjustment_id = cur.lastrowid
            onboard = self.onboard_passengers(conn, trip_ref)
            affected = []
            for passenger_ref, passenger_deck in sorted(onboard.items()):
                if passenger_deck != deck:
                    continue
                handling = handling_map.get(passenger_ref, default_handling)
                conn.execute(
                    "INSERT INTO deck_closure_affected(adjustment_id, "
                    "passenger_ref, handling) VALUES (?, ?, ?)",
                    (adjustment_id, passenger_ref, handling),
                )
                affected.append({"passenger_ref": passenger_ref,
                                 "handling": handling})
        return {
            "trip_ref": trip_ref,
            "deck": deck,
            "adjustment_id": adjustment_id,
            "affected": affected,
        }

    # ------------------------------------------------------------------
    # 安全事件
    # ------------------------------------------------------------------

    def report_incident(self, trip_ref: str, data: dict[str, Any]) -> dict[str, Any]:
        _require(data, "category", "occurred_at")
        with self.connect() as conn:
            self._must_exist(conn, "trips", "trip_ref", trip_ref)
            cur = conn.execute(
                "INSERT INTO safety_incidents(trip_ref, category, description, "
                "responsible_ref, occurred_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (trip_ref, data["category"], data.get("description", ""),
                 data.get("responsible_ref", ""), data["occurred_at"], now_iso()),
            )
            row = conn.execute(
                "SELECT * FROM safety_incidents WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _row_to_dict(row)

    # ------------------------------------------------------------------
    # 行程复盘
    # ------------------------------------------------------------------

    def replay_trip(self, trip_ref: str) -> dict[str, Any]:
        """复盘一次行程：采用的路线、各时点在车人数、茶歇处理结果与异常责任链。"""
        with self.connect() as conn:
            trip = self._get_trip_row(conn, trip_ref)
            trip_dict = _row_to_dict(trip)

            import json

            def revision_stops(revision: int) -> list[str]:
                row = conn.execute(
                    "SELECT stops_json FROM route_revisions "
                    "WHERE route_ref = ? AND revision = ?",
                    (trip["route_ref"], revision),
                ).fetchone()
                return json.loads(row["stops_json"]) if row else []

            # 各时点在车人数：按发生时序回放登离车事件
            events = conn.execute(
                "SELECT occurred_at, passenger_ref, action, deck, stop_name, "
                "offline FROM passenger_events WHERE trip_ref = ? "
                "ORDER BY occurred_at ASC, id ASC",
                (trip_ref,),
            ).fetchall()
            timeline = []
            onboard = 0
            for event in events:
                onboard += 1 if event["action"] == "board" else -1
                timeline.append({
                    "occurred_at": event["occurred_at"],
                    "passenger_ref": event["passenger_ref"],
                    "action": event["action"],
                    "deck": event["deck"],
                    "stop_name": event["stop_name"],
                    "offline": bool(event["offline"]),
                    "onboard_after": onboard,
                })

            # 茶歇处理结果：配载计划 + 全部批次事件
            food_rows = conn.execute(
                "SELECT tfl.lot_ref, tfl.loaded_qty, fl.name, fl.status "
                "FROM trip_food_lots tfl "
                "JOIN food_lots fl ON fl.lot_ref = tfl.lot_ref "
                "WHERE tfl.trip_ref = ? ORDER BY tfl.created_at ASC",
                (trip_ref,),
            ).fetchall()
            food_events = conn.execute(
                "SELECT lot_ref, action, quantity, occurred_at "
                "FROM food_events WHERE trip_ref = ? "
                "ORDER BY occurred_at ASC, id ASC",
                (trip_ref,),
            ).fetchall()
            remaining = self._lot_remaining(conn, trip_ref)
            food = []
            for lot in food_rows:
                food.append({
                    "lot_ref": lot["lot_ref"],
                    "name": lot["name"],
                    "lot_status": lot["status"],
                    "loaded_qty": lot["loaded_qty"],
                    "remaining_qty": remaining.get(lot["lot_ref"], 0),
                    "events": [
                        {"action": fe["action"], "quantity": fe["quantity"],
                         "occurred_at": fe["occurred_at"]}
                        for fe in food_events if fe["lot_ref"] == lot["lot_ref"]
                    ],
                })

            # 异常责任链：安全事件 + 班次调整（含甲板封闭受影响人员），按时序排列
            incidents = [
                _row_to_dict(row)
                for row in conn.execute(
                    "SELECT * FROM safety_incidents WHERE trip_ref = ? "
                    "ORDER BY occurred_at ASC, id ASC",
                    (trip_ref,),
                ).fetchall()
            ]
            adjustments = []
            for adj in conn.execute(
                "SELECT * FROM trip_adjustments WHERE trip_ref = ? "
                "ORDER BY created_at ASC, id ASC",
                (trip_ref,),
            ).fetchall():
                item = _row_to_dict(adj)
                if adj["kind"] == "deck_closure":
                    item["affected"] = [
                        _row_to_dict(a)
                        for a in conn.execute(
                            "SELECT passenger_ref, handling FROM "
                            "deck_closure_affected WHERE adjustment_id = ? "
                            "ORDER BY id ASC",
                            (adj["id"],),
                        ).fetchall()
                    ]
                adjustments.append(item)

            check = conn.execute(
                "SELECT * FROM departure_checks WHERE trip_ref = ?", (trip_ref,)
            ).fetchone()

        return {
            "trip": trip_dict,
            "route": {
                "route_ref": trip["route_ref"],
                "planned_revision": trip["planned_revision"],
                "planned_stops": revision_stops(trip["planned_revision"]),
                "active_revision": trip["active_revision"],
                "active_stops": revision_stops(trip["active_revision"]),
            },
            "departure_check": _row_to_dict(check) if check else None,
            "passenger_timeline": timeline,
            "food": food,
            "incidents": incidents,
            "adjustments": adjustments,
        }
