# UI Redesign Implementation Brief

Date: 2026-10-04

This brief supersedes the earlier image-first UI direction. SeaweedFS Console is now a SeaweedFS OSS Admin-compatible management console first, with image enhancement as a secondary grouped workspace. Preserve the current light neutral visual direction and verified image functionality; do not redo or roll back working image surfaces just to change the product framing.

The design source of truth is `DESIGN.md`.

## Aesthetic direction

- Purpose: make SeaweedFS Console feel like a practical storage management console that also has strong image workflow tools.
- Tone: light, neutral, precise, source-evidence-first.
- Constraints: React + TypeScript + Vite, plain CSS, one FastAPI app, one worker, SQLite, no new dependencies without approval.
- Memorable detail: the default Dashboard is an official-source management evidence board, while image enhancement keeps the thumbnail wall as a secondary power surface.

## Source blueprint

- Use the pinned SeaweedFS 4.48 Admin source evidence as the management blueprint.
- Parent task evidence: source SHA `530be3`, broad Admin route matrix, and `output/official-admin-read-probe.json` with actual JSON 200 probes including 3 volumes, 1 master, and 1 worker.
- Backend owns:
  - session cookie and CSRF;
  - exact endpoint binding;
  - `secret_ref` lookup without exposing secrets;
  - DTO redaction;
  - approved Master/Filer/Volume HTTP service access when Admin pages lack JSON.
- Frontend owns readable management UI. It must not iframe/jump to native Admin for functions this app claims, and must not scrape Admin HTML.

## Product correction

Official SeaweedFS OSS Admin management is primary. Image enhancement is secondary.

Required top-level navigation order:

| Route group | Purpose | Selector |
| --- | --- | --- |
| Dashboard | Default landing; service inventory, probe evidence, capacity split, dependency states | Management Connection |
| 拓扑服务 | Master/Filer/Volume/Worker/service topology | Management Connection |
| Volumes / EC / Collections | Volumes, erasure coding, collections, disk/capacity/source DTOs | Management Connection |
| 桶 | Bucket inventory and management state | Management Connection |
| 文件对象 | File/object management browsing | Management Connection, optional transition to Project Scope |
| S3 / IAM | S3 accounts, policies, users/groups/access keys where supported | Management Connection |
| Worker 维护 | Worker/subscriber maintenance and service state | Management Connection |
| 其他服务 | MQ, mounts, S3 Tables, Iceberg, and configured dependencies | Management Connection |
| 图片增强 | Existing assets/scans/presets/diagnostics/operations tools | Project Scope |
| Settings | Management endpoints, secret refs, project/scope setup, S3 config | Shared |

Existing image hash aliases must remain valid: `#assets`, `#scans`, `#presets`, `#diagnostics`, `#operations`.

## Scope separation

- Control Plane:
  - Uses Management Connection selector.
  - Binds to an exact configured SeaweedFS endpoint and `secret_ref`.
  - Shows Admin/source probe state, service topology, volume/bucket/IAM/worker/dependency evidence.
- Project Scope:
  - Uses project/scope selector.
  - Handles image/object indexing, previews, diagnostics, presets, variants, reports, and protected operations.
- Do not let management routes depend on project/scope.
- Do not let image routes silently inherit broad management authority.

## Current UI stance

- Preserve the current light neutral style direction from the frontend implementation stream.
- Preserve verified image functions: assets, scan/preview, grid/list, keyboard focus, pinned versions, small-screen layout, diagnostics, presets, operations forms.
- The change needed now is IA and management surface expansion, not a visual reset.
- Keep `ResultSummary` / `StructuredResult` available for replacing JSON-first panels.

## Management surface rules

- Dashboard:
  - Show selected Management Connection, endpoint label, probe state, source version/SHA evidence, and last probe time.
  - Show service inventory only from actual probe/API data.
  - Separate SeaweedFS physical/storage capacity from image logical bytes and private preview cache bytes.
  - No fake KPIs, synthetic trend lines, or invented totals.
- Topology:
  - Masters, filers, volume servers, workers, address, role, version/status where available.
  - Missing fields remain unknown.
- Volumes / EC / Collections:
  - Present real volume, EC, collection, disk/free/used/source DTO fields.
  - Do not turn absent fields into zero.
- Buckets:
  - Bucket inventory and management coverage.
  - Static coverage unknown and dynamic API empty response are different states.
- S3 / IAM:
  - Empty API response means `empty_response`, not "all identities count is 0".
  - Upstream mock `CreatedAt` values must be displayed as unknown/mocked unknown.
- Worker 维护:
  - Show worker/subscriber state only from configured/probed sources.
  - Maintenance actions disabled unless backend capability and permission are explicit.
- 其他服务:
  - MQ, mount, S3 Tables, Iceberg, service table states are important but dependency-gated.
  - If unconfigured, show disabled/unconfigured with required dependency; do not hide them as if irrelevant.

## Image enhancement rules

Keep the image group aligned with the existing verified work:

- Assets: thumbnail-first image wall, compact filters, selection toolbar, duplicate/capacity summaries, export, preview jobs.
- Scans/Jobs: actual state, requested pause/cancel state, processed/errors/total, no fake percentages for unknown totals.
- Presets: immutable presets, derived variants, groups, lineage, compare.
- Diagnostics: structured access/CORS/decode/distribution findings, raw evidence secondary.
- Operations: upload/copy/version/tag/metadata/retention/bucket config/reference/cache/capacity/trash, grouped and guarded.

## Layout contract

- Desktop `1280x900`:
  - Sidebar: 220px.
  - Main padding: 24px.
  - Dashboard first screen: management connection, evidence/probe state, service inventory, capacity split, dependency state cards/table.
  - Image routes: preserve useful thumbnail-first first screen.
- `<= 900px`:
  - Collapse navigation to grouped compact nav.
  - Management Connection remains visible before route-specific management content.
  - Project/Scope appears only inside image/object workflows.
- `<= 640px`:
  - Route groups stack.
  - Management tables scroll horizontally.
  - Image filters use disclosure.
  - No giant nav block consumes the first screen.

## Visual tokens

Continue using light neutral CSS variables:

```css
:root {
  --bg: #f4f4f5;
  --surface: #ffffff;
  --surface-muted: #fafafa;
  --border: #e5e7eb;
  --border-strong: #d4d4d8;
  --text: #18181b;
  --muted: #71717a;
  --primary: #18181b;
  --primary-text: #ffffff;
  --link: #2563eb;
  --success: #15803d;
  --warning: #b45309;
  --danger: #b91c1c;
}
```

Typography:

- UI: `"Segoe UI Variable Text", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif`.
- Code/endpoints/IDs/keys: `Consolas, "Cascadia Mono", monospace`.
- Nav: 13px.
- Forms/body/tables: 14px.
- Titles: 20/24px.
- Use tabular figures for counts, bytes, ports, times, and IDs where useful.

## Component plan

- `ManagementConnectionSelector`
  - Shows endpoint label, probe state, version/source evidence, and last probe time.
  - Does not reveal secret values.
- `DashboardEvidencePanel`
  - Summarizes official/source probe data and raw DTO access.
- `ServiceInventoryTable`
  - Masters, filers, volume servers, workers, status/address/version where available.
- `VolumeMatrix`
  - Volumes, EC shards, collections, disk/free/used fields.
- `BucketInventory`
  - Bucket state, coverage, and management actions.
- `S3IamPanel`
  - Static/dynamic IAM coverage with `unknown` and `empty_response` distinct.
- `WorkerMaintenancePanel`
  - Workers/subscribers and capability-gated actions.
- `ServiceDependencyPanel`
  - MQ, mounts, S3 Tables, Iceberg, service-table states.
- `ImageEnhancementGroup`
  - Keeps current Assets/Scans/Presets/Diagnostics/Operations aliases and verified behavior.
- `EvidenceDetails`
  - Shared collapsed raw JSON/DTO evidence.
- `ResultSummary`
  - Continue using for structured operation/diagnostic/capacity/duplicate/health summaries.

## Accessibility and responsive acceptance

- Every management table has a caption and header cells.
- Icon-only controls have `aria-label` and title.
- Status is text plus color, never color alone.
- Unknown/unconfigured/empty/mocked states are readable to screen readers.
- Long endpoints, volume IDs, bucket names, object keys, hashes, and route names have full text in titles or detail views.
- At 595px, no text overlaps, clipped buttons, or navigation block that hides route identity.

## Verification required after implementation

For code implementation, run and record:

```powershell
npm run build --prefix frontend
```

With local app running at `http://127.0.0.1:18765`, verify:

- Login succeeds.
- Dashboard is the default landing route.
- Management Connection selector appears on management routes.
- Project/Scope selector appears only on image/object routes.
- Official/source probe evidence renders from fixture or live backend DTOs.
- Dashboard separates SeaweedFS physical capacity from image logical/cache bytes.
- S3/IAM empty/unknown states are not shown as zero identities.
- Other services show disabled/unconfigured states when dependencies are absent.
- Existing Assets image wall still works.
- Existing Scans/Presets/Diagnostics/Operations aliases still route.
- Browser console has no errors.
- Capture or screenshot at `1280x900` for Dashboard and Assets.
- Capture or screenshot at `595px` for Dashboard and Assets.

This documentation-only correction does not require runtime tests.

## Stop condition

The management-first redesign pass can be considered complete when:

- Dashboard is the default management landing.
- Management navigation precedes image enhancement navigation.
- Control Plane and Project Scope selectors are separated.
- Official-source DTO/probe evidence is structured and raw JSON is secondary.
- Dependency-gated services show explicit disabled/unconfigured states.
- Existing image enhancement functions and aliases still work.
- Build and browser smoke pass after implementation.
- Desktop and narrow screenshots are recorded for Dashboard and Assets.
