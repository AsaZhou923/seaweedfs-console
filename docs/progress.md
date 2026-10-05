# seaweedfs-console 实现进度指针

更新时间：2026-10-05

唯一进度台账是：

`E:/Project Code/docs/01 - Projects/seaweedfs-console/08 - Plans/seaweedfs-console 开发计划.md`

## 当前方向

用户已纠正产品方向：官方 SeaweedFS OSS Admin 管理能力是主产品，图片资产工作台是增强模块。当前代码已有 01–22 的共享基础与图片增强证据；官方管理核心 28–37 已完成，当前完成数为 10/10。

当前状态：实现任务01–22与28–37保留历史完成记录；全面审查R01–R15已完成本地修复与回归，325 passed, 1 warning。当前证据见第29轮记录。下文所有第14–28轮及311 passed/旧包/真实联调均为历史快照，不替代本轮输入验证。W01继续WATCH，真实LANCE Worker正向与生产全局写入未重跑。

## 已保留的图片/共享证据

- 后端既有事实：102 tests pass；actor/job/snapshot 使用 `user.id`；timezone/RFC3339 回归通过。
- 真实服务器 final QA：`swc-integration-a4a3db42a1` full pass；前端子任务 latest smoke：`swc-integration-cbfa93c432` PASS claim。
- 早期图片阶段前端 build assets 证据：`frontend/dist/assets/index-DKzq38k9.css`、`frontend/dist/assets/index-Dwny1V6r.js`。这些不是当前最新管理台构建。
- 早期本地包 SHA 观察值：`5550efbf3fb5ee35b004791f587a0eccc04d5d8da3154a292616a9060b1bad00`。当前包会在 README/progress 更新后重打，repo 文档不记录当前包 SHA。

## 官方管理基线

- 固定 SeaweedFS 4.48 commit `530be3e37337488ecc34d58441e0bc476e121c93`，本地来源 `output/reference/seaweedfs-4.48`。
- `weed/admin/handlers/admin_handlers.go` 101–296 覆盖 Dashboard/topology、Bucket/IAM、Filer files、Volumes/EC、Worker/plugin/MQ 等 OSS 路由。
- `output/official-admin-read-probe.json`：19 个 readonly GET 返回 200 JSON；无 global mutations。
- live 只读快照：Master 1 leader 1、VolumeServer 1、Volume 3、EC 0、plugin configured/enabled true、workerCount 1、queue empty、IAM dynamic lists null。

## 第 14 轮管理实现进展

- 后端新增/推进：`management.py`、`ops`、`resources`、`native`、`objects`。
- 前端新增/推进：management 主导航、管理 DTO 和相关页面入口。
- 真实只读联调：`scripts/integration_management.py`，15 routes 全部 200，输出 `output/management-integration-read.json`。
- 生产写入：本轮记录为 `0 production writes`。
- 测试：新增管理相关测试约 50 项；最终全量测试总数等待父任务回报，本文不臆测。
- 已修：default 权限缺 flag、`AdminClient` public DTO 取 secret `KeyError`、写 verify 目标值比对、密钥 journal 脱敏和 once receipt、native 文件上传/rename SHA256 readback、live S3 scope/cursor/download null/index select。
- 当前失败：isolated Admin create 两次 HTTP 500；对应测试 bucket 清理后 404。


## 第 15 轮管理读联调与剩余修复

- real reads 继续推进；S3 pagination、select、download 已成功。
- isolated Admin create 的失败方向已从“Admin 500”定位为基本 create 改走 S3 confirmed 路径。
- 后端测试已有 153 项通过记录；最终总数等待父任务 collect/全量回报。
- 测试写入白名单扩展为随机 `swc-integration-*` 和 `swc-management-*` bucket；仍不得写生产 bucket、生产对象或服务全局配置。
- 仍在修：quota `needs_review` 字段、lifecycle 白名单 403、Filer `FullPath` / `Mode` 误过滤全部 entry。


## 第 16 轮参考资料与技术映射校正

- 调研报告和可行性分析正文已改为官方 OSS Admin 管理增强主线，图片工作台是增强模块。
- 官方 Volume/EC/Worker/MQ 不再写作 P2 高级运维后置；Enterprise 只作为商业能力边界。
- 系统/详细技术设计补充 `management_objects` 实时 source/schema 映射、scope/cursor、endpoint-bound credential、S3 binding 和写后精确读回。
- 158 case collect 仍待父任务最终确认；当前不把它写成已通过。


## 第 17 轮 management write fullpass 与模块进度

- 真实 management write full PASS：`swc-management-d28a638ebc`，证据 `output/management-write-swc-management-d28a638ebc.json`。
- 覆盖 CRUD、quota 1GiB、lifecycle、policy、owner、versioning、非空删除拒绝、空桶删除 confirmed、HEAD404；journal 7 条无 secret。
- `output/management-integration-read.json` 最新 read15 + S3 pagination/select/download + volume 两页 PASS。
- lifecycle wire 已按 Go 上游 `{rules}` flat/lower DTO 修正；Admin basic create 500 改为标准 S3 create，不再当作未核实部署 bug。
- 后端曾全量159 pass，新增 core allowlist case 后当前为160case；父任务仍会再跑最终验证。
- MQ/S3Tables official API + UI 已实现 mock/source verified，35 从未开始改为进行中。
- 仍待修：native endpoint registry binding、maintenance false confirmed 两个 HIGH；全局 writes 边界未测。


## 第 18 轮 readonly16、review关闭与 gRPC 接入

记录文件：`2026-10-04-18-readonly16-review关闭与gRPC接入`。

- 父任务回报 175 tests PASS（override addopts summary）。
- 真实 readonly management integration 16 routes PASS，包含 services health；Master、Filer、Volume、S3 四类 configured instances healthy，source version / leader DTO 已补。
- 独立 review 复核两个 finding 已关闭，当前无 HIGH/MEDIUM；缺 LSP 工具卡不作为审批阻塞。
- 文件管理：delete-preview 完整树、partial preview 拒绝、literal `?/#/%` 路径上传读取正在收口。
- maintenance：missing config 返回 422，current config route 正在收尾。
- IAM/object-lock：policies `{policies}`、empty clear、group status `{enabled: bool}`、`is_static` unknown、object-lock not configured 与 unsupported 分开，SDK errors 不泄漏。
- gRPC/protobuf：用户已许可并锁定 `grpcio==1.84.0`、`protobuf==7.36.2`、`grpcio-tools==1.84.0`、dev `setuptools==84.0.0`；vendor proto/stubs、source license 和 `scripts/generate_grpc.py` 已加入。
- Mount API：已有真实 RPC configuredFiler0；该值只代表当前 Filer，不代表 whole cluster。Server SSH 31888 → private Filer 18888 loop 为该验证链路的一部分。

## 仍不能宣称的内容

- 不能宣称官方 OSS Admin 管理台完成；当前仅 28 已完成，29–37仍需最终证据。
- 不能把 01–22 图片/共享任务完成当作 28–37 完成。
- 不能把 fake transport/global mock 写保护当成真实生产写验证。
- 不能在生产全局 IAM/Volume/maintenance/Bucket 写操作上宣称已测；目前只有真实只读联调和随机测试 bucket 写验证事实；生产全局写仍未授权验证。
- 不能把 Enterprise OIDC、集中监控、管理审计、PITR/undelete 冒充为 OSS 主线。


## 第 19 轮 readonly17、Mount API、advanced bucket 与 UI595

记录文件：`2026-10-04-19-readonly17-MountAPI-advancedBucket-UI595`。

- 基础管理只读联调升级为 readonly17；Mount API 有真实 RPC 证据，当前 configuredFiler0 只代表该 Filer，不代表 whole cluster。
- 父任务曾回报 201 tests passed；该数值是本轮历史证据，不写成当前最终总数，native + decoder 新 case final 待回报。
- Filer cursor 修复：cursorShouldFalse + LastFileName 不能推断 Next=true；volume false/missing bool 也不能直接当 confirmed false。
- UI595：先测得 docWidth 683 > viewport 595；修复后 docWidth 580 / viewport 595 / sidebar 60，桌面 Service inventory 3 cols 不强制 scroll，最终截图仍待回报。
- 高级 bucket 真实 full PASS：`output/management-write-swc-management-527c2cf3fc.json`，basic `swc-management-527c2cf3fc`，advanced `swc-management-5ea77bb35a`；lock GOVERNANCE default 1 day、versioning enabled、quota/owner confirmed，cleanup 后两个 bucket HEAD404，journal 9 条无 secret。
- SDK advanced compound partial 进入 needs_review，不自动 cleanup/retry。
- 依赖：用户已授权并锁定 `fastavro==1.12.2`、`pyarrow==25.0.1`；生成开发工具不是部署必需。
- Mount 详情 + TLS localhost wire 已有 24 tests；table native signed POST / GetTable ownmeta 已有 26 tests；table_preview + decoder JSON/Avro/Parquet bounded child 已有 5 + 18 case 记录。
- DataPreview 不是 logical query；delete files 不适用；snapshot ID / int64 以十进制 string 保真。早期 catalogMock 边界已被后续 owned-instance catalog 正向测试替代，历史记录保留。


## 第 20 轮 table preview real S3、decoder 与 native 边界

记录文件：`2026-10-04-20-tablepreview-realS3-decoder-native-boundary`。

- table preview 真实 S3 PASS：`output/table-preview-swc-management-tablepreview-b4d0a95b4d.json`，4 files 覆盖 metadata JSON / Avro list / manifest / Parquet，rows=2，真实 GET/HEAD 字节与 ETag 已 capture。
- `catalog_stub:true` 是第20轮 fixture_catalog_contract 历史证据；第26轮已补 owned 独立实例 full real PASS，不再把该 fixture 当作当前 live catalog 结论。
- 解除 stub 后早期 table-details GetTable 返回 not_configured / `S3_TABLES_NOT_CONFIGURED`；后续已定位为 management 连接缺 `endpoints.s3`，不是服务器缺服务证明；不创建 global table。
- 早期 `2ccf713e34` 失败为 Origin 先判 `FORBIDDEN_ORIGIN`，期望已修正，历史失败保留。
- `fastavro==1.12.2`、`pyarrow==25.0.1` 已 locked + installed，pip check clean。
- table decoder：table JSON 8 MiB，Avro/Parquet 32 MiB，child 512 MiB / 15s，result 1 MiB；18 unit real-file pass。
- table preview：scope/download/cursor/snapshot/file membership/source HEAD/version token + metadataLocation 变化保护，6 case pass。
- native 元信息 JSON signed POST Root `S3Tables.GetTable` 无 Admin Cookie、不做 URI 外取，26 cases。
- snapshot ID 和 Parquet int64 超 JS 2^53 时保真为 string。UI 已有 structural meta info + raw sample 表格；raw file 不是整表 query，totalRows unknown。
- 父任务 full suite 正在跑；201 tests passed 仍只作为历史记录。29/30/31 等逐页 UI 截图后定状态，32–37 不盲目全完成。


## 第 21 轮 fix review、scope URI、column DTO 与 SSR

记录文件：`2026-10-04-21-fixreview-scopeURI-column-SSR`。

- 父任务回报 whole backend 231 tests PASS，但这是最后 scope URI / column fix 之前的历史总数，不写作最终总验收。
- scope URI 防穿越后 6 table preview 窄测 PASS：s3 literal / encoded dot segments、`/buckets`、decoded NUL、double decode。
- review HIGH 已修，仍待独立复核。
- Frontend Parquet columns DTO 修复：object 不再经 `String()` 变成 `[object Object]`，改读 `column.name`。
- `scripts/verify_table_preview_ui.cjs` 用现有 Vite SSR 验证 real component field id/name + int64 值 PASS；不新增依赖，不开网络监听。
- SSR 早期失败保留：TypeScript 7 无 `transpileModule`，Windows ESM 路径 `ERR_UNSUPPORTED_ESM_URL_SCHEME`；改用 Vite SSR + `pathToFileURL` 后 PASS。
- 状态校准：29 和 30 改为已完成；31–37 继续进行中。30 的 mock 维护不等于生产全局 writes 已执行。


## 第 22 轮 最终本轮事实记录

记录文件：`2026-10-04-22-final-round-facts-231-package-ui`。

- 后端当轮完整回归：231 passed，1 个 anyio deprecation warning。此为第22轮历史证据。
- Frontend build 当轮产物：`index-63Efx6nX.css` / `index-C73KUT2H.js`。此为第22轮历史证据。
- readonly17 + S3 实时分页/选择/下载 + volume 两页 PASS。
- review 两个 table finding 复核 APPROVE，当前无 HIGH/MEDIUM。
- Vite SSR 真实组件 columns object / int64 / health version object 值渲染 PASS。
- UI 修复：未选择 Resource ARN 时 Tags 0 改未知；服务版本 `[value, source_field]` object 改 `.value`；窄 SSR + build 通过。
- 最终 UI 证据：Filer 根 3 items（`/buckets`、`/etc`、`/topics`）：`/etc` 和 `/topics` 为 system protected/disabled，`/buckets` 允许元数据浏览；Next disabled。Buckets 高级创建 checkbox 展开 versioning/lock/default retention/mode/days/quota/owner 且 readonly 创建 disabled；截图 `output/management-buckets-1280-final.jpg`。
- 图片增强回归：6 个真实服务器 JPEG 预览仍显示 640x480，截图 `output/management-images-preserved-1280.jpg`。
- Dashboard 1280/595 截图已在 `output`；595 为 docWidth 580 <= 595、sidebar 60。
- 最后 UI 修复：默认 lifecycle JSON 使用小写 `rules`，不是 `Rules`；build 通过。
- Filer 根 3 项系统保护/下一页已禁用（ShouldFalse）；Mount readonly 真实 0 configuredFiler。
- 基础 + advanced 创建真实两桶 PASS，cleanup HEAD404 证据已保存。
- 打包：93 zip entries including manifest；privacy 用真实 secrets bytes 扫全 zip PASS，无 output/data/env/cred。README/progress 更新后需父任务重打包；最终 SHA 只写 vault record22，不写 repo README/progress。
- 状态校准：31 已完成；32–37 保持进行中。整个 goal 仍 active，不写项目完成态。


## 第 23 轮 IAM、Worker 与对象版本删除

记录文件：`2026-10-04-23-iam-worker-object-delete`。

- 32 IAM：真实 `AdminClient` + `httpx.MockTransport` + durable ledger 集成通过，不用 mock ops；修复 Admin 404 before create / after delete handling；service account secret 一次返回、clear empty description/expiration 精确 verify；resources 18 tests pass。
- 34 Worker：`plugin.enabled=true` 且 registry 完整才 dispatch；detect 绑定 requestId，execute 绑定 jobID 且成功态；run 旧逻辑曾把 before=after 的旧同类型成功 job 误判为 confirmed，已修为 response-linked jobID 或 before/after 新增同类型 accepted/success jobID；run 绑定 job type/计数/expire 官方 expired:true；ignored/failed/unknown/replay 覆盖，native 32 pass，独立 review APPROVE。
- 本地 mock 不等于 production writes。34 完成表示实现与契约验证完成，不表示已对生产执行 future/global worker 操作。
- 33 Objects：新增真实路径 `/api/v1/management/{id}/objects/version-info` 和 `/api/v1/management/{id}/objects/delete-version`。object.manage 默认 false，要求 writable scope、S3 connection、固定 Idempotency-Key、两个 confirmation、版本 ETag 核验和 Retention/Hold 保护。
- `scripts/integration_object_delete.py` 两桶真实脚本通过：bucket `b00f18f764` 记录 oldVersion404、current bytes unchanged、replay same intent、WrongETag409、Readonly403、null501、LegalHoldON409、cleanup404、journal 无 secret；其中 null501 是旧行为证据，后续不存在的 null version 已改为正确 404。
- 初轮 `6b05fec0d6` hold404 失败保留；修复 ObjectLockConfigurationNotFoundError / NoRetentionConfiguration 只代表无 retention，可继续 hold；key missing 不吞掉。修后 mandatory hold PASS。
- 状态校准：32、33、34 已完成；33 的强版本删除、条件删除、前端 fence 和 DeleteMarker needs_review 防护已通过独立 review；官方管理核心更新为 7/10。


## 第 24 轮 条件删除闭环、API契约与 S3Tables 配置调查

记录文件：`2026-10-04-24-conditional-delete-contract-s3tables-config`。

- 条件删除闭环进行中；新增 `POST /api/v1/management/{id}/objects/check-conditional-delete`，请求 `{scope_id}` + `Idempotency-Key`，需要 `object.manage`、writable scope 和关联 S3 connection。
- 返回 `status` / `journal_status`（`confirmed`、`needs_review`）与 `capability_status`（`supported`、`unsupported`、`unknown`、`needs_review`）。
- supported 判定必须满足 proof 24h 内、非 future、same scope、prefix、endpoint/TLS、server header、wrong If-Match 412 + GET 相同 + cleanup HEAD404。
- 新增只读 `GET /api/v1/management/{id}/objects/conditional-delete-probes`，仅返回 SafeFields，并在操作总览汇总 probe 记录。
- 条件删除第一次真实运行 `4f6be8b674` 失败，原因是 QA 脚本把未版本化 VersionId `None` 预设成字符串 `null`。脚本改为按实际 HEAD 身份后，`33e5f48d60` PASS：未版本化、暂停、null delete、replay、wrong-etag 均确认通过，bucket cleanup404。
- 强版本删除最新 `398a143767` PASS：oldVersion404、current bytes unchanged、HoldON409，两个 bucket cleanup404；不存在的 null version 现在正确 404，旧 `b00f18f764` 的 null501 只保留为历史旧行为。
- conditions 当前 19 tests，objects 当前 28 tests；后端完整 282 tests exit 0，pytest `-q` 抑制总数但 collect-only 核实 282，1 个 anyio deprecation warning；frontend build `index-B2ksGbYm.js`，SSR 真实 tableColumns/int64/serviceversion PASS，`pip check` clean。
- 新增 `scripts/verify_package.py`：manifest、excluded paths、真实 credential bytes、zip SHA；最终包 hash 仍待父任务重打包后提供，不写入 repo README/docs。
- S3Tables not_configured 调查：此前 integration_table_preview management 缺 `endpoints.s3`，不是服务器缺 S3Tables 服务证明；第26轮在用户批准 owned 独立 4.48 实例上完成合法布局的 catalog 正向 PASS。

## 第 25 轮 Lance/Plugin worker preview 与 readonly18补齐

记录文件：`2026-10-04-25-lance-worker-preview-scope`。

- Lance/Plugin：官方4.48 `weed/admin/dash/iceberg_data_preview.go` 106–109 / 485–530 已有 `Plugin.RequestObjectPreview` 样本预览；旧“Lance只有catalog/no data reader”口径不再作为当前建议。
- LANCE 当前证据：默认位置授权来自 `lance/storage.go` 103–108 / `handlers_table.go` 529–531；catalog warehouse/metadata 为空时原样保留，DTO `authorization_source` 声明推导路径，整 dataset scope 授权，partial prefix 拒绝。`f247358f2a` 真实 dependency-negative PASS：无 Worker 返回 `dependency_unavailable` / `TABLE_WORKER_PREVIEW_ALERT`，rows/total_rows/deletes_applied=null；真实 Worker 正向未运行。
- IAM/principals：新增 `/iam/principals` 来源 `/api/principals`。首次 readonly18 因真实 Go 返回 `principals:null` 而触发 502，mock `[]` 未覆盖；已按 pinned `principal_suggestions.go` 将 nil `[]string` 修为合法空建议 `items:[] / total:0`。最新 read18 重测全 200，coverage 为 `suggestions_only_not_identity_inventory`，不代表整套身份库为空。
- Worker/maintenance：JobType 增加 runs（官方 `/api/plugin/job-types/{type}/runs`），新增 `GET maintenance/jobs/{job_id}` 聚合 job/detail。resources22 测试通过；read18 重测全 200。

## 第 26 轮 read18、catalog PASS 与 403 初诊修正

记录文件：`2026-10-04-26-read18-catalog-403-correction`。

- read18 最新全 200，包含 principals；证据 `output/management-integration-read.json`，productionMutations=0。
- 原服务器对象删除链路保持 full PASS：conditional `33e5f48d60`、strong `398a143767`，清理 HEAD404。
- S3Tables/Iceberg catalog 正向：`table-catalog92fe388482` full real PASS。测试使用 owned 独立 4.48 实例 `swc-test-catalog-eabfac72`，合法 `qa/events/...` / `namespace/table/...` 布局，ordinary S3 PUT/HEAD/GET 4 files，真实 RegisterTable + table-details native + table-preview rows2/snapshot1/4，无 stub，cleanup 全部 OK。
- 初次 `83c1c3569c` preview 403 的修正原因：旧 fixture `table/metadata/v1.json` 只有 3 段，不符合 pinned `weed/s3api/bucket_paths.go` 52–85 的 TableBucket `namespace/table/data|metadata/...` 至少 4 段布局。不是 TableBucket 普通 S3 一律不可读，也不是已证实 Reader 后端 bug。
- Core 禁止高权限 Filer fallback 绕过 403；Reader 保持 ordinary S3。35 功能实现、ICEBERG real positive、LANCE dependency-negative、local positive mock 已有；独立 review 正在复核最后 delta，生产 global writes 未验证，暂保持进行中。

## 第 27 轮 Objects33 完成与 LANCE dependency-negative

记录文件：`2026-10-04-27-objects33-complete-lance-negative`。

- 33 改为已完成：独立 review 明确完成，无 HIGH/MEDIUM 阻塞；强版本、条件删除、前端 fence 均验收。
- DeleteMarker=true 错误永久删除宣称已防护为 `needs_review`；objects 当前 29 tests。
- LANCE `f247358f2a` PASS：默认位置授权按源码推导，warehouse/metadata 空值原样保留，DTO 声明 `authorization_source`，整 dataset scope 授权，partial prefix 拒绝。
- 无 Worker 场景返回 `dependency_unavailable` / `TABLE_WORKER_PREVIEW_ALERT`，rows/total_rows/deletes_applied=null；这是真实 dependency-negative，不是 Worker 正向。
- `7ff66905b4` 409 失败保留为历史：强制 warehouse 不适配真实 GetTable 空字段；后修为保留空字段和授权来源声明。
- helper 安全 4 tests passed：防注入、PID 复用、错误 inspection、ACL 失败不漏资源。Root full suite 仍在跑，不写最终总数或 package hash。

## 第 28 轮 最终包前认证 UX、311 回归与任务35/36收口

记录文件：`2026-10-04-28-final-prepackage-auth-ui-311`。

- 后端完整回归最新证据为 311 passed，1 个既有 anyio deprecation warning，耗时 57.69s；frontend latest build 通过，`verify_auth_ui.cjs` 的 epoch 决策 PASS，table preview SSR PASS，`pip check` clean。
- 认证会话过期 UX 已实测修复：旧页面只显示 `UNAUTHENTICATED` 且不回 Login；现会清 user、CSRF 和全部权限 selection，丢弃旧 401 与旧 generation 的 200，只有真实 Login / 首次 bootstrap 推进 epoch，写请求不重发。本地 owned UI DB 主动让测试 session 过期后，刷新进入 Login 并显示“会话到期请重新登录”，重新登录后 Objects 恢复成功；截图 `output/auth-session-expired-final.png`，密码未记录。
- 35 改为已完成：ICEBERG/S3Tables 有真实正向 `table-catalog92fe388482`；LANCE 有 local positive mock 和真实 dependency-negative `f247358f2a`。真实 Worker 正向未运行，生产 global writes 未验证，按边界记录。
- 36 改为已完成：read18、对象删除、bucket 写入、catalog owned 实例、311后端回归和前端验证形成当前真实服务联调证据。
- 37 改为已完成：package verifier 通过，104 payload files + manifest；manifest hashes、archive、excluded private paths、server-secrets、全部 registry 和 ui-test 密码等真实 credential bytes 扫描均 passed。最终 Dashboard 截图 `output/management-dashboard-final-1280.png` 已重捕，docWidth 1265；最新 JS `index-GlTj8Kzr.js`，stylesheet `index-63Efx6nX.css` 未变，Cua console 无 JS 异常。
- SQLite 恢复验证补充：`output/ui-data/console.db` 备份到 `output/backups/console-final-311.db` 并恢复到 `output/restore-final-311/console.db`；integrity/FK OK，表计数一致（含管理新表），备份与恢复库 SHA 相同。该验证只覆盖控制台 SQLite，不代表 S3 对象灾备。
- `codebundle 0.1.0-local` 只表示本机源代码与 static 可用，不是生产部署；repo README/docs 不记录包 hash，避免自引用。LANCE 真实 Worker 正向未运行，生产 global IAM / Volume / Worker / MQ / Table 写入未执行。



## 2026-10-05 全面审查修复

唯一进度台账新增§1.2，R01–R15共15/15本地修复验证完成。325 passed, 1 warning；独立代码复核COMMENT（无阻塞或分级缺陷发现；使用源码基线、构建/测试复核，LSP/ast-grep不可用）；独立架构WATCH（代码BLOCK已关闭，W01保留）。记录：[[01 - Projects/seaweedfs-console/05 - Testing/Records/2026-10-05-29-全面审查修复与本地回归]]。本轮仅修复与验证本地实现。全部存储/管理副作用使用 fake adapter 或全模拟 UI；没有重新执行真实 OptiPlex、LANCE Worker 正向、生产全局写入、部署、推送或覆盖历史发布包。原 2026-10-04 的任务验收、截图、311 passed 和发布包是历史证据。目录没有 Git 元数据，源码基线和变更摘要保存在 `output/fix-review-2026-10-05/`。

W01 继续为 WATCH：当前 foundation 只有 schema_migrations(version, applied_at)，其他模块以幂等 initializer 和局部兼容 schema 适配初始化。名称/checksum/result 的统一迁移审计，以及正式支持旧版本到当前版本的完整 fixture 矩阵尚未完成。本轮不新增迁移框架、不回填业务历史、不承诺任意旧库升级。R06对旧capacity_snapshots表执行保留行数据的nullable适配，旧数值不改写，以legacy_unknown/measurement在消费层保持未知。新建本地库与本轮明确覆盖的兼容fixture可以按当前测试使用；真实旧库升级应先保留SQLite backup、在副本完成初始化及integrity/FK/count对照，再确定升级范围。R06的测量覆盖兼容回归不能证明全项目迁移审计完成。
