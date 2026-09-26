# 领域资料

主题巴士在行驶中同时承载观光、茶饮、拍摄和公共交通服务，路线、人数、食品批次和车辆状态需要统一核对。

外部主体使用不含真实身份信息的引用编号。交换时间采用带偏移量的 ISO 8601 字符串，原始材料只保存受控引用或 `sha256` 摘要。`contracts/entities.json` 中的字段名称属于稳定接口约定，运行时数据文件位置由 `DATABASE_PATH` 决定。

## 核心实体与时序

车辆（vehicles）、车况检查（vehicle_inspections）、路线（routes）与路线版本（route_revisions）、班次（trips）、停靠窗口（stop_windows）、上下层开放区域（deck_zones）、登离车记录（passenger_events）、站点人数上报（headcount_reports）、茶饮批次（food_lots）、班次库存（trip_inventory）、销售记录（sale_events）、发车核查（departure_checks）、楼层封闭与人员处置（deck_closures / deck_closure_dispositions）、安全事件与责任链（safety_events / responsibility_links）均按 `occurred_at` 时序保存，原始记录只增不改。

## 业务规则

- **发车三要素**：人数（站点上报数须与扫码在车数一致）、库存（已装载批次）、车况（最近一次检查合格）缺一不可，否则 `POST /trips/{ref}/depart` 返回 409 并列出缺项。
- **断网恢复幂等**：登离车扫码与销售记录以 `event_id` 为幂等键（班次内唯一）。恢复后重传同一 `event_id` 返回 `deduplicated: true`，不重复增加乘客或销量；同一 `event_id` 携带不同内容返回 409。
- **路线改道**：`POST /routes/{ref}/revisions` 产生新版本，只切换 `apply_to_trips` 列出班次的 `actual_revision`，各班次的 `planned_revision` 与原计划站点永远保留。
- **食品停用**：`POST /food-lots/{ref}/suspend` 只封存进行中班次的剩余库存并生成安全事件，已完结班次的装载与销售记录不变。
- **车辆故障**：`POST /vehicles/{ref}/breakdown` 记录不合格车况，仅将该车未发车班次标记为 `disrupted`。
- **途中封闭楼层**：`POST /trips/{ref}/deck-closures` 自动列出当前在该层的在车乘客，逐一记录处置（如下层安置、下车、退款，未指定则为 `pending`）。
- **复盘**：`GET /trips/{ref}/replay` 返回采用的路线版本与站点、各时点在车人数、茶歇销售与停用结果、以及异常事件的责任链。
