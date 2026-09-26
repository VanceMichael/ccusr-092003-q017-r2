# 茶歇巴士移动服务联控

主题巴士在行驶中同时承载观光、茶饮、拍摄和公共交通服务，路线、人数、食品批次和车辆状态需要统一核对。

本服务通过 HTTP 接口交换业务记录，并使用 SQLite 文件保存状态。`PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；`fixtures/example.json` 提供不含真实身份的本地示例，`contracts/entities.json` 记录首批字段约定。

## 本地开发

运行 `make migrate` 初始化数据文件，`make test` 执行现有自动化检查，`make run` 启动服务。也可以使用 `docker compose up --build` 在隔离容器中运行，宿主机端口由 `APP_PORT` 调整。服务启动时会自动应用未执行的迁移。

## 领域规则

- 车辆、班次、路线版本、停靠窗口、上下层开放区域、登离车记录、茶饮批次与安全事件均按实际时序保存（`occurred_at` 为业务发生时间，`received_at` 为服务端接收时间）。
- 发车前人数、库存、车况缺一不可：`POST /trips/{ref}/departure-check` 落库三项检查结果，`POST /trips/{ref}/depart` 仅在三项全部通过时放行。
- 登离车与茶饮事件以 `event_id` 作为幂等键：断网扫码恢复后重传返回 `duplicated: true`，不重复增加乘客或销量；同一 `event_id` 携带不同内容会被拒绝（409）。
- 途中封闭二层（`POST /trips/{ref}/deck-closures`）自动列出该层在车乘客并记录逐人处置（默认移至下层，可用 `handling_map` 指定退票等）。
- 食品停用（`POST /food-lots/{ref}/recall`）、故障（`POST /trips/{ref}/breakdown`）、道路调整（`POST /trips/{ref}/reroute`）只影响相关班次：原配载计划与原计划路线版本保留，变化以调整记录和事件形式追加。
- `GET /trips/{ref}/replay` 复盘一次行程：实际采用与原计划路线、各时点在车人数、茶歇处理结果、安全事件与调整构成的异常责任链。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/vehicles` · `/vehicles/{ref}/condition` | 登记车辆 / 更新车况 |
| POST | `/routes` · `/routes/{ref}/revisions` | 登记路线 / 新增路线版本 |
| POST | `/trips` | 创建班次（计划版本即初始采用版本） |
| POST | `/trips/{ref}/stop-windows` | 登记停靠窗口 |
| POST | `/trips/{ref}/deck-zones` | 开放/关闭上下层区域 |
| POST | `/trips/{ref}/passenger-events` | 登离车记录（幂等） |
| POST | `/trips/{ref}/departure-check` · `/depart` | 发车前检查 / 发车 |
| POST | `/food-lots` · `/trips/{ref}/load-food` | 登记批次 / 班次配载 |
| POST | `/trips/{ref}/food-events` | 茶饮销售/赠送/销毁（幂等） |
| POST | `/food-lots/{ref}/recall` | 停用批次（仅影响相关班次） |
| POST | `/trips/{ref}/reroute` · `/breakdown` · `/deck-closures` | 改道 / 故障 / 封闭甲板 |
| POST | `/trips/{ref}/incidents` | 登记安全事件（含责任人引用） |
| GET | `/trips/{ref}/replay` | 行程复盘 |
