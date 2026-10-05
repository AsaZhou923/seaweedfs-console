# SeaweedFS Console

语言：[English](README.md) | 简体中文

第三方 SeaweedFS OSS Admin 管理增强控制台，非 SeaweedFS 官方 Admin。React + TypeScript + Vite、FastAPI、SQLite WAL、独立 worker；当前已完成共享基础与图片资产增强，官方管理核心 28–37 已完成。

界面语言：默认英语。登录前和应用内都提供 English / 简体中文 选择器；存储可用时本地持久化选择。存储不可用时，当前页面仍可切换语言，下次访问从英语开始。

界面翻译维护在 `frontend/src/i18n/`。新增界面文案时同步添加英语和中文条目。对象 key、用户自定义名称、API 标识和原始 API 证据保留原值。

开发与验收依据：[开发计划](<E:/Project Code/docs/01 - Projects/seaweedfs-console/08 - Plans/seaweedfs-console 开发计划.md>)。01–22 是已完成的共享基础与图片增强；官方 OSS Admin 管理核心为新增 28–37，当前 10/10 已完成：28、29、30、31、32、33、34、35、36、37 已完成。23–27 保留为后续高级专题。进度与未测范围以计划及 [测试修复索引](docs/test-fix-log.md) 为准；当前官方管理接口参考见 [docs/management-api.md](docs/management-api.md)。

## 本地启动（当前维护的运行方式）

Windows PowerShell 7.5、Python 3.12、Node 24。安装版本由 `backend/requirements-lock.txt` 和 `frontend/package-lock.json` 锁定。API 只监听本机；生产网络暴露和服务器部署需另行配置 HTTPS。

在项目根目录创建 `secrets.json`，结构参考 `secrets.example.json`。文件只供后端读取；连接表只存 `secret_ref`。`allowed_connection_id` 可在首次创建连接后绑定其 ID。不要把真实凭据加入版本控制或提供给浏览器。

```powershell
$env:CONSOLE_ADMIN_PASSWORD = '<设置独立管理密码>'
$env:CONSOLE_SECRETS_FILE = 'E:\Project Code\my_repo\seaweedfs-console\secrets.json'
pwsh -NoProfile -File scripts/start.ps1 -Install
```

访问 `http://127.0.0.1:18765`，账号默认 `admin`。`start.ps1` 启动独立 API 和 worker，日志与 PID 在 `output/`。首次写入密码哈希后更改环境变量不会重置数据库中的账户。`CONSOLE_DEV_INSECURE_COOKIE=1` 仅供上述本机 HTTP 开发；HTTPS 使用 `0`。

```powershell
pwsh -NoProfile -File scripts/stop.ps1
```

也可分别在两个终端从 `backend/` 启动，使用相同绝对数据目录和相同配置：

```powershell
..\.venv\Scripts\python.exe -m uvicorn console.main:create_app --factory --host 127.0.0.1 --port 18765
..\.venv\Scripts\python.exe -m console.jobs
```

`.env.example` 是环境变量参考，启动脚本不会自动执行其中内容。所有 `CONSOLE_*` 路径建议使用绝对路径，避免 API 与 worker 指向不同数据库。

## 首次接入

1. 在 `secrets.json` 先注册 Admin secret：`kind: "seaweed_admin"`，`allowed_endpoint_url` 绑定 Admin UI，`allowed_endpoints` 明确列出允许访问的 Master、Filer、Volume 管理端点。S3 secret 使用 `allowed_endpoint_url` 绑定 S3 endpoint；需要 S3Tables/DataPreview 时，management connection 的 `endpoints.s3` 必须与这个已批准 S3 endpoint 一致。
2. 设置页新建 Management Connection，绑定 Admin secret，按实际环境填写 Admin、Master、Filer、Volume 和可选 S3 endpoint。Dashboard、Topology、Services、Volumes、Buckets、IAM、Workers 和 Modules 先使用 Management Connection。
3. Dashboard / Topology 验证通过后，再进入 Buckets、Objects、Files 或 Modules 页面。Objects 浏览、索引和预览可使用 readonly scope；派生/上传只要求对应输出 scope 可写；只有手动 S3 物理删除、版本删除和条件删除需要关联 S3 connection，并显式开启 writable / `object.manage`。
4. 图片资产工作台作为增强模块使用：新建逻辑项目，输入已知 bucket / 字面 prefix。没有 ListBuckets 权限仍可接入；预览、原图下载、目标写入分别显式勾选。
5. 提交扫描，在任务页查看真实处理数、失败项、暂停/取消和恢复状态。未知总数不显示百分比。worker 离线时任务保留 queued。
6. 需要精确重复候选时另提交 checksum 深扫描。ETag 不代表 SHA256，未扫描对象不代表零重复。派生写入新 key 并保留源图；没有并发引用保护协议时移动和源图自动删除保持禁用。

## 持久数据与恢复

数据目录包含 `console.db`、WAL/SHM 与私有 `previews/`。连接、资产、任务、规格、派生 manifest 和操作记录在 SQLite 中；缩略图可重建。不要只复制运行中的 `.db` 文件。

```powershell
.venv\Scripts\python.exe scripts/backup.py backup data/console.db output/backups/console-20261004.db
.venv\Scripts\python.exe scripts/backup.py verify output/backups/console-20261004.db
.venv\Scripts\python.exe scripts/backup.py restore output/backups/console-20261004.db output/restore/console.db
```

备份使用 SQLite backup API，校验 integrity/FK 并生成表计数与 SHA256 manifest。恢复目标必须不存在；验证后先停止 API/worker，再配置新的数据目录启动。凭据与环境配置需另行安全备份。此功能只备份控制台数据，S3 对象/Filer 元数据灾备属于后续任务 25。

## 管理连接配置

管理联调需要在 `secrets.json` 中使用 endpoint-bound credential。`secrets.example.json` 已包含两个形态：S3 凭据使用 `allowed_endpoint_url` 绑定 S3 endpoint，Admin 凭据使用 `kind: "seaweed_admin"`、`allowed_endpoint_url` 绑定 Admin UI，并在 `allowed_endpoints` 中列出 master/filer/volume/s3。通过 `scripts/fetch_test_credentials.py` 获取 OptiPlex 测试凭据时，会生成 `server-admin` 和测试 S3 secret；S3Tables/DataPreview 读取要求 management connection 的 `endpoints.s3` 与批准的 S3 endpoint 一致。

OptiPlex 的 Master/Filer/Volume 管理端口通过 SSH loop forwards 映射到本机回环端口后使用；不要把 Admin 密码、S3 secret 或签名 URL 写入报告或前端。真实写入只允许随机 `swc-integration-*` / `swc-management-*` 测试 bucket。

Round19 management boundary: locked `grpcio==1.84.0`, `protobuf==7.36.2`, `fastavro==1.12.2`, `pyarrow==25.0.1`; dev generation tools stay development-only. Mount API has real RPC evidence and currently reports configuredFiler0 for this Filer only, not whole-cluster zero.

## 测试

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -q
npm run build --prefix frontend
npm run test:i18n --prefix frontend
```

i18n 验证（2026-10-05）：前端构建与 50 个双语渲染检查通过。本地浏览器模拟数据覆盖 15 条页面路由、语言记忆、表单草稿、会话到期、跨标签页同步及存储受限情况；对象值和表字段名保持原值。这些检查使用模拟数据，不执行真实存储写入。Vite 仍有不阻塞构建的 JavaScript 分块大小提示。

真实集成按用户指定使用 OptiPlex SeaweedFS 4.48：`10.34.158.137:8333`。凭据通过既有 Windows OpenSSH `ssh 10.34.158.137` 获取到被忽略的 `output/server-secrets.json`，不写入报告。

```powershell
.venv\Scripts\python.exe scripts/fetch_test_credentials.py
.venv\Scripts\python.exe scripts/integration_smoke.py --endpoint http://10.34.158.137:8333 --secrets-file output/server-secrets.json --writable-scope --cleanup
```

脚本只创建本轮随机 `swc-integration-*` 或管理联调专用 `swc-management-*` bucket，测试文件与 bucket 配置都限于该 bucket；清理只作用于本轮创建范围。服务器既有 bucket、生产对象和服务配置不参与写入。生产规模 10 万/百万对象、真实业务浏览器 CORS/CDN 发布与引用删除保护不由小样本测试推定。

## 实现资料

- [S3 条件读取与版本参数](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_object.html)
- [Pillow 解码与像素限制](https://pillow.readthedocs.io/en/stable/reference/Image.html)
- [Windows 解码进程内存限制](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information)
- [SeaweedFS 使用资料](https://github.com/seaweedfs/seaweedfs/wiki/Getting-Started)

源码与测试通过不等于所有后端能力支持。版本/retention/legal hold/桶配置按实际 S3 返回分别显示支持、拒绝、不支持、未知；未接入发布/CDN 和业务引用保护时保守保留。

## 当前本地修复（2026-10-05）

全面审查R01–R15共15项已修复并通过本地验证。后端：325 passed, 1 warning；前端构建、双语非空fixture、表预览、全模拟Scope/反序/幂等及隔离启停/打包回归通过。详见[第29轮修复记录](<E:/Project Code/docs/01 - Projects/seaweedfs-console/05 - Testing/Records/2026-10-05-29-全面审查修复与本地回归.md>)与`output/fix-review-2026-10-05/`。

S3 secret必须具有非空批准`allowed_endpoint_url`，创建和使用均核对。远端管理变更必须携带固定幂等key：resource接口使用请求头，native JSON使用body，native上传使用query；同意图重交复用key，needs_review或响应丢失后先核查历史。未测容量继续显示未知。

W01迁移审计仍为WATCH：当前支持本地初始化和明确测试的兼容路径，统一name/checksum/result迁移台账及任意旧schema升级矩阵未完成。旧库先备份并在隔离副本验证。本次没有真实服务写入、部署、推送或覆盖历史发布包；下方2026-10-04结果为历史证据。

## 官方管理主线状态（2026-10-04 基线）

测试记录索引：`2026-10-04-28-final-prepackage-auth-ui-311`。当前基础版本已完成已核实的 SeaweedFS OSS 4.48 管理核心 28–37；它不是 Enterprise 能力、跨版本完整协议替代或生产全功能验证。最新管理证据包括 read18 全 200、Mount API 真实 RPC configuredFiler0（仅当前 Filer，不代表 whole cluster）、advanced bucket full PASS、S3Tables/Iceberg catalog full real PASS、LANCE dependency-negative、311 passed / 57.69s 后端完整回归、认证会话过期 UX 实测、最终 Dashboard 截图 `output/management-dashboard-final-1280.png`、认证截图 `output/auth-session-expired-final.png` 和打包隐私扫描。UI595 口径沿用 CSS 未变时的历史有效证据 docWidth 580 / viewport 595 / sidebar 60；本轮新 595 override 未生效，因此不新增 595 通过声明。28–37 已完成。

DataPreview 不是 logical query，delete files 不适用；早期 table preview 真实 S3 PASS 证据为 `output/table-preview-swc-management-tablepreview-b4d0a95b4d.json`。最新 S3Tables/Iceberg catalog 正向证据为 `table-catalog92fe388482` full real PASS：在用户批准的 owned 独立 SeaweedFS 4.48 实例 `swc-test-catalog-eabfac72` 上使用合法 `namespace/table/...` 布局，ordinary S3 PUT/HEAD/GET 4 files，真实 RegisterTable、项目 table-details `native_s3tables_get_table` 和 table-preview rows=2 通过，无 stub；cleanup 后 catalog 不再 listed、bucket HEAD404、实例 container/remote_dir/SSH pid 均清理。最新后端完整回归为 311 passed，1 个既有 anyio deprecation warning，耗时 57.69s；frontend latest build 通过，最新 JS 为 `index-GlTj8Kzr.js`，stylesheet `index-63Efx6nX.css` 未变；`verify_auth_ui.cjs` 的 epoch 决策验证通过，table preview SSR PASS，`pip check` clean；Cua console 无 JS 异常。当前包验证通过：104 payload files + manifest，manifest hashes、archive、excluded private paths 和真实 credential bytes 扫描均 passed。review 两个 table finding 复核 APPROVE，无 HIGH/MEDIUM。Buckets 最终截图 `output/management-buckets-1280-final.jpg`，图片增强回归截图 `output/management-images-preserved-1280.jpg`。

第23轮补充：IAM 任务32 和 Worker 任务34 已按本地实现/契约证据完成；生产全局 writes 未因 mock transport 通过而视作已执行。第24轮补充：条件删除第一次真实运行 `4f6be8b674` 失败是 QA 脚本把未版本化 `VersionId None` 预设为字符串 `null`；修脚本按实际 HEAD 身份后 `33e5f48d60` PASS，未版本化/暂停/null delete/replay/wrong-etag 均确认通过且 cleanup HEAD404。强版本删除最新 `398a143767` PASS：oldVersion404、current bytes unchanged、HoldON409、两个 bucket cleanup404；历史 `b00f18f764` 的 null501 仅保留为旧行为证据。33 已由独立 review 明确完成，无 HIGH/MEDIUM 阻塞。第26轮补充：初次 `83c1c3569c` preview 403 是旧 fixture `table/metadata/v1.json` 只有 3 段，不符合 pinned `weed/s3api/bucket_paths.go` 52–85 要求的 `namespace/table/data|metadata/...` 至少 4 段布局；不是 TableBucket 普通 S3 一律不能读，也不是已证实 Reader 后端 bug。 第27轮补充：33 已由独立 review 明确完成，无 HIGH/MEDIUM 阻塞；DeleteMarker=true 错误永久删除宣称已防护为 needs_review，objects29 tests。第28轮补充：35/36/37 已完成，官方管理核心 10/10；LANCE `f247358f2a` 真实 dependency-negative PASS：Worker 返回 dependency_unavailable / TABLE_WORKER_PREVIEW_ALERT，rows/total_rows/deletes_applied 为 null，另有 local positive mock，但真实 Worker 正向仍未运行；生产 global IAM / Maintenance / MQ / Table writes 未实测。`codebundle 0.1.0-local` 只表示本机源代码与 static 可用，不是生产部署；生产 global IAM / Volume / Worker / MQ / Table 写入仍未执行。


打包使用 `scripts/package.py --destination output/task-name/bundle.zip`，可用 `--root` 指定项目根目录。输出必须是新文件，不能位于打包源目录或 `output/releases`。帮助与参数错误不写文件，已有 zip 或哈希旁路文件会被拒绝；Python `build_package(root, destination)` 同样要求显式指定新目标。

包验证使用 `scripts/verify_package.py --package <zip> --registry <实际批准registry.json>`，`--registry`可重复。verifier 的默认路径仍是历史发布包，不能用它冒充本轮修复快照。builder只证明私有路径排除；verifier实际扫描凭据字节后才能返回`real_credentials=absent`，无输入是`not_scanned`，不可读取或显式缺失registry会失败。

第30轮补充修复：管理请求的 pending 门禁覆盖正文读取和回执校验；成功 HTTP 响应中的异常或不完整回执保留原 key 并提示核查历史/读回，只有受支持的 confirmed/failed 操作回执才释放 key。本地管理连接 CRUD 和只读 POST 保持原契约。当前验证见[补充修复记录](<E:/Project Code/docs/01 - Projects/seaweedfs-console/05 - Testing/Records/2026-10-05-30-异常回执幂等与打包CLI补充修复.md>)；第29轮数量与包哈希保留为历史证据。
