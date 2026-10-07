# 测试与修复记录

## 当前补充：第30轮

R02 复查遗漏已补充修复：完整正文/回执 pending、异常与不完整回执保留 key、合法普通/探针终态校验、本地管理连接 CRUD 排除。打包 CLI 必须显式指定新目标，已有/历史/源目录目标和发布竞态均 fail closed。新鲜验证：启停/打包25 passed、前端构建、1388条双语消息/50 SSR、表预览和全模拟浏览器通过。后端输入未变，复用第29轮证据；W01仍为WATCH。

详细记录：[[01 - Projects/seaweedfs-console/05 - Testing/Records/2026-10-05-30-异常回执幂等与打包CLI补充修复]]。下方旧轮次结果保留为历史，不用第29轮的计数或快照哈希冒充第30轮。

## 2026-10-04 第 1 轮

目标：建立首版工程后跑通后端 API、前端构建和文档记录。

计划检查：

- 后端：`pytest backend/tests`
- 前端：`npm run build`
- 运行：启动 FastAPI 与 Vite，确认本地 URL 可访问

结果会在每次执行后追加记录。

## 2026-10-04 第 2 轮

目标：把真实验收目标切换到 OptiPlex 现有 SeaweedFS，并记录本地单元测试与集成缺口。

验收边界：

- 目标服务：`10.34.158.137:8333` 上现有 `optiplex-seaweedfs` 容器，SeaweedFS 4.48。
- SSH：使用既有 Windows 侧 OptiPlex key；账号和key路径不写入文档。
- bucket：只允许随机专用 `swc-integration-*` 测试 bucket。
- 禁止：生产对象、生产 bucket 配置、生产生命周期/删除/迁移。
- 本地容器：仅保留为 bootstrap/参数实验，不作为集成验收目标。

已知测试事实：

- 父任务回报 system tests 27 passing，但缺少统一命令原文和完整套件上下文，暂不计入总通过数。
- 父任务回报 enhancement tests 12 passing separately；`backend/console/enhancements.py` 已出现，但未完成 API 集成和真实后端验收，13–22 仍为进行中。
- API suite 曾因 FastAPI system 不可用而不能运行；虽然当前仓库已有 `backend/tests/test_api.py` 和 `console.main.create_app`，仍需补完整命令输出。
- 文档侧实测根 `.venv`：FastAPI 0.118.2、boto3 1.43.108、Pillow 11.3.0、pytest 8.4.2；父任务回报 FastAPI/Pillow 版本不同，需统一环境。

待执行记录：

- SSH 查看 `optiplex-seaweedfs` 容器、镜像、端口映射和版本。
- 创建随机 `swc-integration-*` bucket，完成最小 PUT/HEAD/GET/List/delete 清理链路。
- 运行 `pytest backend/tests/test_api.py` 并保存完整输出。
- 分别补 core/imaging/enhancements 测试命令原文，确认 27 pass 与 12 pass 的分母。
- 前端构建和页面 smoke 证据。

## 2026-10-04 第 3 轮

目标：在 OptiPlex 真实 SeaweedFS 上跑 integration smoke，确认核心链路和增强链路。

命令：

```powershell
rtk proxy .venv/Scripts/python.exe scripts/integration_smoke.py --endpoint http://10.34.158.137:8333 --secrets-file output/server-secrets.json --writable-scope --cleanup
```

结果：

- bucket：`swc-integration-49cc70ceb8`
- manifest：`output/integration-smoke-swc-integration-49cc70ceb8.json`
- cleanup：manifest 已记录 `cleanup_requested: true`，远端删除等待 QA 确认。
- 已推进：login、scope、scan、filter、preview、diagnostics、checksum、capacity、snapshot/export。
- 失败：create preset HTTP 422 `VALIDATION_ERROR`，`mode must['fill','fit','focal']`。

修复记录：

| 编号 | 现象 | 判断 | 状态 |
| --- | --- | --- | --- |
| BUG-003 | create preset 422，mode 缺失或字段形状不符 | integration payload/API契约不一致 | 当前 `scripts/integration_smoke.py` 已可见 `mode: fit` 修复痕迹；等待真实服务器重跑 |
| BUG-004 | full pytest 疑似 37 pass + 1 fail，失败在 `test_jobs`：`fake_s3.list_calls > 5` | fake pagination 或 page size 期望未触发 | QA修复中；全套不能计通过 |

局部结果：

- `test_core + test_imaging + test_enhancements`：父任务回报 29 pass，需补命令输出。
- API core agent：父任务回报 5 pass，需补命令输出。
- full pytest：当前整体失败，不能标通过。


## 2026-10-04 第 4 轮

目标：恢复第三轮失败后的真实服务器 smoke，记录基础链路通过、closed DB 修复和独立评审修复。

结果：

- OptiPlex 真实 SeaweedFS smoke 回报 `BASIC_PASS bucket=swc-integration-d70be81c82 objects=7 duplicate_groups=2`。
- manifest：`output/integration-smoke-swc-integration-d70be81c82.json`。
- 样本对象：7 个。
- 重复分组：2 个。
- cleanup：manifest 记录 `cleanup_requested: true`。
- 上一轮 `swc-integration-49cc70ceb8` 清理由父任务/QA 回报已用 404 验证。

修复记录：

| 编号 | 现象 | 修复 | 状态 |
| --- | --- | --- | --- |
| BUG-003B | preset 修复后增强段出现 closed DB，内部 `ClosingConnection` 的 `with conn` 关闭共享连接 | 增强 `_transaction` 提交但不关闭共享连接 | 进入增强回归 |
| BUG-004 | fake pagination 期望失败 | 修复 fake pagination 测试/触发口径 | 进入 owner 回归 |
| REVIEW-01 | fencing 先 SELECT 再无条件 UPDATE | 改为 CAS + `BEGIN IMMEDIATE` | owner tests 覆盖 |
| REVIEW-03..07 | copy 403/503、multipart scope、bucket manage、manifest身份、preview read上限 | 增强 API 增加对应防护 | 进入第5轮增强回归 |

局部结果：父任务报告 owner tests `43` 项通过。该结果不等于全项目通过。

## 2026-10-04 第 5 轮

目标：记录增强 API 回归、运行材料和图片处理策略，避免把局部结果串成整体完成。

结果：

- 父任务报告最新 enhancement 回归 `19 pass`，包含 7 项增强 API 回归。
- 文档侧核对到 `backend/console/enhancement_api.py`、`backend/tests/test_enhancement_api.py`、`backend/requirements-lock.txt`、`scripts/start.ps1`、`scripts/stop.ps1`、`scripts/backup.py` 存在。
- 父任务报告根 README 已补 Windows 本地 runbook、SQLite backup/verify/restore、manifest、SQL counts 和 integrity 检查说明。
- 父任务报告图片处理策略加入 Windows 512 MiB job memory bound、15s、36MP；preview target read 上限为 32 MiB。

计数口径：

- `BASIC_PASS` 支撑基础 01–11。
- 任务 12 仍进行中。
- enhancement `19 pass` 是局部增强 API 回归，13–22 仍需真实增强 smoke。
- 当前不能宣称全项目完成。


## 2026-10-04 第 6 轮

目标：记录真实增强 smoke 和新增并发保护修复。

结果：

- `swc-integration-1def3ce762`：父任务报告 `BASIC+ENHANCEDPASS`，真实服务器 full flow 通过，cleanup requested。
- QA 回报 `swc-integration-72aad05e7b` 和 `swc-integration-925c4e1d1c` full PASS，cleanup 404 verified。
- `swc-integration-6cf929a06f`：期望 409，实际 412 `STORAGE_PRECONDITION_FAILED`；判断为保护生效、测试契约过窄。

修复记录：

| 编号 | 现象 | 修复 | 状态 |
| --- | --- | --- | --- |
| REVIEW-08 | copy TOCTOU | bounded read + conditional put；目标存在时不覆盖 | 契约接受 409/412，并回读目标未变 |
| REVIEW-09 | multipart complete race | active→completing CAS；conditional complete / If-None-Match；不支持时 fail closed；active upload key reservation | 后续 `925c4e1d1c` full PASS |

最新限制：父任务报告 latest unit `61 pass / 1 fail`，失败为 metadata copy test ETag quotes，owner fix pending。增强任务仍不标完成。

## 2026-10-04 第 7 轮

目标：记录 Windows 本地恢复、备份、前端构建和浏览器 UI 进展。

结果：

- `scripts/start.ps1` 实际 Windows launch；`scripts/stop.ps1` 修复 child ownership 后停止，父任务报告 port `18765` 不再 listen。
- backup 2 tests pass；restore 对已有目标 fail closed。
- 前端表单扩展覆盖 13–22；grid/list、keyboard focus、pinned versions、小屏布局修复；Vite `8.3.2` build success，React lock 已记录。
- 浏览器初始 UI seed from root 出现 6 个 `resource_limited`，根因是 child `-m console` import cwd；改为 repo-root absolute path 后 decode valid。
- UI 链路父任务报告 6 scan valid、preview ready、no errors，下一步需要 capture 固化证据。
- Windows decoder 512 MiB job limit；imaging tests 由 10 项扩到 15 项通过，source path 改为 absolute imaging path 修复 root cwd 问题。

## 2026-10-04 第 8 轮

目标：修复 metadata copy 回归失败，扩展真实增强 smoke，按新的 UI 方向先完成前端功能契约和浅色 dashboard 基础重做。

结果：

- 后端：`.\.venv\Scripts\python -m pytest backend/tests -q` → 65 项通过。
- 前端：`npm run build --prefix frontend` → 通过，最新输出 `index-G3iYuzt0.css` / `index-BAX6rF32.js`。
- 真实 OptiPlex smoke：`BASIC_PASS bucket=swc-integration-5c9dd071a5 objects=7 duplicate_groups=2`；`ENHANCED_PASS derived=variant_e55318561b2fa0671681c40338527076 multipart=upload_d6c6483f0e934c94818dba6334192ba2 snapshot=capacity_effbc024c4724c449c6d55e08417c41b`。

修复记录：

| 编号 | 现象 | 修复 | 回归 |
| --- | --- | --- | --- |
| BUG-008 | metadata copy 因 S3 ETag 引号规范化误判源对象改变 | `put_metadata` 改用 `_assert_head_matches_indexed_object` | 定点单测、后端65项、真实 smoke 均通过 |
| UX-008 | 缺 compare、asset tags、metadata content-type、group members、owned trash、inline snapshot | 前端补对应控件和真实 API 调用 | 前端 build 通过 |
| UI-008 | 用户要求放弃深色 teal 风格，参考 shadcn dashboard/Immich/MinIO 重做 | 读取 `DESIGN.md` 和 `docs/ui-redesign.md`，先切浅色中性 shell、SVG nav、顶部 project/scope context、折叠 evidence | 前端 build 通过；仍需浏览器截图验收 |

状态影响：01–22 主链路已有当前通过证据；23–27 仍后续；整个目标不标完成，因为 redesign 仍需浏览器验证、截图和进一步结构整理。


## 2026-10-04 第 8 轮

目标：记录用户对前端观感的否定反馈，明确前端重做参考、状态影响和保留的后端证据。

用户反馈：前端不好看，需要仿照开源优秀面板优化，必要时重做。

参考和边界：

- shadcn/ui `dashboard-01`：主参考，采用浅色、紧凑侧栏、卡片、表格和清晰操作区的信息结构。
- Immich：图片浏览体验参考，只作视觉/交互启发；不复制 AGPL 源码、样式、品牌或布局实现。
- MinIO Console：父任务检查官方仓库时遇到 404，已丢弃为本轮参考。

保留证据：

- 后端测试：父任务报告 latest 65 pass。
- 真实服务器：`swc-integration-5c9dd071a5` manifest 存在，父任务报告 `BASIC_PASS`/`ENHANCED_PASS`。
- 前端旧UI：browser rescan 6 valid、preview 6 ready、snapshot count 1、no errors。
- 新增/补强 API：asset tags、asset detail/visible lineage、group members、metadata editor、CORS strict origin/url reject、approved endpoint OPTIONS。

状态影响：

- 08、09、12 重新改为进行中。
- 13–22 后端证据保留，但 UI/final review 前保持进行中。
- 当前不宣称全项目完成。

## 2026-10-04 第 9 轮

目标：落实用户“前端不好看，参考优秀开源面板优化/重做”的反馈，对前端进行浅色 neutral dashboard 重做并补齐浏览器证据。

结果：

- 前端：`npm run build --prefix frontend` → 通过，最新产物 `index-DFFWMYvJ.css` / `index-DwpP5mTJ.js`。
- 浏览器：使用 `output/ui-fixture.json` 的真实 SeaweedFS fixture 登录 `http://127.0.0.1:18765`，资产页显示 6 张真实样本图。
- 交互：grid/list 切换、两图选择 compare、筛选变更清空选择、详情 drawer、操作页 multipart tab、桶配置 tab 均通过 smoke。
- 已登录会话：`playwright-cli console error` → 0 errors / 0 warnings。未登录首次 `/auth/me` 401 属于预期鉴权探测，不计入已登录 smoke。
- 截图：`output/playwright/assets-redesign-1280.png`、`output/playwright/assets-redesign-595.png`。
- 追加验证：Assets 选中对象后点击“生成派生”可带入 Presets 批量派生对象列表；创建本地 preset 后，多 preset 勾选、预计任务数、派生健康分组、补建入口可见。
- 追加截图：`output/playwright/presets-batch-health.png`。

修复记录：

| 编号 | 现象 | 修复 | 回归 |
| --- | --- | --- | --- |
| UI-009 | 旧深色 teal 风格不符合用户要求 | 改为浅色 neutral shell、220px 侧栏、SVG nav、bucket/prefix 上下文、图片墙优先布局 | 1280/595 截图已保存 |
| UX-009 | 筛选过于平铺且缺后端支持的 height/ratio/size/time/presence | 改为主筛选 + 高级筛选 details，参数与 `FILTER_NAMES` 对齐 | 浏览器筛选 smoke 通过 |
| UX-010 | 任务/容量结果只有 raw JSON | 增加结构化摘要表/状态卡，raw evidence 默认折叠 | 资产页截图和 snapshot 可见 |
| SAFETY-009 | 桶配置保存不能自动确认并发安全 | 保存/回滚需读取 hash 且手动勾选 `exclusive_writer_ack`，默认 disabled | 操作页桶配置 tab smoke 通过 |
| UX-011 | 14/21 需要批量派生和健康检查入口 | Presets 增加 object_ids/snapshot_id、多 preset 勾选、预计任务数、batch detail 入口和健康分组/补建按钮 | 前端 build 通过；Presets browser smoke 通过 |

状态影响：08、09 的前端主体验已有当前 capture 证据；12 和 13–22 仍需 final review 与逐页验收后才能宣称完成。当前不宣称全项目完成。

## 2026-10-04 第 10 轮

目标：补齐父任务新增的批量复制、指定版本下载契约，并把主界面切到设计方提供的结构化结果组件。

结果：

- 前端：`npm run build --prefix frontend` → 通过，最新产物 `index-ChXljViH.css` / `index-DGqrFlgm.js`。
- Playwright CLI smoke：使用 `output/ui-fixture.json` 登录 `http://127.0.0.1:18765`，资产页真实 6 张样本图可见。
- 资产页：勾选 `gallery/sample-06.jpg` 后，“批量复制/生成派生”按钮启用；切回资产页后选择清空，stale-selection guard 生效。
- Operations：资产页选中对象流入“复制/移动”tab 的批量复制表单；`object_id`、`target_scope_id`、`target_key` 预览表可见；填写 `gallery/copy-smoke-no-submit.jpg` 后“提交批量复制”启用。本轮未点击提交，避免生产式写入。
- 详情 drawer：版本接口 shape 修正为兼容 `{versions}`；`gallery/sample-06.jpg` 显示 `version_id=null` 行，下载链接为 `/download?version_id=null`，DeleteMarker 下载保持禁用逻辑。
- 已登录会话：`playwright-cli console error` → 0 errors / 0 warnings；未登录首次 `/auth/me` 401 是预期鉴权探测。
- 收尾：`scripts/stop.ps1` 停止本地 API/worker；`Get-NetTCPConnection -LocalPort 18765 -State Listen` 无监听。

修复记录：

| 编号 | 现象 | 修复 | 回归 |
| --- | --- | --- | --- |
| UX-012 | 主界面仍使用本地临时 `StructuredResult`，设计方 `ResultSummary` 未接入 | `main.tsx` 导入 `components/ResultSummary`，ReportBox/ResultPane/派生健康复用共享结构化组件 | 前端 build 通过 |
| UX-013 | 16 缺批量 copy 操作入口 | Assets 选中对象提升到 App 层，Operations 增加 copy batch 表单、幂等 key、映射预览和 batch detail 读取入口 | Playwright CLI smoke 验证预填/预览/启用状态 |
| BUG-010 | 17 详情 drawer 版本列表读 `{items}`，但后端返回 `{versions}` | 版本读取兼容 `versions/items`，null 版本下载链接按字面量 `version_id=null` 构造 | Playwright CLI smoke 显示版本行和下载链接 |

状态影响：16/17 的前端入口已有当前浏览器证据；14/21 的批量派生/健康入口仍需最终 batch/checker 后端联调后才能标完成；当前不宣称全项目完成。

## 2026-10-04 第 11 轮

目标：完成 12 和 13–22 的最终收口证据，避免继续把已存在接口停留在“待联调”状态。

结果：

- 后端全量测试：`.\.venv\Scripts\python -m pytest backend/tests -q` → 98 passed。
- 前端构建：`npm run build --prefix frontend` → 通过，产物 `index-ChXljViH.css` / `index-DGqrFlgm.js`。
- 真实 OptiPlex SeaweedFS 4.48 smoke：
  - 命令：`.\.venv\Scripts\python scripts/integration_smoke.py --endpoint http://10.34.158.137:8333 --secrets-file output/server-secrets.json --secret-ref server-test --bucket-prefix swc-integration --writable-scope --cleanup`
  - 输出：`BASIC_PASS bucket=swc-integration-cbfa93c432 objects=7 duplicate_groups=2`
  - 输出：`ENHANCED_PASS derived=variant_7ecbbc901378e5f8987b4f261460b7ba multipart=upload_ebd83ac9b4fa4018b6fdfe43f5969ee7 snapshot=capacity_dd194c706f304386beed571dc7de565e`
  - 输出：`PASS bucket=swc-integration-cbfa93c432`
  - manifest：`output/integration-smoke-swc-integration-cbfa93c432.json`
- 本地 UI fixture final review：
  - 登录 `http://127.0.0.1:18765` 后资产页 6 张真实样本图可见。
  - Presets 页规格、单派生、批量派生、多 preset、健康 unknown、补建、分组/成员入口可见。
  - Operations 页 multipart、copy batch、版本/标签/metadata/retention、桶配置手动确认、Manifest、审计/缓存/容量、owned trash 入口可见。
  - 在本地 fixture 上点击审计读取、缓存回收、容量趋势、容量采样、阈值检查；最终结构化结果显示 `status 正常`、`alerts 0 项`、`sample_complete 是`。
  - 已登录后 console error 仅剩登录前 `/auth/me` 401 预期鉴权探测。
- 本地打包：`.\.venv\Scripts\python scripts/package.py` → `output\releases\seaweedfs-console-0.1.0-local.zip`，61 files，`credentials_included=false`，sha256 写入旁路 `.zip.sha256` 文件。
- 收尾：Playwright 会话关闭，`scripts/stop.ps1` 停止本地 API/worker，18765 端口关闭。

修复记录：

| 编号 | 现象 | 修复/验证 | 回归 |
| --- | --- | --- | --- |
| VERIFY-011 | 14/21 文档仍写 batch/checker 待最终联调 | 后端路由已存在，真实 smoke 覆盖 derived batch、health missing/corrupt/outdated/restore 和补建相关契约 | OptiPlex `ENHANCED_PASS` |
| VERIFY-012 | 12 缺最终本地打包和 runbook 收口证据 | README 已含 Windows runbook；`scripts/package.py` 产出本地 zip 且声明不含凭据 | package 命令通过 |
| VERIFY-013 | 13–22 仍缺逐页 UI final review | Playwright CLI 逐页检查 Presets 与 Operations 增强入口，并实际点击审计/缓存/容量低风险按钮 | 已登录 console 无新增错误 |

状态影响：本轮范围 01–22 已有当前通过证据；23–27 仍按开发计划保留为后续专题，不在本轮实现范围。



## 2026-10-04 第 12 轮

目标：只做文档对账，不运行 API 测试、不停止 preview 进程、不改 native goal 状态。

最新事实：

- 后端：父任务回报 102 tests pass，包含 4 项 timezone/RFC3339 回归；actor/job/snapshot 改为使用 `user.id`，targeted 13 pass。
- 真实服务器 final QA：`swc-integration-a4a3db42a1` full pass，cleanup all versions 404。
- 前端子任务 latest smoke：`swc-integration-cbfa93c432` PASS claim，manifest 存在。
- 前端 build：`index-DKzq38k9.css`、`index-Dwny1V6r.js`。
- 当前 package：`output/releases/seaweedfs-console-0.1.0-local.zip`，observed SHA `5550efbf3fb5ee35b004791f587a0eccc04d5d8da3154a292616a9060b1bad00`。
- 打包安全：脚本排除 `.env`、`secrets.json`、`credentials.json`、`data/`、`output/`、`.venv/`、`node_modules/`；父任务报告 credential/private key/runtime/index data 也不进入最终包。

状态影响：01–22 表内完成状态可保留；父级 final review 仍未完成，项目整体不改 complete。23–27 保持 deferred。


## 2026-10-04 第 13 轮

目标：按用户最新方向纠正文档，不改代码、不运行服务。

事实：

- 用户要求官方 SeaweedFS OSS Admin 管理能力为主产品，图片资产工作台改为增强模块。
- 固定源码基线：SeaweedFS 4.48 commit `530be3e37337488ecc34d58441e0bc476e121c93`，本地来源 `output/reference/seaweedfs-4.48`。
- Admin 路由基线：`weed/admin/handlers/admin_handlers.go` 101–296 覆盖 Dashboard/topology、Buckets/IAM、Filer files、Volumes/EC/Collections、Worker/plugin/jobs/schedulers、MQ 等。
- Live readonly probe：`output/official-admin-read-probe.json`，19 个 GET 200 JSON，无 global mutations，secret redacted。
- 新增开发任务：28–37，官方管理核心当前 0/10。

状态影响：01–22 完成证据保留；23–27 仍后续；官方管理核心 28–37 未开始，下一步实现必须先做 management connection 与 fixed 4.48 Admin adapter。


## 2026-10-04 第 14 轮

目标：记录官方 OSS Admin 管理核心进入实现后的测试、修复、失败和状态影响，不把局部只读通过写成完整完成。

事实：

- 后端管理实现已出现/推进：`management.py`、`ops`、`resources`、`native`、`objects`。
- 前端已出现/推进：management 主导航、管理 DTO 和相关页面入口。
- 真实只读联调：`scripts/integration_management.py` → 15 routes 全部 200，输出 `output/management-integration-read.json`。
- 本轮 live 管理联调记录为 `0 production writes`。
- 新增管理相关测试约 50 项；最终总数等待父任务统一回报，本文不臆测。
- isolated Admin create 两次返回 HTTP 500；对应测试 bucket 清理后 404。该失败仍在 QA 排查。

修复记录：

| 编号 | 现象 | 修复/验证 | 回归/限制 |
| --- | --- | --- | --- |
| MGMT-001 | default 权限缺管理 flag | 补充默认权限与显式管理权限判断 | 权限通过不等于写操作授权 |
| MGMT-002 | `AdminClient` public DTO 取 secret 抛 `KeyError` | public DTO 不读取 secret 明文 | secret 只留后端 registry |
| MGMT-003 | 写后 verify 未比对真实目标值 | verify 改为读取并比较目标值 | global prod write 未授权测试 |
| MGMT-004 | key journal/receipt 可能泄露 secret | journal 脱敏；secret 只在 create receipt 一次性返回 | 列表、审计、数据库不能保存明文secret |
| MGMT-005 | native upload/rename 契约不清 | 上传/rename 后做 SHA256 readback；`mv.from` 记录 rename 来源 | 后台不保证原子 no-overwrite，UI/文档需如实显示 |
| MGMT-006 | live S3 scope、cursor、download null、index select 边界 | 修复scope绑定和null/分页/index语义 | 仍需UI逐页显示 unknown/empty 区别 |
| MGMT-007 | isolated Admin create 失败 | 两次 HTTP 500 已记录，bucket 404 cleanup成功 | 阻塞31/36/37完成，不能用fake写保护替代真实验证 |

状态影响：28–34、36–37 改为进行中；35 仍未开始；官方管理核心完成数仍为 0/10。01–22 共享/图片完成证据保留。write global mock != prod verified。


## 2026-10-04 第 15 轮

目标：校正当前优先级与测试白名单，并记录管理读联调、S3对象链路成功和仍在修复的失败点。

事实：

- 当前优先级改为：28–34 基础管理优先，35 服务依赖模块，36/37 与真实服务联调、UI收口、安全打包同步验收。01–22 只作为已完成历史和复用基础保留。
- real reads 继续推进；S3 pagination、select、download 已成功。
- isolated Admin create 的失败方向已从“Admin 500”定位为基本 create 改走 S3 confirmed 路径。
- 后端测试已有 153 项通过记录；最终总数等父任务 collect/全量回报。
- 真实写入白名单从 `swc-integration-*` 扩展为 `swc-integration-*` 与 `swc-management-*` 随机测试 bucket；仍不得写生产 bucket、生产对象或服务全局配置。

仍在修复：

| 编号 | 现象 | 当前事实 | 状态影响 |
| --- | --- | --- | --- |
| MGMT-008 | quota 实际写入结果需要标注 review | `needs_review` 字段修复进行中 | 31 Bucket 管理不能完成 |
| MGMT-009 | lifecycle read 被白名单挡住 | 403 修复进行中 | 31/36 不能完成 |
| MGMT-010 | Filer list 空 | `FullPath` / `Mode` 误过滤全部 entry，正在修 | 33 Filer 文件不能完成 |

状态影响（当时）：官方管理核心当时仍为 0/10 已完成；28–34、36–37 继续进行中，35 当时未开始。Round17 已更新为 1/10，28 已完成，35 进行中；不能把153项后端测试或S3对象链路成功写成完整管理台完成。


## 2026-10-04 第 16 轮

目标：同步参考资料正文和技术设计，消除 image-first 与官方运维后置的旧口径。

事实：

- 调研报告路线比较改为：固定 4.48 Admin JSON/HTTP adapter + 自有 UI/API 是主路线；fork 官方 Admin UI 只适合上游明确缺口，不作为本项目主路线。
- 可行性分析改为：官方 OSS Admin 管理增强是主产品，图片工作台是增强模块；Volume/EC/Worker/MQ 等官方 OSS 基础管理能力不后置到 P2。
- Enterprise 建议改为商业能力边界，不把 OSS 管理核心推给商业版评估。
- 系统设计和详细技术设计补充 `management_objects` 实时 source/schema、scope/cursor、allowed endpoint URL、S3 binding、Admin cookie/CSRF 后端会话和写后精确读回。
- 158 case collect 仍待最终确认，未写成已通过。

状态影响（当时）：官方管理核心当时仍为 0/10 已完成；28–34、36–37 进行中，35 当时未开始。Round17 已更新为 1/10，28 已完成，35 进行中。


## 2026-10-04 第 17 轮

目标：记录真实 management write full PASS、160case、28完成、35进入进行中，以及最新独立 review 待修项。

事实：

- 真实 management write full PASS：bucket `swc-management-d28a638ebc`。
- 证据：`output/management-write-swc-management-d28a638ebc.json`。
- 覆盖 CRUD、quota 1GiB、lifecycle、policy、owner、versioning、非空删除拒绝、空桶删除 confirmed、HEAD404。
- journal 7 条，无 secret 明文。
- `output/management-integration-read.json` 最新 read15 + S3 pagination/select/download + volume 两页 PASS。
- lifecycle wire 修复为 Go 上游接受的 flat/lower `{rules}` DTO；此前 nested `{lifecycle}` 会导致上游成功但空规则。
- Admin 基础 create 500 改为标准 S3 create，不再记录为未经核实部署 bug。
- 后端 full 159 pass 后新增 core allowlist case，当前为 160case；父任务仍会再跑最终验证。
- MQ/S3Tables official API + UI 已实现 mock/source verified，35 不能继续标未开始。

仍待修复：

| 编号 | 现象 | 状态影响 |
| --- | --- | --- |
| MGMT-011 | native endpoint registry binding，独立 review HIGH | 29/36/37不能完成 |
| MGMT-012 | maintenance false confirmed，独立 review HIGH | 30/34/36/37不能完成 |
| MGMT-013 | 全局 writes 边界未测 | 31/36不能完整完成 |

状态影响：官方管理核心改为 1/10 已完成。28 已完成；29–37 保持进行中；35 从未开始改为进行中。不能因 write full PASS 宣称整个官方管理台完成。


## 2026-10-04 第 18 轮

记录文件：`2026-10-04-18-readonly16-review关闭与gRPC接入`。

目标：记录 175 tests PASS、readonly16、review关闭、IAM/文件/maintenance修复进展和 gRPC/Mount 接入边界。

事实：

- 父任务回报 175 tests PASS，使用 override addopts summary 口径。
- readonly management integration 16 routes 真实 PASS，包含 services health；Master、Filer、Volume、S3 四类 configured instances healthy，source version / leader DTO 进一步补齐。
- 独立 review 复核两个 finding 已关闭，当前无 HIGH/MEDIUM；不受未提供 LSP 工具卡审批阻塞。
- 文件：delete-preview 完整树、partial preview 拒绝、literal `?/#/%` 路径上传读取正在收口。
- maintenance：missing config 返回 422，current config route 正在收尾。
- IAM：policies wire 为 `{policies}`，empty clear、group status `{enabled: bool}`、`is_static` unknown 已修；SDK errors 不泄漏。
- object-lock：object-lock not configured 与 unsupported 分开。
- gRPC/protobuf：已安装并锁定 `grpcio==1.84.0`、`protobuf==7.36.2`、`grpcio-tools==1.84.0`、dev `setuptools==84.0.0`；readonly vendor proto/stubs、source license 和 `scripts/generate_grpc.py` 已加入。
- Mount API：已有真实 RPC configuredFiler0；该值只代表当前 Filer。Server SSH 31888 → private Filer 18888 loop 已作为验证链路记录。
- 新增 `docs/management-api.md` 为实际 API reference；进度仍以开发计划为唯一台账。

状态影响：官方管理核心仍为 1/10 已完成。28 已完成；29–37 保持进行中。29/30 有更多真实只读验收证据，35 的 Mount gRPC 进入实现但不能完成，37 的 review 风险从历史 HIGH 项更新为无 HIGH/MEDIUM。


## 2026-10-04 第 19 轮

记录文件：`2026-10-04-19-readonly17-MountAPI-advancedBucket-UI595`。

目标：记录 readonly17、Mount API 真实 RPC、advanced bucket full PASS、UI595 修复、DataPreview decoder 边界和依赖锁定。

- readonly17 PASS；Mount API 真实 RPC configuredFiler0 只代表当前 Filer。
- 201 tests passed 是轮次历史证据；当前最终总数等待父任务 native + decoder final 回报。
- advanced bucket full PASS：`output/management-write-swc-management-527c2cf3fc.json`；basic bucket `swc-management-527c2cf3fc`，advanced bucket `swc-management-5ea77bb35a`，cleanup 两个 HEAD404，journal 9 无 secret。
- SDK advanced compound partial 返回 needs_review，不自动 cleanup/retry。
- UI595 修复后口径：docWidth 580 / viewport 595 / sidebar 60；最终截图仍未回报。
- 新依赖锁定：`fastavro==1.12.2`、`pyarrow==25.0.1`；生成开发工具不属于部署必需。
- DataPreview 不是 logical query，delete files 不适用，snapshot ID / int64 十进制 string 保真；catalogMock -> 真 S3 fixture QA 进行中。

状态影响：官方管理核心仍按唯一计划记录。28 已完成；29–37 继续进行中。29/30/31 证据显著增强，但完成状态等待最终 UI/总验收；35 的 Mount/Table/DataPreview 覆盖增加但不能写成 full live 完成。


## 2026-10-04 第 20 轮

记录文件：`2026-10-04-20-tablepreview-realS3-decoder-native-boundary`。

目标：记录 table preview 真实 S3 PASS、GetTable not_configured 边界、decoder/native 限额和 UI DataPreview 边界。

- table preview 真实 S3 PASS：`output/table-preview-swc-management-tablepreview-b4d0a95b4d.json`；4 files，rows=2，GET/HEAD bytes + ETag captured，cleanup HEAD404。
- `catalog_stub:true` 明确为 fixture_catalog_contract；live catalog 没有正向通过证明。
- 早期真实 table-details GetTable 返回 `S3_TABLES_NOT_CONFIGURED`；后续定位为 management 连接缺 `endpoints.s3`，不是服务器缺服务证明；不创建 global table。
- 早期 `2ccf713e34` 失败为 `FORBIDDEN_ORIGIN` 先判，期望已修。
- `fastavro==1.12.2`、`pyarrow==25.0.1` locked + installed，pip check clean。
- decoder 18 unit real-file pass（table JSON 8 MiB、Avro/Parquet 32 MiB、child 512 MiB / 15s、result 1 MiB）；preview 6 case pass；native signed POST `S3Tables.GetTable` 26 cases。
- UI raw sample 不是整表 query；raw file 不是整表 query；delete files 未应用到 DataPreview；totalRows unknown。

状态影响：35/36 证据增强，但仍不写成 live catalog 全完成。29/30/31 等父任务逐页 UI 截图后再决定完成状态；32–37 继续进行中。201 tests passed 保留为历史记录，不当最新总数。


## 2026-10-04 第 21 轮

记录文件：`2026-10-04-21-fixreview-scopeURI-column-SSR`。

目标：记录 review 真实问题修复、scope URI 防穿越、Parquet columns DTO 修复和 UI SSR 验证。

- whole backend 231 tests PASS 是最后 scope URI / column fix 之前的历史总数，不写作最终总数。
- scope URI 防穿越后 6 table preview 窄测 PASS：s3 literal / encoded dot segments、`/buckets`、decoded NUL、double decode。
- review HIGH 已修，仍待独立复核。
- Parquet columns DTO object -> `[object Object]` 已修为 `column.name`。
- `scripts/verify_table_preview_ui.cjs` 基于现有 Vite SSR PASS；早期 TypeScript `transpileModule` 和 Windows ESM `ERR_UNSUPPORTED_ESM_URL_SCHEME` 失败保留记录，改用 `pathToFileURL` 后 PASS。
- 状态影响：官方管理核心完成数校准为3/10；29、30 改已完成；31–37 继续进行中；37 不因局部修复自动完成。


## 2026-10-04 第 22 轮

记录文件：`2026-10-04-22-final-round-facts-231-package-ui`。

目标：记录本轮最新完整回归、review复核、UI显示修复、打包隐私扫描和31完成校准。

- 后端完整 231 passed，1 个 anyio deprecation warning；这是第22轮历史证据。
- Frontend build 产物：`index-63Efx6nX.css` / `index-C73KUT2H.js`；这是第22轮历史证据。
- review 两个 table finding 复核 APPROVE，无 HIGH/MEDIUM。
- Vite SSR columns object / int64 / health version object PASS；Tags 0 -> 未知，服务版本 object -> `.value`。
- Filer 根 ShouldFalse；Mount readonly 真实 0 configuredFiler。
- 基础 + advanced 两桶 PASS，cleanup HEAD404；打包 93 zip entries + manifest，真实 secrets bytes 扫 zip PASS，无 output/data/env/cred。
- 状态影响：31 改已完成，官方管理核心 4/10；32–37 保持进行中，goal 仍 active。


## 2026-10-04 第 23 轮

记录文件：`2026-10-04-23-iam-worker-object-delete`。

目标：记录 IAM 全 CRUD、Worker dispatch/run/execute 契约、对象版本删除真实脚本和状态校准。

- IAM：AdminClient + httpx.MockTransport + durable ledger passed；404 before create / after delete handling 修复；service account secret once、empty description/expiration verify；resources 18 tests。
- Worker：plugin.enabled + registry gate；detect requestId、execute jobID、run jobtype/count/official expired:true；ignored/failed/unknown/replay 覆盖，native 32 pass，独立 review APPROVE。
- Worker review 修复：旧 run 逻辑会把 before=after 的旧同类型成功 job 误判为 confirmed；现只认可 response-linked jobID，或 before/after 新增的同类型 accepted/success jobID。
- Objects：`version-info` / `delete-version` 路径已确认；要求 object.manage、固定 Idempotency-Key；对象测试 14 pass。
- 真实脚本：`scripts/integration_object_delete.py`，bucket `b00f18f764`，oldVersion404/current unchanged/replay same intent/WrongETag409/Readonly403/null501/LegalHoldON409/cleanup404，journal 无 secret；null501 为旧行为证据，后续不存在 null version 已改为正确 404。
- 历史失败：`6b05fec0d6` hold404；ObjectLockConfigurationNotFoundError / NoRetentionConfiguration 只视为无 retention，key missing 不吞掉；修复后 mandatory hold PASS。33 仍缺 null/unversioned conditional delete。
- 状态影响：32、34 改已完成，33 保持进行中，官方管理核心 6/10。mock/local 验证不等于 production writes。


## 2026-10-04 第 24 轮

记录文件：`2026-10-04-24-conditional-delete-contract-s3tables-config`。

目标：记录条件删除 capability probe、package verifier 和 S3Tables 配置遗漏调查。

- 新增 `POST /api/v1/management/{id}/objects/check-conditional-delete`，要求 `{scope_id}`、`Idempotency-Key`、`object.manage`、writable scope 和关联 S3 connection。
- capability_status 可为 `supported` / `unsupported` / `unknown` / `needs_review`；status/journal_status 可为 `confirmed` / `needs_review`。
- supported 条件严格为 proof 24h、非 future、same scope、prefix、endpoint/TLS、server header、wrong If-Match 412 + GET 相同 + cleanup HEAD404。
- 新增只读 `GET /api/v1/management/{id}/objects/conditional-delete-probes`，只返回 SafeFields，并在操作总览汇总 probe 记录。
- 条件删除第一次真实运行 `4f6be8b674` 失败，原因是 QA 脚本把未版本化 VersionId `None` 预设成字符串 `null`；修脚本按实际 HEAD 身份后，`33e5f48d60` PASS：未版本化、暂停、null delete、replay、wrong-etag 均确认通过，bucket cleanup404。
- 强版本删除最新 `398a143767` PASS：oldVersion404/currentbytes unchanged/HoldON409，两个 bucket cleanup404；不存在 null version 现在正确 404。
- conditions 当前 19 tests，objects 当前 28 tests；后端完整 282 tests exit 0，pytest `-q` 抑制总数但 collect-only 核实 282，1 个 anyio deprecation warning；frontend build `index-B2ksGbYm.js`，SSR 真实 tableColumns/int64/serviceversion PASS，`pip check` clean。
- `scripts/verify_package.py` 新增，用于 manifest、excluded paths、真实 credential bytes、zip SHA；最终包 hash 仍待父任务重打包后提供，不写入 repo README/docs。
- S3Tables not_configured 原因更新为 management 缺 `endpoints.s3`，不是服务器缺服务；用户已允许临时独立 SeaweedFS 4.48 测试实例。第26轮已补 owned-instance catalog 正向 PASS，本条保留为当时待证据状态。


## 2026-10-04 第 25 轮

记录文件：`2026-10-04-25-lance-worker-preview-scope`。

目标：记录官方 Lance/Plugin worker preview 纠错与任务35当前边界。

- 纠错：官方 SeaweedFS 4.48 `weed/admin/dash/iceberg_data_preview.go` 106–109 / 485–530 已有 `Plugin.RequestObjectPreview` 样本预览，不能再写成“Lance 只有 catalog / 没有 data reader”。
- 当前实现方向：项目正在补 scope 授权 + 固定 Admin HTML 数据页转换为自有 JSON 的有界 `worker_sample`；不执行或输出原 HTML，不做跳转。
- 配置边界：worker 缺失时显示 `dependency_unavailable` / `unknown`，不能 fake 0，也不能宣称已有 worker live positive。
- 模块归属：`table_worker_preview.py` 由 enhancements 拥有，父任务接入 table preview；后端不新增依赖。
- IAM/principals：新增官方只读 `/iam/principals`，来源 `/api/principals`；首次 readonly18 因真实 Go 返回 `principals:null` 触发 502，mock `[]` 未覆盖，已按 pinned `principal_suggestions.go` 将 nil `[]string` 转成空建议。
- Principals coverage 为 `suggestions_only_not_identity_inventory`，Role listing best-effort；`items:[] / total:0` 不代表整套身份库为空。
- Worker/maintenance：JobType 增加 runs（`/api/plugin/job-types/{type}/runs`），新增 `GET maintenance/jobs/{job_id}` 聚合 job/detail；resources22 测试通过，readonly18 重测待最终回报。
- 状态影响：35 保持进行中；等待 helper tests、review 与真实 worker / catalog 证据。


## 2026-10-04 第 26 轮

记录文件：`2026-10-04-26-read18-catalog-403-correction`。

目标：记录 read18 全200、S3Tables/Iceberg catalog 正向 PASS、403 初诊修正和 cleanup 证据。

- read18 全 200，包含 `/iam/principals`；证据 `output/management-integration-read.json`，productionMutations=0。
- 原服务器 conditional `33e5f48d60` 与 strong `398a143767` 继续 full PASS，清理 HEAD404。
- `table-catalog92fe388482` full real PASS：owned 独立 SeaweedFS 4.48 `swc-test-catalog-eabfac72`，合法 `qa/events/...` scope 布局，ordinary S3 PUT/HEAD/GET 4 files，真实 RegisterTable、table-details `native_s3tables_get_table`、table-preview rows2/snapshot1/4，无 stub，`S3_upload_error:null`。
- cleanup：S3Tables REST cleanup OK，catalog 不再 listed，bucket HEAD404；`eabfac72-cleanup.json` 记录 container_absent、remote_dir_absent、SSH pid stopped。
- 403 修正：初次 `83c1c3569c` preview403 是 fixture 三段路径 `table/metadata/v1.json` 不合 pinned `weed/s3api/bucket_paths.go` 52–85 的 TableBucket 布局要求；不是普通 S3 一律不能读，也不是已证实 Reader 后端 bug。
- Core 禁止高权限 Filer fallback；Reader 保持普通 S3。35 仍等 Lance helper/UI最后审、full suite/package hash/截图，不标完成。


## 2026-10-04 第 27 轮

记录文件：`2026-10-04-27-objects33-complete-lance-negative`。

目标：记录任务33完成、DeleteMarker防误报、LANCE dependency-negative 和35剩余边界。

- 任务33：独立 review 明确完成，无 HIGH/MEDIUM 阻塞；强版本、条件删除、前端 fence 均验收。
- DeleteMarker=true 时不宣称永久删除，进入 `needs_review`；objects29 tests。
- LANCE：默认位置授权据 `lance/storage.go` 103–108 / `handlers_table.go` 529–531；catalog warehouse/metadata 空值原样保留，DTO `authorization_source` 声明推导路径，整 dataset scope 授权，partial prefix 拒绝。
- 真实 LANCE `f247358f2a` PASS，scope 仍为 `qa/events/`；无 Worker 返回 `dependency_unavailable` / `TABLE_WORKER_PREVIEW_ALERT`，rows/total_rows/deletes_applied=null。
- `7ff66905b4` 409 失败保留：强制 warehouse 不适配真实 GetTable 空字段；后修为不假造字段。
- helper 安全 4 tests passed：防注入、PID复用、错误inspection、ACL失败不漏资源。
- 35 仍进行中：ICEBERG real positive + LANCE dependency-negative + local positive mock 已有，真实 Worker 正向未运行、生产 global writes 未验证，独立 review 最后 delta 待复核。

## 2026-10-04 第 28 轮

目标：记录最终包前的 311 后端回归、认证会话过期 UX 修复、任务35/36状态校准，以及37仍未完成的包验证边界。

结果：

- 后端完整回归：311 passed，1 个既有 anyio deprecation warning，57.69s。
- 前端：latest build 通过；`verify_auth_ui.cjs` 的 epoch 决策 PASS；table preview SSR PASS；`pip check` clean。
- 认证 UX：旧行为是 session 过期后只显示 `UNAUTHENTICATED`，不会回 Login。修复后清理 user、CSRF 和全部权限 selection；旧 401 与旧 generation 的 200 响应被丢弃；只有真正 Login / 首次 bootstrap 推进 epoch；写请求不自动重发。
- 浏览器证据：本地 owned UI DB 主动让最新测试 session 过期，刷新后自动进入 Login 并显示“会话到期请重新登录”；重新登录后 Objects 页面恢复成功。截图 `output/auth-session-expired-final.png`。密码未记录；本轮只修改本地测试 session，不改账户或存储。
- 包验证：104 payload files + manifest，manifest hashes、archive、excluded private paths 和真实 credential bytes 扫描均 passed；repo README/API 不写包 hash。
- UI最终证据：`output/management-dashboard-final-1280.png` 已重捕，docWidth 1265；最新 assets 为 `index-GlTj8Kzr.js`，stylesheet `index-63Efx6nX.css` 未变；Cua console 无 JS 异常。`auth-session-expired-final.png` 像素核对为中文 Login 过期提示，管理内容已清除，re-login 恢复 Objects 已验证。595 新 override 对已有 tab 未生效，不新增本轮 595 pass，沿用 CSS 不变的 Round22 历史 595 doc580 证据。
- SQLite恢复验证：`backup.py backup output/ui-data/console.db` 到 `output/backups/console-final-311.db`，restore 到 `output/restore-final-311/console.db`；integrity/FK OK，所有表计数相同（含 management_connections / operations / condition_probes / assets=6），备份与恢复库 SHA 相同。该验证只覆盖控制台 SQLite，不冒充 S3 对象灾备，原数据未覆盖。
- 状态影响：35、36、37 改已完成，官方管理核心 10/10。`codebundle 0.1.0-local` 只表示本机源代码与 static 可用，不是生产部署。
- 边界：ICEBERG/S3Tables 有真实正向；LANCE 有 local positive mock 与真实 dependency-negative，无真实 Worker 正向；生产 global IAM / Maintenance / MQ / Table writes 未实测。



## 第29轮：全面审查修复与当前本地回归（2026-10-05）

| ID | 等级 | 本次修复 | 回归范围 | 状态 |
| --- | --- | --- | --- | --- |
| R01 | HIGH | S3 secret 必须具有非空 allowed_endpoint_url；创建与每次使用均核对批准端点和可选连接绑定。 | 批准缺失/空/null、匹配、不匹配、撤销批准与既有连接回归 | 已修复，本地验证通过 |
| R02 | HIGH | 远端管理写必须携带固定幂等 key；UI 固定当前意图并阻止 pending 重交，未知结果保留 key。 | 无 key 不派发；同 key replay；hash 冲突；needs_review 与中断重交；模拟 UI 双击 | 已修复，本地验证通过 |
| R03 | HIGH | 单对象复制在目标副作用前核对被选中的索引身份，并保留读取期间的条件保护。 | 索引漂移、版本身份与目标零写入回归 | 已修复，本地验证通过 |
| R04 | HIGH | Assets Scope/query 变化失效请求 generation，清空列表、详情、任务及派生状态；旧 poll 不再提交。 | 全模拟 A→空、A→B、反序响应、旧 jobs poll 与打开 drawer | 已修复，本地验证通过 |
| R05 | HIGH | worker 成功 finalization 保存经核验的 output_size；恢复已有输出也保留实际大小。 | worker 输出字节、容量汇总及恢复路径回归 | 已修复，本地验证通过 |
| R06 | HIGH | 未测容量通过测量覆盖契约保持未知；阈值消费者不把未测零值视为正常。 | partial/no sample、未枚举版本、未知字段阈值及旧 schema 兼容 | 已修复，本地验证通过 |
| R07 | MEDIUM | 表详情/样本绑定完整当前请求身份，输入或 Scope 变化即失效，拒绝过期异步响应。 | 表/格式/连接/Scope/snapshot/file/limit 变化与反序响应 | 已修复，本地验证通过 |
| R08 | MEDIUM | 专门核查提示按 status/state 接收 needs_review，用户需先查管理历史。 | status/state 回执 SSR 及未确定写入 UI | 已修复，本地验证通过 |
| R09 | MEDIUM | 行级/条件 title、placeholder 和 unknown 文案全部进入双语目录。 | 非空 Volume/Filer/Object/LANCE/Topology 场景与目录检查 | 已修复，本地验证通过 |
| R10 | MEDIUM | 包验证扫描根目录、配置及显式外部 registry；没有足够扫描输入不宣称凭据 absent。 | 合成 root/external/output registry 泄漏与缺失输入回归 | 已修复，本地验证通过 |
| R11 | MEDIUM | 引用条目存储完整 Scope bucket/connection 身份，完整性 hash 与校验使用同一身份契约。 | 缺失 bucket、身份不匹配和 manifest 完整性回归 | 已修复，本地验证通过 |
| R12 | MEDIUM | 启动中任一步失败回收本次创建的进程及已核对的子进程；记录失败也进入清理。 | API/worker 起失败、PID 写失败、正常启停，全部模拟进程 | 已修复，本地验证通过 |
| R13 | MEDIUM | 打包 builder/verifier 同时要求英中 README 并检查包内相对链接。 | 隔离新包、缺中文 README 与相对链接回归 | 已修复，本地验证通过 |
| R14 | MEDIUM | missing-proto 测试显式模拟协议不可用；unreachable 另用隔离通道测试。 | GRPC_PROTO_UNAVAILABLE / GRPC_UNREACHABLE 分开断言；全量恢复通过 | 已修复，本地验证通过 |
| R15 | LOW | 无 Scope 时禁用报告导出并解释前置条件。 | 空 Scope 模拟 UI 回归 | 已修复，本地验证通过 |

| 检查 | 当前结果 | 原始证据 |
| --- | --- | --- |
| pip-check | PASS；exit 0 | `output/fix-review-2026-10-05/pip-check.log` |
| backend-pytest | 325 passed, 1 warning；exit 0 | `output/fix-review-2026-10-05/backend-pytest.log` |
| runtime-packaging | PASS；exit 0 | `output/fix-review-2026-10-05/runtime-packaging.log` |
| frontend-build | PASS；exit 0 | `output/fix-review-2026-10-05/frontend-build.log` |
| frontend-i18n | PASS；exit 0 | `output/fix-review-2026-10-05/frontend-i18n.log` |
| table-preview-ui | PASS；exit 0 | `output/fix-review-2026-10-05/table-preview-ui.log` |
| frontend-regressions | PASS；exit 0 | `output/fix-review-2026-10-05/frontend-regressions.log` |

启停/打包回归16 passed；双语目录1387条消息和50个SSR场景通过，另有非空Volume/Filer/Object/LANCE/Topology英文动态扫描。全模拟浏览器验证A→空/A→B、旧jobs poll和assets晚到、双在途Tables preview反序、details输入变化后旧generation失效、snapshot/file/limit/format清理、needs_review后reload复用key、双击pending门禁、严格Bucket header-only、upload query/header一致和登录失败不存密码派生fingerprint。容量等时间戳fixture已加入，原flake重复20/20通过。Tables details的动态证据为distinct-generation fence，未将其冒称双在途反序。历史发布包SHA256与原审查记录仍相同（eff33bb94842bae0f48f8c2bb52d9aaaa0b10e76bef0baad0af5ae31a918949c）。最终隔离快照包113个payload文件、manifest/hash/双语README及包内相对链接检查通过；扫描8个凭据来源，real_credentials=absent。builder自身仅报告路径排除和credential_values=not_scanned。

独立代码复核COMMENT（无阻塞或分级缺陷发现；使用源码基线、构建/测试复核，LSP/ast-grep不可用）；独立架构WATCH（代码BLOCK已关闭，W01保留）。完整记录：[[01 - Projects/seaweedfs-console/05 - Testing/Records/2026-10-05-29-全面审查修复与本地回归]]。

W01 继续为 WATCH：当前 foundation 只有 schema_migrations(version, applied_at)，其他模块以幂等 initializer 和局部兼容 schema 适配初始化。名称/checksum/result 的统一迁移审计，以及正式支持旧版本到当前版本的完整 fixture 矩阵尚未完成。本轮不新增迁移框架、不回填业务历史、不承诺任意旧库升级。R06对旧capacity_snapshots表执行保留行数据的nullable适配，旧数值不改写，以legacy_unknown/measurement在消费层保持未知。新建本地库与本轮明确覆盖的兼容fixture可以按当前测试使用；真实旧库升级应先保留SQLite backup、在副本完成初始化及integrity/FK/count对照，再确定升级范围。R06的测量覆盖兼容回归不能证明全项目迁移审计完成。

本轮仅修复与验证本地实现。全部存储/管理副作用使用 fake adapter 或全模拟 UI；没有重新执行真实 OptiPlex、LANCE Worker 正向、生产全局写入、部署、推送或覆盖历史发布包。原 2026-10-04 的任务验收、截图、311 passed 和发布包是历史证据。目录没有 Git 元数据，源码基线和变更摘要保存在 `output/fix-review-2026-10-05/`。

## 2026-10-06 / Round 31: live e2e and image workbench

- Used the built local `useful_repo/e2e` SDK against an actual HTTP Console, standalone worker, and approved OptiPlex SeaweedFS 4.48, with fresh owned buckets and an isolated database. The final desktop/narrow browser suite passed **14/14**.
- Fixed the executable worker's split `__main__` / `console.jobs` handler registry. A real `python -m console.jobs` subprocess regression reproduces the old failure and proves the registered enhancement handler executes. New live derived outputs succeeded with verified size/hash; old uncertain test jobs were preserved.
- Fixed late Scope hydration overriding explicit selection, and Scope/Project state leaks in Scans, Diagnostics, Operations, and Presets. The delayed-response script fails against the preserved original build and passes against the current build. Its Table-details timing issue was corrected in the harness; no management production UI change was needed.
- Unified the image workbench with management tokens, toolbars, cards, evidence sidebar, and responsive layouts. Batch/group preset tools use native collapsible sections. Actual 1280/595 captures show no document overflow or JavaScript page errors.
- Final checks: backend **326 passed, 1 warning**, script suite **27 passed**, build/i18n/table-preview/dynamic browser checks passed; i18n covers 1406 messages and 50 SSR views. Independent final review found no blocking issue.
- Official source/authenticated readback matrix preserves gaps in MQ lists, storage detail pages, some Filer tools, and first-class Iceberg navigation. Empty live data and absent Lance Workers remain limited evidence; full official parity, production global writes, scale, and W01 migration auditing are not claimed.
- Cleaned all six run-owned buckets (HEAD 404), stopped the fixture API/worker and owned SSH forwarding. Historical packages/data and the deployed Console were preserved. No commit, push, deployment, or new package was requested.
- Evidence: `output/e2e-2026-10-06/verification-summary.json`, `runner/summary.md`, `official-baseline.md`, before/after screenshots, and scoped cleanup logs. Full [Round 31 record](<E:/Project Code/docs/01 - Projects/seaweedfs-console/05 - Testing/Records/2026-10-06-31-真实E2E与图片工作台修复.md>); progress remains in the development plan.

## 2026-10-06 / Round 32: continuous integration and source publication

- Added `.github/workflows/ci.yml` for main pushes, pull requests, and manual dispatch. Python 3.12 and Node.js 24 run backend/frontend jobs on Ubuntu 24.04 and Windows 2025. Packaging is checked on both; Windows additionally exercises startup/shutdown script regressions.
- CI includes dependency compatibility, backend behavior, frontend build, bilingual/SSR validation, table rendering, and mocked Chrome request-order regressions. Real SeaweedFS integrations stay opt-in and no server credential is configured in hosted CI.
- Actions are pinned to verified commit SHAs, checkout does not persist authentication, token permissions are read-only, overlapping runs are cancelled, job durations are bounded, and JUnit artifacts have a seven-day retention period.
- Local workflow validation: official actionlint 1.7.12 (download checksum verified) and YAML parsing passed; current dependencies passed `pip check`, both README package links passed, and source candidates contained no credential values from the actual registry. Round 31 behavior checks remain valid for unchanged application inputs.
- Independent CI review found no actionable issue. Hosted results and publication SHA are recorded in the [Round 32 record](<E:/Project Code/docs/01 - Projects/seaweedfs-console/05 - Testing/Records/2026-10-06-32-CI与源码推送.md>) after the push. Runtime output, registries, screenshots, databases, and historical packages are excluded from publication.
- First hosted run passed both backend suites but exposed a browser startup deadline and a mock-server leak on launch failure. The runner now bounds startup/teardown, reports Chrome stderr, and closes owned resources even on startup errors. A black-box early-exit regression and a request-based S3 Tables input barrier preserve failure reporting and remove the observed input flake. Local script checks now pass 28 tests and the full mocked browser check passes with CI enabled; the first failure remains recorded.

## 2026-10-07 / Round 33: UI spacing and information hierarchy

- Unified panel padding, label/control spacing, content gaps, section dividers, and action rows. Grid panels align to their contents. Settings columns share the same form rhythm; checkboxes retain their natural width.
- Bucket inventory is full-width, followed by selected-bucket status and separate Versioning/Object Lock and lifecycle/policy sections. Creation and selected owner/quota/deletion are distinct disclosures with independent drafts; selected owner/quota inputs clear on bucket/connection changes.
- Filer browsing is full-width with compact pagination; file tools follow in grouped sections. Live Objects uses a labeled definition list for bucket, authorized root prefix, and current browsing prefix. A missing Scope stays unknown rather than implying bucket root. Added explanatory EN/Chinese text and translated route titles.
- Final checks: frontend build PASS, i18n **1432 messages / 50 SSR views**, table-preview PASS, full mocked asynchronous/request regression PASS (**183 requests**), runner failure-path regression **1 passed**, and independent source review with no blocking finding.
- New layout verifier: **20 cases / 32 screenshots**, all five routes at Chinese 1280/595/320 pixels plus English desktop; no document overflow or browser errors; **177 mocked requests, zero writes**. Actual field separation is 8px, form sibling gap at least 12px, and panel-content gap 16px. Separated bucket drafts and populated read-only guards are checked.
- Initial narrow captures found a 320px body minimum exceeding the 305px content viewport after the browser scrollbar; removed the body minimum and reran successfully. Preserved the failure in `output/ui-layout-2026-10-07/overflow-before-fix.json`. The first asynchronous run timed out because its generic field selector could borrow MQ's Namespace field while the S3 Tables panel was loading; scoped the helper to the titled panel and reran successfully.
- Evidence: `output/ui-layout-2026-10-07/after/verification.json`, before/after screenshots, and `frontend-regressions.log`; [Round 33 record](<E:/Project Code/docs/01 - Projects/seaweedfs-console/05 - Testing/Records/2026-10-07-33-UI间距与信息层次优化.md>). Checks use a mocked local service; no real SeaweedFS writes, deployment, commit, push, or package was performed. The existing Vite bundle-size advisory remains; backend, startup, packaging, and historical evidence are unchanged.
