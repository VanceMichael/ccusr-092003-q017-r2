# 茶歇巴士移动服务联控

主题巴士在行驶中同时承载观光、茶饮、拍摄和公共交通服务，路线、人数、食品批次和车辆状态需要统一核对。

本服务通过 HTTP 接口交换业务记录，并使用 SQLite 文件保存状态。`PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；`fixtures/example.json` 提供不含真实身份的本地示例，`contracts/entities.json` 记录字段约定与枚举。

## 本地开发

运行 `make migrate` 初始化数据文件，`make test` 执行现有自动化检查，`make run` 启动服务（启动时会自动应用未执行的迁移）。也可以使用 `docker compose up --build` 在隔离容器中运行，宿主机端口由 `APP_PORT` 调整。

## 接口概览

所有业务时间字段使用带偏移量的 ISO 8601 字符串；错误响应为 `{"error": 代码, "message": 说明}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/vehicles` | 登记车辆（上下层载客量） |
| POST | `/vehicles/{ref}/inspections` | 车况检查（pass/fail） |
| POST | `/vehicles/{ref}/breakdown` | 车辆故障：仅影响未发车班次，原计划保留 |
| POST | `/routes` | 创建路线（第 1 版站点） |
| POST | `/routes/{ref}/revisions` | 道路调整：新版本只应用到指定班次 |
| POST | `/trips` | 创建班次（绑定车辆与路线版本，可带停靠窗口/开放区域） |
| GET | `/trips/{ref}` | 班次详情（在车人数、库存、发车闸门预览） |
| POST | `/trips/{ref}/stop-windows` | 追加停靠窗口 |
| POST | `/trips/{ref}/deck-zones` | 上下层区域开放/关闭 |
| POST | `/trips/{ref}/scans` | 登离车扫码（`event_id` 幂等，断网重传不重复计数） |
| POST | `/trips/{ref}/headcounts` | 站点人数上报 |
| POST | `/trips/{ref}/inventory` | 茶饮批次装车 |
| POST | `/trips/{ref}/sales` | 茶饮销售（`event_id` 幂等，不重复扣库存） |
| POST | `/trips/{ref}/departure-check` | 发车三要素核查（人数/库存/车况） |
| POST | `/trips/{ref}/depart` | 发车（缺任一要素返回 409） |
| POST | `/trips/{ref}/deck-closures` | 途中封闭楼层：列出受影响人员并记录处置 |
| POST | `/trips/{ref}/complete` | 班次完结 |
| GET | `/trips/{ref}/replay` | 复盘：采用路线、各时点在车人数、茶歇处理、异常责任链 |
| POST | `/food-lots` | 登记茶饮批次 |
| POST | `/food-lots/{ref}/suspend` | 食品停用：只封存进行中班次，已完结班次保留原记录 |
| POST | `/safety-events` | 登记安全事件（可带责任链） |
| POST | `/safety-events/{ref}/responsibility` | 追加责任链环节 |

`fixtures/example.json` 给出一条可顺序执行的演示流程。
