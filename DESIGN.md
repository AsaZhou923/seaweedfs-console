# Design

## Source of truth

- Status: Active
- Last refreshed: 2026-10-07
- Primary product surfaces: SeaweedFS OSS Admin-compatible management console first; image asset enhancement workbench second. The same app owns both surfaces, but Control Plane management and Project Scope image workflows must stay visually and conceptually distinct.
- Evidence reviewed:
  - `frontend/package.json`: React 19.3, React DOM 19.3, TypeScript 7, Vite 8.
  - `frontend/src/main.tsx`: current hash-route SPA with login, assets, scans, diagnostics, operations, presets, settings.
  - `frontend/src/styles.css`: current UI has already moved toward a light neutral console in the frontend implementation stream; preserve that direction and do not roll back to the old dark teal look.
  - `README.md`: local runbook, one API process, one worker, SQLite WAL, OptiPlex validation boundary.
  - `docs/progress.md` and `docs/test-fix-log.md`: current validation state and remaining non-completion boundaries.
  - `E:\Project Code\docs\01 - Projects\seaweedfs-console\08 - Plans\seaweedfs-console 开发计划.md`: authoritative 01-22 implementation/test scope.
  - `E:\Project Code\docs\01 - Projects\seaweedfs-console\02 - Architecture\seaweedfs-console 系统设计.md` and `seaweedfs-console 详细技术设计.md`: API boundaries, source/object/revision model, permissions, and fail-closed semantics.
  - Official SeaweedFS 4.48 Admin source evidence from the parent task: pinned source SHA `530be3`, broad Admin route matrix, and `output/official-admin-read-probe.json` with actual JSON 200 probes including 3 volumes, 1 master, and 1 worker.
  - Design reference: restrained light dashboard structure similar to shadcn/ui `dashboard-01`. Immich-style gallery interaction remains secondary inspiration for image enhancement only, not the product identity.

## Brand

- Personality: practical SeaweedFS operations console with a precise image-workflow extension.
- Trust signals: live control-plane endpoint binding, real Admin/API probe results, explicit dependency state, DTO redaction, source/object identity, timestamped evidence, and unknown states shown as unknown.
- Avoid:
  - Treating this as only an image gallery or PicSpeak-specific tool.
  - Faking dashboard KPIs, service counts, IAM identity counts, MQ status, or object safety claims.
  - Interpreting an empty dynamic API result as "zero identities" or "all clear"; use `unknown`, `not_configured`, or `empty_response` based on evidence.
  - Treating upstream mocked `CreatedAt` values as real timestamps; display them as `unknown` or `mocked_unknown` where needed.
  - Raw JSON as the primary UI; raw evidence belongs in secondary `<details>`.
  - Reintroducing dark teal glow, purple gradients, marketing cards, or decorative dashboards.

## Product goals

- Goals:
  - Provide a third-party SeaweedFS management console grounded in the OSS Admin surface: topology, volumes, erasure coding, collections, buckets, file/object browsing, S3/IAM, workers, and configured satellite services.
  - Preserve existing image enhancement capabilities: object indexing, previews, diagnostics, presets, derived variants, comparisons, reports, and protected operations.
  - Keep implementation simple: one FastAPI application, one worker, SQLite, same-origin UI, no enterprise SSO, no multi-tenant HA, no separate frontend/backend product split.
  - Use official Admin routes and JSON/read probes as product requirements, not as a visual iframe or HTML scraping target.
- Non-goals:
  - Do not jump to SeaweedFS native Admin pages for core functions that this app claims to own.
  - Do not scrape HTML Admin pages as data sources. If a management area has no JSON endpoint, use approved Master/Filer/Volume HTTP service access and DTOs.
  - Do not implement enterprise OIDC, centralized multi-cluster governance, multi-tenant RBAC, or HA unless the plan is explicitly expanded.
  - Do not claim image source cleanup, publish health, object disaster recovery, MQ health, S3 Tables, or Iceberg readiness without configured dependencies and live proof.
- Success signals:
  - Default landing Dashboard shows official-source management evidence, service inventory, and dependency states without fake counts.
  - Header has a Management Connection selector for Admin/control-plane routes.
  - Project/Scope selectors appear only where image/object workflows need them.
  - Dashboard capacity separates SeaweedFS physical/storage capacity from image logical bytes and private cache bytes.
  - At `1280x900`, management navigation is first-class and image enhancement remains reachable without dominating the app.
  - At `595px`, navigation remains compact and the first screen still tells the user which control-plane connection is active.

## Personas and jobs

- Primary personas:
  - Solo developer/operator managing a local or LAN SeaweedFS deployment.
  - Technical owner of an image-heavy project that also needs storage visibility.
  - Maintainer validating OptiPlex SeaweedFS behavior against project docs and probes.
- User jobs:
  - Select a configured SeaweedFS management connection and inspect topology/service health.
  - Inspect masters, filers, volumes, erasure-coded shards, collections, buckets, S3/IAM state, workers, and configured satellite services.
  - Browse files/objects and then move into Project Scope workflows when image-specific indexing or enhancement is needed.
  - Scan image objects, generate safe previews, diagnose access/CORS/decode issues, find exact checksum duplicates, and manage presets/variants.
  - Perform controlled write/config operations with explicit backend reasons and audit evidence.
- Key contexts of use:
  - Local browser at `http://127.0.0.1:18765`.
  - OptiPlex SeaweedFS 4.48 integration testing on LAN.
  - Personal/small-project operation where explicit unknowns are better than enterprise-style claims.

## Information architecture

- Primary navigation, in required order:
  - Dashboard
  - 拓扑服务
  - Volumes / EC / Collections
  - 桶
  - 文件对象
  - S3 / IAM
  - Worker 维护
  - 其他服务
  - 图片增强
  - Settings
- Core management routes/screens:
  - `#dashboard`: default landing. Shows selected management connection, official-source probe status, service inventory, capacity split, dependency states, and recent management evidence.
  - `#topology`: masters, filers, volume servers, workers, connectivity, role/status, and raw official DTO evidence.
  - `#volumes`: volumes, erasure coding, collections, layout, disk/free/used indicators, and volume-server details.
  - `#buckets`: bucket inventory and bucket-level management state. Static S3/IAM coverage may be unknown.
  - `#files`: file/object browsing from SeaweedFS/S3 surfaces. It is management browsing first; image Project Scope tools are a secondary transition.
  - `#s3-iam`: S3 accounts, policies, users/groups/access keys where supported. Dynamic API empty responses must not become "all identities zero"; label as empty response with coverage.
  - `#workers`: worker/subscriber maintenance, queue status where configured, job/service process signals.
  - `#services`: MQ, mounts, S3 Tables, Iceberg, and other SeaweedFS-adjacent services. They are disabled/unconfigured by default unless a dependency is configured and probed.
  - `#settings`: shared place for management connections, endpoint binding, secret references, project/scope setup, and S3 configuration.
- Image enhancement aliases that must be preserved:
  - `#assets`: image wall, object indexing, compact filters, selection toolbar, duplicate/capacity summaries, export, preview jobs.
  - `#scans`: image/index/checksum/preview jobs. The label may become Jobs inside the image group, but the hash alias remains.
  - `#presets`: immutable image presets, derived variants, groups, lineage, side-by-side comparison.
  - `#diagnostics`: image/storage access diagnostics, decode, CORS, distribution evidence.
  - `#operations`: controlled upload/copy/version/tag/metadata/retention/bucket config/reference/cache/capacity/trash operations.
- Scope model:
  - Control Plane routes use a Management Connection selector. This binds to an exact configured endpoint and `secret_ref`.
  - Project Scope routes use project/scope selectors and object authorization.
  - Do not mix the selectors: management topology should not depend on an image project, and image operations should not silently inherit all control-plane authority.

## Design principles

- Principle 1: Official Admin parity first, image workflow second. The product should answer "what is my SeaweedFS doing?" before "how do I process these images?"
- Principle 2: Evidence beats decoration. Every dashboard value must come from an actual API/probe/source DTO or be labeled unknown/unconfigured.
- Principle 3: Two scopes, one app. Control Plane and Project Scope share shell and visual language but keep selectors, permissions, and microcopy separate.
- Principle 4: Dependency absence is a state. MQ, mounts, S3 Tables, Iceberg, IAM, and service-table features can be important but must appear disabled/unconfigured when not wired.
- Tradeoffs:
  - Prefer readable source DTO summaries over live-count spectacle.
  - Prefer repo-native React/CSS over new design dependencies.
  - Preserve verified image functions while adding management surfaces incrementally.

## Visual language

- Color:
  - `--bg: #f4f4f5`
  - `--surface: #ffffff`
  - `--surface-muted: #fafafa`
  - `--border: #e5e7eb`
  - `--border-strong: #d4d4d8`
  - `--text: #18181b`
  - `--muted: #71717a`
  - `--faint: #a1a1aa`
  - `--primary: #18181b`
  - `--primary-text: #ffffff`
  - `--link: #2563eb`
  - `--success: #15803d`
  - `--warning: #b45309`
  - `--danger: #b91c1c`
  - `--preview-canvas: #f8fafc`
- Typography:
  - UI text: `"Segoe UI Variable Text", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif`.
  - Code, hashes, object keys, endpoint IDs: `Consolas, "Cascadia Mono", monospace`.
  - Navigation: 13px. Forms/body/tables: 14px. Route title: 20-24px.
  - Numeric values use tabular figures.
- Layout rhythm:
  - Management desktop: 220px sidebar, 22px main padding, 12px control gaps, 16px panel-content gaps, 24px page-section gaps.
  - Panels use 20px padding on desktop and 16px at narrow widths. Labels have 8px separation from controls; independent form sections have a divider and 24px padding above.
  - Panel titles, explanatory text, input groups, actions, and evidence occupy separate grid rows. Do not place a save button directly against a control or the next field label.
  - Management tables: compact 42-44px rows.
  - Buttons: 34px height, 6px radius.
  - Cards/panels: max 10px radius; avoid cards inside cards.
- Motion:
  - Fast, restrained transitions under 160ms.
  - Respect reduced motion.
  - No looping glow, background animation, or hero effects.
- Imagery/iconography:
  - Management routes use compact monochrome line icons.
  - Image enhancement still uses the real thumbnail wall as its memorable detail.
  - Use repo-native inline SVG icons at 20px; no new icon dependency without approval.

## Components

- Existing components to reuse:
  - App shell, login/session flow, API requester, image assets/scans/diagnostics/operations/presets/settings flows.
  - `ResultSummary` / `StructuredResult` for replacing JSON-first result boxes where applicable.
- New/changed management components:
  - `ManagementShell` or shell extension: clearly separates Management Connection from Project Scope.
  - `ManagementConnectionSelector`: endpoint, version/probe state, last probe time, secret-ref identity without exposing secret values.
  - `DashboardEvidencePanel`: source route, probe status, and official DTO summary.
  - `TopologyServiceTable`: masters, filers, volume servers, workers, status, address, version where available.
  - `VolumeMatrix`: volumes, EC shards, collections, disk/capacity fields with unknown handling.
  - `BucketInventory`: bucket state and management coverage.
  - `S3IamPanel`: static/dynamic IAM coverage with `unknown` and `empty_response` distinct.
  - `WorkerMaintenancePanel`: workers/subscribers and maintenance actions guarded by backend capability.
  - `ServiceDependencyPanel`: MQ, mounts, S3 Tables, Iceberg, and service-table states.
  - `EvidenceDetails`: raw JSON/DTO collapsed by default.
- Variants and states:
  - `ok`, `warning`, `error`, `unknown`, `not_configured`, `unsupported`, `empty_response`, `mocked_unknown`.
  - Disabled controls include reason text: missing endpoint, missing secret_ref, unsupported route, dependency not configured, permission denied, or stale probe.
- Token/component ownership:
  - Visual tokens live in `frontend/src/styles.css`.
  - Route/component files may be split by the frontend implementation, but backend API behavior remains authoritative.
  - No Tailwind, shadcn CLI, lucide, Radix, or state-library dependency unless explicitly approved.

## Accessibility

- Target standard: practical WCAG 2.1 AA for contrast, focus, labels, keyboard access, and reduced motion.
- Keyboard/focus behavior:
  - Management tables, tabs, drawers, and evidence panes are keyboard reachable.
  - Image asset tiles remain keyboard openable with Enter/Space.
  - Icon-only controls have `aria-label` and tooltip/title text.
- Contrast/readability:
  - Status color is never the only signal.
  - Long endpoints, keys, volume IDs, hashes, and route names wrap in detail views and ellipsize in dense tables.
- Screen-reader semantics:
  - Tables use captions and header cells.
  - Raw JSON is not the only representation of important state.
- Reduced motion:
  - `prefers-reduced-motion` disables route/drawer transitions.

## Responsive behavior

- Supported breakpoints/devices:
  - Desktop reference: 1280x900.
  - Narrow reference: 595px width.
  - Minimum content width: 320px.
- Layout adaptations:
  - `>= 1280px`: full management sidebar, dashboard grids, evidence panels.
  - `901-1279px`: context selectors wrap, tables remain scrollable.
  - `<= 900px`: navigation collapses to compact grouped nav. Management connection stays visible above route content.
  - `<= 640px`: route groups stack; tables scroll; image filters use disclosure; no giant nav block before content.
- Touch/hover differences:
  - Hover affordances also appear through focus and selected states.
  - Dense rows remain at least 42px high where interactive.

## Interaction states

- Loading:
  - Show route-local loading without hiding selected management connection.
  - Probe refreshes should not blank known prior evidence; show stale timestamp.
- Empty:
  - Dynamic API empty result: display `empty_response`, coverage, and source route.
  - Unconfigured service: display `not_configured`, required dependency, and setup location.
  - Image scope empty: suggest scan only when a project/scope is selected and action is allowed.
- Error:
  - Show backend code, message, source route, endpoint, and retry.
  - Keep raw response in `EvidenceDetails`.
- Success:
  - Show concrete object/job/service ID and timestamp.
- Disabled:
  - Disabled management actions explain capability, dependency, permission, or stale evidence reason.
- Offline/slow network:
  - Polling/probe errors do not turn unknown services into failed services unless the backend proves failure.

## Content voice

- Tone: short, technical, evidence-first Chinese. Keep exact English terms for SeaweedFS concepts and API states.
- Terminology:
  - Management: `Management Connection`, `Control Plane`, `Master`, `Filer`, `Volume`, `EC`, `Collection`, `Bucket`, `S3/IAM`, `Worker`, `MQ`, `Mount`, `S3 Tables`, `Iceberg`.
  - Image scope: `Project Scope`, `asset`, `object`, `revision`, `checksum`, `manifest`, `derived variant`, `preview cache`.
  - State: `未知`, `未配置`, `不支持`, `空响应`, `模拟时间未知`, `覆盖未知`.
- Microcopy rules:
  - Do not call unknown IAM identities `0`.
  - Do not call missing service dependencies `healthy`.
  - Do not call upstream mocked `CreatedAt` real creation time.
  - Use concrete verbs: `刷新探测`, `查看 DTO`, `打开对象`, `扫描 scope`, `生成预览`, `复制并校验`, `保存配置`.

## Implementation constraints

- Framework/styling system:
  - React + TypeScript + Vite SPA.
  - Styling is plain CSS.
  - Current image aliases must keep working.
- Backend/source contract:
  - One FastAPI app, one worker, SQLite.
  - Management backend owns session cookie/CSRF, exact endpoint binding, `secret_ref`, DTO redaction, and approved HTTP service access.
  - Do not rely on browser-side direct SeaweedFS credentials.
  - Use official Admin/source routes and JSON/read probes where available.
  - For missing JSON pages, use approved Master/Filer/Volume HTTP APIs rather than HTML scraping.
- Design-token constraints:
  - Keep light neutral tokens and current verified image-function styling direction.
  - Do not redo or roll back verified image surfaces solely because the IA changed.
- Performance constraints:
  - Do not load full buckets into the browser for filtering.
  - Management probes should be bounded and cacheable with timestamps.
- Compatibility constraints:
  - No new dependencies without explicit user approval.
  - Preserve existing 01-22 image/backend contracts and fail-closed semantics.
- Test/screenshot expectations for implementation:
  - Build after code changes.
  - Management Dashboard at `1280x900` and `595px`.
  - Image Assets route at `1280x900` and `595px`.
  - Browser console has no errors.
  - Raw official DTO evidence remains accessible but secondary.

## Image enhancement workbench

- Assets, Jobs, Diagnostics, Operations, and Presets share the management console's light neutral tokens, restrained borders, form spacing, and section hierarchy.
- Keep the image grid primary. Filters and selected-object actions sit above it; recent jobs and duplicate/capacity evidence sit beside it on wide screens and below it on narrow screens.
- Keep scope context in the shared Project/Scope selector. Page summaries describe the current visible data, rather than repeat that selector or imply a total inventory count.
- Treat every Scope or Project change as a new request context. Clear bound inputs, results, jobs, versions, upload handles, batches, and selections before rendering the new context; discard late responses from the previous context.
- Validate populated image and management screens at 1280 and 595 pixels with the live isolated Console. Mocked delayed-response regressions remain separate evidence for request ordering.

## Management information hierarchy

- Keep the page order understandable: active context, browsable data, configuration/actions, then raw evidence. Brief descriptions explain the user's task; transport flags and raw DTOs remain secondary.
- Bucket inventory uses the full content width. A selected-bucket heading identifies the configuration target; current status is distinct from editable drafts. Versioning, Object Lock, lifecycle, and policy each have a description, field, and separate save action.
- New-bucket creation and selected-bucket owner/quota/deletion use separate disclosures and drafts. Selecting a different bucket or management connection clears the selected-bucket owner/quota draft. Creation inputs do not populate existing-bucket changes.
- Filer browsing uses the full content width. File tools follow in a collapsible area with separate create, upload, rename, and delete groups. Keep preview/hash and permission guards intact.
- Live Object context uses a definition list for bucket, authorized root prefix, and current browsing prefix. Each value is labeled once. Without a bound Scope, values remain unknown; an empty prefix means bucket root only after Scope binding is known.
- Table pagination is a compact footer with first/next actions and muted status text. It does not use a bordered status card or imply that a missing cursor proves inventory completeness.
- `scripts/verify_ui_layout.cjs` captures populated mocked Buckets, Files, Objects, Dashboard, and Assets. It checks spacing, read-only gates, separated bucket drafts, collapsed evidence, page errors, and document overflow at 1280/595/320 widths, with additional English desktop smoke. These captures are local UI evidence.

## Open questions

- [ ] Which management API route names should map to Dashboard/Topology/Volumes/Buckets/S3-IAM in the first frontend implementation? Owner: management frontend/backend integration. Impact: route wiring.
- [ ] Which Master/Filer/Volume HTTP endpoints are approved as non-scraping sources for Admin pages that lack JSON? Owner: backend integration. Impact: DTO shape and permissions.
- [ ] Should the current `#settings` route split into management settings and project/scope settings, or remain one shared Settings route with tabs? Owner: frontend implementation. Impact: navigation clarity.
- [ ] Which probe fields in `output/official-admin-read-probe.json` become required fixtures for UI smoke tests? Owner: parent task. Impact: deterministic frontend validation.


## 2026-10-05 状态与管理意图修复契约

Assets的Scope/query变更清空旧对象、drawer、选择、任务和报告；旧请求/轮询不能提交。无Scope禁用导出。S3Tables详情/样本绑定连接、Scope、表、格式、snapshot/file/limit，输入变化失效并拒绝旧响应。远端管理写固定幂等意图并有pending门禁，未知回执保留key并显示管理历史核查提示；status/state均识别。非空行、条件title/placeholder的自有文案进入双语目录，真实对象/字段/raw证据不改写。
