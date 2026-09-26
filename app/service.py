"""茶歇巴士移动服务联控：核心业务逻辑。

所有函数接收 (conn, data)，返回 (HTTP 状态码, 响应体 dict)。
业务错误抛出 ServiceError，由 HTTP 层转换为 JSON 错误响应。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .timeutil import now_iso, parse_iso8601, sort_key

ACTIVE_TRIP_STATUSES = ("scheduled", "boarding", "departed", "disrupted")
SAFETY_CATEGORIES = (
    "food_suspension",
    "breakdown",
    "road_adjustment",
    "deck_closure",
    "passenger",
    "other",
)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 基础工具

def _require(data: dict, *fields: str) -> None:
    missing = [f for f in fields if data.get(f) in (None, "")]
    if missing:
        raise ServiceError(400, "missing_fields", "缺少必填字段：" + ", ".join(missing))


def _parse_time(data: dict, field: str = "occurred_at") -> str:
    try:
        parse_iso8601(data.get(field), field)
    except ValueError as exc:
        raise ServiceError(400, "invalid_time", str(exc))
    return data[field].strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ServiceError(400, "invalid_quantity", f"{field} 必须是正整数")
    return value


def _new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _get_trip(conn: sqlite3.Connection, trip_ref: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM trips WHERE trip_ref = ?", (trip_ref,)).fetchone()
    if row is None:
        raise ServiceError(404, "trip_not_found", f"班次不存在：{trip_ref}")
    return row


def _get_vehicle(conn: sqlite3.Connection, vehicle_ref: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM vehicles WHERE vehicle_ref = ?", (vehicle_ref,)).fetchone()
    if row is None:
        raise ServiceError(404, "vehicle_not_found", f"车辆不存在：{vehicle_ref}")
    return row


def _get_lot(conn: sqlite3.Connection, lot_ref: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM food_lots WHERE lot_ref = ?", (lot_ref,)).fetchone()
    if row is None:
        raise ServiceError(404, "lot_not_found", f"茶饮批次不存在：{lot_ref}")
    return row


def _record_safety_event(
    conn: sqlite3.Connection,
    *,
    category: str,
    description: str,
    occurred_at: str,
    trip_ref: str | None = None,
    vehicle_ref: str | None = None,
    event_ref: str | None = None,
    responsibility: list[dict] | None = None,
) -> str:
    ref = event_ref or _new_ref("SE")
    conn.execute(
        "INSERT INTO safety_events(event_ref, trip_ref, vehicle_ref, category, description, occurred_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ref, trip_ref, vehicle_ref, category, description, occurred_at, now_iso()),
    )
    for link in responsibility or []:
        _insert_responsibility_link(conn, ref, link)
    return ref


def _insert_responsibility_link(conn: sqlite3.Connection, event_ref: str, link: dict) -> None:
    _require(link, "actor_ref", "role", "action")
    at = link.get("at")
    if at:
        _parse_time(link, "at")
    conn.execute(
        "INSERT INTO responsibility_links(event_ref, actor_ref, role, action, at) VALUES (?, ?, ?, ?, ?)",
        (event_ref, link["actor_ref"], link["role"], link["action"], at or now_iso()),
    )


# ---------------------------------------------------------------- 乘客在车状态

def _passenger_events(conn: sqlite3.Connection, trip_ref: str) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM passenger_events WHERE trip_ref = ?", (trip_ref,)
    ).fetchall()
    return sorted(rows, key=lambda r: (sort_key(r["occurred_at"]), r["recorded_seq"]))


def _onboard_state(conn: sqlite3.Connection, trip_ref: str) -> dict[str, str]:
    """当前在车乘客 -> 所在层。"""
    onboard: dict[str, str] = {}
    for row in _passenger_events(conn, trip_ref):
        if row["direction"] == "board":
            onboard[row["passenger_ref"]] = row["deck"]
        else:
            onboard.pop(row["passenger_ref"], None)
    return onboard


# ---------------------------------------------------------------- 健康检查 / 车辆

def health(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    return 200, {"status": "ok"}


def create_vehicle(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "vehicle_ref")
    try:
        conn.execute(
            "INSERT INTO vehicles(vehicle_ref, name, capacity_lower, capacity_upper, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                data["vehicle_ref"],
                data.get("name", ""),
                int(data.get("capacity_lower", 0)),
                int(data.get("capacity_upper", 0)),
                now_iso(),
            ),
        )
    except sqlite3.IntegrityError:
        raise ServiceError(409, "vehicle_exists", f"车辆已存在：{data['vehicle_ref']}")
    return 201, {"vehicle_ref": data["vehicle_ref"], "created": True}


def list_vehicles(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    rows = conn.execute("SELECT * FROM vehicles ORDER BY vehicle_ref").fetchall()
    return 200, {"vehicles": [dict(r) for r in rows]}


def add_inspection(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "vehicle_ref", "result", "inspected_at")
    _get_vehicle(conn, data["vehicle_ref"])
    if data["result"] not in ("pass", "fail"):
        raise ServiceError(400, "invalid_result", "result 只能是 pass 或 fail")
    _parse_time(data, "inspected_at")
    conn.execute(
        "INSERT INTO vehicle_inspections(vehicle_ref, result, note, inspected_at) VALUES (?, ?, ?, ?)",
        (data["vehicle_ref"], data["result"], data.get("note", ""), data["inspected_at"]),
    )
    return 201, {"vehicle_ref": data["vehicle_ref"], "result": data["result"]}


def vehicle_breakdown(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    """车辆故障：记录不合格车况，仅将影响该车的未发车/登车中班次标记为 disrupted，原计划保留。"""
    _require(data, "vehicle_ref", "description", "occurred_at")
    _get_vehicle(conn, data["vehicle_ref"])
    occurred_at = _parse_time(data)
    conn.execute(
        "INSERT INTO vehicle_inspections(vehicle_ref, result, note, inspected_at) VALUES (?, 'fail', ?, ?)",
        (data["vehicle_ref"], data["description"], occurred_at),
    )
    affected = conn.execute(
        "SELECT trip_ref FROM trips WHERE vehicle_ref = ? AND status IN ('scheduled', 'boarding')",
        (data["vehicle_ref"],),
    ).fetchall()
    affected_refs = []
    for row in affected:
        conn.execute("UPDATE trips SET status = 'disrupted' WHERE trip_ref = ?", (row["trip_ref"],))
        _record_safety_event(
            conn,
            category="breakdown",
            description=data["description"],
            occurred_at=occurred_at,
            trip_ref=row["trip_ref"],
            vehicle_ref=data["vehicle_ref"],
            responsibility=data.get("responsibility"),
        )
        affected_refs.append(row["trip_ref"])
    return 200, {
        "vehicle_ref": data["vehicle_ref"],
        "inspection": "fail",
        "affected_trips": affected_refs,
        "note": "仅受影响班次标记为 disrupted，原计划路线与停靠窗口保留",
    }


# ---------------------------------------------------------------- 路线与版本

def create_route(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "route_ref", "stops")
    stops = data["stops"]
    if not isinstance(stops, list) or not stops:
        raise ServiceError(400, "invalid_stops", "stops 必须是非空数组")
    try:
        conn.execute(
            "INSERT INTO routes(route_ref, name, created_at) VALUES (?, ?, ?)",
            (data["route_ref"], data.get("name", ""), now_iso()),
        )
        conn.execute(
            "INSERT INTO route_revisions(route_ref, revision, stops_json, reason, created_at)"
            " VALUES (?, 1, ?, ?, ?)",
            (data["route_ref"], json.dumps(stops, ensure_ascii=False), data.get("reason", "初始版本"), now_iso()),
        )
    except sqlite3.IntegrityError:
        raise ServiceError(409, "route_exists", f"路线已存在：{data['route_ref']}")
    return 201, {"route_ref": data["route_ref"], "revision": 1}


def add_route_revision(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    """道路调整：产生新 revision，只切换指定班次的 actual_revision，planned_revision 保留。"""
    _require(data, "route_ref", "stops")
    stops = data["stops"]
    if not isinstance(stops, list) or not stops:
        raise ServiceError(400, "invalid_stops", "stops 必须是非空数组")
    route = conn.execute("SELECT * FROM routes WHERE route_ref = ?", (data["route_ref"],)).fetchone()
    if route is None:
        raise ServiceError(404, "route_not_found", f"路线不存在：{data['route_ref']}")
    row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) AS max_rev FROM route_revisions WHERE route_ref = ?",
        (data["route_ref"],),
    ).fetchone()
    new_revision = row["max_rev"] + 1
    conn.execute(
        "INSERT INTO route_revisions(route_ref, revision, stops_json, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (data["route_ref"], new_revision, json.dumps(stops, ensure_ascii=False), data.get("reason", ""), now_iso()),
    )
    occurred_at = _parse_time(data) if data.get("occurred_at") else now_iso()
    affected = []
    for trip_ref in data.get("apply_to_trips", []):
        trip = _get_trip(conn, trip_ref)
        if trip["route_ref"] != data["route_ref"]:
            raise ServiceError(409, "route_mismatch", f"班次 {trip_ref} 不属于路线 {data['route_ref']}")
        if trip["status"] not in ACTIVE_TRIP_STATUSES:
            continue  # 已完结班次保持原样
        conn.execute("UPDATE trips SET actual_revision = ? WHERE trip_ref = ?", (new_revision, trip_ref))
        _record_safety_event(
            conn,
            category="road_adjustment",
            description=data.get("reason") or f"路线 {data['route_ref']} 切换至第 {new_revision} 版",
            occurred_at=occurred_at,
            trip_ref=trip_ref,
            responsibility=data.get("responsibility"),
        )
        affected.append({
            "trip_ref": trip_ref,
            "planned_revision": trip["planned_revision"],
            "actual_revision": new_revision,
        })
    return 201, {
        "route_ref": data["route_ref"],
        "revision": new_revision,
        "affected_trips": affected,
        "note": "仅列出的班次切换到新版本，各班次 planned_revision 保留原计划",
    }


# ---------------------------------------------------------------- 班次

def create_trip(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "trip_ref", "vehicle_ref", "route_ref", "route_revision", "service_date")
    _get_vehicle(conn, data["vehicle_ref"])
    revision = conn.execute(
        "SELECT * FROM route_revisions WHERE route_ref = ? AND revision = ?",
        (data["route_ref"], data["route_revision"]),
    ).fetchone()
    if revision is None:
        raise ServiceError(
            404, "revision_not_found",
            f"路线 {data['route_ref']} 不存在第 {data['route_revision']} 版",
        )
    try:
        conn.execute(
            "INSERT INTO trips(trip_ref, vehicle_ref, route_ref, planned_revision, actual_revision,"
            " service_date, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?)",
            (
                data["trip_ref"], data["vehicle_ref"], data["route_ref"],
                data["route_revision"], data["route_revision"], data["service_date"], now_iso(),
            ),
        )
    except sqlite3.IntegrityError:
        raise ServiceError(409, "trip_exists", f"班次已存在：{data['trip_ref']}")
    for window in data.get("stop_windows", []):
        add_stop_window(conn, {**window, "trip_ref": data["trip_ref"]})
    for zone in data.get("deck_zones", []):
        set_deck_zone(conn, {**zone, "trip_ref": data["trip_ref"]})
    return 201, {
        "trip_ref": data["trip_ref"],
        "planned_revision": data["route_revision"],
        "actual_revision": data["route_revision"],
        "status": "scheduled",
    }


def get_trip(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    trip = _get_trip(conn, data["trip_ref"])
    onboard = _onboard_state(conn, trip["trip_ref"])
    inventory = _inventory_rows(conn, trip["trip_ref"])
    zones = conn.execute(
        "SELECT deck, zone, state, updated_at FROM deck_zones WHERE trip_ref = ? ORDER BY id",
        (trip["trip_ref"],),
    ).fetchall()
    return 200, {
        **dict(trip),
        "onboard_count": len(onboard),
        "onboard_passengers": sorted(onboard),
        "deck_zones": [dict(z) for z in zones],
        "inventory": inventory,
        "departure_gate": _departure_gate(conn, trip),
    }


def complete_trip(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    trip = _get_trip(conn, data["trip_ref"])
    if trip["status"] in ("completed", "closed"):
        raise ServiceError(409, "trip_already_finished", f"班次 {trip['trip_ref']} 已完结")
    conn.execute("UPDATE trips SET status = 'completed' WHERE trip_ref = ?", (trip["trip_ref"],))
    return 200, {"trip_ref": trip["trip_ref"], "status": "completed"}


def add_stop_window(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "trip_ref", "stop_name", "planned_arrive_at", "planned_depart_at")
    _get_trip(conn, data["trip_ref"])
    _parse_time(data, "planned_arrive_at")
    _parse_time(data, "planned_depart_at")
    cursor = conn.execute(
        "INSERT INTO stop_windows(trip_ref, stop_name, planned_arrive_at, planned_depart_at,"
        " actual_arrive_at, actual_depart_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            data["trip_ref"], data["stop_name"], data["planned_arrive_at"], data["planned_depart_at"],
            data.get("actual_arrive_at"), data.get("actual_depart_at"),
        ),
    )
    return 201, {"window_id": cursor.lastrowid, "trip_ref": data["trip_ref"]}


def set_deck_zone(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "trip_ref", "deck", "zone", "state")
    _get_trip(conn, data["trip_ref"])
    if data["deck"] not in ("lower", "upper"):
        raise ServiceError(400, "invalid_deck", "deck 只能是 lower 或 upper")
    if data["state"] not in ("open", "closed"):
        raise ServiceError(400, "invalid_state", "state 只能是 open 或 closed")
    updated_at = _parse_time(data) if data.get("occurred_at") else now_iso()
    conn.execute(
        "INSERT INTO deck_zones(trip_ref, deck, zone, state, updated_at) VALUES (?, ?, ?, ?, ?)",
        (data["trip_ref"], data["deck"], data["zone"], data["state"], updated_at),
    )
    return 201, {"trip_ref": data["trip_ref"], "deck": data["deck"], "zone": data["zone"], "state": data["state"]}


# ---------------------------------------------------------------- 登离车（幂等）

def record_scan(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    """登离车扫码。event_id 为幂等键：断网恢复重传同一 event_id 不重复计数。"""
    _require(data, "trip_ref", "event_id", "passenger_ref", "passenger_event", "occurred_at")
    _get_trip(conn, data["trip_ref"])
    event = data["passenger_event"]
    if event not in ("boarded", "alighted"):
        raise ServiceError(400, "invalid_passenger_event", "passenger_event 只能是 boarded 或 alighted")
    occurred_at = _parse_time(data)
    deck = data.get("deck") or "lower"
    if deck not in ("lower", "upper"):
        raise ServiceError(400, "invalid_deck", "deck 只能是 lower 或 upper")
    direction = "board" if event == "boarded" else "alight"
    existing = conn.execute(
        "SELECT * FROM passenger_events WHERE trip_ref = ? AND event_id = ?",
        (data["trip_ref"], data["event_id"]),
    ).fetchone()
    if existing is not None:
        if (existing["passenger_ref"], existing["direction"], existing["occurred_at"]) != (
            data["passenger_ref"], direction, occurred_at,
        ):
            raise ServiceError(
                409, "event_id_conflict",
                f"event_id {data['event_id']} 已用于其他内容，不能复用",
            )
        return 200, {
            "event_id": data["event_id"],
            "deduplicated": True,
            "onboard_count": len(_onboard_state(conn, data["trip_ref"])),
        }
    conn.execute(
        "INSERT INTO passenger_events(trip_ref, event_id, passenger_ref, direction, deck, stop_name,"
        " occurred_at, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data["trip_ref"], data["event_id"], data["passenger_ref"], direction, deck,
            data.get("stop_name", ""), occurred_at, now_iso(),
        ),
    )
    return 201, {
        "event_id": data["event_id"],
        "deduplicated": False,
        "onboard_count": len(_onboard_state(conn, data["trip_ref"])),
    }


def report_headcount(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "trip_ref", "reported_count")
    _get_trip(conn, data["trip_ref"])
    if isinstance(data["reported_count"], bool) or not isinstance(data["reported_count"], int) or data["reported_count"] < 0:
        raise ServiceError(400, "invalid_count", "reported_count 必须是非负整数")
    reported_at = _parse_time(data) if data.get("occurred_at") else now_iso()
    conn.execute(
        "INSERT INTO headcount_reports(trip_ref, reported_count, reported_by, reported_at) VALUES (?, ?, ?, ?)",
        (data["trip_ref"], data["reported_count"], data.get("reported_by", ""), reported_at),
    )
    onboard = len(_onboard_state(conn, data["trip_ref"]))
    return 201, {
        "trip_ref": data["trip_ref"],
        "reported_count": data["reported_count"],
        "scanned_onboard": onboard,
        "matches_scans": data["reported_count"] == onboard,
    }


# ---------------------------------------------------------------- 茶饮批次 / 库存 / 销售

def create_food_lot(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "food_lot", "item_name", "quantity")
    quantity = _positive_int(data["quantity"], "quantity")
    try:
        conn.execute(
            "INSERT INTO food_lots(lot_ref, item_name, quantity, status, created_at) VALUES (?, ?, ?, 'active', ?)",
            (data["food_lot"], data["item_name"], quantity, now_iso()),
        )
    except sqlite3.IntegrityError:
        raise ServiceError(409, "lot_exists", f"茶饮批次已存在：{data['food_lot']}")
    return 201, {"food_lot": data["food_lot"], "status": "active"}


def load_inventory(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "trip_ref", "food_lot", "quantity")
    _get_trip(conn, data["trip_ref"])
    lot = _get_lot(conn, data["food_lot"])
    if lot["status"] != "active":
        raise ServiceError(409, "lot_not_active", f"批次 {data['food_lot']} 已停用，不能装车")
    quantity = _positive_int(data["quantity"], "quantity")
    conn.execute(
        "INSERT INTO trip_inventory(trip_ref, lot_ref, loaded_qty, sold_qty, suspended_qty) VALUES (?, ?, ?, 0, 0)"
        " ON CONFLICT(trip_ref, lot_ref) DO UPDATE SET loaded_qty = loaded_qty + excluded.loaded_qty",
        (data["trip_ref"], data["food_lot"], quantity),
    )
    return 201, {"trip_ref": data["trip_ref"], "food_lot": data["food_lot"], "loaded": quantity}


def _inventory_rows(conn: sqlite3.Connection, trip_ref: str) -> list[dict]:
    rows = conn.execute(
        "SELECT ti.lot_ref, fl.item_name, fl.status AS lot_status, ti.loaded_qty, ti.sold_qty, ti.suspended_qty"
        " FROM trip_inventory ti JOIN food_lots fl ON fl.lot_ref = ti.lot_ref"
        " WHERE ti.trip_ref = ? ORDER BY ti.lot_ref",
        (trip_ref,),
    ).fetchall()
    return [
        {
            "lot_ref": r["lot_ref"],
            "item_name": r["item_name"],
            "lot_status": r["lot_status"],
            "loaded": r["loaded_qty"],
            "sold": r["sold_qty"],
            "suspended": r["suspended_qty"],
            "remaining": r["loaded_qty"] - r["sold_qty"] - r["suspended_qty"],
        }
        for r in rows
    ]


def record_sale(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    """茶饮销售。event_id 幂等：断网恢复重传不重复扣库存、不重复计销量。"""
    _require(data, "trip_ref", "event_id", "food_lot", "quantity", "occurred_at")
    _get_trip(conn, data["trip_ref"])
    quantity = _positive_int(data["quantity"], "quantity")
    occurred_at = _parse_time(data)
    existing = conn.execute(
        "SELECT * FROM sale_events WHERE trip_ref = ? AND event_id = ?",
        (data["trip_ref"], data["event_id"]),
    ).fetchone()
    if existing is not None:
        if (existing["lot_ref"], existing["quantity"], existing["occurred_at"]) != (
            data["food_lot"], quantity, occurred_at,
        ):
            raise ServiceError(
                409, "event_id_conflict",
                f"event_id {data['event_id']} 已用于其他内容，不能复用",
            )
        inventory = next(
            (row for row in _inventory_rows(conn, data["trip_ref"]) if row["lot_ref"] == data["food_lot"]), None
        )
        return 200, {"event_id": data["event_id"], "deduplicated": True, "inventory": inventory}
    inventory = conn.execute(
        "SELECT * FROM trip_inventory WHERE trip_ref = ? AND lot_ref = ?",
        (data["trip_ref"], data["food_lot"]),
    ).fetchone()
    if inventory is None:
        raise ServiceError(409, "inventory_not_loaded", f"班次 {data['trip_ref']} 未装载批次 {data['food_lot']}")
    available = inventory["loaded_qty"] - inventory["sold_qty"] - inventory["suspended_qty"]
    if quantity > available:
        raise ServiceError(
            409, "insufficient_stock",
            f"批次 {data['food_lot']} 可用库存 {available}，不足 {quantity}",
        )
    conn.execute(
        "INSERT INTO sale_events(trip_ref, event_id, lot_ref, quantity, occurred_at, synced_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (data["trip_ref"], data["event_id"], data["food_lot"], quantity, occurred_at, now_iso()),
    )
    conn.execute(
        "UPDATE trip_inventory SET sold_qty = sold_qty + ? WHERE trip_ref = ? AND lot_ref = ?",
        (quantity, data["trip_ref"], data["food_lot"]),
    )
    inventory_after = next(
        row for row in _inventory_rows(conn, data["trip_ref"]) if row["lot_ref"] == data["food_lot"]
    )
    return 201, {"event_id": data["event_id"], "deduplicated": False, "inventory": inventory_after}


def suspend_food_lot(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    """食品停用：只影响仍装载该批次的进行中班次，已完结班次的原始记录保留。"""
    _require(data, "food_lot", "reason", "occurred_at")
    lot = _get_lot(conn, data["food_lot"])
    if lot["status"] != "active":
        raise ServiceError(409, "lot_already_suspended", f"批次 {data['food_lot']} 已是 {lot['status']} 状态")
    occurred_at = _parse_time(data)
    conn.execute(
        "UPDATE food_lots SET status = 'suspended', suspended_reason = ? WHERE lot_ref = ?",
        (data["reason"], data["food_lot"]),
    )
    rows = conn.execute(
        "SELECT ti.trip_ref, ti.loaded_qty, ti.sold_qty, ti.suspended_qty, t.status"
        " FROM trip_inventory ti JOIN trips t ON t.trip_ref = ti.trip_ref WHERE ti.lot_ref = ?",
        (data["food_lot"],),
    ).fetchall()
    affected, untouched = [], []
    for row in rows:
        if row["status"] not in ACTIVE_TRIP_STATUSES:
            untouched.append(row["trip_ref"])
            continue
        remaining = row["loaded_qty"] - row["sold_qty"] - row["suspended_qty"]
        conn.execute(
            "UPDATE trip_inventory SET suspended_qty = suspended_qty + ? WHERE trip_ref = ? AND lot_ref = ?",
            (remaining, row["trip_ref"], data["food_lot"]),
        )
        event_ref = _record_safety_event(
            conn,
            category="food_suspension",
            description=f"批次 {data['food_lot']} 停用：{data['reason']}（封存 {remaining} 份）",
            occurred_at=occurred_at,
            trip_ref=row["trip_ref"],
            responsibility=data.get("responsibility"),
        )
        affected.append({
            "trip_ref": row["trip_ref"],
            "suspended_qty": remaining,
            "safety_event_ref": event_ref,
        })
    return 200, {
        "food_lot": data["food_lot"],
        "status": "suspended",
        "affected_trips": affected,
        "untouched_trips": untouched,
        "note": "仅进行中班次封存剩余库存，已完结班次的销售与装载记录保留",
    }


# ---------------------------------------------------------------- 发车闸门

def _departure_gate(conn: sqlite3.Connection, trip: sqlite3.Row) -> dict:
    trip_ref = trip["trip_ref"]
    onboard = len(_onboard_state(conn, trip_ref))
    report = conn.execute(
        "SELECT * FROM headcount_reports WHERE trip_ref = ? ORDER BY id DESC LIMIT 1", (trip_ref,)
    ).fetchone()
    headcount_ok = report is not None and report["reported_count"] == onboard
    loaded = conn.execute(
        "SELECT COALESCE(SUM(loaded_qty), 0) AS total FROM trip_inventory WHERE trip_ref = ?", (trip_ref,)
    ).fetchone()["total"]
    inventory_ok = loaded > 0
    inspection = conn.execute(
        "SELECT result FROM vehicle_inspections WHERE vehicle_ref = ? ORDER BY id DESC LIMIT 1",
        (trip["vehicle_ref"],),
    ).fetchone()
    vehicle_ok = inspection is not None and inspection["result"] == "pass"
    missing = []
    if not headcount_ok:
        missing.append("headcount")
    if not inventory_ok:
        missing.append("inventory")
    if not vehicle_ok:
        missing.append("vehicle")
    return {
        "headcount_ok": headcount_ok,
        "inventory_ok": inventory_ok,
        "vehicle_ok": vehicle_ok,
        "onboard_count": onboard,
        "reported_count": report["reported_count"] if report else None,
        "missing": missing,
    }


def departure_check(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    trip = _get_trip(conn, data["trip_ref"])
    gate = _departure_gate(conn, trip)
    conn.execute(
        "INSERT INTO departure_checks(trip_ref, headcount_ok, inventory_ok, vehicle_ok, onboard_count,"
        " missing_json, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            trip["trip_ref"], int(gate["headcount_ok"]), int(gate["inventory_ok"]), int(gate["vehicle_ok"]),
            gate["onboard_count"], json.dumps(gate["missing"], ensure_ascii=False), now_iso(),
        ),
    )
    return (200 if not gate["missing"] else 409), {"trip_ref": trip["trip_ref"], **gate}


def depart(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    """发车：人数、库存、车况缺一不可。"""
    trip = _get_trip(conn, data["trip_ref"])
    if trip["status"] not in ("scheduled", "boarding", "disrupted"):
        raise ServiceError(409, "invalid_trip_status", f"班次当前状态 {trip['status']} 不能发车")
    gate = _departure_gate(conn, trip)
    conn.execute(
        "INSERT INTO departure_checks(trip_ref, headcount_ok, inventory_ok, vehicle_ok, onboard_count,"
        " missing_json, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            trip["trip_ref"], int(gate["headcount_ok"]), int(gate["inventory_ok"]), int(gate["vehicle_ok"]),
            gate["onboard_count"], json.dumps(gate["missing"], ensure_ascii=False), now_iso(),
        ),
    )
    if gate["missing"]:
        raise ServiceError(
            409, "departure_blocked",
            "发车条件未满足，缺少：" + ", ".join(gate["missing"]),
        )
    conn.execute("UPDATE trips SET status = 'departed' WHERE trip_ref = ?", (trip["trip_ref"],))
    return 200, {"trip_ref": trip["trip_ref"], "status": "departed", "onboard_count": gate["onboard_count"]}


# ---------------------------------------------------------------- 途中封闭楼层

def close_deck(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    """途中封闭楼层：列出当前在该层的在车乘客并记录各自处置。"""
    _require(data, "trip_ref", "deck", "reason", "occurred_at")
    _get_trip(conn, data["trip_ref"])
    if data["deck"] not in ("lower", "upper"):
        raise ServiceError(400, "invalid_deck", "deck 只能是 lower 或 upper")
    occurred_at = _parse_time(data)
    onboard = _onboard_state(conn, data["trip_ref"])
    affected_refs = sorted(p for p, deck in onboard.items() if deck == data["deck"])
    provided = {d.get("passenger_ref"): d for d in data.get("dispositions", [])}
    cursor = conn.execute(
        "INSERT INTO deck_closures(trip_ref, deck, reason, closed_at) VALUES (?, ?, ?, ?)",
        (data["trip_ref"], data["deck"], data["reason"], occurred_at),
    )
    closure_id = cursor.lastrowid
    affected = []
    for passenger_ref in affected_refs:
        entry = provided.get(passenger_ref, {})
        disposition = entry.get("disposition", "pending")
        note = entry.get("note", "")
        conn.execute(
            "INSERT INTO deck_closure_dispositions(closure_id, passenger_ref, disposition, note)"
            " VALUES (?, ?, ?, ?)",
            (closure_id, passenger_ref, disposition, note),
        )
        affected.append({"passenger_ref": passenger_ref, "disposition": disposition, "note": note})
    conn.execute(
        "INSERT INTO deck_zones(trip_ref, deck, zone, state, updated_at) VALUES (?, ?, '全部区域', 'closed', ?)",
        (data["trip_ref"], data["deck"], occurred_at),
    )
    event_ref = _record_safety_event(
        conn,
        category="deck_closure",
        description=f"封闭{data['deck']}层：{data['reason']}（受影响 {len(affected)} 人）",
        occurred_at=occurred_at,
        trip_ref=data["trip_ref"],
        responsibility=data.get("responsibility"),
    )
    return 201, {
        "closure_id": closure_id,
        "trip_ref": data["trip_ref"],
        "deck": data["deck"],
        "affected": affected,
        "safety_event_ref": event_ref,
    }


# ---------------------------------------------------------------- 安全事件与责任链

def add_safety_event(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "category", "description", "occurred_at")
    if data["category"] not in SAFETY_CATEGORIES:
        raise ServiceError(400, "invalid_category", "category 必须是：" + ", ".join(SAFETY_CATEGORIES))
    occurred_at = _parse_time(data)
    if data.get("trip_ref"):
        _get_trip(conn, data["trip_ref"])
    event_ref = data.get("event_ref") or _new_ref("SE")
    try:
        ref = _record_safety_event(
            conn,
            category=data["category"],
            description=data["description"],
            occurred_at=occurred_at,
            trip_ref=data.get("trip_ref"),
            vehicle_ref=data.get("vehicle_ref"),
            event_ref=event_ref,
            responsibility=data.get("responsibility"),
        )
    except sqlite3.IntegrityError:
        raise ServiceError(409, "event_exists", f"安全事件已存在：{event_ref}")
    return 201, {"event_ref": ref}


def add_responsibility_link(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    _require(data, "event_ref", "actor_ref", "role", "action")
    row = conn.execute(
        "SELECT event_ref FROM safety_events WHERE event_ref = ?", (data["event_ref"],)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "event_not_found", f"安全事件不存在：{data['event_ref']}")
    _insert_responsibility_link(conn, data["event_ref"], data)
    return 201, {"event_ref": data["event_ref"], "linked": True}


# ---------------------------------------------------------------- 复盘

def replay(conn: sqlite3.Connection, data: dict) -> tuple[int, dict]:
    """行程复盘：采用的路线、各时点在车人数、茶歇处理结果、异常责任链。"""
    trip = _get_trip(conn, data["trip_ref"])
    revision = conn.execute(
        "SELECT * FROM route_revisions WHERE route_ref = ? AND revision = ?",
        (trip["route_ref"], trip["actual_revision"]),
    ).fetchone()
    planned = conn.execute(
        "SELECT * FROM route_revisions WHERE route_ref = ? AND revision = ?",
        (trip["route_ref"], trip["planned_revision"]),
    ).fetchone()

    timeline = []
    onboard = 0
    for row in _passenger_events(conn, trip["trip_ref"]):
        onboard += 1 if row["direction"] == "board" else -1
        timeline.append({
            "occurred_at": row["occurred_at"],
            "passenger_ref": row["passenger_ref"],
            "passenger_event": "boarded" if row["direction"] == "board" else "alighted",
            "deck": row["deck"],
            "stop_name": row["stop_name"],
            "onboard_after": onboard,
        })

    headcounts = conn.execute(
        "SELECT reported_count, reported_by, reported_at FROM headcount_reports WHERE trip_ref = ? ORDER BY id",
        (trip["trip_ref"],),
    ).fetchall()
    checks = conn.execute(
        "SELECT headcount_ok, inventory_ok, vehicle_ok, onboard_count, missing_json, checked_at"
        " FROM departure_checks WHERE trip_ref = ? ORDER BY id",
        (trip["trip_ref"],),
    ).fetchall()
    sales = conn.execute(
        "SELECT event_id, lot_ref, quantity, occurred_at FROM sale_events WHERE trip_ref = ?"
        " ORDER BY recorded_seq",
        (trip["trip_ref"],),
    ).fetchall()
    closures = conn.execute(
        "SELECT * FROM deck_closures WHERE trip_ref = ? ORDER BY id", (trip["trip_ref"],)
    ).fetchall()
    closure_views = []
    for closure in closures:
        dispositions = conn.execute(
            "SELECT passenger_ref, disposition, note FROM deck_closure_dispositions WHERE closure_id = ?"
            " ORDER BY passenger_ref",
            (closure["id"],),
        ).fetchall()
        closure_views.append({
            "closure_id": closure["id"],
            "deck": closure["deck"],
            "reason": closure["reason"],
            "closed_at": closure["closed_at"],
            "affected": [dict(d) for d in dispositions],
        })

    events = conn.execute(
        "SELECT * FROM safety_events WHERE trip_ref = ? OR (trip_ref IS NULL AND vehicle_ref = ?)"
        " ORDER BY occurred_at, event_ref",
        (trip["trip_ref"], trip["vehicle_ref"]),
    ).fetchall()
    event_views = []
    for event in sorted(events, key=lambda e: (sort_key(e["occurred_at"]), e["event_ref"])):
        links = conn.execute(
            "SELECT actor_ref, role, action, at FROM responsibility_links WHERE event_ref = ? ORDER BY at, id",
            (event["event_ref"],),
        ).fetchall()
        event_views.append({
            "event_ref": event["event_ref"],
            "category": event["category"],
            "description": event["description"],
            "occurred_at": event["occurred_at"],
            "responsibility_chain": [dict(link) for link in links],
        })

    windows = conn.execute(
        "SELECT * FROM stop_windows WHERE trip_ref = ? ORDER BY planned_arrive_at, id", (trip["trip_ref"],)
    ).fetchall()

    return 200, {
        "trip_ref": trip["trip_ref"],
        "status": trip["status"],
        "vehicle_ref": trip["vehicle_ref"],
        "service_date": trip["service_date"],
        "route": {
            "route_ref": trip["route_ref"],
            "planned_revision": trip["planned_revision"],
            "planned_stops": json.loads(planned["stops_json"]) if planned else [],
            "adopted_revision": trip["actual_revision"],
            "adopted_stops": json.loads(revision["stops_json"]) if revision else [],
        },
        "stop_windows": [dict(w) for w in windows],
        "passenger_timeline": timeline,
        "headcount_reports": [dict(h) for h in headcounts],
        "departure_checks": [
            {**{k: c[k] for k in ("headcount_ok", "inventory_ok", "vehicle_ok", "onboard_count", "checked_at")},
             "missing": json.loads(c["missing_json"])}
            for c in checks
        ],
        "tea_service": {
            "inventory": _inventory_rows(conn, trip["trip_ref"]),
            "sales": [dict(s) for s in sales],
        },
        "deck_closures": closure_views,
        "safety_events": event_views,
    }
