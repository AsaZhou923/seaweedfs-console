import React, { FormEvent, useEffect, useRef, useState } from "react";
import { ResultSummary, StructuredResult } from "../components/ResultSummary";
import { t, translateMessage, useLocale } from "../i18n";

export type Requester = <T = unknown>(path: string, init?: RequestInit) => Promise<T>;

export type ManagementConnection = {
  id: string;
  name: string;
  admin_url: string;
  s3_connection_id?: string | null;
  protocol_baseline?: string;
  endpoints?: Record<string, string>;
  management_write_enabled?: boolean;
  permissions?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type S3ConnectionOption = { id: string; display_name: string; endpoint_url: string };
export type StorageScopeOption = { id: string; project_id?: string; connection_id?: string; display_name?: string; bucket?: string; prefix?: string; allow_original_download?: boolean; allow_preview?: boolean; writable?: boolean; authz_epoch?: number };
type ManagementEnvelope = { connection_id?: string; source?: string; checked_at?: string; source_updated_at?: string | null; protocol_baseline?: string; module_source_pin?: Record<string, unknown>; metadata?: Record<string, unknown>; [key: string]: unknown };
type AsyncState<T> = { loading: boolean; error: string; data: T | null };
type Notice = { error: string; result: unknown };

const MANAGEMENT_ROUTES = ["dashboard", "topology", "storage", "buckets", "files", "objects", "iam", "maintenance", "services"];
const PERMISSION_KEYS = ["bucket.manage", "iam.manage", "maintenance.execute", "volume.manage", "file.manage", "object.manage", "mq.manage", "table.manage"];
const LIST_PERMISSION_KEYS = ["bucket_write_prefixes", "file.read_roots", "file.write_roots"];

export function isManagementRoute(route: string) {
  return MANAGEMENT_ROUTES.includes(route);
}

export function managementTitle(route: string) {
  const title = ({
    dashboard: "Dashboard",
    topology: "Topology",
    storage: "Volumes / EC / Collections",
    buckets: "Buckets",
    files: "Filer Files",
    objects: "Live Objects",
    iam: "S3 / IAM",
    maintenance: "Worker Maintenance",
    services: "Services"
  } as Record<string, string>)[route] || "Dashboard";
  return t(title);
}

export function ManagementConnectionSelector({ connections, value, onChange, onRefresh, compact = false }: { connections: ManagementConnection[]; value: string; onChange: (value: string) => void; onRefresh?: () => void; compact?: boolean }) {
  useLocale();
  const current = connections.find((item) => item.id === value);
  return (
    <div className={compact ? "managementSelector compact" : "managementSelector"}>
      <label className="field compact">{" "}{t("管理连接")}{" "}<select value={value} onChange={(event) => onChange(event.target.value)}>
          <option value="">{t("未配置")}</option>
          {connections.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>
      <div className="selectorEvidence">
        <StatusBadge state={current ? "configured" : "not_configured"} label={current ? "已配置" : "未配置"} />
        <span title={current?.admin_url || ""}>{current?.admin_url || "没有管理连接"}</span>
        <code>{current?.protocol_baseline || "4.48"}</code>
      </div>
      {onRefresh && <button type="button" className="secondary" onClick={onRefresh}>{t("刷新连接")}</button>}
    </div>
  );
}

export function ManagementRoute({ route, request, managementId, connections, s3Connections, refreshConnections, scopes = [], scopeId = "", setScopeId, onObjectSelected }: { route: string; request: Requester; managementId: string; connections: ManagementConnection[]; s3Connections: S3ConnectionOption[]; refreshConnections: () => Promise<void>; scopes?: StorageScopeOption[]; scopeId?: string; setScopeId?: (value: string) => void; onObjectSelected?: (objectId: string) => void }) {
  useLocale();
  const connection = connections.find((item) => item.id === managementId);
  if (!managementId) {
    return (
      <section className="panel managementPage">
        <div className="managementHero">
          <div><p>{t("Control Plane")}</p><h2>{t("先绑定 SeaweedFS Admin 连接")}</h2><span>{t("管理页面只展示服务端适配后的官方 Admin / HTTP API 数据；没有连接时不推断集群指标。")}</span></div>
          <StatusBadge state="not_configured" label="未配置" />
        </div>
        <ConnectionSetup request={request} s3Connections={s3Connections} refreshConnections={refreshConnections} />
      </section>
    );
  }
  if (route === "topology") return <TopologyPage request={request} managementId={managementId} />;
  if (route === "storage") return <StoragePage request={request} managementId={managementId} connection={connection} />;
  if (route === "buckets") return <BucketsPage request={request} managementId={managementId} connection={connection} />;
  if (route === "files") return <FilesPage request={request} managementId={managementId} connection={connection} />;
  if (route === "objects") return <ObjectsPage request={request} managementId={managementId} connection={connection} scopes={scopes} scopeId={scopeId} setScopeId={setScopeId} onObjectSelected={onObjectSelected} />;
  if (route === "iam") return <IamPage request={request} managementId={managementId} connection={connection} />;
  if (route === "maintenance") return <MaintenancePage request={request} managementId={managementId} connection={connection} />;
  if (route === "services") return <ServicesPage request={request} managementId={managementId} connection={connection} scopes={scopes} scopeId={scopeId} setScopeId={setScopeId} />;
  return <DashboardPage request={request} managementId={managementId} />;
}

export function ManagementSettings({ request, s3Connections, managementConnections, refreshConnections }: { request: Requester; s3Connections: S3ConnectionOption[]; managementConnections: ManagementConnection[]; refreshConnections: () => Promise<void> }) {
  useLocale();
  const [selectedId, setSelectedId] = useState(managementConnections[0]?.id || "");
  const selected = managementConnections.find((item) => item.id === selectedId) || managementConnections[0];

  useEffect(() => {
    if (!selectedId && managementConnections[0]) setSelectedId(managementConnections[0].id);
    if (selectedId && !managementConnections.some((item) => item.id === selectedId)) setSelectedId(managementConnections[0]?.id || "");
  }, [managementConnections, selectedId]);

  return (
    <div className="stack">
      <ConnectionSetup request={request} s3Connections={s3Connections} refreshConnections={refreshConnections} />
      <div><h2>{t("已配置 Management Connections")}</h2><ManagementConnectionTable items={managementConnections} selectedId={selected?.id || ""} onSelect={setSelectedId} /></div>
      {selected && <AccessPolicyEditor request={request} connection={selected} refreshConnections={refreshConnections} />}
      {selected && <OperationsHistory request={request} managementId={selected.id} />}
      <EvidenceDetails title={t("连接配置字段")} data={{ api: "/api/v1/management/connections", access_policy: "/api/v1/management/connections/{id}/access-policy", writes: "默认只读；开启 writes 必须提交 acknowledge_management_write=true，并由 permission flags/roots/prefixes 继续限权。", secret_policy: "admin_secret_ref is a server-side reference; plaintext credentials never render in the browser", endpoint_keys: ["master", "filer", "volume", "iceberg", "lance", "s3", "mq"] }} />
    </div>
  );
}

function DashboardPage({ request, managementId }: { request: Requester; managementId: string }) {
  const overview = useApi<ManagementEnvelope>(() => request(`/management/${managementId}/overview`), [managementId]);
  const services = useApi<ManagementEnvelope>(() => request(`/management/${managementId}/services`), [managementId]);
  const volumes = useApi<ManagementEnvelope>(() => request(`/management/${managementId}/volumes?limit=6`), [managementId]);
  const operations = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/operations?limit=6`), [managementId]);
  const admin = record(overview.data?.admin);
  const config = record(record(overview.data?.config).config_info);
  const serviceData = record(services.data);
  const volumeCounts = record(record(volumes.data).counts);
  const volumeItems = array(record(volumes.data).items).map(record);
  const physicalBytes = knownNumber(admin.total_size) ?? sumKnown(volumeItems.map((item) => item.size));
  const capacityBytes = firstKnownNumber([record(array(admin.volume_servers)[0]).disk_capacity, record(array(admin.tier_stats)[0]).disk_capacity]);
  const tierRows = array(admin.tier_stats).map(record);
  return (
    <section className="managementPage">
      <div className="managementHero">
        <div><p>{t("Control Plane Dashboard")}</p><h2>{t("集群概览")}</h2><span>{t("查看集群容量、服务与维护状态。")}</span></div>
        <StatusBadge state={overview.error ? "error" : overview.loading ? "unknown" : overview.data ? "reported" : "unknown"} label={overview.error ? "权限不足/不可达" : overview.loading ? "读取中" : "官方报告"} />
      </div>
      <div className="summaryGrid managementMetrics">
        <Metric label="Master" value={countOrUnknown(admin.master_nodes)} />
        <Metric label="Volume Server" value={countOrUnknown(admin.volume_servers)} />
        <Metric label="Filer" value={countOrUnknown(admin.filer_nodes)} />
        <Metric label="S3" value={countOrUnknown(admin.s3_nodes)} />
        <Metric label="Worker" value={displayValue(record(record(serviceData.plugin).data).worker_count)} />
        <Metric label="Physical data" value={formatBytesOrUnknown(physicalBytes)} />
        <Metric label="Disk capacity" value={formatBytesOrUnknown(capacityBytes)} />
        <Metric label="Volume chunks" value={displayValue(admin.total_chunks)} />
        <Metric label="Total volumes" value={displayValue(admin.total_volumes)} />
        <Metric label="Logical volumes" value={displayValue(volumeCounts.logical_volumes)} />
      </div>
      <div className="managementGrid twoColumn">
        <DataPanel title={t("集群服务")} loading={services.loading} error={services.error}><ServiceInventory data={services.data} /></DataPanel>
        <DataPanel title={t("容量边界")} loading={volumes.loading} error={volumes.error}>
          <div className="stateList">
            <StateRow label="SeaweedFS physical/storage" value={formatBytesOrUnknown(physicalBytes)} state={physicalBytes == null ? "unknown" : "reported"} />
            <StateRow label="Volume disk capacity" value={formatBytesOrUnknown(capacityBytes)} state={capacityBytes == null ? "unknown" : "reported"} />
            <StateRow label="Image logical bytes" value={t("Project Scope 页面读取")} state="unknown" />
            <StateRow label="Preview cache bytes" value={t("图片增强私有缓存")} state="unknown" />
          </div>
        </DataPanel>
      </div>
      <div className="managementGrid twoColumn">
        <DataPanel title={t("依赖状态")} loading={services.loading} error={services.error}><DependencyStates data={services.data} /></DataPanel>
        <DataPanel title={t("最近管理操作")} loading={operations.loading} error={operations.error}><OperationTable data={operations.data} /></DataPanel>
      </div>
      <div className="managementGrid twoColumn">
        <DataPanel title={t("容量层级")} loading={overview.loading} error={overview.error}><TierStatsTable rows={tierRows} /></DataPanel>
        <DataPanel title={t("趋势")} loading={overview.loading} error={overview.error}><TrendPanel data={admin.trends} /></DataPanel>
      </div>
      <DataPanel title={t("官方来源")} loading={overview.loading} error={overview.error}><SourceEvidence data={overview.data} extra={config} /></DataPanel>
    </section>
  );
}

function TopologyPage({ request, managementId }: { request: Requester; managementId: string }) {
  const topology = useApi<ManagementEnvelope>(() => request(`/management/${managementId}/topology`), [managementId]);
  const services = useApi<ManagementEnvelope>(() => request(`/management/${managementId}/services`), [managementId]);
  const workers = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/workers`), [managementId]);
  const health = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/services/health`), [managementId]);
  const topo = record(topology.data?.topology);
  const svc = record(services.data);
  const workerRows = array(record(record(workers.data).workers).data).map(record);
  return (
    <section className="managementPage">
      <DataPanel title={t("拓扑树")} loading={topology.loading || services.loading || workers.loading} error={topology.error || services.error || workers.error}>
        <div className="topologyTree">
          <TopologyGroup title={t("Master")} nodes={array(svc.master_nodes ?? topo.masters).map(record)} addressKey="address" leaderKey="is_leader" />
          <TopologyGroup title={t("Filer")} nodes={array(svc.filer_nodes).map(record)} addressKey="address" />
          <TopologyGroup title={t("S3")} nodes={array(svc.s3_nodes).map(record)} addressKey="address" />
          <TopologyGroup title={t("Volume Server")} nodes={array(svc.volume_servers ?? topo.volume_servers).map(record)} addressKey="address" />
          <TopologyGroup title={t("MQ Broker")} nodes={array(svc.message_brokers ?? topo.message_brokers).map(record)} addressKey="address" />
          <TopologyGroup title={t("Worker")} nodes={workerRows} addressKey="Address" />
        </div>
      </DataPanel>
      <DataPanel title={t("服务健康")} loading={health.loading} error={health.error}><ServiceHealthTable data={health.data} /></DataPanel>
      <EvidenceDetails title={t("官方 DTO / topology")} data={{ topology: topology.data, services: services.data, workers: workers.data, health: health.data }} />
    </section>
  );
}

function StoragePage({ request, managementId, connection }: { request: Requester; managementId: string; connection?: ManagementConnection }) {
  const [filters, setFilters] = useState({ collection: "", readonly: "", disk_type: "", limit: "100", cursor: "" });
  const [refreshKey, setRefreshKey] = useState(0);
  const volumeQuery = new URLSearchParams({ limit: String(Math.max(1, Math.min(1000, Number(filters.limit) || 100))) });
  if (filters.cursor) volumeQuery.set("cursor", filters.cursor);
  if (filters.collection) volumeQuery.set("collection", filters.collection);
  if (filters.readonly) volumeQuery.set("readonly", filters.readonly);
  if (filters.disk_type) volumeQuery.set("disk_type", filters.disk_type);
  const volumes = useApi<ManagementEnvelope>(() => request(`/management/${managementId}/volumes?${volumeQuery.toString()}`), [managementId, filters.collection, filters.readonly, filters.disk_type, filters.limit, filters.cursor, refreshKey]);
  const collections = useApi<ManagementEnvelope>(() => request(`/management/${managementId}/collections`), [managementId]);
  const ec = useApi<ManagementEnvelope>(() => request(`/management/${managementId}/ec`), [managementId]);
  const [notice, setNotice] = useState<Notice>({ error: "", result: null });
  const canVolumeWrite = can(connection, "volume.manage");
  const volumeRows = array(record(volumes.data).items).map(record);
  const collectionRows = array(record(collections.data).items).map(record);
  const ecRows = array(record(ec.data).items).map(record);
  const nextCursor = typeof record(volumes.data).next_cursor === "string" ? String(record(volumes.data).next_cursor) : "";
  async function volumeAction(fn: () => Promise<unknown>) {
    const result = await run(setNotice, fn);
    if (result) setRefreshKey((value) => value + 1);
  }
  return (
    <section className="managementPage">
      <div className="tabs staticTabs"><span>{t("Volumes")}</span><span>{t("Collections")}</span><span>{t("EC")}</span></div>
      <DataPanel title={t("Volumes")} loading={volumes.loading} error={volumes.error}>
        <div className="miniGrid"><label className="field">{t("Collection")}<input value={filters.collection} onChange={(event) => setFilters({ ...filters, cursor: "", collection: event.target.value })} placeholder={t("default or collection name")} /></label><label className="field">{t("Readonly")}<select value={filters.readonly} onChange={(event) => setFilters({ ...filters, cursor: "", readonly: event.target.value })}><option value="">{t("全部")}</option><option value="true">{t("readonly")}</option><option value="false">{t("writable")}</option></select></label><label className="field">{t("Disk type")}<input value={filters.disk_type} onChange={(event) => setFilters({ ...filters, cursor: "", disk_type: event.target.value })} placeholder={t("hdd / ssd")} /></label><label className="field">{t("Limit")}<input value={filters.limit} onChange={(event) => setFilters({ ...filters, cursor: "", limit: event.target.value })} /></label></div>
        <DataTable caption="SeaweedFS volume export" columns={["ID", "Collection", "Server", "Disk", "Size", "Files", "Garbage", "Readonly", "Maintenance"]} rows={volumeRows.map((item) => [displayValue(item.id), displayValue(item.collection || "default"), textCell(item.server), displayValue(item.disk_type), formatBytesOrUnknown(item.size), displayValue(item.file_count), percentOrUnknown(item.garbage_ratio), yesNoUnknown(item.read_only), <div className="inlineActions"><button type="button" disabled={!canVolumeWrite} title={canVolumeWrite ? t("提交 read-only 切换并读取 export 核验") : disabledTitle("volume.manage")} onClick={() => volumeAction(() => request(`/management/${managementId}/volumes/${encodeURIComponent(String(item.id))}/actions`, { method: "POST", body: JSON.stringify({ action: "read_only", server_id: String(item.server || ""), read_only: !Boolean(item.read_only) }) }))}>{t("readonly")}</button><button type="button" disabled={!canVolumeWrite} title={canVolumeWrite ? t("vacuum 结果需要操作历史核验") : disabledTitle("volume.manage")} onClick={() => volumeAction(() => request(`/management/${managementId}/volumes/${encodeURIComponent(String(item.id))}/actions`, { method: "POST", body: JSON.stringify({ action: "vacuum", server_id: String(item.server || "") }) }))}>{t("vacuum")}</button></div>])} />
        <PaginationBar firstDisabled={!filters.cursor} nextDisabled={!nextCursor} onFirst={() => setFilters({ ...filters, cursor: "" })} onNext={() => setFilters({ ...filters, cursor: nextCursor })} status={nextCursor ? t("有下一页") : t("无下一页或未知")} />
      </DataPanel>
      <div className="managementGrid twoColumn">
        <DataPanel title={t("Collections")} loading={collections.loading} error={collections.error}><DataTable caption="SeaweedFS collections" columns={["Name", "Logical volumes", "Physical replicas", "Size", "State"]} rows={collectionRows.map((item) => [displayValue(item.name), displayValue(item.logical_volumes), displayValue(item.physical_replicas), formatBytesOrUnknown(item.size_bytes), displayValue(item.state)])} /></DataPanel>
        <DataPanel title={t("Erasure Coding")} loading={ec.loading} error={ec.error}>
          <DataTable caption="SeaweedFS EC shards" columns={["Volume", "Server", "Shard", "State"]} rows={ecRows.map((item) => [displayValue(item.volume_id), textCell(item.server), displayValue(item.shard_id ?? item.id), displayValue(item.state ?? item.status)])} />
          {!ecRows.length && <StateRow label="EC" value={t("官方返回空 shard 列表；这才显示为空，不扩展推断其他 EC 状态")} state={record(ec.data).module_status === "supported" ? "empty_response" : "unknown"} />}
        </DataPanel>
      </div>
      <ResultPane error={notice.error} result={notice.result} />
      <EvidenceDetails title={t("Volumes raw DTO")} data={{ volumes: volumes.data, collections: collections.data, ec: ec.data }} />
    </section>
  );
}

function BucketsPage({ request, managementId, connection }: { request: Requester; managementId: string; connection?: ManagementConnection }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const buckets = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/buckets`), [managementId, refreshKey]);
  const [selected, setSelected] = useState("");
  const [createForm, setCreateForm] = useState({ name: "", region: "us-east-1", advanced: false, owner: "", quota_size: "", quota_unit: "GB", quota_enabled: false, versioning_enabled: false, object_lock_enabled: false, set_default_retention: false, object_lock_mode: "GOVERNANCE", object_lock_duration: "1" });
  const [bucketForm, setBucketForm] = useState({ owner: "", quota_size: "", quota_unit: "GB", quota_enabled: false });
  const [setting, setSetting] = useState({ versioning: "Suspended", object_lock_configuration: "{\n  \"ObjectLockEnabled\": \"Enabled\"\n}", lifecycle: "{\n  \"rules\": []\n}", policy: "{\n  \"Version\": \"2012-10-17\",\n  \"Statement\": []\n}" });
  const [notice, setNotice] = useState<Notice>({ error: "", result: null });
  const rows = array(buckets.data?.items).map(record);
  const canWrite = can(connection, "bucket.manage");
  const bucketDetail = useApi<Record<string, unknown>>(() => selected ? request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}`) : Promise.resolve({}), [managementId, selected, refreshKey]);
  const versioning = useApi<Record<string, unknown>>(() => selected ? request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/versioning`) : Promise.resolve({}), [managementId, selected, refreshKey]);
  const objectLock = useApi<Record<string, unknown>>(() => selected ? request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/object-lock`) : Promise.resolve({}), [managementId, selected, refreshKey]);
  const lifecycle = useApi<Record<string, unknown>>(() => selected ? request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/lifecycle`) : Promise.resolve({}), [managementId, selected, refreshKey]);
  const policy = useApi<Record<string, unknown>>(() => selected ? request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/policy`) : Promise.resolve({}), [managementId, selected, refreshKey]);
  const createInvalid = createForm.advanced && createForm.set_default_retention && (!Number.isInteger(Number(createForm.object_lock_duration)) || Number(createForm.object_lock_duration) < 1);
  useEffect(() => { setBucketForm({ owner: "", quota_size: "", quota_unit: "GB", quota_enabled: false }); }, [managementId, selected]);
  async function bucketWrite(fn: () => Promise<unknown>) {
    const result = await run(setNotice, fn);
    if (result) setRefreshKey((value) => value + 1);
  }
  return (
    <section className="managementPage">
        <DataPanel title={t("Bucket inventory")} loading={buckets.loading} error={buckets.error}>
          <p className="hint">{t("Select a bucket to inspect its status and edit its configuration below.")}</p>
          <DataTable caption="SeaweedFS buckets" columns={["Bucket", "Owner", "Logical", "Physical", "Versioning", "Lifecycle", "Policy"]} rows={rows.map((item) => [<button type="button" className="linkButton" onClick={() => setSelected(String(item.name || ""))}>{displayValue(item.name)}</button>, displayValue(item.owner), formatBytesOrUnknown(item.logical_size), formatBytesOrUnknown(item.physical_size), displayValue(item.versioning_status || "未知"), displayValue(item.lifecycle_rule_count), displayValue(item.policy_statement_count)])} />
          {!rows.length && <StateRow label="Bucket dynamic inventory" value={buckets.data ? t("空响应；不代表静态 S3 identity 为 0") : t("未知")} state={buckets.data ? "empty_response" : "unknown"} />}
        </DataPanel>
        <details className="panel actionDisclosure">
          <summary>{t("Create a bucket")}<span className="hint">{t("Choose a name and region. Optional initial settings are available under advanced creation.")}</span></summary>
          <div className="panelContent">
          <p className="hint">{t("Bucket changes require write access. Deletion is available only after the bucket is confirmed empty, including versions and unfinished uploads.")}</p>
          <label className="field">{t("Bucket")}<input value={createForm.name} onChange={(event) => setCreateForm({ ...createForm, name: event.target.value })} placeholder={t("swc-management-example")} /></label>
          <label className="field">{t("Region")}<input value={createForm.region} onChange={(event) => setCreateForm({ ...createForm, region: event.target.value })} placeholder={t("us-east-1")} /></label>
          <label className="ack"><input type="checkbox" checked={createForm.advanced} onChange={(event) => setCreateForm({ ...createForm, advanced: event.target.checked })} />{" "}{t("高级创建（复合设置非原子；partial / needs_review 后请查操作历史，不自动重发）")}</label>
          {createForm.advanced && <div className="stateList">
            <label className="ack"><input type="checkbox" checked={createForm.versioning_enabled || createForm.object_lock_enabled} disabled={createForm.object_lock_enabled} onChange={(event) => setCreateForm({ ...createForm, versioning_enabled: event.target.checked })} />{" "}{t("创建后启用 versioning")}{createForm.object_lock_enabled ? "（Object Lock 强制启用）" : ""}</label>
            <label className="ack"><input type="checkbox" checked={createForm.object_lock_enabled} onChange={(event) => setCreateForm({ ...createForm, object_lock_enabled: event.target.checked, versioning_enabled: event.target.checked ? true : createForm.versioning_enabled })} />{" "}{t("Object Lock enabled")}</label>
            <label className="ack"><input type="checkbox" checked={createForm.set_default_retention} onChange={(event) => setCreateForm({ ...createForm, set_default_retention: event.target.checked, object_lock_enabled: event.target.checked ? true : createForm.object_lock_enabled, versioning_enabled: event.target.checked ? true : createForm.versioning_enabled })} />{" "}{t("设置默认 retention")}</label>
            <div className="miniGrid"><label className="field">{t("Retention mode")}<select value={createForm.object_lock_mode} onChange={(event) => setCreateForm({ ...createForm, object_lock_mode: event.target.value })}><option value="GOVERNANCE">{t("GOVERNANCE")}</option><option value="COMPLIANCE">{t("COMPLIANCE")}</option></select></label><label className="field">{t("Duration days")}<input type="number" min="1" value={createForm.object_lock_duration} onChange={(event) => setCreateForm({ ...createForm, object_lock_duration: event.target.value })} /></label></div>
            <label className="field">{t("Owner")}<input value={createForm.owner} onChange={(event) => setCreateForm({ ...createForm, owner: event.target.value })} /></label>
            <div className="miniGrid"><label className="field">{t("Quota")}<input value={createForm.quota_size} onChange={(event) => setCreateForm({ ...createForm, quota_size: event.target.value })} /></label><label className="field">{t("Unit")}<select value={createForm.quota_unit} onChange={(event) => setCreateForm({ ...createForm, quota_unit: event.target.value })}><option value="B">{t("B")}</option><option value="KB">{t("KB")}</option><option value="MB">{t("MB")}</option><option value="GB">{t("GB")}</option><option value="TB">{t("TB")}</option></select></label></div>
            <label className="ack"><input type="checkbox" checked={createForm.quota_enabled} onChange={(event) => setCreateForm({ ...createForm, quota_enabled: event.target.checked })} />{" "}{t("quota enabled")}</label>
            {createInvalid && <div className="notice error">{t("Default retention duration days 必须是大于等于 1 的整数。")}</div>}
          </div>}
          <div className="toolbar"><button type="button" disabled={!canWrite || !createForm.name || createInvalid} title={canWrite ? createForm.advanced ? t("高级创建提交复合设置；partial/needs_review 不会自动重发") : t("普通创建只提交 name/region；quota/owner 单独保存") : disabledTitle("bucket.manage")} onClick={() => bucketWrite(() => request(`/management/${managementId}/buckets`, { method: "POST", body: JSON.stringify(bucketCreatePayload(createForm)) }))}>{createForm.advanced ? t("高级创建 Bucket") : t("创建 Bucket")}</button></div>
          <StateRow label="写权限" value={canWrite ? t("已授权") : t("未授权，动作禁用")} state={canWrite ? "reported" : "permission_denied"} />
          </div>
        </details>
      <div className="sectionHeading">
        <div><p className="eyebrow">{t("Bucket configuration")}</p><h2>{selected || t("Select a bucket")}</h2><p className="hint">{selected ? t("These settings apply to the selected bucket. Each section is saved separately.") : t("Select a bucket from the inventory above to inspect and configure it.")}</p></div>
        <StatusBadge state={canWrite ? "reported" : "permission_denied"} label={canWrite ? "已授权" : "Read-only access"} />
      </div>
        <DataPanel title={t("Bucket status")} loading={bucketDetail.loading || versioning.loading || objectLock.loading || lifecycle.loading || policy.loading} error={bucketDetail.error || versioning.error || objectLock.error || lifecycle.error || policy.error}>
          <dl className="bucketFacts">
            <div><dt>{t("Versioning")}</dt><dd>{displayValue(versioning.data?.status)}</dd></div>
            <div><dt>{t("Object lock")}</dt><dd>{displayValue(record(objectLock.data?.object_lock_configuration).ObjectLockEnabled)}</dd></div>
            <div><dt>{t("Lifecycle rules")}</dt><dd>{displayValue(countOnlyIfList(record(lifecycle.data).Rules ?? record(lifecycle.data).rules ?? record(lifecycle.data).lifecycle))}</dd></div>
            <div><dt>{t("Bucket policy statements")}</dt><dd>{displayValue(countOnlyIfList(record(policy.data).Statement ?? record(policy.data).statement ?? record(policy.data).policy))}</dd></div>
          </dl>
        </DataPanel>
      <div className="managementGrid twoColumn">
        <div className="panel inlinePanel">
          <h2>{t("Versioning / Object Lock")}</h2>
          <section className="formSection">
          <h3>{t("Versioning")}</h3><p className="hint">{t("Keep previous object versions when objects are replaced or deleted.")}</p>
          <label className="field">{t("Versioning")}<select value={setting.versioning} onChange={(event) => setSetting({ ...setting, versioning: event.target.value })}><option value="Suspended">{t("Suspended")}</option><option value="Enabled">{t("Enabled")}</option></select></label>
          <div className="toolbar"><button type="button" disabled={!canWrite || !selected} onClick={() => bucketWrite(() => request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/versioning`, { method: "PUT", body: JSON.stringify({ status: setting.versioning }) }))}>{t("保存 versioning")}</button></div>
          </section>
          <section className="formSection">
          <h3>{t("Object lock")}</h3><p className="hint">{t("Configure retention protection for object versions. Availability depends on the storage service.")}</p>
          <label className="field">{t("Object lock JSON")}<textarea value={setting.object_lock_configuration} onChange={(event) => setSetting({ ...setting, object_lock_configuration: event.target.value })} /></label>
          <div className="toolbar"><button type="button" disabled={!canWrite || !selected} onClick={() => bucketWrite(() => request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/object-lock`, { method: "PUT", body: JSON.stringify({ object_lock_configuration: parseJsonObject(setting.object_lock_configuration, "object_lock_configuration") }) }))}>{t("保存 object lock")}</button></div>
          </section>
        </div>
        <div className="panel inlinePanel">
          <h2>{t("Lifecycle / Bucket Policy")}</h2>
          <section className="formSection">
          <h3>{t("Lifecycle rules")}</h3><p className="hint">{t("Define when objects expire or move between storage tiers. Load the current value before editing.")}</p>
          <label className="field">{t("Lifecycle JSON")}<textarea value={setting.lifecycle} onChange={(event) => setSetting({ ...setting, lifecycle: event.target.value })} /></label>
          <div className="toolbar"><button type="button" disabled={!lifecycle.data} onClick={() => setSetting({ ...setting, lifecycle: JSON.stringify(lifecycle.data, null, 2) })}>{t("填入读取值")}</button><button type="button" disabled={!canWrite || !selected} onClick={() => bucketWrite(() => request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/lifecycle`, { method: "PUT", body: JSON.stringify({ lifecycle: parseJsonObject(setting.lifecycle, "lifecycle") }) }))}>{t("保存 lifecycle")}</button><button type="button" disabled={!canWrite || !selected} onClick={() => bucketWrite(() => request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/lifecycle`, { method: "DELETE" }))}>{t("删除 lifecycle")}</button></div>
          </section>
          <section className="formSection">
          <h3>{t("Bucket policy")}</h3><p className="hint">{t("Control who can access this bucket and which actions they can perform.")}</p>
          <label className="field">{t("Bucket policy JSON")}<textarea value={setting.policy} onChange={(event) => setSetting({ ...setting, policy: event.target.value })} /></label>
          <div className="toolbar"><button type="button" disabled={!policy.data} onClick={() => setSetting({ ...setting, policy: JSON.stringify(policy.data, null, 2) })}>{t("填入读取值")}</button><button type="button" disabled={!canWrite || !selected} onClick={() => bucketWrite(() => request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/policy`, { method: "PUT", body: JSON.stringify({ policy: parseJsonObject(setting.policy, "policy") }) }))}>{t("保存 policy")}</button><button type="button" disabled={!canWrite || !selected} onClick={() => bucketWrite(() => request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/policy`, { method: "DELETE" }))}>{t("删除 policy")}</button></div>
          </section>
        </div>
      </div>
      <details className="panel actionDisclosure">
        <summary>{t("Owner, quota and deletion")}<span className="hint">{t("These actions apply only to the selected bucket.")}</span></summary>
        <div className="panelContent">
          <div className="operationSections">
            <section className="formSection">
              <h3>{t("Owner")}</h3>
              <label className="field">{t("Owner")}<input value={bucketForm.owner} onChange={(event) => setBucketForm({ ...bucketForm, owner: event.target.value })} /></label>
              <div className="toolbar"><button type="button" disabled={!canWrite || !selected || !bucketForm.owner} onClick={() => bucketWrite(() => request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/owner`, { method: "PUT", body: JSON.stringify({ owner: bucketForm.owner }) }))}>{t("保存 owner")}</button></div>
            </section>
            <section className="formSection">
              <h3>{t("Quota")}</h3>
              <div className="miniGrid"><label className="field">{t("Quota")}<input value={bucketForm.quota_size} onChange={(event) => setBucketForm({ ...bucketForm, quota_size: event.target.value })} /></label><label className="field">{t("Unit")}<select value={bucketForm.quota_unit} onChange={(event) => setBucketForm({ ...bucketForm, quota_unit: event.target.value })}><option value="B">{t("B")}</option><option value="KB">{t("KB")}</option><option value="MB">{t("MB")}</option><option value="GB">{t("GB")}</option><option value="TB">{t("TB")}</option></select></label></div>
              <label className="ack"><input type="checkbox" checked={bucketForm.quota_enabled} onChange={(event) => setBucketForm({ ...bucketForm, quota_enabled: event.target.checked })} />{t("quota enabled")}</label>
              <div className="toolbar"><button type="button" disabled={!canWrite || !selected || !bucketForm.quota_size} onClick={() => bucketWrite(() => request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}/quota`, { method: "PUT", body: JSON.stringify({ quota_size: Number(bucketForm.quota_size), quota_unit: bucketForm.quota_unit, quota_enabled: bucketForm.quota_enabled }) }))}>{t("保存 quota")}</button></div>
            </section>
          </div>
          <section className="formSection">
            <h3>{t("删除空 Bucket")}</h3><p className="hint">{t("Bucket changes require write access. Deletion is available only after the bucket is confirmed empty, including versions and unfinished uploads.")}</p>
            <div className="toolbar"><button type="button" disabled={!canWrite || !selected} title={t("删除需要后端确认 bucket 为空、无版本历史、无 multipart 上传")} onClick={() => bucketWrite(() => request(`/management/${managementId}/buckets/${encodeURIComponent(selected)}`, { method: "DELETE" }))}>{t("删除空 Bucket")}</button></div>
          </section>
        </div>
      </details>
      <ResultPane error={notice.error} result={notice.result} />
      <EvidenceDetails title={t("Bucket raw DTO")} data={{ list: buckets.data, detail: bucketDetail.data, versioning: versioning.data, objectLock: objectLock.data, lifecycle: lifecycle.data, policy: policy.data }} />
    </section>
  );
}

function FilesPage({ request, managementId, connection }: { request: Requester; managementId: string; connection?: ManagementConnection }) {
  const [path, setPath] = useState("/");
  const [cursor, setCursor] = useState("");
  const [refreshKey, setRefreshKey] = useState(0);
  const [folderName, setFolderName] = useState("");
  const [deleteForm, setDeleteForm] = useState<{ path: string; is_directory?: boolean }>({ path: "" });
  const [deletePreview, setDeletePreview] = useState<{ loading: boolean; error: string; data: Record<string, unknown> | null }>({ loading: false, error: "", data: null });
  const [renameForm, setRenameForm] = useState({ source_path: "", target_path: "" });
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [notice, setNotice] = useState<Notice>({ error: "", result: null });
  const fileQuery = new URLSearchParams({ path, limit: "100" });
  if (cursor) fileQuery.set("cursor", cursor);
  const files = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/files?${fileQuery.toString()}`), [managementId, path, cursor, refreshKey]);
  const entries = array(files.data?.items).map(record);
  const canFileWrite = can(connection, "file.manage");
  const nextCursor = typeof files.data?.next_cursor === "string" ? files.data.next_cursor : "";
  async function fileWrite(fn: () => Promise<unknown>) {
    const result = await run(setNotice, fn);
    if (result) setRefreshKey((value) => value + 1);
  }
  function selectEntry(item: Record<string, unknown>) {
    const full = String(item.full_path || item.FullPath || item.path || "");
    return full || (path === "/" ? `/${String(item.name || item.Name || "")}` : `${path.replace(/\/$/, "")}/${String(item.name || item.Name || "")}`);
  }
  function protectedFilePath(value: string) {
    return value === "/etc" || value.startsWith("/etc/") || value === "/topics" || value.startsWith("/topics/");
  }
  async function upload() {
    if (!uploadFile) throw new Error("请选择上传文件。");
    const form = new FormData();
    form.append("file", uploadFile);
    return request(`/management/${managementId}/files/upload?path=${encodeURIComponent(path)}`, { method: "POST", body: form });
  }
  async function loadDeletePreview() {
    const target = deleteForm.path.trim();
    if (!target) return;
    setDeletePreview({ loading: true, error: "", data: null });
    try {
      const data = await request<Record<string, unknown>>(`/management/${managementId}/files/delete-preview?path=${encodeURIComponent(target)}`);
      setDeletePreview({ loading: false, error: "", data });
    } catch (err) {
      setDeletePreview({ loading: false, error: String((err as Error).message || err), data: null });
    }
  }
  function deletePreviewReady() {
    return Boolean(deletePreview.data?.preview_hash && deletePreview.data?.complete === true && deletePreview.data?.truncated !== true && deletePreview.data?.path === deleteForm.path.trim());
  }
  function deleteRequiresPreview() {
    return deleteForm.is_directory !== false;
  }
  function deletePayload() {
    if (deleteRequiresPreview() && deletePreviewReady()) return { path: deleteForm.path.trim(), confirm_recursive: true, recursive_preview_path: deleteForm.path.trim(), recursive_preview_hash: deletePreview.data?.preview_hash };
    return { path: deleteForm.path.trim() };
  }
  return (
    <section className="managementPage">
      <div className="managementHero"><div><p>{t("Filer / Object Browser")}</p><h2>{t("文件对象浏览")}</h2><span>{t("Browse Filer directories and files. Protected system paths are shown for reference only.")}</span></div><StatusBadge state={files.error ? "error" : files.data ? "reported" : "unknown"} label={files.error ? "读取失败" : files.data ? "官方报告" : "未知"} /></div>
      <div className="managementGrid browseLayout">
        <DataPanel title={t("Raw Filer files")} loading={files.loading} error={files.error}>
          <label className="field">{t("路径")}<input value={path} onChange={(event) => { setCursor(""); setPath(event.target.value || "/"); }} /></label>
          <DataTable caption="Filer directory entries" columns={["Name", "Directory", "Protected", "Size", "Modified", "Actions"]} rows={entries.map((item) => { const name = String(item.name || item.Name || ""); const child = selectEntry(item); const directory = Boolean(item.is_directory ?? item.IsDirectory); const protectedPath = Boolean(item.protected) || protectedFilePath(child); const s3RawBytes = child.startsWith("/buckets/") && !directory; return [textCell(name), yesNoUnknown(directory), protectedPath ? <StatusBadge state="permission_denied" label="保护" /> : t("No"), formatBytesOrUnknown(item.size ?? item.FileSize), displayValue(item.mtime ?? item.Mtime), <div className="inlineActions"><button type="button" disabled={protectedPath} title={protectedPath ? t("受保护 Filer 系统路径仅显示元数据") : ""} onClick={() => directory ? (setCursor(""), setPath(child)) : setRenameForm({ ...renameForm, source_path: child })}>{directory ? t("打开") : t("选择")}</button><button type="button" disabled={protectedPath} title={protectedPath ? t("受保护路径禁止写操作") : ""} onClick={() => { setDeleteForm({ path: child, is_directory: directory }); setDeletePreview({ loading: false, error: "", data: null }); }}>{t("选择删除")}</button>{!directory && !protectedPath && !s3RawBytes && <a className="buttonLink" href={`/api/v1/management/${managementId}/files/download?path=${encodeURIComponent(child)}`}>{t("下载")}</a>}{s3RawBytes && <span className="hint">{t("S3 scoped APIs")}</span>}</div>]; })} />
          {!entries.length && <StateRow label="Files" value={files.data ? t("空响应") : t("未知")} state={files.data ? "empty_response" : "unknown"} />}
          <PaginationBar firstDisabled={!cursor} nextDisabled={!nextCursor} onFirst={() => setCursor("")} onNext={() => setCursor(nextCursor)} status={nextCursor ? t("有下一页") : t("无下一页或未知")} />
        </DataPanel>
        <details className="panel actionDisclosure" open>
          <summary>{t("文件写入守卫")}<span className="hint">{canFileWrite ? t("Files may be changed only within approved write paths. Preview directory contents before confirming deletion.") : t("Read-only access. Write actions are disabled for this connection.")}</span></summary>
          <div className="panelContent">
          <div className="operationSections">
            <section className="formSection">
              <h3>{t("创建目录")}</h3><p className="hint">{t("New folders are created inside the current path.")}</p>
              <label className="field">{t("新目录名")}<input value={folderName} onChange={(event) => setFolderName(event.target.value)} /></label>
              <div className="toolbar"><button type="button" disabled={!canFileWrite || !folderName || protectedFilePath(path)} title={canFileWrite ? t("创建目录后 readback") : disabledTitle("file.manage")} onClick={() => fileWrite(() => request(`/management/${managementId}/files/mkdir`, { method: "POST", body: JSON.stringify({ path, folder_name: folderName }) }))}>{t("创建目录")}</button></div>
            </section>
            <section className="formSection">
              <h3>{t("上传文件")}</h3><p className="hint">{t("The selected file is uploaded to the current path.")}</p>
              <label className="field">{t("上传文件")}<input type="file" onChange={(event) => setUploadFile(event.target.files?.[0] || null)} /></label>
              <div className="toolbar"><button type="button" disabled={!canFileWrite || !uploadFile || protectedFilePath(path)} onClick={() => fileWrite(upload)}>{t("上传到当前路径")}</button></div>
            </section>
            <section className="formSection">
              <h3>{t("重命名/移动")}</h3>
              <div className="miniGrid"><label className="field">{t("Source path")}<input value={renameForm.source_path} onChange={(event) => setRenameForm({ ...renameForm, source_path: event.target.value })} /></label><label className="field">{t("Target path")}<input value={renameForm.target_path} onChange={(event) => setRenameForm({ ...renameForm, target_path: event.target.value })} /></label></div>
              <div className="toolbar"><button type="button" disabled={!canFileWrite || !renameForm.source_path || !renameForm.target_path || protectedFilePath(renameForm.source_path) || protectedFilePath(renameForm.target_path)} onClick={() => fileWrite(() => request(`/management/${managementId}/files/rename`, { method: "POST", body: JSON.stringify({ source_path: renameForm.source_path, target_path: renameForm.target_path }) }))}>{t("重命名/移动")}</button></div>
            </section>
            <section className="formSection">
              <h3>{t("删除路径")}</h3>
              <label className="field">{t("删除路径")}<input value={deleteForm.path} onChange={(event) => { setDeleteForm({ path: event.target.value }); setDeletePreview({ loading: false, error: "", data: null }); }} placeholder={t("/safe/path/file-or-dir")} /></label>
              <div className="toolbar"><button type="button" disabled={!deleteForm.path || protectedFilePath(deleteForm.path) || deletePreview.loading} onClick={loadDeletePreview}>{t("读取删除预览")}</button><button type="button" disabled={!canFileWrite || !deleteForm.path || protectedFilePath(deleteForm.path) || (deleteRequiresPreview() && !deletePreviewReady())} onClick={() => fileWrite(() => request(`/management/${managementId}/files/delete`, { method: "POST", body: JSON.stringify(deletePayload()) }))}>{t("确认删除")}</button></div>
              <DeletePreviewPanel preview={deletePreview} />
            </section>
          </div>
          <EvidenceDetails title={t("Protected paths")} data={{ "/etc / /topics": t("受保护，仅显示元数据；不允许打开/下载/写入"), "/buckets": t("可目录浏览 metadata；raw S3 bytes 使用 scoped object APIs") }} />
          </div>
        </details>
      </div>
      <ResultPane error={notice.error} result={notice.result} />
      <EvidenceDetails title={t("Files raw DTO")} data={files.data} />
    </section>
  );
}

function DeletePreviewPanel({ preview }: { preview: { loading: boolean; error: string; data: Record<string, unknown> | null } }) {
  if (preview.loading) return <div className="notice">{t("正在读取删除预览…")}</div>;
  if (preview.error) return <div className="notice error">{preview.error}</div>;
  if (!preview.data) return <p className="hint">{t("先读取删除预览；目录删除必须使用后端返回的 preview_hash。")}</p>;
  const rows = array(preview.data.items).map(record);
  const complete = Boolean(preview.data.preview_hash && preview.data.complete === true && preview.data.truncated !== true);
  return (
    <div className="stack">
      <div className="stateList">
        <StateRow label="Preview path" value={displayValue(preview.data.path)} state={preview.data.path ? "reported" : "unknown"} />
        <StateRow label="Preview hash" value={displayValue(preview.data.preview_hash)} state={preview.data.preview_hash ? "reported" : "unknown"} />
        <StateRow label="Entry count" value={displayValue(preview.data.entry_count)} state={preview.data.entry_count == null ? "unknown" : "reported"} />
        <StateRow label="Truncated" value={yesNoUnknown(preview.data.truncated)} state={preview.data.truncated === true ? "needs_review" : preview.data.truncated === false ? "reported" : "unknown"} />
        <StateRow label="Preview complete" value={complete ? t("完整，可确认") : t("截断或缺少 hash，不允许提交")} state={complete ? "confirmed" : "needs_review"} />
      </div>
      <DataTable caption="Delete preview entries" columns={["Name", "Directory", "Protected", "Size"]} rows={rows.slice(0, 20).map((item) => [displayValue(item.name ?? item.Name), yesNoUnknown(item.is_directory ?? item.IsDirectory), yesNoUnknown(item.protected), formatBytesOrUnknown(item.size ?? item.FileSize)])} />
    </div>
  );
}

function ObjectsPage({ request, managementId, connection, scopes, scopeId, setScopeId, onObjectSelected }: { request: Requester; managementId: string; connection?: ManagementConnection; scopes: StorageScopeOption[]; scopeId: string; setScopeId?: (value: string) => void; onObjectSelected?: (objectId: string) => void }) {
  const associatedScopes = scopes.filter((scope) => connection?.s3_connection_id && scope.connection_id === connection.s3_connection_id);
  const selectedScope = associatedScopes.find((scope) => scope.id === scopeId);
  const selectedAnyScope = scopes.find((scope) => scope.id === scopeId);
  const [prefix, setPrefix] = useState(selectedScope?.prefix || "");
  const [delimiter, setDelimiter] = useState("/");
  const [limit, setLimit] = useState("100");
  const [cursor, setCursor] = useState("");
  const [notice, setNotice] = useState<Notice>({ error: "", result: null });
  const [selectedObject, setSelectedObject] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    if ((!scopeId || !associatedScopes.some((scope) => scope.id === scopeId)) && associatedScopes[0] && setScopeId) setScopeId(associatedScopes[0].id);
  }, [associatedScopes, scopeId, setScopeId]);

  useEffect(() => {
    setCursor("");
    setSelectedObject(null);
    setPrefix(selectedScope?.prefix || "");
  }, [selectedScope?.id]);

  const query = new URLSearchParams({ scope_id: scopeId, prefix, delimiter, limit: String(Math.max(1, Math.min(1000, Number(limit) || 100))) });
  if (cursor) query.set("cursor", cursor);
  const objects = useApi<Record<string, unknown>>(() => selectedScope ? request(`/management/${managementId}/objects?${query.toString()}`) : Promise.resolve({}), [managementId, scopeId, prefix, delimiter, limit, cursor, selectedScope?.id]);
  const items = array(objects.data?.items).map(record);
  const folders = array(objects.data?.folders).map((item) => String(item));
  const nextCursor = typeof objects.data?.next_cursor === "string" ? objects.data.next_cursor : "";
  const mismatch = Boolean(scopeId && selectedAnyScope && !selectedScope);

  async function selectKey(key: string) {
    const result = await request<Record<string, unknown>>(`/management/${managementId}/objects/select`, { method: "POST", body: JSON.stringify({ scope_id: scopeId, key }) });
    const obj = record(result.object);
    setSelectedObject(obj);
    if (typeof obj.id === "string" && onObjectSelected) onObjectSelected(obj.id);
    return result;
  }

  function downloadHref(item: Record<string, unknown>) {
    const params = new URLSearchParams({ scope_id: scopeId, key: String(item.key || "") });
    if (typeof item.version_id === "string" && item.version_id) params.set("version_id", item.version_id);
    return `/api/v1/management/${encodeURIComponent(managementId)}/objects/download?${params.toString()}`;
  }

  return (
    <section className="managementPage">
      <div className="managementHero"><div><p>{t("Live S3 Object Browser")}</p><h2>{t("实时对象浏览")}</h2><span>{t("在已授权存储范围内浏览对象。")}</span></div><StatusBadge state={mismatch ? "permission_denied" : selectedScope ? "reported" : "not_configured"} label={mismatch ? "权限不足" : selectedScope ? "已绑定" : "未配置"} /></div>
      <div className="managementGrid twoColumn">
        <DataPanel title={t("授权范围")} loading={false} error="">
          <p className="hint">{t("Choose a storage scope to see its bucket and authorized prefix. Browsing stays within this boundary.")}</p>
          <label className="field">{t("切换 Scope")}<select value={scopeId} onChange={(event) => setScopeId?.(event.target.value)}><option value="">{t("未选择")}</option>{associatedScopes.map((scope) => <option key={scope.id} value={scope.id}>{scope.display_name || `${scope.bucket}/${scope.prefix || ""}`}</option>)}</select></label>
          <dl className="contextFacts">
            <dt>{t("Bucket")}</dt><dd>{displayValue(selectedScope?.bucket)}</dd>
            <dt>{t("Root prefix")}</dt><dd>{selectedScope ? selectedScope.prefix || t("bucket root") : t("未知")}</dd>
            <dt>{t("当前前缀")}</dt><dd>{selectedScope ? prefix || selectedScope.prefix || t("bucket root") : t("未知")}</dd>
          </dl>
          <EvidenceDetails title={t("Scope 绑定证据")} data={{ management_s3_connection: connection?.s3_connection_id, selected_scope_connection: selectedAnyScope?.connection_id, selected_scope_id: scopeId, authorized_scope: selectedScope || null, mismatch }} />
        </DataPanel>
        <div className="panel inlinePanel"><h2>{t("浏览条件")}</h2><label className="field">{t("对象前缀")}<input value={prefix} onChange={(event) => { setCursor(""); setPrefix(event.target.value); }} placeholder={selectedScope?.prefix || "scope prefix"} /></label><div className="miniGrid"><label className="field">{t("分隔符")}<select value={delimiter} onChange={(event) => { setCursor(""); setDelimiter(event.target.value); }}><option value="/">/</option><option value="">{t("不分组")}</option></select></label><label className="field">{t("每页数量")}<input value={limit} onChange={(event) => { setCursor(""); setLimit(event.target.value); }} /></label></div><p className="hint">{t("只能浏览所选 Scope 授权范围内的对象。")}</p></div>
      </div>
      <DataPanel title={t("Folders / objects")} loading={objects.loading} error={objects.error}>
        {folders.length ? <DataTable caption="Live S3 prefixes" columns={["Folder", "Action"]} rows={folders.map((folder) => [<code>{folder}</code>, <button type="button" onClick={() => { setCursor(""); setPrefix(folder); }}>{t("打开")}</button>])} /> : null}
        <DataTable caption="Live S3 objects" columns={["Key", "Size", "ETag", "Version", "Actions"]} rows={items.map((item) => [<code>{displayValue(item.key)}</code>, formatBytesOrUnknown(item.size), displayValue(item.etag), displayValue(item.version_id), <div className="inlineActions"><button type="button" onClick={() => run(setNotice, () => selectKey(String(item.key || "")))}>{t("查看详情")}</button><a className="buttonLink" href={downloadHref(item)}>{t("下载")}</a></div>])} />
        {!items.length && <StateRow label="Objects" value={objects.data ? t("空响应") : t("未知")} state={objects.data ? "empty_response" : "unknown"} />}
        <PaginationBar firstDisabled={!cursor} nextDisabled={!nextCursor} onFirst={() => setCursor("")} onNext={() => setCursor(nextCursor)} status={t("Total: {count}", { count: objects.data?.total == null ? t("未知") : String(objects.data.total) })} />
      </DataPanel>
      {selectedObject && <div className="panel inlinePanel"><h2>{t("对象详情")}</h2><div className="stateList"><StateRow label="Object ID" value={displayValue(selectedObject.id)} state={selectedObject.id ? "reported" : "unknown"} /><StateRow label="Key" value={displayValue(selectedObject.key)} state={selectedObject.key ? "reported" : "unknown"} /><StateRow label="Version" value={displayValue(selectedObject.version_id)} state={selectedObject.version_id ? "reported" : "unknown"} /></div><ObjectVersionDeletePanel request={request} managementId={managementId} connection={connection} scopeId={scopeId} scope={selectedScope} object={selectedObject} /><div className="toolbar"><button type="button" onClick={() => { location.hash = "operations"; }}>{t("对象操作")}</button><button type="button" onClick={() => { location.hash = "assets"; }}>{t("资产检索")}</button></div></div>}
      <ResultPane error={notice.error} result={notice.result} />
      <EvidenceDetails title={t("对象 DTO / cursor")} data={{ list: objects.data, selected: selectedObject, api: "/api/v1/management/{id}/objects" }} />
    </section>
  );
}

function ObjectVersionDeletePanel({ request, managementId, connection, scopeId, scope, object }: { request: Requester; managementId: string; connection?: ManagementConnection; scopeId: string; scope?: StorageScopeOption; object: Record<string, unknown> }) {
  const [target, setTarget] = useState({ key: String(object.key || ""), version_id: typeof object.version_id === "string" ? object.version_id : "" });
  const [info, setInfo] = useState<AsyncState<Record<string, unknown>>>({ loading: false, error: "", data: null });
  const [probe, setProbe] = useState<AsyncState<Record<string, unknown>>>({ loading: false, error: "", data: null });
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [ackReferences, setAckReferences] = useState(false);
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [probeIdempotencyKey, setProbeIdempotencyKey] = useState("");
  const [deleteIntentDispatched, setDeleteIntentDispatched] = useState(false);
  const [notice, setNotice] = useState<Notice>({ error: "", result: null });
  const generation = useRef(0);
  const deleteDispatchedRef = useRef(false);

  useEffect(() => {
    generation.current += 1;
    deleteDispatchedRef.current = false;
    setTarget({ key: String(object.key || ""), version_id: typeof object.version_id === "string" ? object.version_id : "" });
    setInfo({ loading: false, error: "", data: null });
    setProbe({ loading: false, error: "", data: null });
    setConfirmDelete(false);
    setAckReferences(false);
    setIdempotencyKey("");
    setProbeIdempotencyKey("");
    setDeleteIntentDispatched(false);
    setNotice({ error: "", result: null });
  }, [scopeId, object.key, object.version_id]);

  function updateTarget(next: Partial<{ key: string; version_id: string }>) {
    generation.current += 1;
    deleteDispatchedRef.current = false;
    setTarget((value) => ({ ...value, ...next }));
    setInfo({ loading: false, error: "", data: null });
    setConfirmDelete(false);
    setAckReferences(false);
    setIdempotencyKey("");
    setDeleteIntentDispatched(false);
  }

  async function fetchVersionInfo(captured = target, capturedScopeId = scopeId) {
    const myGeneration = generation.current;
    setInfo({ loading: true, error: "", data: null });
    setConfirmDelete(false);
    setAckReferences(false);
    try {
      const params = new URLSearchParams({ scope_id: capturedScopeId, key: captured.key });
      if (captured.version_id.trim()) params.set("version_id", captured.version_id.trim());
      const data = await request<Record<string, unknown>>(`/management/${managementId}/objects/version-info?${params.toString()}`);
      if (generation.current !== myGeneration || capturedScopeId !== scopeId || captured.key !== target.key || captured.version_id !== target.version_id) return;
      setInfo({ loading: false, error: "", data });
      if (!deleteDispatchedRef.current) setIdempotencyKey((value) => value || randomIntentKey());
    } catch (err) {
      if (generation.current !== myGeneration || capturedScopeId !== scopeId || captured.key !== target.key || captured.version_id !== target.version_id) return;
      setInfo({ loading: false, error: String((err as Error).message || err), data: null });
      if (!deleteDispatchedRef.current) setIdempotencyKey("");
    }
  }

  async function readVersionInfo() {
    await fetchVersionInfo({ ...target }, scopeId);
  }

  async function checkConditionalDelete() {
    const key = probeIdempotencyKey || randomIntentKey();
    const captured = { ...target };
    const capturedScopeId = scopeId;
    const myGeneration = generation.current;
    setProbeIdempotencyKey(key);
    setProbe({ loading: true, error: "", data: probe.data });
    try {
      const data = await request<Record<string, unknown>>(`/management/${managementId}/objects/check-conditional-delete`, { method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify({ scope_id: capturedScopeId }) });
      if (generation.current !== myGeneration || capturedScopeId !== scopeId || captured.key !== target.key || captured.version_id !== target.version_id) return;
      setProbe({ loading: false, error: "", data });
      if (!deleteDispatchedRef.current && (data.status === "confirmed" || data.capability_status === "supported")) await fetchVersionInfo(captured, capturedScopeId);
    } catch (err) {
      if (generation.current !== myGeneration || capturedScopeId !== scopeId || captured.key !== target.key || captured.version_id !== target.version_id) return;
      setProbe({ loading: false, error: String((err as Error).message || err), data: probe.data });
    }
  }

  async function deleteVersion() {
    const identity = record(info.data);
    deleteDispatchedRef.current = true;
    setDeleteIntentDispatched(true);
    return run(setNotice, () => request(`/management/${managementId}/objects/delete-version`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify({ scope_id: scopeId, key: target.key, version_id: identity.version_id === undefined ? (target.version_id.trim() || null) : identity.version_id, expected_etag: String(identity.etag || ""), confirm_version_delete: confirmDelete, acknowledge_unknown_references: ackReferences }) }));
  }

  const identity = record(info.data);
  const probeData = record(probe.data);
  const explicitVersion = target.version_id.trim();
  const identityKeyMatches = identity.key === target.key;
  const identityVersionKnown = identity.version_id !== undefined;
  const identityVersionMatches = explicitVersion ? String(identity.version_id ?? "") === explicitVersion : identityVersionKnown;
  const backendAllowsDelete = identityKeyMatches && identityVersionMatches && identity.can_delete === true;
  const canProbe = can(connection, "object.manage") && scope?.writable === true && Boolean(scopeId);
  const canDelete = can(connection, "object.manage") && scope?.writable === true && backendAllowsDelete && Boolean(identity.etag) && confirmDelete && ackReferences && Boolean(idempotencyKey);

  return <div className="panel inlinePanel"><h2>{t("固定版本删除")}</h2><p className="hint">{t("先读取真实 version-info；空 Version 输入会采用本次响应的真实版本身份，显式输入则必须逐字匹配。失败或 unknown 不会自动换幂等 key 重试。")}</p><div className="miniGrid"><label className="field">{t("Key")}<input value={target.key} onChange={(event) => updateTarget({ key: event.target.value })} /></label><label className="field">{t("Version ID（可选）")}<input value={target.version_id} onChange={(event) => updateTarget({ version_id: event.target.value })} placeholder={t("留空查询当前/未版本化身份")} /></label></div><div className="toolbar"><button type="button" disabled={!scopeId || !target.key || info.loading} onClick={readVersionInfo}>{t("读取版本身份")}</button><button type="button" disabled={!canProbe || probe.loading} title={canProbe ? t("会在当前可写 Scope 前缀下创建并清理 .console-probe 临时对象") : t("需要 object.manage 与 scope writable")} onClick={checkConditionalDelete}>{t("探测条件删除能力")}</button><StatusBadge state={can(connection, "object.manage") ? "reported" : "permission_denied"} label={can(connection, "object.manage") ? "object.manage" : "object.manage off"} /><StatusBadge state={scope?.writable ? "reported" : "permission_denied"} label={scope?.writable ? "scope writable" : "scope readonly"} /></div>{info.error && <div className="notice error">{info.error}</div>}{probe.error && <div className="notice error">{probe.error}</div>}<div className="stateList"><StateRow label="Version identity" value={displayValue(identity.identity_strength)} state={identity.can_delete === true ? "confirmed" : identity.identity_strength ? "needs_review" : "unknown"} /><StateRow label="Can delete" value={yesNoUnknown(identity.can_delete)} state={identity.can_delete === true ? "confirmed" : identity.can_delete === false ? "needs_review" : "unknown"} /><StateRow label="Delete block reason" value={displayValue(identity.delete_block_reason)} state={identity.delete_block_reason ? "needs_review" : "unknown"} /><StateRow label="Version ID" value={versionIdentityLabel(identity.version_id)} state={identity.version_id !== undefined ? "reported" : "unknown"} /><StateRow label="ETag" value={displayValue(identity.etag)} state={identity.etag ? "reported" : "unknown"} /><StateRow label="Size" value={formatBytesOrUnknown(identity.size)} state={identity.size == null ? "unknown" : "reported"} /><StateRow label="Legal hold" value={displayValue(identity.legal_hold)} state={identity.legal_hold === "ON" ? "needs_review" : identity.legal_hold ? "reported" : "unknown"} /><StateRow label="Retention until" value={displayValue(identity.retention_until)} state={identity.retention_until ? "needs_review" : "unknown"} /><StateRow label="Delete Idempotency-Key" value={idempotencyKey ? deleteIntentDispatched ? t("已派发并冻结") : t("已固定") : t("读取身份后生成")} state={idempotencyKey ? "confirmed" : "unknown"} /><StateRow label="Probe capability" value={displayValue(probeData.capability_status || probeData.status)} state={probeData.status === "confirmed" || probeData.capability_status === "supported" ? "confirmed" : probeData.status === "needs_review" ? "needs_review" : "unknown"} /><StateRow label="Probe Idempotency-Key" value={probeIdempotencyKey ? t("已固定") : t("未探测")} state={probeIdempotencyKey ? "confirmed" : "unknown"} /></div><label className="ack"><input type="checkbox" checked={confirmDelete} onChange={(event) => setConfirmDelete(event.target.checked)} />{" "}{t("我确认永久删除此 key/version/ETag 对应的对象版本")}</label><label className="ack"><input type="checkbox" checked={ackReferences} onChange={(event) => setAckReferences(event.target.checked)} />{" "}{t("我确认外部引用状态未知，删除后需人工承担引用影响")}</label><button type="button" disabled={!canDelete} title={canDelete ? t("提交固定版本删除") : t("需要 object.manage、scope writable、can_delete=true、ETag、两个确认和固定 Idempotency-Key")} onClick={deleteVersion}>{t("永久删除此版本")}</button><ResultPane error={notice.error} result={notice.result} /><EvidenceDetails title={t("version-info / probe DTO")} data={{ version_info: info.data || { state: "unknown" }, conditional_delete_probe: probe.data || { state: "unknown" } }} /></div>;
}

function IamPage({ request, managementId, connection }: { request: Requester; managementId: string; connection?: ManagementConnection }) {
  const users = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/iam/users`), [managementId]);
  const groups = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/iam/groups`), [managementId]);
  const serviceAccounts = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/iam/service-accounts`), [managementId]);
  const policies = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/iam/policies`), [managementId]);
  const principals = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/iam/principals`), [managementId]);
  const [notice, setNotice] = useState<Notice>({ error: "", result: null });
  const [userForm, setUserForm] = useState({ username: "", email: "", actions: "", policy_names: "", generate_key: false, access_key_id: "", access_key_status: "Active" });
  const [groupForm, setGroupForm] = useState({ name: "", username: "", policy_name: "", status: "enabled" });
  const [serviceForm, setServiceForm] = useState({ account_id: "", parent_user: "", description: "", expiration: "", status: "Active" });
  const [policyForm, setPolicyForm] = useState({ name: "", document: "{\n  \"Version\": \"2012-10-17\",\n  \"Statement\": []\n}" });
  const canWrite = can(connection, "iam.manage");
  const userRows = array(users.data?.items).map(record);
  const groupRows = array(groups.data?.items).map(record);
  const serviceRows = array(serviceAccounts.data?.items).map(record);
  const policyRows = canonicalPolicyRows(policies.data);
  const principalRows = principalPolicyRows(principals.data);
  return (
    <section className="managementPage">
      <div className="managementGrid twoColumn">
        <DataPanel title={t("Users / Access Keys")} loading={users.loading} error={users.error}>
          <DataTable caption="S3 IAM users" columns={["User", "Email", "Access IDs", "Groups", "Source"]} rows={userRows.map((item) => [displayValue(item.username), displayValue(item.email), array(item.access_keys).map((key) => displayValue(record(key).access_key)).join(", ") || "未知", array(item.groups).join(", ") || "未知", displayValue(item.is_static ? "static" : users.data?.source || "dynamic_api")])} />
          {!userRows.length && <StateRow label="Dynamic users" value={users.data ? t("空响应；静态 S3 配置覆盖未知") : t("未知")} state={users.data ? "empty_response" : "unknown"} />}
        </DataPanel>
        <div className="panel inlinePanel"><h2>{t("User CRUD / key rotation")}</h2><label className="field">{t("Username")}<input value={userForm.username} onChange={(event) => setUserForm({ ...userForm, username: event.target.value })} /></label><label className="field">{t("Email")}<input value={userForm.email} onChange={(event) => setUserForm({ ...userForm, email: event.target.value })} /></label><label className="field">{t("Actions")}<textarea value={userForm.actions} onChange={(event) => setUserForm({ ...userForm, actions: event.target.value })} placeholder={t("每行一个 action")} /></label><label className="field">{t("Policies")}<input value={userForm.policy_names} onChange={(event) => setUserForm({ ...userForm, policy_names: event.target.value })} placeholder={t("comma or newline separated")} /></label><label className="ack"><input type="checkbox" checked={userForm.generate_key} onChange={(event) => setUserForm({ ...userForm, generate_key: event.target.checked })} />{" "}{t("创建时由后端生成一次性 secret")}</label><div className="toolbar"><button type="button" disabled={!canWrite || !userForm.username} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/users`, { method: "POST", body: JSON.stringify(userPayload(userForm)) }))}>{t("创建用户")}</button><button type="button" disabled={!canWrite || !userForm.username} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/users/${encodeURIComponent(userForm.username)}`, { method: "PUT", body: JSON.stringify(userPayload(userForm)) }))}>{t("更新用户")}</button><button type="button" disabled={!canWrite || !userForm.username} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/users/${encodeURIComponent(userForm.username)}`, { method: "DELETE" }))}>{t("删除用户")}</button><button type="button" disabled={!canWrite || !userForm.username} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/users/${encodeURIComponent(userForm.username)}/access-keys`, { method: "POST", body: "{}" }))}>{t("创建 access key")}</button></div><div className="miniGrid"><label className="field">{t("Access key ID")}<input value={userForm.access_key_id} onChange={(event) => setUserForm({ ...userForm, access_key_id: event.target.value })} /></label><label className="field">{t("Status")}<select value={userForm.access_key_status} onChange={(event) => setUserForm({ ...userForm, access_key_status: event.target.value })}><option value="Active">{t("Active")}</option><option value="Inactive">{t("Inactive")}</option></select></label></div><div className="toolbar"><button type="button" disabled={!canWrite || !userForm.username || !userForm.access_key_id} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/users/${encodeURIComponent(userForm.username)}/access-keys/${encodeURIComponent(userForm.access_key_id)}/status`, { method: "PUT", body: JSON.stringify({ status: userForm.access_key_status }) }))}>{t("更新 key 状态")}</button><button type="button" disabled={!canWrite || !userForm.username || !userForm.access_key_id} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/users/${encodeURIComponent(userForm.username)}/access-keys/${encodeURIComponent(userForm.access_key_id)}`, { method: "DELETE" }))}>{t("删除 key")}</button></div><p className="hint">{t("secret 只允许后端生成并在创建响应中出现一次；列表永不展示 secret。")}</p></div>
      </div>
      <div className="managementGrid threeColumn">
        <DataPanel title={t("Groups")} loading={groups.loading} error={groups.error}><DataTable caption="S3 IAM groups" columns={["Name", "Status"]} rows={groupRows.map((item) => [displayValue(item.name ?? item.Name), displayValue(item.status ?? item.Status)])} />{!groupRows.length && <StateRow label="Groups" value={groups.data ? t("空响应") : t("未知")} state={groups.data ? "empty_response" : "unknown"} />}</DataPanel>
        <DataPanel title={t("Policies")} loading={policies.loading} error={policies.error}><DataTable caption="S3 IAM policies" columns={["Name", "Statements"]} rows={policyRows.map((item) => [displayValue(item.name ?? item.Name), displayValue(item.statement_count ?? item.StatementCount ?? array(item.Statement).length)])} />{!policyRows.length && <StateRow label="Policies" value={policies.data ? t("空响应") : t("未知")} state={policies.data ? "empty_response" : "unknown"} />}</DataPanel>
        <DataPanel title={t("Service Accounts")} loading={serviceAccounts.loading} error={serviceAccounts.error}><DataTable caption="S3 service accounts" columns={["ID", "Parent", "Access ID", "Status"]} rows={serviceRows.map((item) => [displayValue(item.id), displayValue(item.parent_user), displayValue(item.access_key_id), displayValue(item.status)])} />{!serviceRows.length && <StateRow label="Service accounts" value={serviceAccounts.data ? t("空响应") : t("未知")} state={serviceAccounts.data ? "empty_response" : "unknown"} />}</DataPanel>
      </div>
      <div className="managementGrid threeColumn"><IamGroupForm form={groupForm} setForm={setGroupForm} disabled={!canWrite} request={request} managementId={managementId} setNotice={setNotice} /><ServiceAccountForm form={serviceForm} setForm={setServiceForm} disabled={!canWrite} request={request} managementId={managementId} setNotice={setNotice} /><PolicyForm form={policyForm} setForm={setPolicyForm} disabled={!canWrite} request={request} managementId={managementId} setNotice={setNotice} /></div>
      <DataPanel title={t("Policy Principal 建议")} loading={principals.loading} error={principals.error}>
        <p className="hint">{t("仅为策略编辑提供候选 principal，不是完整身份库存。")}</p>
        <div className="stateList"><StateRow label="Source" value={displayValue(principals.data?.source)} state={principals.data?.source ? "reported" : "unknown"} /><StateRow label="Coverage" value={displayValue(principals.data?.coverage)} state={principals.data?.coverage ? "reported" : "unknown"} /><StateRow label="Total" value={principals.data?.total == null ? t("未知") : displayValue(principals.data.total)} state={principals.data?.total == null ? "unknown" : "reported"} /></div>
        <DataTable caption="Policy principal candidates" columns={["Principal", "Type", "Name"]} rows={principalRows.map((item) => [displayValue(item.principal), displayValue(item.type), displayValue(item.name)])} />
        {!principalRows.length && <StateRow label="Principals" value={principals.data ? t("空响应") : t("未知")} state={principals.data ? "empty_response" : "unknown"} />}
      </DataPanel>
      <ResultPane error={notice.error} result={notice.result} />
      <EvidenceDetails title={t("IAM raw DTO")} data={{ users: users.data, groups: groups.data, policies: policies.data, serviceAccounts: serviceAccounts.data, principals: principals.data }} />
    </section>
  );
}

function principalPolicyRows(data: unknown) {
  return array(record(data).items).map(record);
}

function jobTypeRunRows(data: unknown) {
  const runs = record(data).runs;
  const payload = record(runs).data ?? runs;
  if (Array.isArray(payload)) return payload.map(record);
  return normalizeItems(payload);
}

function JobDetailPanel({ data }: { data: unknown }) {
  if (!data) return <StateRow label="Job detail" value={t("未读取")} state="unknown" />;
  const root = record(data);
  const job = record(root.job);
  const detail = record(root.detail);
  return <div className="stateList"><StateRow label="Source" value={displayValue(root.source)} state={root.source ? "reported" : "unknown"} /><StateRow label="Job wrapper" value={displayValue(job.status)} state={job.status === "supported" ? "reported" : job.status ? String(job.status) : "unknown"} /><StateRow label="Detail wrapper" value={displayValue(detail.status)} state={detail.status === "supported" ? "reported" : detail.status ? String(detail.status) : "unknown"} /><EvidenceDetails title={t("Job DTO")} data={data} /></div>;
}

function IamGroupForm({ form, setForm, disabled, request, managementId, setNotice }: { form: any; setForm: (value: any) => void; disabled: boolean; request: Requester; managementId: string; setNotice: (notice: Notice) => void }) {
  return <div className="panel inlinePanel"><h2>{t("Group CRUD")}</h2><label className="field">{t("Group")}<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label><label className="field">{t("Username")}<input value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} /></label><label className="field">{t("Policy")}<input value={form.policy_name} onChange={(event) => setForm({ ...form, policy_name: event.target.value })} /></label><label className="field">{t("Status")}<select value={form.status} onChange={(event) => setForm({ ...form, status: event.target.value })}><option value="enabled">{t("enabled")}</option><option value="disabled">{t("disabled")}</option></select></label><div className="toolbar"><button type="button" disabled={disabled || !form.name} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/groups`, { method: "POST", body: JSON.stringify({ name: form.name }) }))}>{t("创建组")}</button><button type="button" disabled={disabled || !form.name} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/groups/${encodeURIComponent(form.name)}/status`, { method: "PUT", body: JSON.stringify({ status: form.status }) }))}>{t("更新状态")}</button><button type="button" disabled={disabled || !form.name} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/groups/${encodeURIComponent(form.name)}`, { method: "DELETE" }))}>{t("删除组")}</button><button type="button" disabled={disabled || !form.name || !form.username} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/groups/${encodeURIComponent(form.name)}/members`, { method: "POST", body: JSON.stringify({ username: form.username }) }))}>{t("加成员")}</button><button type="button" disabled={disabled || !form.name || !form.username} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/groups/${encodeURIComponent(form.name)}/members/${encodeURIComponent(form.username)}`, { method: "DELETE" }))}>{t("移成员")}</button><button type="button" disabled={disabled || !form.name || !form.policy_name} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/groups/${encodeURIComponent(form.name)}/policies`, { method: "POST", body: JSON.stringify({ policy_name: form.policy_name }) }))}>{t("绑定 policy")}</button><button type="button" disabled={disabled || !form.name || !form.policy_name} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/groups/${encodeURIComponent(form.name)}/policies/${encodeURIComponent(form.policy_name)}`, { method: "DELETE" }))}>{t("解绑 policy")}</button></div></div>;
}

function ServiceAccountForm({ form, setForm, disabled, request, managementId, setNotice }: { form: any; setForm: (value: any) => void; disabled: boolean; request: Requester; managementId: string; setNotice: (notice: Notice) => void }) {
  return <div className="panel inlinePanel"><h2>{t("Service account CRUD")}</h2><label className="field">{t("Account ID")}<input value={form.account_id} onChange={(event) => setForm({ ...form, account_id: event.target.value })} /></label><label className="field">{t("Parent user")}<input value={form.parent_user} onChange={(event) => setForm({ ...form, parent_user: event.target.value })} /></label><label className="field">{t("Description")}<input value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label><label className="field">{t("Expiration")}<input value={form.expiration} onChange={(event) => setForm({ ...form, expiration: event.target.value })} /></label><label className="field">{t("Status")}<input value={form.status} onChange={(event) => setForm({ ...form, status: event.target.value })} /></label><div className="toolbar"><button type="button" disabled={disabled || !form.parent_user} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/service-accounts`, { method: "POST", body: JSON.stringify(servicePayload(form)) }))}>{t("创建 service account")}</button><button type="button" disabled={disabled || !form.account_id} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/service-accounts/${encodeURIComponent(form.account_id)}`, { method: "PUT", body: JSON.stringify(servicePayload(form)) }))}>{t("更新")}</button><button type="button" disabled={disabled || !form.account_id} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/service-accounts/${encodeURIComponent(form.account_id)}`, { method: "DELETE" }))}>{t("删除")}</button></div></div>;
}

function PolicyForm({ form, setForm, disabled, request, managementId, setNotice }: { form: any; setForm: (value: any) => void; disabled: boolean; request: Requester; managementId: string; setNotice: (notice: Notice) => void }) {
  return <div className="panel inlinePanel"><h2>{t("Policy CRUD")}</h2><label className="field">{t("Policy name")}<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label><label className="field">{t("Document JSON")}<textarea value={form.document} onChange={(event) => setForm({ ...form, document: event.target.value })} /></label><div className="toolbar"><button type="button" disabled={!form.document} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/policies/validate`, { method: "POST", body: JSON.stringify({ document: parseJsonObject(form.document, "policy") }) }))}>{t("校验 policy")}</button><button type="button" disabled={disabled || !form.name} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/policies`, { method: "POST", body: JSON.stringify({ name: form.name, document: parseJsonObject(form.document, "policy") }) }))}>{t("创建 policy")}</button><button type="button" disabled={disabled || !form.name} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/policies/${encodeURIComponent(form.name)}`, { method: "PUT", body: JSON.stringify({ document: parseJsonObject(form.document, "policy") }) }))}>{t("更新 policy")}</button><button type="button" disabled={disabled || !form.name} onClick={() => run(setNotice, () => request(`/management/${managementId}/iam/policies/${encodeURIComponent(form.name)}`, { method: "DELETE" }))}>{t("删除 policy")}</button></div></div>;
}

function MaintenancePage({ request, managementId, connection }: { request: Requester; managementId: string; connection?: ManagementConnection }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const maintenance = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/maintenance`), [managementId, refreshKey]);
  const operations = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/operations?limit=20`), [managementId, refreshKey]);
  const [jobTypeDetail, setJobTypeDetail] = useState<AsyncState<Record<string, unknown>>>({ loading: false, error: "", data: null });
  const [jobDetail, setJobDetail] = useState<AsyncState<Record<string, unknown>>>({ loading: false, error: "", data: null });
  const [notice, setNotice] = useState<Notice>({ error: "", result: null });
  const [form, setForm] = useState({ action: "detect", job_type: "", job_id: "", params: "{}" });
  const canExecute = can(connection, "maintenance.execute");
  const status = record(maintenance.data?.status);
  const workers = record(maintenance.data?.workers);
  const jobTypes = record(maintenance.data?.job_types);
  const activities = record(maintenance.data?.activities);
  const jobTypeRows = normalizeItems(jobTypes.data);
  const workerRows = normalizeItems(workers.data);
  const activityRows = normalizeItems(activities.data);
  const selectedJobType = jobTypeRows.find((item) => String(item.job_type ?? item.type ?? item.name ?? "") === form.job_type);
  const runRows = jobTypeRunRows(jobTypeDetail.data);
  async function maintenanceWrite() {
    const result = await run(setNotice, () => request(`/management/${managementId}/maintenance/actions`, { method: "POST", body: JSON.stringify(maintenancePayload(form)) }));
    if (result) setRefreshKey((value) => value + 1);
  }
  async function loadJobTypeDetail() {
    if (!form.job_type) return;
    setJobTypeDetail({ loading: true, error: "", data: null });
    try {
      const data = await request<Record<string, unknown>>(`/management/${managementId}/maintenance/job-types/${encodeURIComponent(form.job_type)}`);
      setJobTypeDetail({ loading: false, error: "", data });
    } catch (err) {
      setJobTypeDetail({ loading: false, error: String((err as Error).message || err), data: null });
    }
  }
  function loadCurrentConfigIntoEditor() {
    const config = record(record(jobTypeDetail.data?.config).data);
    setForm((value) => ({ ...value, action: "config_update", params: JSON.stringify(config, null, 2) }));
  }
  async function loadJobDetail() {
    if (!form.job_id) return;
    setJobDetail({ loading: true, error: "", data: null });
    try {
      const data = await request<Record<string, unknown>>(`/management/${managementId}/maintenance/jobs/${encodeURIComponent(form.job_id)}`);
      setJobDetail({ loading: false, error: "", data });
    } catch (err) {
      setJobDetail({ loading: false, error: String((err as Error).message || err), data: null });
    }
  }
  return (
    <section className="managementPage">
      <div className="managementGrid twoColumn">
        <DataPanel title={t("Worker / Plugin")} loading={maintenance.loading} error={maintenance.error}><div className="stateList"><StateRow label="Plugin configured" value={yesNoUnknown(record(status.data).configured)} state={record(status.data).configured === true ? "reported" : "not_configured"} /><StateRow label="Plugin enabled" value={yesNoUnknown(record(status.data).enabled)} state={record(status.data).enabled === true ? "reported" : "not_configured"} /><StateRow label="Worker count" value={displayValue(record(status.data).worker_count)} state={record(status.data).worker_count == null ? "unknown" : "reported"} /><StateRow label="Source" value="official_admin_plugin_api" state="reported" /></div></DataPanel>
        <div className="panel inlinePanel"><h2>{t("维护动作")}</h2><p className="hint">{t("detect/run/job execute/expire 只有后端显式授权并写入 operation journal 后才可触发；状态以 receipt 和历史为准。")}</p><div className="miniGrid"><label className="field">{t("Action")}<select value={form.action} onChange={(event) => setForm({ ...form, action: event.target.value })}><option value="detect">{t("detect")}</option><option value="run">{t("run")}</option><option value="config_update">{t("config_update")}</option><option value="job_expire">{t("job_expire")}</option><option value="job_execute">{t("job_execute")}</option></select></label><label className="field">{t("Job type")}<input value={form.job_type} onChange={(event) => setForm({ ...form, job_type: event.target.value })} /></label><label className="field">{t("Job ID")}<input value={form.job_id} onChange={(event) => setForm({ ...form, job_id: event.target.value })} /></label></div><label className="field">{form.action === "config_update" ? "Config JSON" : form.action === "job_execute" ? "Job JSON" : "Params JSON"}<textarea value={form.params} onChange={(event) => setForm({ ...form, params: event.target.value })} /></label><div className="stateList"><StateRow label="Job type registry" value={selectedJobType ? t("已由上游 registry 报告") : form.job_type ? t("未在当前 registry 中找到") : t("未选择")} state={selectedJobType ? "reported" : "unknown"} /><StateRow label="Current config" value={displayValue(record(jobTypeDetail.data?.config).status || (jobTypeDetail.loading ? t("读取中") : t("未读取")))} state={record(jobTypeDetail.data?.config).status === "supported" ? "reported" : jobTypeDetail.error ? "error" : "unknown"} /></div><div className="toolbar"><button type="button" onClick={() => setRefreshKey((value) => value + 1)}>{t("刷新维护证据")}</button><button type="button" disabled={!form.job_type || jobTypeDetail.loading} onClick={loadJobTypeDetail}>{t("读取 JobType 详情")}</button><button type="button" disabled={!form.job_id || jobDetail.loading} onClick={loadJobDetail}>{t("读取 Job ID 详情")}</button><button type="button" disabled={record(jobTypeDetail.data?.config).status !== "supported"} onClick={loadCurrentConfigIntoEditor}>{t("载入 Config 编辑")}</button><button type="button" disabled={!canExecute} title={canExecute ? "提交维护动作" : disabledTitle("maintenance.execute")} onClick={maintenanceWrite}>{t("提交维护动作")}</button></div>{jobTypeDetail.error && <div className="notice error">{jobTypeDetail.error}</div>}<StateRow label="远端授权" value={canExecute ? t("已授权") : t("未授权，动作禁用")} state={canExecute ? "reported" : "permission_denied"} /><EvidenceDetails title={t("选中 Job type 编辑前证据")} data={{ registry: selectedJobType || { state: "unknown", job_type: form.job_type || null, source: "maintenance.job_types" }, detail: jobTypeDetail.data || { state: jobTypeDetail.loading ? "loading" : "unknown" } }} /></div>
      </div>
      <div className="managementGrid threeColumn"><DataPanel title={t("Workers")} loading={maintenance.loading} error={maintenance.error}><DataTable caption="Official plugin workers" columns={["Worker", "Version", "Last seen"]} rows={workerRows.map((item) => [displayValue(item.WorkerID ?? item.worker_id), displayValue(item.WorkerVersion ?? item.version), displayValue(item.LastSeenAt ?? item.last_seen_at)])} /></DataPanel><DataPanel title={t("Job types")} loading={maintenance.loading} error={maintenance.error}><DataTable caption="Official plugin job types" columns={["Type", "Detect", "Run"]} rows={jobTypeRows.map((item) => [displayValue(item.job_type ?? item.type ?? item.name), yesNoUnknown(item.can_detect), yesNoUnknown(item.can_execute)])} /></DataPanel><DataPanel title={t("Activities")} loading={maintenance.loading} error={maintenance.error}><DataTable caption="Official plugin activities" columns={["Stage", "Message", "At"]} rows={activityRows.slice(0, 20).map((item) => [displayValue(item.stage), displayValue(item.message), displayValue(item.occurred_at)])} /></DataPanel></div>
      <div className="managementGrid twoColumn"><DataPanel title={t("JobType 运行历史")} loading={jobTypeDetail.loading} error={jobTypeDetail.error}><DataTable caption="Official plugin job type runs" columns={["Job", "State", "Started", "Updated", "Source"]} rows={runRows.map((item) => [displayValue(item.job_id ?? item.id), displayValue(item.state ?? item.status), displayValue(item.started_at ?? item.created_at), displayValue(item.updated_at ?? item.completed_at), displayValue(item.source ?? record(jobTypeDetail.data?.runs).source)])} />{!runRows.length && <StateRow label="Runs" value={jobTypeDetail.data ? t("空响应或 upstream 未提供 runs") : t("未知")} state={jobTypeDetail.data ? "empty_response" : "unknown"} />}</DataPanel><DataPanel title={t("Job ID 详情")} loading={jobDetail.loading} error={jobDetail.error}><JobDetailPanel data={jobDetail.data} /></DataPanel></div>
      <DataPanel title={t("Operation history")} loading={operations.loading} error={operations.error}><OperationTable data={operations.data} /></DataPanel>
      <ResultPane error={notice.error} result={notice.result} />
      <EvidenceDetails title={t("Worker raw DTO")} data={maintenance.data} />
    </section>
  );
}

function ServicesPage({ request, managementId, connection, scopes = [], scopeId = "", setScopeId }: { request: Requester; managementId: string; connection?: ManagementConnection; scopes?: StorageScopeOption[]; scopeId?: string; setScopeId?: (value: string) => void }) {
  const services = useApi<ManagementEnvelope>(() => request(`/management/${managementId}/services`), [managementId]);
  const modules = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/modules`), [managementId]);
  const health = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/services/health`), [managementId]);
  const mountClients = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/modules/mount-clients`), [managementId]);
  const [refreshKey, setRefreshKey] = useState(0);
  const s3tables = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/modules/s3-tables`), [managementId, refreshKey]);
  const buckets = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/modules/s3-tables/buckets`), [managementId, refreshKey]);
  const [notice, setNotice] = useState<Notice>({ error: "", result: null });
  const [tableDetail, setTableDetail] = useState<AsyncState<Record<string, unknown>>>({ loading: false, error: "", data: null });
  const [tablePreview, setTablePreview] = useState<AsyncState<Record<string, unknown>>>({ loading: false, error: "", data: null });
  const tableRequestGeneration = useRef(0);
  const [mq, setMq] = useState({ namespace: "default", name: "", partition_count: "1", retention: "{\n  \"enabled\": false\n}" });
  const [table, setTable] = useState({ bucket_arn: "", bucket_name: "", namespace: "", table_name: "", format: "iceberg", confirm_empty: false, policy: "", resource_arn: "", tags: "{\n  \"env\": \"dev\"\n}", tag_keys: "", scope_id: "", snapshot_id: "", file_location: "", preview_limit: "20" });
  const canMq = can(connection, "mq.manage");
  const canTable = can(connection, "table.manage");
  const associatedScopes = scopes.filter((scope) => connection?.s3_connection_id && scope.connection_id === connection.s3_connection_id);
  const previewScopeId = table.scope_id || scopeId;
  const previewScope = associatedScopes.find((scope) => scope.id === previewScopeId);
  const canPreviewTable = Boolean(previewScope?.allow_original_download);
  const namespaceQuery = table.bucket_arn ? `?bucket_arn=${encodeURIComponent(table.bucket_arn)}` : "";
  const tableQuery = table.bucket_arn ? `?bucket_arn=${encodeURIComponent(table.bucket_arn)}&namespace=${encodeURIComponent(table.namespace)}` : "";
  const namespaces = useApi<Record<string, unknown>>(() => table.bucket_arn ? request(`/management/${managementId}/modules/s3-tables/namespaces${namespaceQuery}`) : Promise.resolve({}), [managementId, table.bucket_arn, refreshKey]);
  const tables = useApi<Record<string, unknown>>(() => table.bucket_arn ? request(`/management/${managementId}/modules/s3-tables/tables${tableQuery}`) : Promise.resolve({}), [managementId, table.bucket_arn, table.namespace, refreshKey]);
  const bucketPolicy = useApi<Record<string, unknown>>(() => table.bucket_arn ? request(`/management/${managementId}/modules/s3-tables/bucket-policy?bucket_arn=${encodeURIComponent(table.bucket_arn)}`) : Promise.resolve({}), [managementId, table.bucket_arn, refreshKey]);
  const tablePolicy = useApi<Record<string, unknown>>(() => table.bucket_arn && table.namespace && table.table_name ? request(`/management/${managementId}/modules/s3-tables/table-policy?bucket_arn=${encodeURIComponent(table.bucket_arn)}&namespace=${encodeURIComponent(table.namespace)}&name=${encodeURIComponent(table.table_name)}`) : Promise.resolve({}), [managementId, table.bucket_arn, table.namespace, table.table_name, refreshKey]);
  const tableTags = useApi<Record<string, unknown>>(() => table.resource_arn ? request(`/management/${managementId}/modules/s3-tables/tags?resource_arn=${encodeURIComponent(table.resource_arn)}`) : Promise.resolve({}), [managementId, table.resource_arn, refreshKey]);
  const detailRequestKey = tableDetailKey(managementId, table);
  const previewRequestKey = tablePreviewKey(managementId, table, previewScopeId);
  const visibleTableDetail = record(tableDetail.data).__request_key === detailRequestKey ? tableDetail.data : null;
  const visibleTablePreview = record(tablePreview.data).__request_key === previewRequestKey ? tablePreview.data : null;
  useEffect(() => {
    tableRequestGeneration.current += 1;
    setTableDetail({ loading: false, error: "", data: null });
    setTablePreview({ loading: false, error: "", data: null });
  }, [detailRequestKey, previewRequestKey]);
  async function serviceWrite(fn: () => Promise<unknown>) {
    const result = await run(setNotice, fn);
    if (result) setRefreshKey((value) => value + 1);
  }
  async function loadTableDetails() {
    if (!table.bucket_arn || !table.namespace || !table.table_name) return;
    const requestKey = detailRequestKey;
    const generation = tableRequestGeneration.current;
    setTableDetail({ loading: true, error: "", data: null });
    try {
      const params = new URLSearchParams({ bucket_arn: table.bucket_arn, namespace: table.namespace, name: table.table_name });
      const data = await request<Record<string, unknown>>(`/management/${managementId}/modules/s3-tables/table-details?${params.toString()}`);
      if (generation !== tableRequestGeneration.current || requestKey !== tableDetailKey(managementId, table)) return;
      setTableDetail({ loading: false, error: "", data: { ...data, __request_key: requestKey } });
    } catch (err) {
      if (generation !== tableRequestGeneration.current || requestKey !== tableDetailKey(managementId, table)) return;
      setTableDetail({ loading: false, error: String((err as Error).message || err), data: null });
    }
  }
  async function loadTablePreview() {
    if (!previewScopeId || !table.bucket_arn || !table.namespace || !table.table_name) return;
    const requestKey = previewRequestKey;
    const generation = tableRequestGeneration.current;
    setTablePreview({ loading: true, error: "", data: null });
    try {
      const data = await request<Record<string, unknown>>(`/management/${managementId}/modules/s3-tables/table-preview`, { method: "POST", body: JSON.stringify(tablePreviewPayload(table, previewScopeId)) });
      if (generation !== tableRequestGeneration.current || requestKey !== tablePreviewKey(managementId, table, previewScopeId)) return;
      setTablePreview({ loading: false, error: "", data: { ...data, __request_key: requestKey } });
    } catch (err) {
      if (generation !== tableRequestGeneration.current || requestKey !== tablePreviewKey(managementId, table, previewScopeId)) return;
      setTablePreview({ loading: false, error: String((err as Error).message || err), data: null });
    }
  }
  return <section className="managementPage">
    <div className="managementGrid twoColumn">
      <DataPanel title={t("其他服务 / 依赖状态")} loading={services.loading || modules.loading} error={services.error || modules.error}><DependencyStates data={services.data} modules={modules.data} expanded /></DataPanel>
      <DataPanel title={t("服务健康")} loading={health.loading} error={health.error}><ServiceHealthTable data={health.data} /></DataPanel>
    </div>
    <DataPanel title={t("Mount 客户端")} loading={mountClients.loading} error={mountClients.error}><MountClientsTable data={mountClients.data} /></DataPanel>
    <div className="managementGrid twoColumn">
      <DataPanel title={t("MQ Topics")} loading={false} error="">
        <div className="miniGrid"><label className="field">{t("Namespace")}<input value={mq.namespace} onChange={(event) => setMq({ ...mq, namespace: event.target.value })} /></label><label className="field">{t("Topic")}<input value={mq.name} onChange={(event) => setMq({ ...mq, name: event.target.value })} /></label><label className="field">{t("Partitions")}<input value={mq.partition_count} onChange={(event) => setMq({ ...mq, partition_count: event.target.value })} /></label></div>
        <label className="field">{t("Retention JSON")}<textarea value={mq.retention} onChange={(event) => setMq({ ...mq, retention: event.target.value })} /></label>
        <div className="toolbar"><button type="button" disabled={!mq.namespace || !mq.name} onClick={() => run(setNotice, () => request(`/management/${managementId}/modules/mq/topics/${encodeURIComponent(mq.namespace)}/${encodeURIComponent(mq.name)}`))}>{t("读取 topic")}</button><button type="button" disabled={!canMq || !mq.namespace || !mq.name} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/mq/topics`, { method: "POST", body: JSON.stringify({ namespace: mq.namespace, name: mq.name, partition_count: Number(mq.partition_count), retention: parseJsonObject(mq.retention, "retention") }) }))}>{t("创建 topic")}</button><button type="button" disabled={!canMq || !mq.namespace || !mq.name} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/mq/topics/retention`, { method: "POST", body: JSON.stringify({ namespace: mq.namespace, name: mq.name, retention: parseJsonObject(mq.retention, "retention") }) }))}>{t("更新 retention")}</button><button type="button" disabled={!canMq} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/mq/retention/purge`, { method: "POST", body: "{}" }))}>{t("清理 retention")}</button></div>
        <StateRow label="MQ 写权限" value={canMq ? t("已授权") : t("未授权，动作禁用")} state={canMq ? "reported" : "permission_denied"} />
      </DataPanel>
      <DataPanel title={t("S3 Tables buckets")} loading={buckets.loading} error={buckets.error}>
        <DataTable caption="S3 table buckets" columns={["Name", "ARN", "Owner", "Format"]} rows={normalizeItems(buckets.data).map((item) => [displayValue(item.name ?? item.Name), textCell(item.arn ?? item.bucket_arn ?? item.ARN), displayValue(item.owner), displayValue(item.format)])} />
        <div className="miniGrid"><label className="field">{t("Bucket ARN")}<input value={table.bucket_arn} onChange={(event) => setTable({ ...table, bucket_arn: event.target.value })} /></label><label className="field">{t("New bucket name")}<input value={table.bucket_name} onChange={(event) => setTable({ ...table, bucket_name: event.target.value })} /></label><label className="field">{t("Format")}<input value={table.format} onChange={(event) => setTable({ ...table, format: event.target.value })} /></label></div>
        <label className="ack"><input type="checkbox" checked={table.confirm_empty} onChange={(event) => setTable({ ...table, confirm_empty: event.target.checked })} />{" "}{t("删除前确认 observed empty")}</label>
        <div className="toolbar"><button type="button" disabled={!canTable || !table.bucket_name} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/buckets`, { method: "POST", body: JSON.stringify({ name: table.bucket_name, format: table.format }) }))}>{t("创建 table bucket")}</button><button type="button" disabled={!canTable || !table.bucket_arn || !table.confirm_empty} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/buckets`, { method: "DELETE", body: JSON.stringify({ bucket_arn: table.bucket_arn, confirm_empty: table.confirm_empty }) }))}>{t("删除 table bucket")}</button></div>
        <StateRow label="S3 Tables 写权限" value={canTable ? t("已授权") : t("未授权，动作禁用")} state={canTable ? "reported" : "permission_denied"} />
      </DataPanel>
    </div>
    <div className="managementGrid twoColumn">
      <DataPanel title={t("S3 Tables namespaces")} loading={namespaces.loading} error={namespaces.error}>
        <DataTable caption="Namespaces" columns={["Namespace", "Raw"]} rows={normalizeItems(namespaces.data).map((item) => [displayValue(item.name ?? item.namespace ?? item.Namespace), <EvidenceDetails title={t("DTO")} data={item} />])} />
        <label className="field">{t("Namespace")}<input value={table.namespace} onChange={(event) => setTable({ ...table, namespace: event.target.value })} /></label>
        <div className="toolbar"><button type="button" disabled={!canTable || !table.bucket_arn || !table.namespace} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/namespaces`, { method: "POST", body: JSON.stringify({ bucket_arn: table.bucket_arn, name: table.namespace }) }))}>{t("创建 namespace")}</button><button type="button" disabled={!canTable || !table.bucket_arn || !table.namespace} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/namespaces`, { method: "DELETE", body: JSON.stringify({ bucket_arn: table.bucket_arn, name: table.namespace }) }))}>{t("删除 namespace")}</button></div>
      </DataPanel>
      <DataPanel title={t("S3 Tables tables")} loading={tables.loading} error={tables.error}>
        <DataTable caption="Tables" columns={["Table", "ARN", "Format"]} rows={normalizeItems(tables.data).map((item) => [displayValue(item.name ?? item.table_name ?? item.Name), textCell(item.table_arn ?? item.arn ?? item.ARN), displayValue(item.format)])} />
        <label className="field">{t("Table name")}<input value={table.table_name} onChange={(event) => setTable({ ...table, table_name: event.target.value })} /></label>
        <div className="toolbar"><button type="button" disabled={!table.bucket_arn || !table.namespace || !table.table_name || tableDetail.loading} onClick={loadTableDetails}>{t("读取 table details")}</button><button type="button" disabled={!canTable || !table.bucket_arn || !table.namespace || !table.table_name} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/tables`, { method: "POST", body: JSON.stringify({ bucket_arn: table.bucket_arn, namespace: table.namespace, name: table.table_name, format: table.format }) }))}>{t("创建 table")}</button><button type="button" disabled={!canTable || !table.bucket_arn || !table.namespace || !table.table_name || !table.confirm_empty} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/tables`, { method: "DELETE", body: JSON.stringify({ bucket_arn: table.bucket_arn, namespace: table.namespace, name: table.table_name, confirm_empty: table.confirm_empty }) }))}>{t("删除 empty table")}</button></div>
      </DataPanel>
    </div>
    <DataPanel title={t("S3 Tables table details / data preview")} loading={tableDetail.loading || tablePreview.loading} error={tableDetail.error || tablePreview.error}>
      <TableDetailsPanel data={visibleTableDetail} />
      <div className="miniGrid"><label className="field">{t("预览 Scope")}<select value={previewScopeId} onChange={(event) => { setTable({ ...table, scope_id: event.target.value }); setScopeId?.(event.target.value); }}><option value="">{t("未选择")}</option>{associatedScopes.map((scope) => <option key={scope.id} value={scope.id}>{scope.display_name || `${scope.bucket}/${scope.prefix || ""}`}</option>)}</select></label><label className="field">{t("Snapshot ID")}<input disabled={isLanceTable(table)} value={table.snapshot_id} onChange={(event) => setTable({ ...table, snapshot_id: event.target.value })} placeholder={isLanceTable(table) ? t("LANCE worker sample 不支持") : t("decimal string, optional")} /></label><label className="field">{t("File location")}<input disabled={isLanceTable(table)} value={table.file_location} onChange={(event) => setTable({ ...table, file_location: event.target.value })} placeholder={isLanceTable(table) ? t("LANCE worker sample 不支持") : t("optional exact manifest member")} /></label><label className="field">{t("Limit")}<input type="number" min="1" max="100" value={table.preview_limit} onChange={(event) => setTable({ ...table, preview_limit: event.target.value })} /></label></div>
      <div className="toolbar"><button type="button" disabled={!canPreviewTable || !table.bucket_arn || !table.namespace || !table.table_name || tablePreview.loading} title={canPreviewTable ? t("读取授权 scope 内的原始文件样本") : t("需要选择关联连接且允许原图读取的 Scope")} onClick={loadTablePreview}>{t("读取数据样本")}</button><StateRow label="Preview authorization" value={previewScope ? previewScope.allow_original_download ? t("允许原图读取") : t("Scope 未开启原图读取") : t("未选择关联 Scope")} state={canPreviewTable ? "reported" : "permission_denied"} /></div>
      <TablePreviewPanel data={visibleTablePreview} />
    </DataPanel>
    <div className="managementGrid twoColumn">
      <DataPanel title={t("S3 Tables policies")} loading={bucketPolicy.loading || tablePolicy.loading} error={bucketPolicy.error || tablePolicy.error}>
        <div className="stateList"><StateRow label="Bucket policy" value={displayValue(record(bucketPolicy.data).policy)} state={bucketPolicy.data && record(bucketPolicy.data).policy != null ? "reported" : "unknown"} /><StateRow label="Table policy" value={displayValue(record(tablePolicy.data).policy)} state={tablePolicy.data && record(tablePolicy.data).policy != null ? "reported" : "unknown"} /></div>
        <label className="field">{t("Policy JSON/string")}<textarea value={table.policy} onChange={(event) => setTable({ ...table, policy: event.target.value })} placeholder={t("{\"Version\":\"2012-10-17\",\"Statement\":[]}")} /></label>
        <div className="toolbar"><button type="button" disabled={bucketPolicy.data == null} onClick={() => setTable({ ...table, policy: String(record(bucketPolicy.data).policy || "") })}>{t("填入 bucket policy")}</button><button type="button" disabled={tablePolicy.data == null} onClick={() => setTable({ ...table, policy: String(record(tablePolicy.data).policy || "") })}>{t("填入 table policy")}</button><button type="button" disabled={!canTable || !table.bucket_arn || !table.policy} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/bucket-policy`, { method: "PUT", body: JSON.stringify({ bucket_arn: table.bucket_arn, policy: table.policy }) }))}>{t("保存 bucket policy")}</button><button type="button" disabled={!canTable || !table.bucket_arn} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/bucket-policy`, { method: "DELETE", body: JSON.stringify({ bucket_arn: table.bucket_arn }) }))}>{t("删除 bucket policy")}</button></div>
        <div className="toolbar"><button type="button" disabled={!canTable || !table.bucket_arn || !table.namespace || !table.table_name || !table.policy} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/table-policy`, { method: "PUT", body: JSON.stringify({ bucket_arn: table.bucket_arn, namespace: table.namespace, name: table.table_name, policy: table.policy }) }))}>{t("保存 table policy")}</button><button type="button" disabled={!canTable || !table.bucket_arn || !table.namespace || !table.table_name} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/table-policy`, { method: "DELETE", body: JSON.stringify({ bucket_arn: table.bucket_arn, namespace: table.namespace, name: table.table_name }) }))}>{t("删除 table policy")}</button></div>
      </DataPanel>
      <DataPanel title={t("S3 Tables tags")} loading={tableTags.loading} error={tableTags.error}>
        <div className="stateList"><StateRow label="Resource ARN" value={displayValue(table.resource_arn)} state={table.resource_arn ? "reported" : "unknown"} /><StateRow label="Tags" value={table.resource_arn && tableTags.data ? displayValue(Object.keys(record(record(tableTags.data).tags ?? tableTags.data)).length) : t("未知")} state={table.resource_arn && tableTags.data ? "reported" : "unknown"} /></div>
        <label className="field">{t("Resource ARN")}<input value={table.resource_arn} onChange={(event) => setTable({ ...table, resource_arn: event.target.value })} placeholder={t("bucket arn or table arn")} /></label>
        <label className="field">{t("Tags JSON")}<textarea value={table.tags} onChange={(event) => setTable({ ...table, tags: event.target.value })} /></label>
        <label className="field">{t("Tag keys to delete")}<textarea value={table.tag_keys} onChange={(event) => setTable({ ...table, tag_keys: event.target.value })} placeholder={t("one key per line or comma separated")} /></label>
        <div className="toolbar"><button type="button" disabled={!table.resource_arn || !tableTags.data} onClick={() => setTable({ ...table, tags: JSON.stringify(record(tableTags.data).tags ?? tableTags.data, null, 2) })}>{t("填入读取 tags")}</button><button type="button" disabled={!canTable || !table.resource_arn} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/tags`, { method: "PUT", body: JSON.stringify({ resource_arn: table.resource_arn, tags: parseJsonObject(table.tags, "tags") }) }))}>{t("保存 tags")}</button><button type="button" disabled={!canTable || !table.resource_arn || !lines(table.tag_keys).length} onClick={() => serviceWrite(() => request(`/management/${managementId}/modules/s3-tables/tags`, { method: "DELETE", body: JSON.stringify({ resource_arn: table.resource_arn, tag_keys: lines(table.tag_keys) }) }))}>{t("删除 tag keys")}</button></div>
      </DataPanel>
    </div>
    <DataPanel title={t("S3 Tables / Iceberg / Lance status")} loading={s3tables.loading} error={s3tables.error}><ModuleStatusTable data={s3tables.data} /></DataPanel>
    <ResultPane error={notice.error} result={notice.result} />
    <EvidenceDetails title={t("Services raw DTO")} data={{ services: services.data, modules: modules.data, s3tables: s3tables.data, buckets: buckets.data, namespaces: namespaces.data, tables: tables.data, tableDetail: visibleTableDetail, tablePreview: visibleTablePreview, bucketPolicy: bucketPolicy.data, tablePolicy: tablePolicy.data, tableTags: tableTags.data }} />
  </section>;
}

function ConnectionSetup({ request, s3Connections, refreshConnections }: { request: Requester; s3Connections: S3ConnectionOption[]; refreshConnections: () => Promise<void> }) {
  const [form, setForm] = useState({ name: "OptiPlex Admin", admin_url: "http://127.0.0.1:23646", admin_secret_ref: "server-admin", s3_connection_id: "", master: "", filer: "", volume: "", iceberg: "", lance: "", s3: "", mq: "" });
  const [notice, setNotice] = useState<Notice>({ error: "", result: null });
  async function submit(event: FormEvent) {
    event.preventDefault();
    await run(setNotice, async () => {
      const endpoints = Object.fromEntries(Object.entries({ master: form.master, filer: form.filer, volume: form.volume, iceberg: form.iceberg, lance: form.lance, s3: form.s3, mq: form.mq }).filter(([, value]) => value.trim()));
      const created = await request("/management/connections", { method: "POST", body: JSON.stringify({ name: form.name, admin_url: form.admin_url, admin_secret_ref: form.admin_secret_ref, s3_connection_id: form.s3_connection_id || undefined, endpoints, permissions: {} }) });
      await refreshConnections();
      return created;
    });
  }
  return <form className="managementForm" onSubmit={submit}><h2>{t("Management Connection")}</h2><div className="miniGrid"><label className="field">{t("Name")}<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label><label className="field">{t("Admin URL")}<input value={form.admin_url} onChange={(event) => setForm({ ...form, admin_url: event.target.value })} /></label><label className="field">{t("Admin Secret Ref")}<input value={form.admin_secret_ref} onChange={(event) => setForm({ ...form, admin_secret_ref: event.target.value })} /></label><label className="field">{t("Linked S3 Connection")}<select value={form.s3_connection_id} onChange={(event) => setForm({ ...form, s3_connection_id: event.target.value })}><option value="">{t("未关联")}</option>{s3Connections.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label>{(["master", "filer", "volume", "iceberg", "lance", "s3", "mq"] as const).map((key) => <label className="field" key={key}>{key}{" "}{t("endpoint")}<input value={form[key]} onChange={(event) => setForm({ ...form, [key]: event.target.value })} placeholder={t("optional approved endpoint")} /></label>)}</div><button type="submit" disabled={!form.name || !form.admin_url || !form.admin_secret_ref}>{t("保存管理连接")}</button><p className="hint">{t("只提交 secret_ref。真实 Admin password 保留在服务端 secrets registry；浏览器不接收 plaintext secret。")}</p><ResultPane error={notice.error} result={notice.result} /></form>;
}

function AccessPolicyEditor({ request, connection, refreshConnections }: { request: Requester; connection: ManagementConnection; refreshConnections: () => Promise<void> }) {
  const [writeEnabled, setWriteEnabled] = useState(Boolean(connection.management_write_enabled));
  const [ack, setAck] = useState(false);
  const [reason, setReason] = useState("");
  const [bools, setBools] = useState<Record<string, boolean>>(() => Object.fromEntries(PERMISSION_KEYS.map((key) => [key, connection.permissions?.[key] === true])));
  const [lists, setLists] = useState<Record<string, string>>(() => Object.fromEntries(LIST_PERMISSION_KEYS.map((key) => [key, array(connection.permissions?.[key]).join("\n")])));
  const [notice, setNotice] = useState<Notice>({ error: "", result: null });
  useEffect(() => { setWriteEnabled(Boolean(connection.management_write_enabled)); setAck(false); setBools(Object.fromEntries(PERMISSION_KEYS.map((key) => [key, connection.permissions?.[key] === true]))); setLists(Object.fromEntries(LIST_PERMISSION_KEYS.map((key) => [key, array(connection.permissions?.[key]).join("\n")]))); }, [connection.id, connection.management_write_enabled, connection.permissions]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    await run(setNotice, async () => {
      const permissions = { ...bools, "bucket_write_prefixes": lines(lists.bucket_write_prefixes || ""), "file.read_roots": lines(lists["file.read_roots"] || "/"), "file.write_roots": lines(lists["file.write_roots"] || "") };
      const updated = await request(`/management/connections/${connection.id}/access-policy`, { method: "PUT", body: JSON.stringify({ management_write_enabled: writeEnabled, acknowledge_management_write: writeEnabled ? ack : false, permissions, reason: reason || undefined }) });
      await refreshConnections();
      return updated;
    });
  }
  return <form className="managementForm" onSubmit={submit}><h2>{t("Access policy")}</h2><label className="ack"><input type="checkbox" checked={writeEnabled} onChange={(event) => setWriteEnabled(event.target.checked)} />{" "}{t("开启此 Management Connection 的写操作总开关")}</label><label className="ack"><input type="checkbox" checked={ack} onChange={(event) => setAck(event.target.checked)} />{" "}{t("我确认这是控制面写权限，会触发后端 journal/readback 守卫")}</label><div className="permissionGrid">{PERMISSION_KEYS.map((key) => <label key={key}><input type="checkbox" checked={bools[key] || false} onChange={(event) => setBools({ ...bools, [key]: event.target.checked })} /> {key}</label>)}</div><div className="miniGrid">{LIST_PERMISSION_KEYS.map((key) => <label className="field" key={key}>{key}<textarea value={lists[key] || ""} onChange={(event) => setLists({ ...lists, [key]: event.target.value })} /></label>)}</div><label className="field">{t("Reason")}<input value={reason} onChange={(event) => setReason(event.target.value)} placeholder={t("why this access policy changes")} /></label><button type="submit" disabled={writeEnabled && !ack}>{t("保存 access policy")}</button><ResultPane error={notice.error} result={notice.result} /></form>;
}

function OperationsHistory({ request, managementId }: { request: Requester; managementId: string }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const operations = useApi<Record<string, unknown>>(() => request(`/management/${managementId}/operations?limit=100`), [managementId, refreshKey]);
  return <DataPanel title={t("管理操作历史")} loading={operations.loading} error={operations.error}><button type="button" className="secondary" onClick={() => setRefreshKey((value) => value + 1)}>{t("刷新历史")}</button><OperationTable data={operations.data} /></DataPanel>;
}

function ServiceInventory({ data }: { data: unknown }) {
  const value = record(data);
  const mountClients = record(value.mount_clients).count ?? value.total_mount_clients;
  const rows = [["Master", countOnlyIfList(value.master_nodes), value.master_nodes], ["Filer", countOnlyIfList(value.filer_nodes), value.filer_nodes], ["Volume Server", countOnlyIfList(value.volume_servers), value.volume_servers], ["S3", countOnlyIfList(value.s3_nodes), value.s3_nodes], ["MQ", record(value.mq).count, value.mq], ["Mount Clients", mountClients, value.mount_clients ?? { total_mount_clients: value.total_mount_clients }], ["Plugin Worker", record(record(value.plugin).data).worker_count, record(value.plugin).data]];
  return <DataTable caption="Service inventory" columns={["Service", "Reported count", "Source"]} rows={rows.map(([name, count, raw]) => [t(String(name)), displayValue(count), <span className="monoCell" title={JSON.stringify(raw)}>{count == null ? t("未知") : t("source")}</span>])} />;
}

function DependencyStates({ data, modules, expanded = false }: { data: unknown; modules?: unknown; expanded?: boolean }) {
  const value = record(data);
  const pluginData = record(record(value.plugin).data);
  const moduleData = record(modules);
  const optional = record(moduleData.optional_services);
  const mountCount = record(value.mount_clients).count ?? value.total_mount_clients;
  const dependencies = [{ name: "MQ", state: record(value.mq).state || optional.mq || "unknown", value: displayValue(record(value.mq).count) }, { name: "Mount Clients", state: record(value.mount_clients).state || (mountCount !== undefined ? "reported" : optional.mount_clients || "unknown"), value: displayValue(mountCount) }, { name: "Plugin Worker", state: pluginData.configured ? "reported" : "not_configured", value: displayValue(pluginData.worker_count) }, { name: "Filer endpoint", state: optional.filer || "unknown", value: displayValue(optional.filer) }, { name: "Master endpoint", state: optional.master || "unknown", value: displayValue(optional.master) }, { name: "S3 Tables", state: moduleStatus(record(moduleData.s3_tables)), value: t("official module route") }, { name: "Iceberg", state: optional.iceberg || "unknown", value: t("依赖 plugin / table bucket") }, { name: "Lance", state: optional.lance || "unknown", value: t("依赖 worker plugin") }];
  return <div className={expanded ? "dependencyGrid expanded" : "dependencyGrid"}>{dependencies.map((item) => <StateRow key={item.name} label={item.name} value={item.value} state={String(item.state)} />)}</div>;
}

function ModuleStatusTable({ data }: { data: unknown }) {
  const rows = Object.entries(record(data)).map(([key, value]) => { const item = record(value); return [key, displayValue(item.status), displayValue(item.error_code), <EvidenceDetails title={t("DTO")} data={item.data ?? item} />]; });
  return <DataTable caption="Module endpoints" columns={["Module", "State", "Error", "Evidence"]} rows={rows} />;
}

function TierStatsTable({ rows }: { rows: Record<string, any>[] }) {
  return <DataTable caption="Official tier capacity" columns={["Tier", "Remote", "Volumes", "EC shards", "Data", "Disk used", "Capacity", "Max volumes"]} rows={rows.map((item) => [displayValue(item.name), yesNoUnknown(item.is_remote), displayValue(item.volume_count), displayValue(item.ec_shard_count), formatBytesOrUnknown(item.data_size), formatBytesOrUnknown(item.disk_used), formatBytesOrUnknown(item.disk_capacity), displayValue(item.max_volumes)])} />;
}

function TrendPanel({ data }: { data: unknown }) {
  const value = record(data);
  const entries = Object.entries(value);
  if (!entries.length) return <StateRow label="Trends" value={t("未知")} state="unknown" />;
  return <div className="stateList">{entries.map(([key, raw]) => {
    const points = trendPoints(raw);
    return <div className="stateRow" key={key}><div><strong>{key}</strong><span>{Array.isArray(raw) ? `${points.length} points` : displayValue(raw)}</span>{points.length ? <Sparkline points={points} /> : null}</div><StatusBadge state={points.length || raw !== null && raw !== undefined ? "reported" : "unknown"} label={points.length ? "趋势点" : stateLabel(raw === null || raw === undefined ? "unknown" : "reported")} /></div>;
  })}</div>;
}

function Sparkline({ points }: { points: number[] }) {
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const d = points.map((value, index) => {
    const x = points.length === 1 ? 50 : (index / (points.length - 1)) * 100;
    const y = 30 - ((value - min) / span) * 28;
    return `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(" ");
  return <svg className="sparkline" viewBox="0 0 100 32" role="img" aria-label={t("trend sparkline")}><path d={d} fill="none" stroke="currentColor" strokeWidth="2" /></svg>;
}

function SourceEvidence({ data, extra }: { data: ManagementEnvelope | null; extra?: unknown }) {
  return <div className="stateList"><StateRow label="source" value={displayValue(data?.source)} state={data?.source ? "reported" : "unknown"} /><StateRow label="checked_at" value={displayValue(data?.checked_at)} state={data?.checked_at ? "reported" : "unknown"} /><StateRow label="source_updated_at" value={displayValue(data?.source_updated_at)} state={data?.source_updated_at ? "reported" : "unknown"} /><StateRow label="protocol" value={displayValue(data?.protocol_baseline)} state={data?.protocol_baseline ? "reported" : "unknown"} />{extra ? <StateRow label="s3 public endpoint" value={displayValue(record(extra).s3_public_endpoint)} state={record(extra).s3_public_endpoint ? "reported" : "unknown"} /> : null}<StructuredResult title={t("module source pin")} data={data?.module_source_pin || { state: "unknown" }} /></div>;
}

function TableDetailsPanel({ data }: { data: unknown }) {
  const root = record(data);
  const details = record(root.details);
  const metadata = record(details.metadata);
  const catalog = record(details.catalog);
  const snapshots = array(metadata.snapshots).map(record);
  const history = array(metadata.history).map(record);
  if (!data) return <StateRow label="Table details" value={t("未读取")} state="unknown" />;
  return <div className="stateList"><StateRow label="Status" value={displayValue(root.status)} state={root.status === "supported" ? "reported" : "unknown"} /><StateRow label="Catalog version" value={displayValue(catalog.versionToken ?? catalog.version_token)} state={catalog.versionToken || catalog.version_token ? "reported" : "unknown"} /><StateRow label="Current snapshot" value={displayValue(metadata["current-snapshot-id"] ?? metadata.current_snapshot_id)} state={metadata["current-snapshot-id"] || metadata.current_snapshot_id ? "reported" : "unknown"} /><DataTable caption="Iceberg snapshots" columns={["Snapshot ID", "Timestamp", "Operation", "Manifest list"]} rows={snapshots.slice(0, 8).map((item) => [displayValue(item["snapshot-id"] ?? item.snapshot_id), displayValue(item["timestamp-ms"] ?? item.timestamp_ms), displayValue(item.summary ? record(item.summary).operation : item.operation), textCell(item["manifest-list"] ?? item.manifest_list)])} /><DataTable caption="Iceberg history" columns={["Snapshot ID", "Made current at"]} rows={history.slice(0, 8).map((item) => [displayValue(item["snapshot-id"] ?? item.snapshot_id), displayValue(item["made-current-at"] ?? item.made_current_at)])} /><EvidenceDetails title={t("Table metadata DTO")} data={details} /></div>;
}

function TablePreviewPanel({ data }: { data: unknown }) {
  const root = record(data);
  const files = array(root.files).map(record);
  const rows = array(root.rows).map(record);
  const columns = tablePreviewColumns(root.columns);
  const kind = String(root.preview_kind || "");
  const supported = root.status === "supported";
  if (!data) return <StateRow label="Data preview" value={t("未读取；metadata details 不需要 Scope，样本读取需要授权 Scope")} state="unknown" />;
  return <div className="stateList"><StateRow label="Status" value={displayValue(root.status)} state={supported ? "reported" : root.status === "dependency_unavailable" ? "needs_review" : "unknown"} /><StateRow label="Preview kind" value={previewKindLabel(kind)} state={kind ? "reported" : "unknown"} /><StateRow label="Snapshot ID" value={displayValue(root.snapshot_id)} state={root.snapshot_id == null ? "unknown" : "reported"} /><StateRow label="File list complete" value={yesNoUnknown(root.file_list_complete)} state={root.file_list_complete === true ? "reported" : root.file_list_complete === false ? "needs_review" : "unknown"} /><StateRow label="Delete files" value={root.has_delete_files == null ? t("未知") : root.has_delete_files ? t("存在；未应用") : t("未发现")} state={root.has_delete_files ? "needs_review" : root.has_delete_files === false ? "reported" : "unknown"} /><StateRow label="Deletes applied" value={yesNoUnknown(root.deletes_applied)} state={root.deletes_applied === true ? "reported" : root.deletes_applied === false ? "needs_review" : "unknown"} /><StateRow label="Sample truncated" value={yesNoUnknown(root.sample_truncated)} state={root.sample_truncated === true ? "needs_review" : root.sample_truncated === false ? "reported" : "unknown"} /><StateRow label="Total rows" value={displayValue(root.total_rows)} state={root.total_rows == null ? "unknown" : "reported"} /><p className="hint">{kind === "worker_sample" ? t("样本来自官方 Admin Worker 预览，不是逻辑整表查询。") : t("样本来自原始数据文件，不代表完整逻辑表查询。")}</p>{kind === "raw_file_sample" ? <DataTable caption="Snapshot files" columns={["Location", "Key", "Format", "Records", "Size"]} rows={files.slice(0, 20).map((item) => [textCell(item.location), textCell(item.key), displayValue(item.format), displayValue(item.record_count), formatBytesOrUnknown(item.size_bytes)])} /> : null}{supported ? <DataTable caption={kind === "worker_sample" ? "Worker sample rows" : "Raw file sample rows"} translateColumns={!columns.length} columns={columns.length ? columns.map((column) => column.name) : ["Row"]} rows={rows.map((row) => columns.length ? columns.map((column) => displayValue(row[column.name])) : [<EvidenceDetails title={t("row")} data={row} />])} /> : <StateRow label="Sample rows" value={t("依赖不可用或未知，未读取样本")} state={root.status === "dependency_unavailable" ? "needs_review" : "unknown"} />}<EvidenceDetails title={t("Preview DTO / notes")} data={{ selected_file: root.selected_file, notes: root.notes, bytes_read: root.bytes_read, source: root.source, checked_at: root.checked_at, columns: root.columns }} /></div>;
}
function MountClientsTable({ data }: { data: unknown }) {
  const root = record(data);
  const rows = normalizeItems(data);
  const status = String(root.status || "unknown");
  const count = root.count;
  return <div className="stateList"><StateRow label="Scope" value={displayValue(root.health_scope || "configured_filer")} state={root.health_scope ? "reported" : "unknown"} /><StateRow label="Source" value={displayValue(root.source)} state={root.source ? "reported" : "unknown"} /><StateRow label="Status" value={displayValue(status)} state={serviceHealthState(status)} /><StateRow label="Count" value={count == null ? t("未知") : displayValue(count)} state={count == null ? "unknown" : "reported"} /><DataTable caption="Filer ListMetadataSubscribers" columns={["Client", "Type", "Address", "Path prefix", "Client ID", "Epoch", "Connected", "Filer"]} rows={rows.map((item) => [displayValue(item.client_name ?? item.name ?? item.id), displayValue(item.client_type), textCell(item.address), displayValue(item.path_prefix ?? item.path), displayValue(item.client_id ?? item.id), displayValue(item.client_epoch), displayValue(item.connected_at), textCell(item.filer_address)])} /></div>;
}

function ServiceHealthTable({ data }: { data: unknown }) {
  const root = record(data);
  const rows = serviceHealthRows(root);
  if (!rows.length) return <StateRow label="服务健康" value={t("未提供服务健康探测明细")} state="unknown" />;
  return <DataTable caption="Configured endpoint health" columns={["Role", "Address", "Status", "Version", "Leader", "Source", "Checked"]} rows={rows.map((item) => [displayValue(item.role), textCell(item.address || item.endpoint || item.url), <StatusBadge state={serviceHealthState(item.status)} label={displayValue(item.status)} />, displayValue(item.version), yesNoUnknown(item.leader ?? item.is_leader), displayValue(item.source), displayValue(item.checked_at)])} />;
}

function serviceHealthRows(root: Record<string, any>): Record<string, any>[] {
  const direct = normalizeItems(root.items ?? root.health ?? root.probes);
  if (direct.length) return direct.filter(hasProbeEvidence).map((item) => ({ ...item, version: serviceProbeVersion(item) }));
  const services = record(root.services);
  const rows: Record<string, any>[] = [];
  for (const [role, value] of Object.entries(services)) {
    const item = record(value);
    if (!hasProbeEvidence(item)) continue;
    rows.push({ role, ...item, version: serviceProbeVersion(item) });
  }
  return rows;
}

function hasProbeEvidence(item: Record<string, any>) {
  return Boolean(item.status || item.address || item.endpoint || item.url || item.version || item.source || item.checked_at || item.leader !== undefined || item.is_leader !== undefined || array(item.observations).length);
}

function serviceProbeVersion(item: Record<string, any>) {
  const value = (version: unknown) => typeof version === "string" ? version : typeof record(version).value === "string" ? String(record(version).value) : undefined;
  if (value(item.version)) return value(item.version);
  for (const observation of array(item.observations).map(record)) {
    if (value(observation.version)) return value(observation.version);
  }
  return undefined;
}

function serviceHealthState(status: unknown) {
  const text = String(status || "unknown").toLowerCase();
  if (["healthy", "ok", "reachable", "supported", "reported", "configured"].includes(text)) return "reported";
  if (text === "not_configured") return "not_configured";
  if (text === "permission_denied") return "permission_denied";
  if (["unreachable", "bad_response", "failed", "error"].includes(text)) return "error";
  return "unknown";
}

function TopologyGroup({ title, nodes, addressKey, leaderKey }: { title: string; nodes: Record<string, unknown>[]; addressKey: string; leaderKey?: string }) {
  return <div className="topologyGroup"><h3>{title}</h3>{nodes.length ? nodes.map((node, index) => <div className="topologyNode" key={`${title}-${index}`}><strong title={String(node[addressKey] || node.id || "未知")}>{displayValue(node[addressKey] || node.id)}</strong><span>{displayValue(node.datacenter)} / {displayValue(node.rack)}</span>{leaderKey && node[leaderKey] === true ? <StatusBadge state="reported" label="leader" /> : <StatusBadge state="unknown" label={leaderKey ? "follower/未知" : "官方报告"} />}</div>) : <StateRow label={title} value={t("未知或未配置")} state="unknown" />}</div>;
}

function ManagementConnectionTable({ items, selectedId, onSelect }: { items: ManagementConnection[]; selectedId: string; onSelect: (value: string) => void }) {
  return <DataTable caption="Management connections" columns={["Name", "Admin URL", "Baseline", "Writes", "Permissions", "Updated"]} rows={items.map((item) => [<button type="button" className="linkButton" onClick={() => onSelect(item.id)}>{item.name}{selectedId === item.id ? t(" · selected") : ""}</button>, textCell(item.admin_url), item.protocol_baseline || "4.48", item.management_write_enabled ? t("enabled") : t("disabled"), Object.entries(item.permissions || {}).filter(([, value]) => value === true || (Array.isArray(value) && value.length)).map(([key]) => key).join(", ") || t("readonly"), item.updated_at || t("未知")])} />;
}

function OperationTable({ data }: { data: unknown }) {
  const rows = array(record(data).items).map(record);
  return <DataTable caption="Management operation journal" columns={["ID", "Action", "State", "Error", "Updated"]} rows={rows.map((item) => [textCell(item.id), displayValue(item.action), <StatusBadge state={String(item.state || "unknown")} label={displayValue(item.state)} />, displayValue(item.error_code), displayValue(item.updated_at)])} />;
}

function DataPanel({ title, loading, error, children }: { title: string; loading: boolean; error: string; children: React.ReactNode }) {
  return <div className="panel inlinePanel"><h2>{t(title)}</h2><div className="panelContent">{loading && <div className="notice">{t("读取中")}</div>}{error && <div className="notice error">{translateMessage(error)}</div>}{!loading && !error ? children : null}</div></div>;
}

function PaginationBar({ firstDisabled, nextDisabled, onFirst, onNext, status }: { firstDisabled: boolean; nextDisabled: boolean; onFirst: () => void; onNext: () => void; status: React.ReactNode }) {
  return <div className="paginationBar"><div className="paginationActions"><button type="button" disabled={firstDisabled} onClick={onFirst}>{t("第一页")}</button><button type="button" disabled={nextDisabled} onClick={onNext}>{t("下一页")}</button></div><span className="paginationStatus">{status}</span></div>;
}

function DataTable({ caption, columns, rows, translateColumns = true }: { caption: string; columns: string[]; rows: React.ReactNode[][]; translateColumns?: boolean }) {
  if (!rows.length) return <div className="empty">{t("No records")}</div>;
  return <div className={`tableWrap${columns.length <= 4 ? " compactTable" : ""}`}><table><caption>{t(caption)}</caption><thead><tr>{columns.map((column) => <th key={column}>{translateColumns ? t(column) : column}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div>;
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return <div className="summaryCard"><span>{t(label)}</span><strong>{value}</strong></div>;
}

function StateRow({ label, value, state }: { label: string; value: React.ReactNode; state: string }) {
  return <div className="stateRow"><div><strong>{t(label)}</strong><span>{value}</span></div><StatusBadge state={state} label={stateLabel(state)} /></div>;
}

function StatusBadge({ state, label, title }: { state: string; label?: string; title?: string }) {
  const kind = ["reported", "supported", "ok", "confirmed", "configured"].includes(state) ? "good" : ["error", "permission_denied", "unsupported", "failed", "needs_review"].includes(state) ? "bad" : "";
  return <span className={`pill ${kind}`} title={title ? t(title) : state}>{label ? t(label) : stateLabel(state)}</span>;
}

function EvidenceDetails({ title, data }: { title: string; data: unknown }) {
  return <details className="evidence"><summary>{t(title)}</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>;
}

function ResultPane({ error, result }: Notice) {
  const state = result && typeof result === "object" ? String(record(result).state || record(result).status || "") : "";
  return <>{error && <div className="notice error">{translateMessage(error)}</div>}{state === "needs_review" && <div className="notice">{t("操作返回 needs_review：请在管理操作历史中核查 readback/状态，不会自动重发。")}</div>}{result ? <ResultSummary title={t("执行结果")} data={result} /> : null}</>;
}

function useApi<T>(loader: () => Promise<T>, deps: React.DependencyList): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>({ loading: true, error: "", data: null });
  useEffect(() => { let alive = true; setState((current) => ({ ...current, loading: true, error: "" })); loader().then((data) => { if (alive) setState({ loading: false, error: "", data }); }).catch((err) => { if (alive) setState({ loading: false, error: String((err as Error).message || err), data: null }); }); return () => { alive = false; }; }, deps);
  return state;
}

async function run(setNotice: (notice: Notice) => void, fn: () => Promise<unknown>) {
  setNotice({ error: "", result: null });
  try { const result = await fn(); setNotice({ error: "", result }); return result; } catch (err) { setNotice({ error: String((err as Error).message || err), result: null }); return null; }
}

function record(value: unknown): Record<string, any> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, any> : {};
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function normalizeItems(value: unknown): Record<string, any>[] {
  if (Array.isArray(value)) return value.map(record);
  const data = record(value);
  for (const key of ["items", "job_types", "workers", "activities", "jobs", "lanes", "buckets", "Buckets", "namespaces", "Namespaces", "tables", "Tables"]) if (Array.isArray(data[key])) return data[key].map(record);
  return [];
}

function canonicalPolicyRows(value: unknown): Record<string, any>[] {
  const data = record(value);
  for (const key of ["items", "policies", "Policies"]) if (Array.isArray(data[key])) return data[key].map(record);
  if (Array.isArray(value)) return value.map(record);
  return [];
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return t("Unknown");
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : t("Unknown");
  if (typeof value === "string" && value.startsWith("0001-01-01")) return t("Synthetic time unknown");
  return String(value);
}

function textCell(value: unknown) {
  const text = displayValue(value);
  return <span className="monoCell" title={text}>{text}</span>;
}

function countOrUnknown(value: unknown) {
  return Array.isArray(value) ? String(value.length) : t("Unknown");
}

function countOnlyIfList(value: unknown) {
  if (Array.isArray(value)) return value.length;
  const nested = record(value);
  for (const key of ["Rules", "rules", "Statement", "statement", "statements"]) {
    if (Array.isArray(nested[key])) return nested[key].length;
  }
  return null;
}

function knownNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function firstKnownNumber(values: unknown[]) {
  for (const value of values) { const number = knownNumber(value); if (number !== null) return number; }
  return null;
}

function trendPoints(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    if (typeof item === "number" && Number.isFinite(item)) return item;
    const row = record(item);
    return knownNumber(row.value) ?? knownNumber(row.count) ?? knownNumber(row.total) ?? knownNumber(row.y);
  }).filter((item): item is number => item !== null);
}

function sumKnown(values: unknown[]) {
  let seen = false;
  let total = 0;
  for (const value of values) { const number = knownNumber(value); if (number !== null) { seen = true; total += number; } }
  return seen ? total : null;
}

function formatBytesOrUnknown(value: unknown) {
  const number = knownNumber(value);
  if (number === null) return t("Unknown");
  if (number < 1024) return `${number} B`;
  if (number < 1024 * 1024) return `${(number / 1024).toFixed(1)} KiB`;
  if (number < 1024 * 1024 * 1024) return `${(number / 1024 / 1024).toFixed(1)} MiB`;
  return `${(number / 1024 / 1024 / 1024).toFixed(1)} GiB`;
}

function percentOrUnknown(value: unknown) {
  const number = knownNumber(value);
  return number === null ? t("Unknown") : `${(number * 100).toFixed(2)}%`;
}

function yesNoUnknown(value: unknown) {
  if (value === true) return t("Yes");
  if (value === false) return t("No");
  return t("Unknown");
}

function stateLabel(state: string) {
  const label = ({ reported: "Official report", supported: "Official report", configured: "Configured", empty_response: "Empty response", not_configured: "Not configured", unsupported: "Unsupported", permission_denied: "Permission denied", confirmed: "Verified", needs_review: "Needs review", failed: "Failed", error: "Error", unknown: "Unknown" } as Record<string, string>)[state] || "Unknown";
  return t(label);
}

function moduleStatus(value: Record<string, any>) {
  const states = Object.values(value).map((item) => record(item).status).filter(Boolean);
  if (states.includes("supported")) return "reported";
  if (states.includes("not_configured")) return "not_configured";
  if (states.includes("unsupported")) return "unsupported";
  return "unknown";
}

function can(connection: ManagementConnection | undefined, permission: string) {
  return Boolean(connection?.management_write_enabled && connection.permissions?.[permission] === true);
}

function disabledTitle(permission: string) {
  return t("management_write_enabled is disabled or {permission} is not granted", { permission });
}

function compactObject(value: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== "" && item !== undefined));
}

function lines(value: string) {
  return value.split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean);
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(value || "{}");
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("not object");
    return parsed as Record<string, unknown>;
  } catch (err) {
    throw new Error(t("{label} JSON parse failed: {message}", { label, message: String((err as Error).message) }));
  }
}

function userPayload(value: { username: string; email: string; actions: string; policy_names: string; generate_key: boolean }) {
  return { username: value.username, email: value.email, actions: lines(value.actions), policy_names: lines(value.policy_names), generate_key: value.generate_key };
}

function servicePayload(value: { parent_user: string; description: string; expiration: string; status: string }) {
  return compactObject({ parent_user: value.parent_user, description: value.description, expiration: value.expiration, status: value.status });
}

function bucketCreatePayload(value: { name: string; region: string; advanced: boolean; owner: string; quota_size: string; quota_unit: string; quota_enabled: boolean; versioning_enabled: boolean; object_lock_enabled: boolean; set_default_retention: boolean; object_lock_mode: string; object_lock_duration: string }) {
  const payload: Record<string, unknown> = { name: value.name, region: value.region || "us-east-1" };
  if (!value.advanced) return payload;
  const versioningEnabled = Boolean(value.versioning_enabled || value.object_lock_enabled || value.set_default_retention);
  payload.versioning_enabled = versioningEnabled;
  payload.object_lock_enabled = Boolean(value.object_lock_enabled || value.set_default_retention);
  payload.set_default_retention = Boolean(value.set_default_retention);
  if (value.set_default_retention) {
    payload.object_lock_mode = value.object_lock_mode;
    payload.object_lock_duration = Number(value.object_lock_duration);
  }
  if (value.quota_size || value.quota_enabled) {
    payload.quota_size = Number(value.quota_size);
    payload.quota_unit = value.quota_unit || "GB";
    payload.quota_enabled = Boolean(value.quota_enabled);
  }
  if (value.owner) payload.owner = value.owner;
  return payload;
}

function versionIdentityLabel(value: unknown) {
  if (value === null) return t("Unversioned (no Version ID)");
  if (value === "null") return t("null (mutable version)");
  return displayValue(value);
}

function randomIntentKey() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `intent-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function tablePreviewPayload(value: { bucket_arn: string; namespace: string; table_name: string; format: string; snapshot_id: string; file_location: string; preview_limit: string }, scopeId: string) {
  const limit = Math.max(1, Math.min(100, Number(value.preview_limit) || 20));
  const lance = isLanceTable(value);
  return compactObject({ scope_id: scopeId, bucket_arn: value.bucket_arn, namespace: value.namespace, name: value.table_name, snapshot_id: lance ? undefined : value.snapshot_id.trim() || undefined, file_location: lance ? undefined : value.file_location.trim() || undefined, limit });
}

function tableDetailKey(managementId: string, value: { bucket_arn: string; namespace: string; table_name: string }) {
  return JSON.stringify({ managementId, bucket_arn: value.bucket_arn, namespace: value.namespace, name: value.table_name });
}

function tablePreviewKey(managementId: string, value: { bucket_arn: string; namespace: string; table_name: string; format: string; snapshot_id: string; file_location: string; preview_limit: string }, scopeId: string) {
  return JSON.stringify({ managementId, ...tablePreviewPayload(value, scopeId) });
}

function isLanceTable(value: { format?: string }) {
  return String(value.format || "").toLowerCase() === "lance";
}

function previewKindLabel(value: string) {
  if (value === "raw_file_sample") return t("Raw data file sample");
  if (value === "worker_sample") return t("Worker sample");
  return displayValue(value);
}

function tablePreviewColumns(value: unknown) {
  return array(value).map((item) => {
    if (typeof item === "string") return { name: item };
    const row = record(item);
    return { name: String(row.name || row.field || row.column || "unknown"), type: row.type, nullable: row.nullable };
  }).filter((item) => item.name !== "unknown");
}

function maintenancePayload(value: { action: string; job_type: string; job_id: string; params: string }) {
  const parsed = parseJsonObject(value.params || "{}", value.action === "config_update" ? "maintenance config" : value.action === "job_execute" ? "maintenance job" : "maintenance params");
  if (value.action === "job_execute") return { action: value.action, job: parsed };
  if (value.action === "config_update") return compactObject({ action: value.action, job_type: value.job_type, config: parsed });
  return compactObject({ action: value.action, job_type: value.job_type, job_id: value.job_id, params: parsed });
}

