# 官方管理模块接口与数据来源

本文说明已经接入的接口；进度仍只维护在项目开发计划中。全部路径以 `/api/v1` 为前缀，均需要本项目登录。接口细节也可以通过本地 `/openapi.json` 查看。相关设计见 `E:/Project Code/docs/01 - Projects/seaweedfs-console/02 - Architecture/seaweedfs-console 详细技术设计.md`；运行入口见仓库 `README.md`。

管理能力以 SeaweedFS 4.48、源码提交 `530be3e37337488ecc34d58441e0bc476e121c93` 为基线。浏览器调用本项目 API，后端认证官方 Admin 或访问明确批准的原生服务地址。官方页面跳转不计入覆盖。

## 连接与权限

| 接口 | 参数及作用 | 主要异常 |
| --- | --- | --- |
| `GET /management/connections` | 查询连接的公开配置，不返回 Admin 密码或其凭据引用 | 未登录 401 |
| `POST /management/connections` | `name, admin_url, admin_secret_ref`；可选 `s3_connection_id, endpoints, permissions` | 未注册引用、地址不匹配 403；无效字段 422 |
| `PUT /management/connections/{id}/access-policy` | `management_write_enabled, acknowledge_management_write, permissions, reason`；不能改变地址和凭据身份 | 未确认启用写入 422；尝试改身份字段 422 |
| `GET /management/{id}/operations` | 本地持久操作记录，可用 `limit` 限制返回数 | 连接不存在 404 |

Admin 注册项必须包含 `kind: seaweed_admin`、`username`、`password` 和 `allowed_endpoint_url`。原生服务地址需要在该注册项的 `allowed_endpoints` 中逐项批准，例如 `master/filer/volume`。创建连接及每次使用既有连接都会重新检查；修改数据库地址或撤销注册许可不能绕过检查。

关联 S3 凭据继续由后端注册文件提供；非空 `allowed_endpoint_url` 是必填批准字段，创建和使用均检查；`allowed_connection_id` 可额外限制连接身份，不能替代端点批准。不要将真实注册文件、会话、签名 URL 或生成密钥写进文档和安装包。

写入默认关闭。权限包括 `bucket.manage`、`iam.manage`、`volume.manage`、`maintenance.execute`、`file.manage`、`mq.manage`、`table.manage`。桶写入还需要匹配 `bucket_write_prefixes`；它明确授权该字面前缀匹配的所有桶，并非单个桶名。文件另有 `file.read_roots/file.write_roots`，S3 对象另有项目存储范围。

## 集群、容量和服务

| 接口 | 数据来源及参数 | 显示边界 |
| --- | --- | --- |
| `GET /management/{id}/overview` | 官方 `/api/admin`、`/api/config` | 管理库存、真实 tier/trend 字段；没有数据不补随机曲线 |
| `GET /management/{id}/topology` | 官方 `/api/cluster/topology` | Master leader、数据中心、机架和节点 |
| `GET /management/{id}/services` | 官方 Admin 库存和 plugin 状态 | 库存数量不等同于所有节点健康 |
| `GET /management/{id}/services/health` | 已批准 Master `/dir/status`、`/cluster/status`，Filer JSON 根目录，Volume `/status`，关联 S3 ListBuckets | 只代表已配置实例；无配置、拒绝、超时、非 JSON 分别呈现 |
| `GET /management/{id}/volumes` | 官方 `/api/volumes/export` JSON；`limit,cursor,collection,readonly,disk_type` | 区分逻辑 volume 和物理副本；游标绑定内容、查询及期限，导出生成时间变化不会单独令游标失效 |
| `GET /management/{id}/collections` | 同一 volume export 汇总 | Collection 不是图片项目或任意推导出的 bucket |
| `GET /management/{id}/ec` | 真实 EC shard 信息 | 空列表和未取得数据分开；不能把无响应当零 EC |
| `POST /management/{id}/volumes/{volume_id}/actions` | `server_id, action`；read-only 另需 `read_only: boolean` | 只对当前 export 中已知副本执行；vacuum 无充分读回证据时不声称完成 |

健康探测使用独立、无 Admin cookie 的 HTTP 客户端，不跟随重定向，限时和限制响应体。版本仅取响应中的真实字段，并附来源。

## 桶和 S3 配置

| 接口 | 参数/行为 |
| --- | --- |
| `GET/POST /management/{id}/buckets` | 库存；基础创建使用 `{name, region}` 和关联 S3 CreateBucket，写前 HEAD、写后 HEAD 核验 |
| `GET/DELETE /management/{id}/buckets/{bucket}` | 详情；删除先检查当前对象、历史版本、删除标记和未完成 multipart，再使用标准 S3 DeleteBucket |
| `PUT /management/{id}/buckets/{bucket}/quota` | `{quota_size: integer, quota_unit: B/KB/MB/GB/TB, quota_enabled: boolean}`；按 1024 倍率核验官方字节值 |
| `PUT /management/{id}/buckets/{bucket}/owner` | `{owner: string}`，读回目标 owner |
| `GET/PUT/DELETE /management/{id}/buckets/{bucket}/lifecycle` | `{lifecycle:{rules:[...]}}` 或顶层规则；后端转为官方顶层 `{rules:[...]}`，不能将未解析字段作为成功 |
| `GET/PUT/DELETE /management/{id}/buckets/{bucket}/policy` | `{policy: AWS policy document}`；按实际文档核验 |
| `GET/PUT /management/{id}/buckets/{bucket}/versioning` | 写入 `{status: Enabled/Suspended}`，读回核验 |
| `GET/PUT /management/{id}/buckets/{bucket}/object-lock` | 写入 `{object_lock_configuration: ...}`；object-lock not configured 与 unsupported 分开，版本/保留约束仍由实际服务执行 |

生命周期官方规则示例：

```json
{"lifecycle":{"rules":[{"id":"expire-tmp","status":"Enabled","prefix":"tmp/","expiration_days":30}]}}
```

带 quota/versioning/object-lock/owner 等高级字段的官方 Admin 创建路径保留，但目标部署此前返回过 500。基础创建成功不能证明该高级创建路径已经可用。该路径结果不明时不会自动重发或切换接口重复创建。

## IAM

用户、密钥、Group、Service Account、Object Store Policy 使用 `/management/{id}/iam` 下的自有接口，对接官方 `/api/users`、`/api/groups`、`/api/service-accounts`、`/api/object-store/policies` 和 `/api/principals`。

| 资源 | 接口 |
| --- | --- |
| 用户 | `GET/POST /iam/users`；`GET/PUT/DELETE /iam/users/{username}` |
| 密钥 | `POST /iam/users/{username}/access-keys`；`DELETE /access-keys/{key_id}`；`PUT /access-keys/{key_id}/status` |
| 用户权限 | `GET/PUT /iam/users/{username}/policies`；客户端写 `actions`，官方读回字段是 `policies`，空数组表示清空 |
| Group | `GET/POST /iam/groups`；`GET/DELETE /iam/groups/{name}`；`PUT /status`；`GET/POST /members`、`DELETE /members/{username}`；`GET/POST /policies`、`DELETE /policies/{policy_name}` |
| Service Account | `GET/POST /iam/service-accounts`；`GET/PUT/DELETE /iam/service-accounts/{id}` |
| Object Store Policy | `GET/POST /iam/policies`；`GET/PUT/DELETE /iam/policies/{name}`；`POST /iam/policies/validate` |
| Principals | `GET /iam/principals`；来源为官方 `/api/principals`，只作为 role/principal 建议，不是完整 identity inventory |

用户列表未返回记录不证明服务器没有静态 S3 用户；`is_static` 没有来源时为未知。官方用户详情中模拟的密钥创建日期不显示为真实日期。`/iam/principals` 的 coverage 为 `suggestions_only_not_identity_inventory`；真实 Go 返回 `principals:null` 时应转为空建议 `items:[] / total:0`，不能把它写成身份库为空。

已有 secret 永不通过列表/详情返回。生成密钥只有在新 public key 可读回且响应包含可用 secret 时才一次性返回白名单凭据；重放同一幂等请求不再返回 secret，不把 secret 写入操作记录。Group 的客户端 `status` 被转为官方 `enabled` 布尔值。

## Filer 与实时对象

| 接口 | 参数/约束 |
| --- | --- |
| `GET /management/{id}/files` | `path,limit,cursor`；解析真实 `FullPath/Mode`，输出安全文件 DTO；不透传 Content、Extended、Remote |
| `GET /management/{id}/files/properties`、`/files/download` | `path`；只读批准范围；HEAD 仅返回安全元数据头 |
| `POST /management/{id}/files/mkdir` | `{path,folder_name}`；读回目录存在 |
| `POST /management/{id}/files/upload` | multipart `file` + query `path`；限额、拒绝已存在目标，内容 SHA256 读回核验 |
| `POST /management/{id}/files/rename` | `{source_path,target_path}`；可选 `expected_source_sha256`；使用已核实 Filer `mv.from` |
| `GET /management/{id}/files/delete-preview` | `path`；递归分页读取，返回 items、entry_count、preview_hash、complete、truncated |
| `POST /management/{id}/files/delete` | `{path}`；目录另需 `confirm_recursive,recursive_preview_path,recursive_preview_hash`；不完整或变化的预览禁止删除 |
| `GET /management/{id}/objects` | `scope_id,prefix,delimiter,limit,cursor`；实时 ListObjectsV2，无需先跑图片扫描 |
| `POST /management/{id}/objects/select` | `{scope_id,key}`；HEAD 当前对象并复用既有对象身份，保留已索引图片属性 |
| `GET /management/{id}/objects/download` | `scope_id,key,version_id`；范围/下载权限核验，literal `null` 版本保真 |

Filer 文件名的 `?/#/%` 按字面编码，query 参数独立传入。系统路径受保护；`/buckets` 可浏览元数据，原始 S3 对象字节与写入使用 S3 范围接口，不能通过 Filer 绕过版本或保留约束。

原生 Filer 不提供这里所需的原子 no-overwrite/源哈希 CAS 保证。预读、拒绝已有目标、完整删除预览和写后读回都保留，并明确 `cas_atomic:false`；不能把这些观察包装为原子并发保护。源图自动删除与移动仍要求可靠的引用保护接入。

## Worker 与其他官方模块

`GET /management/{id}/workers`、`/maintenance` 聚合 plugin status、workers、jobs、lanes、job types、scheduler、activities 等官方 JSON。新增只读 `GET /management/{id}/maintenance/job-types/{type}/runs` 来源于官方 `/api/plugin/job-types/{type}/runs`；`GET /management/{id}/maintenance/jobs/{job_id}` 聚合 job 与 detail。`POST /maintenance/actions` 支持配置更新、检测、运行和任务动作；配置需要语义匹配，运行需要可核验的任务/活动证据。HTTP 200 或失败回执本身不能证明动作成功。

`/management/{id}/modules` 和 `/modules/s3-tables` 展示依赖状态。已接入的 `/modules/mq` 包括 topic 创建/详情、retention 更新/purge；`/modules/s3-tables` 包括 bucket、namespace、table、policy、tags 的官方 JSON 操作。MQ purge 缺少可靠读回证据时保持需核查。Table 删除要求空内容确认。Iceberg/S3Tables 使用 ordinary S3 + 合法 `namespace/table/data|metadata/...` 布局；第26轮 owned-instance 已真实 RegisterTable + table-details + table-preview PASS。Lance 使用官方 Plugin worker preview 概念，当前正在补自有 JSON 有界 `worker_sample`，暂无 Lance worker live positive。

### Mount 客户端 gRPC

`GET /management/{id}/modules/mount-clients` 调用 Filer `ListMetadataSubscribers`，筛选 `mount/sw-vfs`。目标只来自 Admin 注册项的 `allowed_grpc_endpoints.filer`，客户端不能提交任意 target。

```json
{"allowed_grpc_endpoints":{"filer":{"target":"127.0.0.1:18888","transport":"plaintext"}}}
```

plaintext 默认只允许 loopback；连接测试服务器时使用 SSH 转发，未开放新的 LAN 端口。非 loopback 明文连接必须在后端注册项显式批准。TLS 使用 `transport:tls` 和 `root_cert_ref`；该引用对应注册项 `kind:grpc_tls`、`root_certificate_file`。证书读取限额 256 KiB，不回退明文。

RPC 限时 5 秒、响应限额 8 MiB，禁用环境 HTTP proxy，只提供固定只读 RPC。返回 `health_scope:configured_filer`、真实字段及观测时间。成功空列表为该 Filer 的 0；未配置、不可达、不支持、拒绝时 count 为未知，不宣称整个集群无客户端。

生成文件和 Apache 许可位于 `backend/console/vendor/`。普通部署仅安装锁定的 grpcio/protobuf；grpcio-tools 和 setuptools 只用于开发时运行 `scripts/generate_grpc.py`。[gRPC 官方 Python 指南](https://grpc.io/docs/languages/python/quickstart/)说明了运行包与生成工具的分工。

Admin OIDC、Enterprise 集中监控/管理审计、PITR 等不冒充 OSS 功能。本项目的操作记录只涵盖本项目执行的动作。

## 写入结果与测试边界

远端管理变更必须传入固定幂等key：resource类路由接收 `Idempotency-Key`（兼容 `X-Idempotency-Key`）请求头；native JSON写入接收body `idempotency_key`，native文件上传接收query `idempotency_key`。缺失/空key返回422 `IDEMPOTENCY_KEY_REQUIRED`，在持久意图与派发前拒绝。同key同意图返回持久回执，不再派发；同key不同意图返回409 `IDEMPOTENCY_CONFLICT`。前端为当前输入意图保存key并阻止pending重交，needs_review/响应丢失保留key；输入变化或明确新意图才另建key。写入前持久化意图并重新检查权限。结果为 `confirmed/failed/needs_review`；相同幂等请求不会重复调用远端，不确定结果不能自动重发。管理配置接口不承诺远端原子 CAS。

真实服务器测试只读现有集群，只在本轮随机 `swc-management-*` / `swc-integration-*` 桶写入并清理。IAM/Volume/Worker/MQ/Table 的生产全局写入没有因 mock 测试通过而变成已验证；SDK errors 不泄漏，对应证据和剩余缺口在开发计划及测试记录中保留。


测试结果及各模块剩余缺口统一查看开发计划和测试记录；本文不维护另一份进度计数。


### 管理请求回执与 pending 生命周期（第30轮补充）

前端 pending 门禁覆盖 fetch、响应正文读取、JSON 解析、认证状态核对和回执校验，最后统一释放。HTTP 2xx 不能单独证明管理操作已完成：HTML、截断 JSON、空正文、null、空对象、缺少状态或不受支持的回执形状均保留原幂等 key，并提示先查操作历史/读回。只有明确 confirmed/failed、非空 operation_id 和布尔 replayed 的受支持终态回执才释放当前 key；needs_review 保留 key。本地 `/management/connections` CRUD、登录/退出及既有只读 POST 不进入远端持久意图。header/body/query 的传输分工保持不变。

## 第19轮接口边界更新

- readonly17 已覆盖新的管理只读路径；实际路径以本文件列出的 `/management/{id}/...` 为准，计划中的 phantom alias 不作为已实现 API。
- Mount API 已有真实 RPC 证据。`configuredFiler0` 只表示当前 Filer 未返回 configured mount clients，不表示 whole cluster 没有 Mount 配置。
- 高级 bucket 写入证据为 `output/management-write-swc-management-527c2cf3fc.json`，advanced bucket `swc-management-5ea77bb35a`：lock GOVERNANCE default 1 day、versioning enabled、quota/owner confirmed，cleanup 后两个 bucket HEAD404，journal 9 无 secret。
- SDK advanced compound partial 进入 `needs_review`，不自动 cleanup/retry，避免在不确定远端状态下扩大写入。
- Mount详情+TLS localhost wire 24 tests；table native signed POST / GetTable ownmeta 26 tests；table_preview + decoder JSON/Avro/Parquet bounded child 5 + 18 case。
- DataPreview 是有界预览能力，不是 logical query。delete files 不适用于 DataPreview。JSON/Avro/Parquet decoder 使用 bounded child；snapshot ID 和 int64 以十进制 string 保真。
- `fastavro==1.12.2` 和 `pyarrow==25.0.1` 是 runtime 依赖；生成开发工具不作为部署必需。
- 早期 catalogMock / fixture 不能写成 full live PASS；第26轮已在 owned 独立 4.48 实例补齐 S3Tables/Iceberg catalog 正向 PASS。

- 201 tests passed 只作历史证据，不写作当前最终总数。


## 第20轮 table preview 与 native 边界

- table preview 真实 S3 PASS 证据：`output/table-preview-swc-management-tablepreview-b4d0a95b4d.json`。fixture 包含 metadata JSON、Avro list、manifest、Parquet，preview rows=2，GET/HEAD 字节和 ETag 已 capture。
- `catalog_stub:true` 是第20轮 fixture_catalog_contract 历史证据；第26轮 `table-catalog92fe388482` 已在 owned 独立实例完成真实 RegisterTable、table-details native 与 table-preview，无 stub。
- decoder 限额：table JSON 8 MiB，Avro/Parquet 32 MiB，child 512 MiB / 15s，result 1 MiB。
- native 元信息 JSON signed POST Root `S3Tables.GetTable` 不带 Admin Cookie，不做 URI 外取。
- snapshot ID 和 Parquet int64 超过 JS 2^53 时返回十进制 string。raw file 不等于整表 query，totalRows unknown；delete files 不适用于 DataPreview。
- 本文件只列实际 API 和边界；`/go` 别名、`/object-storage/*` 等未核实别名不写作已实现接口。


## 第21轮 review 修复与 UI SSR 边界

记录文件：`2026-10-04-21-fixreview-scopeURI-column-SSR`。

- table preview scope URI 防穿越新增 6 个窄测 PASS，覆盖 s3 literal / encoded dot segments、`/buckets`、decoded NUL、double decode。
- Parquet columns DTO 在前端显示时使用 `column.name`，避免 object 被 `String()` 渲染成 `[object Object]`。
- `scripts/verify_table_preview_ui.cjs` 使用现有 Vite SSR 验证真实组件，不新增依赖，不监听网络。
- 231 tests PASS 是 scope URI / column fix 前历史总数；review HIGH 后续已复核 APPROVE。


## 第22轮 newTable / DataPreview / package 边界

- newTable / DataPreview 已按实际能力记录：decoder libraries 为 `fastavro==1.12.2`、`pyarrow==25.0.1`；限制仍为 table JSON 8 MiB、Avro/Parquet 32 MiB、child 512 MiB / 15s、result 1 MiB。
- 精度边界：columns object、int64、health version object 均需按真实字段渲染；int64 / snapshot ID 超 JS 安全整数时保留十进制 string。
- scope 边界：scope URI 防穿越继续生效；DataPreview 不是 delete/query 接口。
- catalog 边界：早期真实 GetTable 返回 `S3_TABLES_NOT_CONFIGURED` / not_configured；后续定位为 management 连接缺 `endpoints.s3`，不是服务器缺服务证明。第26轮正向 PASS 不创建 global table，只在 owned 测试实例和合法布局下验证。
- Package 边界：privacy 扫描覆盖真实 secrets bytes；API reference 不记录包 SHA，避免 repo 文档自引用影响包哈希。


## 第23轮 IAM、Worker 与对象版本删除接口

记录文件：`2026-10-04-23-iam-worker-object-delete`。

- IAM 已覆盖 Users、Access Keys、Groups、Service Accounts、Policies 的 CRUD 契约。Service account create 只在一次响应中返回 secret；空 description / expiration 按空值精确验证。Admin 404 before create / after delete 被当作可解释状态处理，不泄露 secret。
- Worker 操作只在 `plugin.enabled=true` 且 registry 完整时 dispatch。detect 绑定 requestId；execute 绑定 jobID 且需要成功态。独立 review 发现旧 run 逻辑会把 before=after 的旧同类型成功 job 误判为 confirmed；已修为只认可 response-linked jobID，或 before/after 新增的同类型 accepted/success jobID。run 绑定 job type、计数和官方 `expired:true` 读回。ignored / failed / unknown / replay 均有测试覆盖，native 32 pass，review APPROVE。
- 对象版本信息：`GET /management/{id}/objects/version-info`，参数为 `scope_id,key,version_id`；返回版本 ETag、retention/hold 状态和身份强度。
- 对象版本删除：`POST /management/{id}/objects/delete-version`；要求 `object.manage`、writable scope、关联 S3 connection、固定 Idempotency-Key、两个 confirmation、版本 ETag 核验以及 Retention/Hold 保护。
- 删除旧版本后必须验证旧版本 HEAD404，且当前版本字节不变；脚本证据包含 WrongETag409、Readonly403、LegalHoldON409、cleanup404。历史 `b00f18f764` 中的 null501 只保留为旧行为证据；后续不存在 null version 已改为正确 404。 DeleteMarker=true 时不宣称永久删除，返回 needs_review。
- 本地 MockTransport 与 registry 测试只证明契约和实现路径，不代表 production writes 已执行。


## 第24轮 条件删除 capability probe 与 S3Tables 配置边界

记录文件：`2026-10-04-24-conditional-delete-contract-s3tables-config`。

- 条件删除探测：`POST /api/v1/management/{id}/objects/check-conditional-delete`。请求体为 `{scope_id}`，必须带 `Idempotency-Key`。
- 权限要求：`object.manage`、writable scope、关联 S3 connection。该接口是临时 probe，不代表对象删除能力全部完成。
- 响应字段：`status`、`journal_status`（`confirmed` 或 `needs_review`）和 `capability_status`（`supported`、`unsupported`、`unknown`、`needs_review`）。
- `supported` 的证据必须同时满足 proof 24h 内、非 future、same scope、prefix、endpoint/TLS、server header、wrong If-Match 返回 412、GET 内容相同、cleanup HEAD404。
- 条件删除 probe 查询：`GET /api/v1/management/{id}/objects/conditional-delete-probes`。该接口只读，只返回 SafeFields，并供操作总览汇总 probe 记录。
- 条件删除真实证据：`4f6be8b674` 首次失败来自 QA 脚本把未版本化 `VersionId None` 预设成字符串 `null`；修为按实际 HEAD 身份后，`33e5f48d60` PASS，覆盖未版本化、暂停、null delete、replay、wrong-etag 和 cleanup404。强版本删除最新 `398a143767` PASS，覆盖 oldVersion404、currentbytes unchanged、HoldON409 和两个 bucket cleanup404。
- `GET /api/v1/management/{id}/objects/version-info` 省略 `version_id` 时读取当前真实身份；`POST /api/v1/management/{id}/objects/delete-version` 中 JSON `null` 仅表示未版本化当前对象，字符串 `"null"` 表示指定可变版本，二者不能混淆。
- S3Tables 边界：此前 `S3_TABLES_NOT_CONFIGURED` 来自 integration_table_preview management 缺 `endpoints.s3`，不是服务器缺服务的正向证明。第26轮使用用户批准 owned 独立 SeaweedFS 4.48 实例 `swc-test-catalog-eabfac72`，合法 `qa/events/...` / `namespace/table/...` 布局，ordinary S3 PUT/HEAD/GET 4 files，真实 RegisterTable、table-details `native_s3tables_get_table` 和 table-preview rows2/snapshot1/4 均 PASS；cleanup 后 catalog 不再 listed、bucket HEAD404，实例 container/remote_dir/SSH pid 均清理。
- 当前验证：conditions 19 tests、objects 29 tests；后端完整 311 passed，1 个既有 anyio deprecation warning，57.69s；frontend latest build 通过，`verify_auth_ui.cjs` epoch 决策 PASS，table preview SSR PASS，`pip check` clean。
- Package verifier：`scripts/verify_package.py` 覆盖 manifest、excluded paths、真实 credential bytes 和 zip SHA；API reference 不记录当前包 hash。

## 第25轮 Lance / Plugin worker preview 纠错

- 官方 SeaweedFS 4.48 `weed/admin/dash/iceberg_data_preview.go` 106–109 / 485–530 已有 `Plugin.RequestObjectPreview` 样本预览，不能把 Lance 写成没有数据读取能力。
- 本项目当前做法是把固定 Admin HTML 数据页背后的数据能力转成自有 JSON 有界 `worker_sample`，并套用 scope 授权；不输出原 HTML，不 iframe，不跳转。
- LANCE worker preview：默认位置授权依据 pinned `lance/storage.go` 103–108 / `handlers_table.go` 529–531；catalog `warehouse` / `metadata` 为空时原样保留，DTO `authorization_source` 声明推导路径；授权是整 dataset scope，partial prefix 拒绝。无 Worker 时返回 `dependency_unavailable` / `TABLE_WORKER_PREVIEW_ALERT`，`rows`、`total_rows`、`deletes_applied` 为 `null`。真实 LANCE `f247358f2a` PASS 是 dependency-negative；真实 Worker 正向未运行。

## 第26轮 S3Tables catalog 正向与 403 修正

- read18 最新全 200，包含 `/iam/principals`；productionMutations=0。
- `table-catalog92fe388482` 是 S3Tables/Iceberg 正向证据：owned 独立 4.48 实例、合法 TableBucket 布局、ordinary S3 读写、真实 RegisterTable、table-details native 和 table-preview 均通过。
- 初次 `83c1c3569c` preview 403 的原因是旧 fixture `table/metadata/v1.json` 只有 3 段，不符合 pinned `weed/s3api/bucket_paths.go` 52–85 的 `namespace/table/data|metadata/...` 至少 4 段规则。
- 该 403 不表示 TableBucket 普通 S3 一律不能读，也不证明 Reader 后端 bug；Core 不允许高权限 Filer fallback，Reader 保持 ordinary S3。

## 第27轮 Objects 与 LANCE API边界

- 任务33对象删除完成；DeleteMarker=true 防误报为 `needs_review`，objects29 tests。
- LANCE `f247358f2a` PASS 后仍不声称 Worker 正向；无 Worker 的 dependency-negative 是预期能力边界。
- API预算：HTML worker/sample 总预算 20s，包含 login；catalog 相关调用各 8s。不要写成整个流程统一 20s。

## 第28轮 Auth Session / Epoch 行为

- 会话过期时，前端不继续停在只显示 `UNAUTHENTICATED` 的状态；会清理 user、CSRF 和所有权限 selection，并回到 Login。
- 旧 401 和旧 generation 的 200 响应必须丢弃；只有真正 Login 或首次 bootstrap 能推进 epoch。
- 写请求在会话过期后不自动重发，避免旧权限或旧 CSRF 被重复使用。
- 浏览器证据：本地 owned UI DB 主动过期测试 session 后，刷新显示“会话到期请重新登录”；重新登录后 Objects 成功恢复；截图 `output/auth-session-expired-final.png`。密码未记录。
- 该修复只改变本地测试 session 和前端状态处理，不代表生产账户、存储对象或远端配置被修改。Cua 像素核对确认 `output/auth-session-expired-final.png` 为中文 Login 过期提示，管理内容已清除；重新登录恢复 Objects。

第28轮状态口径：35、36、37 已完成；package verifier、最终截图和打包安全均已有证据。`codebundle 0.1.0-local` 是本机源代码与 static 可用，不是生产部署包。



## 2026-10-05 引用与容量消费者契约

引用manifest key-based entry省略bucket/connection_id时先用已校验Scope权威身份补齐，再计算并存储canonical entry hash；无bucket权威则拒绝。声明declared_hash必须基于同一完整身份，不能对缺字段原始entry计算hash再期待complete。完整枚举与业务可信引用/并发删除保护仍分开。

容量快照新未测字段保存null；旧快照raw值保留，以legacy_unknown/measurement在返回与阈值中屏蔽。无完整扫描的source、未测temporary/version、缺output_size的派生不能显示已测零值。真实超阈值加未知字段返回alerting并附unknown_fields；纯未测返回unknown。
