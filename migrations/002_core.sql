-- 茶歇巴士移动服务联控：核心业务实体
-- 所有业务时间均为带偏移量的 ISO 8601 字符串，记录按发生时序保存。

-- 车辆
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_ref    TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    capacity_lower INTEGER NOT NULL DEFAULT 0,
    capacity_upper INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

-- 车况检查（发车前车况闸门依据最新一条）
CREATE TABLE IF NOT EXISTS vehicle_inspections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_ref  TEXT NOT NULL,
    result       TEXT NOT NULL CHECK (result IN ('pass', 'fail')),
    note         TEXT NOT NULL DEFAULT '',
    inspected_at TEXT NOT NULL
);

-- 路线（原计划永远保留，改道只产生新 revision）
CREATE TABLE IF NOT EXISTS routes (
    route_ref  TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS route_revisions (
    route_ref  TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    stops_json TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (route_ref, revision)
);

-- 班次：planned_revision 保留原计划，actual_revision 记录实际采用
CREATE TABLE IF NOT EXISTS trips (
    trip_ref         TEXT PRIMARY KEY,
    vehicle_ref      TEXT NOT NULL,
    route_ref        TEXT NOT NULL,
    planned_revision INTEGER NOT NULL,
    actual_revision  INTEGER NOT NULL,
    service_date     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'scheduled'
                     CHECK (status IN ('scheduled', 'boarding', 'departed',
                                       'disrupted', 'completed', 'closed')),
    created_at       TEXT NOT NULL
);

-- 停靠窗口
CREATE TABLE IF NOT EXISTS stop_windows (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref          TEXT NOT NULL,
    stop_name         TEXT NOT NULL,
    planned_arrive_at TEXT NOT NULL,
    planned_depart_at TEXT NOT NULL,
    actual_arrive_at  TEXT,
    actual_depart_at  TEXT
);

-- 上下层开放区域（每次变更追加一条，时序保留）
CREATE TABLE IF NOT EXISTS deck_zones (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref   TEXT NOT NULL,
    deck       TEXT NOT NULL CHECK (deck IN ('lower', 'upper')),
    zone       TEXT NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ('open', 'closed')),
    updated_at TEXT NOT NULL
);

-- 登离车记录：event_id 为客户端幂等键，断网恢复重传不重复计数
CREATE TABLE IF NOT EXISTS passenger_events (
    recorded_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref      TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    passenger_ref TEXT NOT NULL,
    direction     TEXT NOT NULL CHECK (direction IN ('board', 'alight')),
    deck          TEXT NOT NULL DEFAULT 'lower' CHECK (deck IN ('lower', 'upper')),
    stop_name     TEXT NOT NULL DEFAULT '',
    occurred_at   TEXT NOT NULL,
    synced_at     TEXT NOT NULL,
    UNIQUE (trip_ref, event_id)
);

-- 站点人数上报（发车前人数闸门）
CREATE TABLE IF NOT EXISTS headcount_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref       TEXT NOT NULL,
    reported_count INTEGER NOT NULL,
    reported_by    TEXT NOT NULL DEFAULT '',
    reported_at    TEXT NOT NULL
);

-- 茶饮批次
CREATE TABLE IF NOT EXISTS food_lots (
    lot_ref          TEXT PRIMARY KEY,
    item_name        TEXT NOT NULL,
    quantity         INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'suspended', 'disposed')),
    suspended_reason TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);

-- 班次装载库存
CREATE TABLE IF NOT EXISTS trip_inventory (
    trip_ref      TEXT NOT NULL,
    lot_ref       TEXT NOT NULL,
    loaded_qty    INTEGER NOT NULL,
    sold_qty      INTEGER NOT NULL DEFAULT 0,
    suspended_qty INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (trip_ref, lot_ref)
);

-- 茶饮销售：event_id 幂等键，断网恢复重传不重复扣库存
CREATE TABLE IF NOT EXISTS sale_events (
    recorded_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref     TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    lot_ref      TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    occurred_at  TEXT NOT NULL,
    synced_at    TEXT NOT NULL,
    UNIQUE (trip_ref, event_id)
);

-- 发车核查记录（人数/库存/车况缺一不可）
CREATE TABLE IF NOT EXISTS departure_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref      TEXT NOT NULL,
    headcount_ok  INTEGER NOT NULL,
    inventory_ok  INTEGER NOT NULL,
    vehicle_ok    INTEGER NOT NULL,
    onboard_count INTEGER NOT NULL,
    missing_json  TEXT NOT NULL,
    checked_at    TEXT NOT NULL
);

-- 途中封闭楼层
CREATE TABLE IF NOT EXISTS deck_closures (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_ref  TEXT NOT NULL,
    deck      TEXT NOT NULL CHECK (deck IN ('lower', 'upper')),
    reason    TEXT NOT NULL,
    closed_at TEXT NOT NULL
);

-- 封闭时受影响人员及处置
CREATE TABLE IF NOT EXISTS deck_closure_dispositions (
    closure_id    INTEGER NOT NULL,
    passenger_ref TEXT NOT NULL,
    disposition   TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (closure_id, passenger_ref)
);

-- 安全事件（食品停用/故障/道路调整/封闭/其他）
CREATE TABLE IF NOT EXISTS safety_events (
    event_ref   TEXT PRIMARY KEY,
    trip_ref    TEXT,
    vehicle_ref TEXT,
    category    TEXT NOT NULL,
    description TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 异常责任链
CREATE TABLE IF NOT EXISTS responsibility_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_ref  TEXT NOT NULL,
    actor_ref  TEXT NOT NULL,
    role       TEXT NOT NULL,
    action     TEXT NOT NULL,
    at         TEXT NOT NULL
);
