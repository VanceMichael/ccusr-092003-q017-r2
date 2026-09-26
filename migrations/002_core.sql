-- 核心领域模型：车辆、路线版本、班次、停靠窗口、甲板开放区域、
-- 登离车记录、茶饮批次、安全事件与班次调整，全部按实际发生时序保存。

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_ref      TEXT PRIMARY KEY,
    name             TEXT NOT NULL DEFAULT '',
    capacity_lower   INTEGER NOT NULL DEFAULT 0,
    capacity_upper   INTEGER NOT NULL DEFAULT 0,
    condition_status TEXT NOT NULL DEFAULT 'serviceable', -- serviceable / defective
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_ref  TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 路线版本不可变：改道产生新版本，历史版本保留供复盘。
CREATE TABLE IF NOT EXISTS route_revisions (
    route_ref  TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    stops_json TEXT NOT NULL, -- JSON 数组，按顺序的停靠点
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (route_ref, revision)
);

CREATE TABLE IF NOT EXISTS trips (
    trip_ref          TEXT PRIMARY KEY,
    vehicle_ref       TEXT NOT NULL REFERENCES vehicles(vehicle_ref),
    route_ref         TEXT NOT NULL REFERENCES routes(route_ref),
    planned_revision  INTEGER NOT NULL, -- 原计划版本，永远保留
    active_revision   INTEGER NOT NULL, -- 实际采用版本，改道时更新
    status            TEXT NOT NULL DEFAULT 'scheduled', -- scheduled/boarding/departed/completed/cancelled
    planned_departure TEXT,
    departed_at       TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stop_windows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref   TEXT NOT NULL REFERENCES trips(trip_ref),
    stop_name  TEXT NOT NULL,
    opens_at   TEXT NOT NULL,
    closes_at  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 上下层开放区域变更流水，最新一条为当前状态。
CREATE TABLE IF NOT EXISTS deck_zones (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref   TEXT NOT NULL REFERENCES trips(trip_ref),
    deck       TEXT NOT NULL CHECK (deck IN ('lower', 'upper')),
    is_open    INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    changed_at TEXT NOT NULL
);

-- 登离车记录：event_id 为客户端幂等键，断网重传不会重复计数。
CREATE TABLE IF NOT EXISTS passenger_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT NOT NULL UNIQUE,
    trip_ref      TEXT NOT NULL REFERENCES trips(trip_ref),
    passenger_ref TEXT NOT NULL,
    action        TEXT NOT NULL CHECK (action IN ('board', 'alight')),
    deck          TEXT CHECK (deck IN ('lower', 'upper') OR deck IS NULL),
    stop_name     TEXT NOT NULL DEFAULT '',
    offline       INTEGER NOT NULL DEFAULT 0,
    occurred_at   TEXT NOT NULL,
    received_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passenger_events_trip
    ON passenger_events (trip_ref, occurred_at, id);

CREATE TABLE IF NOT EXISTS food_lots (
    lot_ref    TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    quantity   INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'active', -- active / recalled
    created_at TEXT NOT NULL
);

-- 班次配载的茶饮批次（原计划，停用后不删除，仅追加处置事件）。
CREATE TABLE IF NOT EXISTS trip_food_lots (
    trip_ref   TEXT NOT NULL REFERENCES trips(trip_ref),
    lot_ref    TEXT NOT NULL REFERENCES food_lots(lot_ref),
    loaded_qty INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trip_ref, lot_ref)
);

-- 茶饮事件：sold/served/recalled/disposed，event_id 幂等。
CREATE TABLE IF NOT EXISTS food_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    trip_ref    TEXT NOT NULL REFERENCES trips(trip_ref),
    lot_ref     TEXT NOT NULL REFERENCES food_lots(lot_ref),
    action      TEXT NOT NULL CHECK (action IN ('sold', 'served', 'recalled', 'disposed')),
    quantity    INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_events_trip
    ON food_events (trip_ref, occurred_at, id);

CREATE TABLE IF NOT EXISTS safety_incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref        TEXT NOT NULL REFERENCES trips(trip_ref),
    category        TEXT NOT NULL, -- food / vehicle / road / deck / other
    description     TEXT NOT NULL DEFAULT '',
    responsible_ref TEXT NOT NULL DEFAULT '', -- 责任人受控引用
    occurred_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 发车前检查：人数、库存、车况缺一不可。
CREATE TABLE IF NOT EXISTS departure_checks (
    trip_ref      TEXT PRIMARY KEY REFERENCES trips(trip_ref),
    headcount_ok  INTEGER NOT NULL,
    inventory_ok  INTEGER NOT NULL,
    vehicle_ok    INTEGER NOT NULL,
    checked_by    TEXT NOT NULL DEFAULT '',
    checked_at    TEXT NOT NULL
);

-- 班次调整：改道/故障/食品停用/甲板封闭，只影响相关班次，原计划不变。
CREATE TABLE IF NOT EXISTS trip_adjustments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref      TEXT NOT NULL REFERENCES trips(trip_ref),
    kind          TEXT NOT NULL, -- reroute / breakdown / food_recall / deck_closure
    detail        TEXT NOT NULL DEFAULT '',
    from_revision INTEGER,
    to_revision   INTEGER,
    created_at    TEXT NOT NULL
);

-- 甲板封闭时被影响的乘客及逐人处置结果。
CREATE TABLE IF NOT EXISTS deck_closure_affected (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    adjustment_id INTEGER NOT NULL REFERENCES trip_adjustments(id),
    passenger_ref TEXT NOT NULL,
    handling      TEXT NOT NULL -- moved_to_lower / alighted / refunded / other
);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_core');
