# SeaweedFS Console

[![CI](https://github.com/AsaZhou923/seaweedfs-console/actions/workflows/ci.yml/badge.svg)](https://github.com/AsaZhou923/seaweedfs-console/actions/workflows/ci.yml)

**面向 SeaweedFS 集群管理与图片资产工作流的第三方 Web 控制台。**

[English](README.md) | 简体中文

在一个界面中管理集群、Bucket、IAM、文件和后台服务，并通过图片工作台完成浏览、预览、诊断、重复候选检测和派生图片处理。

SeaweedFS Console 是独立项目，非 SeaweedFS 官方 Admin UI。管理适配以固定的 **SeaweedFS OSS 4.48** 为基线，实际能力取决于部署中可用的服务及权限。

## 功能

| 模块 | 能力 |
| --- | --- |
| 集群 | Dashboard、拓扑、已配置服务健康、Volume、纠删码和 Collection |
| Bucket | 创建与受保护删除、配额、所有者、生命周期、策略、版本控制和 Object Lock |
| IAM | 用户、Access Key、Group、Service Account 和策略 |
| 文件与对象 | Filer 浏览及文件操作；限定 Scope 的 S3 浏览、上传、复制、下载及受保护版本删除 |
| Worker 与模块 | Worker/Plugin 状态、任务、维护操作、MQ 管理、已配置 Mount 客户端及 S3 Tables/Iceberg/Lance 预览 |
| 图片工作台 | 索引浏览与筛选、有界预览、元数据与访问诊断、SHA256 重复候选、规格、派生图片及容量报告 |

- **英语和简体中文**界面，默认英语。
- **本地管理员登录**，包含服务端会话、CSRF 保护及动作/Scope 检查。
- **凭据仅留在后端**，绑定明确批准的端点。
- **受保护写入**，使用持久意图、固定幂等 key 和读回核验。
- **持久任务**，由独立 worker 和 SQLite WAL 支撑。

## 快速开始

当前维护的本地运行方式使用 **Windows、PowerShell 7.5、Python 3.12 和 Node.js 24**。需要自行准备可访问的 SeaweedFS 部署；本项目不会启动 SeaweedFS 服务。

以下命令从项目根目录执行。

### 1. 配置凭据

```powershell
if (-not (Test-Path .\secrets.json)) {
    Copy-Item .\secrets.example.json .\secrets.json
}
```

编辑 `secrets.json`，将示例凭据和端点替换为实际配置。[secrets.example.json](secrets.example.json) 包含 S3、Admin、原生 HTTP 及可选 gRPC 配置示例。

S3 凭据必须具有非空 `allowed_endpoint_url`。Admin 凭据绑定 Admin URL，并明确列出批准的原生服务端点。仅配置允许后端访问的地址。

### 2. 安装并启动

```powershell
$env:CONSOLE_ADMIN_PASSWORD = 'replace-with-a-strong-password'
$env:CONSOLE_SECRETS_FILE = Join-Path (Get-Location) 'secrets.json'
pwsh -NoProfile -File scripts/start.ps1 -Install
```

启动脚本会创建 Python 虚拟环境、安装锁定依赖、构建前端，并在后台启动 API 和 worker。

访问 **http://127.0.0.1:18765**，使用 `admin` 和配置的密码登录。日志与进程记录保存在 `output/`。

管理员密码在首次启动时以哈希保存。之后更改环境变量不会重置已有账户。

### 3. 接入服务

1. 在 **Settings** 创建 Management Connection，使用已注册的 Admin 凭据引用。
2. 配置批准的 Admin 和原生服务端点；需要 S3 功能时关联 S3 连接。
3. 使用图片工作台时，创建项目和绑定 Bucket、字面 Key 前缀的 Scope。按需开启预览、原图下载或写入。
4. 提交扫描建立对象索引；需要精确重复候选时，另行提交 checksum 扫描。

管理写入默认关闭，只开启需要的动作和目标范围。依赖缺失或能力不支持时会明确显示。

图片工作台与管理页面使用统一布局。Assets 将筛选和选择操作放在图库上方，任务、重复与容量证据放在侧栏。Jobs、Diagnostics、Operations 和 Presets 在切换上下文时清空绑定旧 Scope 的输入与结果。

管理页面区分浏览内容、当前状态和待编辑设置。先从列表选择桶，再配置该桶；新桶创建和已有桶的所有者、配额管理各有独立区域。Filer 文件列表使用完整宽度，文件操作放在列表下方。实时对象页集中说明所在桶、授权根前缀和当前浏览前缀，原始证据仍可从折叠区查看。

### 停止或重启

```powershell
pwsh -NoProfile -File scripts/stop.ps1
pwsh -NoProfile -File scripts/start.ps1
```

重启时保留配置环境变量。也可从 `backend/` 分别启动 API 和 worker，使用相同的绝对数据及凭据路径。

## 配置

[.env.example](.env.example) 列出环境变量。启动脚本**不会自动加载这个文件**。

| 变量 | 作用 |
| --- | --- |
| `CONSOLE_ADMIN_USERNAME` | 管理员用户名，默认 `admin` |
| `CONSOLE_ADMIN_PASSWORD` | 启动时必填，首次使用时初始化账户 |
| `CONSOLE_DATA_DIR` | SQLite 数据库和私有预览数据；启动脚本默认使用项目的 `data/` |
| `CONSOLE_SECRETS_FILE` | 后端凭据 registry 路径 |
| `CONSOLE_ALLOWED_ORIGINS` | 逗号分隔的浏览器来源白名单 |
| `CONSOLE_DEV_INSECURE_COOKIE` | 本地 HTTP 开发使用 `1`，HTTPS 使用 `0` |
| `CONSOLE_CACHE_MAX_BYTES` | 私有预览缓存大小上限 |
| `CONSOLE_CACHE_TTL_SECONDS` | 私有预览缓存保留时间 |

从不同目录启动进程时，使用绝对数据与 registry 路径。不要提交 `secrets.json`、`.env`、数据库、会话或生成的凭据。

## 工作方式

```text
浏览器 → FastAPI API → 批准的 SeaweedFS Admin / 原生 / S3 端点
                   ↘ SQLite WAL ← 独立 worker
```

本地运行时 React 应用与 API 同源。后端持有上游凭据和会话，浏览器只调用控制台 API。SQLite 保存配置、索引、任务状态、manifest 和操作历史，源对象仍保存在 SeaweedFS。

技术栈包括 **React、TypeScript、Vite、FastAPI、boto3、Pillow 和 SQLite**；可选模块适配使用 gRPC 和有界表文件解码器。

## 开发

依赖版本记录在 [backend/requirements-lock.txt](backend/requirements-lock.txt) 和 [frontend/package-lock.json](frontend/package-lock.json)。

首次安装后，可将 API 运行在 `8000` 端口，配合前端开发服务器：

```powershell
pwsh -NoProfile -File scripts/stop.ps1
$env:CONSOLE_ALLOWED_ORIGINS = 'http://127.0.0.1:5173,http://127.0.0.1:8000'
pwsh -NoProfile -File scripts/start.ps1 -Port 8000
npm run dev --prefix frontend
```

使用与快速开始相同的管理员密码及凭据环境变量，访问 **http://127.0.0.1:5173**。Vite 将 `/api` 代理到 `http://127.0.0.1:8000`；后端修改后重启 API/worker。

### 验证

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -q -p no:cacheprovider -o addopts=
.venv\Scripts\python.exe -m pytest tests/scripts -q -p no:cacheprovider -o addopts=
npm run build --prefix frontend
npm run test:i18n --prefix frontend
node scripts/verify_table_preview_ui.cjs
node scripts/verify_frontend_review_regressions.cjs
```

前端回归脚本需要已构建的 `frontend/dist` 和 Chrome 或 Edge，使用本地全模拟服务。真实集成脚本有实际副作用，运行前先阅读脚本并确认目标范围。

[GitHub Actions CI](https://github.com/AsaZhou923/seaweedfs-console/actions/workflows/ci.yml) 在 main 推送、PR 和手动触发时执行。Ubuntu 24.04 与 Windows 2025 运行后端、依赖兼容、打包、前端构建、双语/SSR 和模拟 Chrome 回归；Windows 额外验证 PowerShell 启停脚本。Python 3.12 与 Node.js 24 使用已提交的依赖文件，Python 检查保留七天 JUnit 报告。CI 不接入真实 SeaweedFS 或读取服务器凭据，真实联调保留为单独显式执行的检查。

### 使用本机 e2e 项目进行真实浏览器测试

源码工作区的 `tests/e2e/live-console.e2e.ts` 浏览器套件使用已有 `e2e` 项目构建好的 SDK 和 Web engine，不新增应用依赖。用 `SWC_E2E_REPO` 指定该项目。Fixture 工具在凭据注册表批准的端点新建一个随机 `swc-integration-e2e-*` 桶，写入合成图片，并创建独立本地 Console 数据库；管理连接保持只读。

第一个终端中使用新的产物目录和明确批准的注册表：

```powershell
$env:SWC_E2E_REPO = 'E:\Project Code\useful_repo\e2e'
$env:SWC_E2E_FIXTURE = 'output/my-e2e-run/live-fixture.json'
$env:SWC_E2E_OUTPUT = 'output/my-e2e-run/runner'
rtk proxy .venv\Scripts\python.exe scripts/e2e_live_fixture.py prepare --directory output/my-e2e-run --registry output/server-secrets.json
rtk proxy .venv\Scripts\python.exe scripts/e2e_live_fixture.py serve --directory output/my-e2e-run
```

第二个终端设置相同的三个变量后，执行 `rtk proxy node "$env:SWC_E2E_REPO\packages\e2e\dist\cli\bin.js" run --config e2e.config.ts`。先在服务终端按 Ctrl+C 停止 fixture，再执行 `rtk proxy .venv\Scripts\python.exe scripts/e2e_live_fixture.py cleanup --directory output/my-e2e-run`。清理只操作该 fixture 记录为本轮自有的桶，并保留本地产物。密码与保存的会话属于私有运行数据，不要发布 fixture 目录。

### 本地包

先构建前端，再生成源码与静态资源包。目标必须是新路径，并位于源码及历史发布目录之外：

```powershell
.venv\Scripts\python.exe scripts/package.py --destination output/local-build/console.zip
.venv\Scripts\python.exe scripts/verify_package.py --package output/local-build/console.zip --registry secrets.json
```

验证器检查压缩包、manifest、必需文件、文档相对链接，并扫描显式传入及自动发现的 registry 凭据值。仅构建成功不能证明包内没有凭据字节。

## 备份与恢复

```powershell
.venv\Scripts\python.exe scripts/backup.py backup data/console.db output/backups/console.db
.venv\Scripts\python.exe scripts/backup.py verify output/backups/console.db
.venv\Scripts\python.exe scripts/backup.py restore output/backups/console.db output/restore/console.db
```

备份和恢复目标必须是新路径。工具使用 SQLite backup API，并验证完整性和外键。启用恢复后的数据库前，先停止 API 和 worker，将 `CONSOLE_DATA_DIR` 指向恢复目录后再启动。凭据与环境配置需单独备份。

这只备份控制台状态，不备份 S3 对象或 Filer 元数据。

## 当前范围

- 面向单个本地管理员和固定的 SeaweedFS OSS 基线；Enterprise OIDC、多用户角色及多集群 HA 不在当前范围内。
- 表预览是有界样本，不是完整逻辑查询；Lance 样本依赖可用 Worker，依赖缺失保持未知。
- 未测容量与未接入的业务引用保持未知；ETag 不能代替完整内容 hash。缺少引用及并发保护时，原图自动删除保持关闭。
- 大规模负载、任意旧库升级和生产全局写入需要单独验证，完整迁移审计台账仍待补齐。
- 启动脚本将 API 绑定到 localhost，对外提供服务需要另行配置 HTTPS 和来源限制。

## 贡献

欢迎提交问题和范围明确的 PR。请提供受影响模块、复现步骤、SeaweedFS 版本及脱敏错误，不要附带凭据、签名 URL、会话或私有对象内容。

保持改动聚焦，为行为变化添加有意义的回归，自有 UI 文案同时更新中英文目录。项目约定见 [AGENTS.md](AGENTS.md)，适配契约见[管理 API 参考](docs/management-api.md)。

## 许可证

项目原创代码采用 [Apache License 2.0](LICENSE)，归属说明见 [NOTICE](NOTICE)。第三方组件保留各自许可证，包括[内置 SeaweedFS 协议文件的上游许可证](backend/console/vendor/SEAWEEDFS-LICENSE.txt)。
