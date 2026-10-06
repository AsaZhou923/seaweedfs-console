import React, { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { t, translateMessage, useLocale } from "./i18n";
import { LanguageSelector } from "./i18n/LanguageSelector";
import { ResultSummary, StructuredResult as ApiStructuredResult } from "./components/ResultSummary";
import { ManagementConnectionSelector, ManagementRoute, ManagementSettings, isManagementRoute, managementTitle, type ManagementConnection, type StorageScopeOption } from "./management/ManagementViews";

type Requester = <T = unknown>(path: string, init?: RequestInit) => Promise<T>;
type Project = { id: string; project_key?: string; display_name: string; description?: string; scopes?: Scope[] };
type Scope = {
  id: string;
  project_id?: string;
  connection_id?: string;
  display_name?: string;
  bucket?: string;
  prefix?: string;
  allow_preview?: boolean;
  allow_original_download?: boolean;
  writable?: boolean;
  manage_bucket?: boolean;
  index_generation?: number;
  authz_epoch?: number;
};
type Connection = {
  id: string;
  display_name: string;
  endpoint_url: string;
  region?: string;
  secret_ref: string;
  addressing_style?: string;
  verify_tls?: boolean;
  capabilities?: Record<string, unknown>;
  server_version?: string | null;
};
type Asset = {
  id: string;
  asset_id: string;
  scope_id: string;
  bucket: string;
  key: string;
  key_display?: string;
  revision: string;
  version_id?: string | null;
  size_bytes: number;
  content_type?: string | null;
  properties?: Record<string, unknown>;
  checksum?: string | null;
  preview_state?: string;
  reference_status?: string;
  last_modified?: string | null;
  observed_at?: string;
};
type Job = {
  id: string;
  scope_id: string;
  kind: string;
  state: string;
  processed: number;
  errors: number;
  total: number | null;
  control_request: string;
  updated_at: string;
};
type Preset = { id: string; name: string; version: number; kind?: string; params?: Record<string, unknown> };

const API = "/api/v1";
const SESSION_EXPIRED_MESSAGE = "SESSION_EXPIRED";
const STALE_AUTH_RESPONSE_MESSAGE = "STALE_AUTH_RESPONSE";
const INITIAL_UNAUTHENTICATED_MESSAGE = "INITIAL_UNAUTHENTICATED";
const MANAGEMENT_RECEIPT_UNCERTAIN_MESSAGE = "MANAGEMENT_RECEIPT_UNCERTAIN";

function isAuthBootstrapPath(path: string) {
  return path === "/auth/me" || path === "/auth/login";
}

function isUnauthenticatedResponse(status: number, data: any) {
  const code = String(data?.error?.code || data?.code || "");
  const message = String(data?.error?.message || data?.message || data?.raw || "");
  return status === 401 || code.includes("UNAUTHENTICATED") || message.includes("UNAUTHENTICATED");
}

function isSessionExpiredMessage(value: unknown) {
  const message = String(value instanceof Error ? value.message : value || "");
  return message.includes(SESSION_EXPIRED_MESSAGE);
}

function isStaleAuthMessage(value: unknown) {
  const message = String(value instanceof Error ? value.message : value || "");
  return message.includes(STALE_AUTH_RESPONSE_MESSAGE);
}

function isInitialUnauthenticatedMessage(value: unknown) {
  const message = String(value instanceof Error ? value.message : value || "");
  return message.includes(INITIAL_UNAUTHENTICATED_MESSAGE);
}

function App() {
  const locale = useLocale();
  const [csrf, setCsrf] = useState("");
  const [user, setUser] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [managementConnections, setManagementConnections] = useState<ManagementConnection[]>([]);
  const [managementLoading, setManagementLoading] = useState(true);
  const [managementId, setManagementId] = useState("");
  const [scopes, setScopes] = useState<Scope[]>([]);
  const [projectId, setProjectId] = useState("");
  const [scopeId, setScopeId] = useState("");
  const [route, setRoute] = useState(location.hash.replace("#", "") || "dashboard");
  const [deriveObjectIds, setDeriveObjectIds] = useState<string[]>([]);
  const [selectedObjectIds, setSelectedObjectIds] = useState<string[]>([]);
  const [operationTab, setOperationTab] = useState("copy");
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [error, setError] = useState("");
  const authGeneration = useRef(0);
  const userRef = useRef("");
  const sessionInitializedRef = useRef(false);
  const projectIdRef = useRef("");
  const scopeIdRef = useRef("");
  const scopesRequestRef = useRef(0);
  const unsafeIntentKeys = useRef(loadStoredIntentKeys());
  const pendingUnsafeRequests = useRef(new Set<string>());

  function selectProject(nextProjectId: string) {
    projectIdRef.current = nextProjectId;
    setProjectId(nextProjectId);
  }

  function selectScope(nextScopeId: string) {
    scopeIdRef.current = nextScopeId;
    setScopeId(nextScopeId);
  }

  function clearSessionState(message?: string, advanceGeneration = true) {
    if (advanceGeneration) authGeneration.current += 1;
    setCsrf("");
    userRef.current = "";
    setUser("");
    setProjects([]);
    setConnections([]);
    setManagementConnections([]);
    setManagementLoading(false);
    setManagementId("");
    setScopes([]);
    selectProject("");
    selectScope("");
    setDeriveObjectIds([]);
    setSelectedObjectIds([]);
    setOperationTab("copy");
    if (message) setError(message);
  }

  function setAuthenticatedSession(username: string, csrfToken: string, advanceGeneration: boolean) {
    if (advanceGeneration) authGeneration.current += 1;
    sessionInitializedRef.current = true;
    setError("");
    userRef.current = username;
    setUser(username);
    setCsrf(csrfToken);
  }

  function handleSessionExpired(requestGeneration: number) {
    if (authGeneration.current !== requestGeneration) return false;
    clearSessionState(SESSION_EXPIRED_MESSAGE);
    return true;
  }

  async function request<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
    const requestGeneration = authGeneration.current;
    const headers = new Headers(init.headers);
    if (csrf && init.method && init.method !== "GET") headers.set("X-CSRF-Token", csrf);
    const method = String(init.method || "GET").toUpperCase();
    const bodyFingerprint = typeof init.body === "string" ? init.body : init.body instanceof FormData ? [...init.body.entries()].map(([key, value]) => [key, value instanceof File ? `${value.name}:${value.size}:${value.lastModified}` : String(value)]) : "";
    const unsafeFingerprint = method === "GET" ? "" : pendingFingerprint(method, path, bodyFingerprint);
    const durableFingerprint = isDurableManagementMutation(method, path) ? intentFingerprint(method, path, bodyFingerprint) : "";
    let requestPath = path;
    let requestBody = init.body;
    if (durableFingerprint) {
      const idempotencyKey = headers.get("Idempotency-Key") || headers.get("X-Idempotency-Key") || intentKeyFor(unsafeIntentKeys.current, durableFingerprint);
      headers.set("Idempotency-Key", idempotencyKey);
      headers.set("X-Idempotency-Key", idempotencyKey);
      if (typeof requestBody === "string" && supportsBodyIdempotency(method, requestPath)) {
        try {
          const parsed = JSON.parse(requestBody || "{}");
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && !Object.hasOwn(parsed, "idempotency_key")) {
            requestBody = JSON.stringify({ ...parsed, idempotency_key: idempotencyKey });
          }
        } catch {
          // Non-JSON unsafe bodies still carry headers.
        }
      } else if (requestBody instanceof FormData && supportsQueryIdempotency(method, requestPath)) {
        const separator = requestPath.includes("?") ? "&" : "?";
        requestPath = `${requestPath}${separator}idempotency_key=${encodeURIComponent(idempotencyKey)}`;
      }
      if (requestBody && !(requestBody instanceof FormData) && !(requestBody instanceof Blob) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    } else if (requestBody && !(requestBody instanceof FormData) && !(requestBody instanceof Blob) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (unsafeFingerprint) {
      if (pendingUnsafeRequests.current.has(unsafeFingerprint)) throw new Error("REQUEST_ALREADY_PENDING");
      pendingUnsafeRequests.current.add(unsafeFingerprint);
    }
    let data: any = {};
    try {
      const response = await fetch(`${API}${requestPath}`, { ...init, body: requestBody, headers, credentials: "same-origin" });
      const text = await readResponseText(response, Boolean(durableFingerprint));
      const parsedJson = parseResponseText(text, (parsed) => {
        data = parsed;
      });
      if (text && !parsedJson) data = { raw: text };
      if (authGeneration.current !== requestGeneration) {
        throw new Error(STALE_AUTH_RESPONSE_MESSAGE);
      }
      if (!response.ok) {
        if (isUnauthenticatedResponse(response.status, data)) {
          if (path === "/auth/login") {
            const err = data?.error;
            throw new Error(err ? `${err.code}: ${err.message}` : `${response.status} ${response.statusText}`);
          }
          if (path === "/auth/me" && !userRef.current && !sessionInitializedRef.current) {
            throw new Error(INITIAL_UNAUTHENTICATED_MESSAGE);
          }
          const handled = handleSessionExpired(requestGeneration);
          throw new Error(handled ? `UNAUTHENTICATED: ${SESSION_EXPIRED_MESSAGE}` : STALE_AUTH_RESPONSE_MESSAGE);
        }
        const err = data?.error;
        throw new Error(err ? `${err.code}: ${err.message}` : `${response.status} ${response.statusText}`);
      }
      if (durableFingerprint) {
        const receipt = classifyDurableManagementReceipt(data, parsedJson);
        if (receipt === "terminal") removeIntentKey(unsafeIntentKeys.current, durableFingerprint);
        else if (receipt === "malformed") throw new Error(MANAGEMENT_RECEIPT_UNCERTAIN_MESSAGE);
      }
      return data as T;
    } finally {
      if (unsafeFingerprint) pendingUnsafeRequests.current.delete(unsafeFingerprint);
    }
  }

  async function refresh(options: { bootstrap?: boolean } = {}) {
    const me = await request<{ user: { username: string }; csrf_token: string; projects: any[] }>("/auth/me");
    const normalized = normalizeProjects(me.projects || []);
    setAuthenticatedSession(me.user.username, me.csrf_token, Boolean(options.bootstrap && !sessionInitializedRef.current));
    setProjects(normalized);
    const currentProjectId = projectIdRef.current;
    const chosenProject = currentProjectId && normalized.some((item) => item.id === currentProjectId) ? currentProjectId : normalized[0]?.id || "";
    if (chosenProject !== currentProjectId) selectProject(chosenProject);
    const list = await request<{ items: Connection[] }>("/connections");
    setConnections(list.items || []);
    await refreshManagementConnections();
    if (chosenProject) await refreshScopes(chosenProject);
  }

  async function refreshScopes(nextProjectId = projectIdRef.current) {
    if (!nextProjectId) return;
    const requestId = ++scopesRequestRef.current;
    const previousProjectId = projectIdRef.current;
    if (previousProjectId !== nextProjectId) {
      projectIdRef.current = nextProjectId;
      selectScope("");
      setScopes([]);
    }
    const result = await request<{ items: Scope[] }>(`/projects/${nextProjectId}/scopes`);
    if (scopesRequestRef.current !== requestId || projectIdRef.current !== nextProjectId) return;
    const items = result.items || [];
    setScopes(items);
    setScopeId((current) => {
      const latest = scopeIdRef.current || current;
      const next = latest && items.some((scope) => scope.id === latest) ? latest : items[0]?.id || "";
      scopeIdRef.current = next;
      return next;
    });
  }

  async function refreshManagementConnections() {
    const requestGeneration = authGeneration.current;
    setManagementLoading(true);
    try {
      const result = await request<{ items: ManagementConnection[] }>("/management/connections");
      const items = result.items || [];
      setManagementConnections(items);
      setManagementId((current) => current && items.some((item) => item.id === current) ? current : items[0]?.id || "");
    } catch (err) {
      if (isSessionExpiredMessage(err)) return;
      if (isStaleAuthMessage(err)) return;
      setManagementConnections([]);
      setManagementId("");
      setError(`管理连接读取失败：${String(err instanceof Error ? err.message : err)}`);
    } finally {
      if (authGeneration.current === requestGeneration) setManagementLoading(false);
    }
  }

  useEffect(() => {
    const onHash = () => setRoute(location.hash.replace("#", "") || "dashboard");
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    refresh({ bootstrap: true }).catch((err) => {
      const message = String(err.message || err);
      if (isInitialUnauthenticatedMessage(message) || isStaleAuthMessage(message) || isSessionExpiredMessage(message)) return;
      clearSessionState();
      setError(message);
    }).finally(() => {
      sessionInitializedRef.current = true;
    });
  }, []);

  useEffect(() => {
    refreshScopes(projectId).catch((err) => {
      if (!isSessionExpiredMessage(err) && !isStaleAuthMessage(err)) setError(String(err.message || err));
    });
  }, [projectId]);

  useEffect(() => {
    setDeriveObjectIds([]);
    setSelectedObjectIds([]);
  }, [scopeId]);

  if (!user) return <Login request={request} onAuthenticated={(username, csrfToken) => setAuthenticatedSession(username, csrfToken, true)} refresh={refresh} error={error} />;

  const project = projects.find((item) => item.id === projectId);
  const scope = scopes.find((item) => item.id === scopeId);
  const scopeLabel = scope?.bucket ? `${scope.bucket}${scope.prefix ? `/${scope.prefix}` : ""}` : t("未选择 scope");
  const imageRoute = isImageRoute(route);
  const managementRoute = isManagementRoute(route);

  return (
    <div className="shell" data-locale={locale}>
      <aside className="sidebar" data-nav-open={navigationOpen}>
        <div className="brand">
          <span className="mark"><AppIcon /></span>
          <div>
            <strong>SeaweedFS Console</strong>
            <small>{t("OSS Admin + Images")}</small>
          </div>
        </div>
        <button type="button" className="mobileNavToggle" aria-controls="main-navigation" aria-expanded={navigationOpen} onClick={() => setNavigationOpen((open) => !open)}>{navigationOpen ? t("收起导航") : t("导航")}</button>
        <nav id="main-navigation" onClick={(event) => { if (event.target instanceof Element && event.target.closest("a")) setNavigationOpen(false); }}>
          <span className="navGroupLabel">{t("管理")}</span>
          {[
            ["dashboard", "dashboard", "Dashboard"],
            ["topology", "topology", "拓扑服务"],
            ["storage", "storage", "Volumes / EC"],
            ["buckets", "bucket", "桶"],
            ["files", "files", "Filer 文件"],
            ["objects", "objects", "实时对象"],
            ["iam", "key", "S3 / IAM"],
            ["maintenance", "wrench", "Worker 维护"],
            ["services", "plug", "其他服务"]
          ].map(([id, icon, label]) => (
            <a key={id} href={`#${id}`} className={route === id ? "active" : ""}><Icon name={icon} />{t(label)}</a>
          ))}
          <span className="navGroupLabel">{t("图片增强")}</span>
          {[
            ["assets", "images", "资产"],
            ["scans", "activity", "任务"],
            ["diagnostics", "shield", "诊断"],
            ["operations", "workflow", "操作"],
            ["presets", "sliders", "规格"]
          ].map(([id, icon, label]) => (
            <a key={id} href={`#${id}`} className={route === id ? "active" : ""}><Icon name={icon} />{t(label)}</a>
          ))}
          <span className="navGroupLabel">{t("系统")}</span>
          <a href="#settings" className={route === "settings" ? "active" : ""}><Icon name="settings" />{t("设置")}</a>
        </nav>
        <button className="ghost" onClick={() => { void request("/auth/logout", { method: "POST", body: "{}" }).catch(() => undefined).finally(() => location.reload()); }}>{t("退出")}</button>
      </aside>
      <main className="main">
        <header className="topbar">
          <div>
            <p>{managementRoute ? t("Control Plane") : imageRoute ? `${project?.display_name || t("no-project")} · ${scope?.display_name || scope?.bucket || t("no-scope")}` : t("Settings")}</p>
            <h1>{title(route)}</h1>
          </div>
          <LanguageSelector />
          {managementRoute && (
            <ManagementConnectionSelector
              connections={managementConnections}
              value={managementId}
              onChange={setManagementId}
              onRefresh={() => refreshManagementConnections().catch((err) => setError(String(err.message || err)))}
              compact
            />
          )}
          {(imageRoute || route === "objects") && <div className="contextBar">
            <label className="field compact">
              {t("项目")}
              <select value={projectId} onChange={(event) => selectProject(event.target.value)}>
                {projects.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}
              </select>
            </label>
            <label className="field compact">
              {t("Scope")}
              <select value={scopeId} onChange={(event) => selectScope(event.target.value)}>
                <option value="">{t("未选择")}</option>
                {scopes.map((item) => <option key={item.id} value={item.id}>{item.display_name || `${item.bucket}/${item.prefix || ""}`}</option>)}
              </select>
            </label>
            <StatusPill text={scopeLabel} />
          </div>}
        </header>
        {error && <div className="notice error">{translateMessage(error)}<button onClick={() => setError("")}>×</button></div>}
        {managementRoute && (managementLoading ? <div className="panel" role="status">{t("正在读取管理连接…")}</div> : <ManagementRoute route={route} request={request} managementId={managementId} connections={managementConnections} s3Connections={connections} refreshConnections={refreshManagementConnections} scopes={scopes as StorageScopeOption[]} scopeId={scopeId} setScopeId={setScopeId} onObjectSelected={(objectId) => { setSelectedObjectIds([objectId]); setOperationTab("object"); }} />)}
        {route === "assets" && <Assets request={request} scopeId={scopeId} projectId={projectId} onSelectionChange={setSelectedObjectIds} sendToOperationsCopy={(ids) => { setSelectedObjectIds(ids); setOperationTab("copy"); location.hash = "operations"; }} sendToPresets={(ids) => { setDeriveObjectIds(ids); location.hash = "presets"; }} />}
        {route === "scans" && <Scans request={request} scopeId={scopeId} onChanged={() => refreshScopes(projectId)} />}
        {route === "diagnostics" && <Diagnostics request={request} scopeId={scopeId} />}
        {route === "operations" && <Operations request={request} scopeId={scopeId} scope={scope} sourceObjectIds={selectedObjectIds} initialTab={operationTab} />}
        {route === "presets" && <Presets request={request} projectId={projectId} scopeId={scopeId} initialObjectIds={deriveObjectIds} />}
        {route === "settings" && <Settings request={request} projects={projects} connections={connections} managementConnections={managementConnections} refresh={refresh} refreshManagementConnections={refreshManagementConnections} projectId={projectId} setProjectId={selectProject} />}
      </main>
    </div>
  );
}

function Login({ request, onAuthenticated, refresh, error }: { request: Requester; onAuthenticated: (username: string, csrfToken: string) => void; refresh: () => Promise<void>; error: string }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setLocalError("");
    try {
      const result = await request<{ user: { username: string }; csrf_token: string }>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
      onAuthenticated(result.user.username, result.csrf_token);
      await refresh();
    } catch (err) {
      setLocalError(String((err as Error).message));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="loginBox" onSubmit={submit}>
        <span className="mark large">S</span>
        <h1>SeaweedFS Console</h1>
        <label>{t("用户名")}<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" /></label>
        <label>{t("密码")}<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>
        <LanguageSelector />
        <button disabled={busy}>{busy ? t("登录中") : t("登录")}</button>
        {(localError || error) && <p className="formError">{translateMessage(localError || error)}</p>}
      </form>
    </div>
  );
}

function Assets({ request, scopeId, projectId, onSelectionChange, sendToOperationsCopy, sendToPresets }: { request: Requester; scopeId: string; projectId: string; onSelectionChange: (objectIds: string[]) => void; sendToOperationsCopy: (objectIds: string[]) => void; sendToPresets: (objectIds: string[]) => void }) {
  const [items, setItems] = useState<Asset[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [filters, setFilters] = useState({
    prefix: "",
    query: "",
    format: "",
    decode_status: "",
    tag: "",
    min_width: "",
    max_width: "",
    min_height: "",
    max_height: "",
    min_ratio: "",
    max_ratio: "",
    min_size: "",
    max_size: "",
    after: "",
    before: "",
    presence: ""
  });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<Asset | null>(null);
  const [viewMode, setViewMode] = useState<"grid" | "list">("grid");
  const [snapshotMessage, setSnapshotMessage] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [duplicates, setDuplicates] = useState<unknown>(null);
  const [capacity, setCapacity] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const token = useRef(0);

  const query = useMemo(() => new URLSearchParams(Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1]))).toString(), [filters]);
  const contextKey = `${scopeId}\n${query}`;

  function clearAssetState() {
    token.current += 1;
    setItems([]);
    setCursor(null);
    setSelected(new Set());
    setDetail(null);
    setJobs([]);
    setDuplicates(null);
    setCapacity(null);
    setSnapshotMessage("");
    setError("");
    setLoading(false);
  }

  async function load(next = false) {
    const requestScopeId = scopeId;
    const requestQuery = query;
    const requestCursor = cursor;
    if (!requestScopeId) {
      clearAssetState();
      return;
    }
    const current = ++token.current;
    setLoading(true);
    setError("");
    try {
      const suffix = [requestQuery, next && requestCursor ? `cursor=${encodeURIComponent(requestCursor)}` : ""].filter(Boolean).join("&");
      const result = await request<{ items: Asset[]; next_cursor: string | null }>(`/scopes/${requestScopeId}/assets${suffix ? `?${suffix}` : ""}`);
      if (token.current !== current || requestScopeId !== scopeId || requestQuery !== query) return;
      setItems((old) => next ? [...old, ...result.items] : result.items);
      setCursor(result.next_cursor);
      if (!next) setSelected(new Set());
      setDetail((old) => old && old.scope_id === requestScopeId && result.items.some((item) => item.id === old.id) ? old : null);
    } catch (err) {
      if (token.current === current) setError(String((err as Error).message));
    } finally {
      if (token.current === current) setLoading(false);
    }
  }

  async function submitPreviews() {
    if (!scopeId) return;
    const ids = selected.size ? [...selected] : items.map((item) => item.id);
    if (!ids.length) return;
    const job = await request<Job>(`/scopes/${scopeId}/previews/batch`, { method: "POST", body: JSON.stringify({ object_ids: ids }) });
    setJobs((old) => [job, ...old.filter((item) => item.id !== job.id)]);
  }

  async function snapshot() {
    if (!scopeId) return;
    const ids = selected.size ? [...selected] : undefined;
    const result = await request(`/scopes/${scopeId}/snapshots`, { method: "POST", body: JSON.stringify(ids ? { object_ids: ids } : { filters }) });
    setSnapshotMessage(`snapshot ${String((result as any).id)} count=${String((result as any).count)}`);
  }

  async function refreshReports() {
    const requestScopeId = scopeId;
    const requestProjectId = projectId;
    const current = token.current;
    if (requestScopeId) {
      const duplicatesResult = await request(`/scopes/${requestScopeId}/duplicates`);
      if (token.current === current && requestScopeId === scopeId) setDuplicates(duplicatesResult);
    }
    if (requestProjectId) {
      const capacityResult = await request(`/projects/${requestProjectId}/capacity`);
      if (token.current === current && requestProjectId === projectId) setCapacity(capacityResult);
    }
  }

  useEffect(() => {
    clearAssetState();
    load(false);
  }, [contextKey]);

  useEffect(() => {
    onSelectionChange([...selected]);
  }, [selected, onSelectionChange]);

  useEffect(() => {
    const requestScopeId = scopeId;
    const requestQuery = query;
    if (!requestScopeId) {
      clearAssetState();
      return;
    }
    let alive = true;
    const poll = async () => {
      const list = await request<{ items: Job[] }>(`/jobs?scope_id=${encodeURIComponent(requestScopeId)}`);
      if (!alive || requestScopeId !== scopeId || requestQuery !== query) return;
      setJobs(list.items || []);
      if ((list.items || []).some((job) => ["running", "queued"].includes(job.state))) load(false);
    };
    poll().catch(() => undefined);
    const id = setInterval(() => poll().catch(() => undefined), 5000);
    return () => { alive = false; clearInterval(id); };
  }, [contextKey]);

  return (
    <section className="panel imageWorkbench assetsWorkbench">
      <div className="workbenchHeader">
        <div>
          <p className="eyebrow">{t("Project Scope")}</p>
          <h2>{t("图片库")}</h2>
          <p className="hint">{t("浏览已索引对象，固定快照后再进入派生、复制和报告流程。")}</p>
        </div>
        <div className="summaryGrid compactMetrics">
          <Metric label={t("对象数")} value={loading ? t("读取中") : items.length} />
          <Metric label={t("已选择")} value={selected.size} />
          <Metric label={t("下一页")} value={cursor ? t("有下一页") : t("无下一页或未知")} />
        </div>
      </div>
      <div className="filters workbenchFilters">
        <input className="searchInput" placeholder={t("key 查询")} value={filters.query} onChange={(event) => setFilters({ ...filters, query: event.target.value })} />
        <input placeholder={t("prefix")} value={filters.prefix} onChange={(event) => setFilters({ ...filters, prefix: event.target.value })} />
        <select value={filters.format} onChange={(event) => setFilters({ ...filters, format: event.target.value })}>
          <option value="">{t("格式")}</option><option value="jpeg">{t("JPEG")}</option><option value="png">{t("PNG")}</option><option value="webp">{t("WebP")}</option>
        </select>
        <select value={filters.decode_status} onChange={(event) => setFilters({ ...filters, decode_status: event.target.value })}>
          <option value="">{t("解码状态")}</option><option value="valid">{t("valid")}</option><option value="unsupported">{t("unsupported")}</option><option value="corrupt">{t("corrupt")}</option><option value="resource_limited">{t("resource_limited")}</option><option value="unknown">{t("unknown")}</option>
        </select>
        <button disabled={!scopeId} title={scopeId ? t("导出当前 Scope 报告") : t("请选择 Scope 后再导出")} onClick={() => { if (scopeId) window.open(`${API}/scopes/${scopeId}/reports/export?${query}`, "_blank"); }}>{t("下载报告")}</button>
        <details className="advancedFilters">
          <summary>{t("高级筛选")}</summary>
          <div>
            <input placeholder={t("tag")} value={filters.tag} onChange={(event) => setFilters({ ...filters, tag: event.target.value })} />
            <select value={filters.presence} onChange={(event) => setFilters({ ...filters, presence: event.target.value })}>
              <option value="">{t("presence")}</option><option value="present">{t("present")}</option><option value="missing_candidate">{t("missing_candidate")}</option><option value="missing_confirmed">{t("missing_confirmed")}</option>
            </select>
            <input placeholder={t("最小宽")} value={filters.min_width} onChange={(event) => setFilters({ ...filters, min_width: event.target.value })} />
            <input placeholder={t("最大宽")} value={filters.max_width} onChange={(event) => setFilters({ ...filters, max_width: event.target.value })} />
            <input placeholder={t("最小高")} value={filters.min_height} onChange={(event) => setFilters({ ...filters, min_height: event.target.value })} />
            <input placeholder={t("最大高")} value={filters.max_height} onChange={(event) => setFilters({ ...filters, max_height: event.target.value })} />
            <input placeholder={t("最小比例")} value={filters.min_ratio} onChange={(event) => setFilters({ ...filters, min_ratio: event.target.value })} />
            <input placeholder={t("最大比例")} value={filters.max_ratio} onChange={(event) => setFilters({ ...filters, max_ratio: event.target.value })} />
            <input placeholder={t("最小字节")} value={filters.min_size} onChange={(event) => setFilters({ ...filters, min_size: event.target.value })} />
            <input placeholder={t("最大字节")} value={filters.max_size} onChange={(event) => setFilters({ ...filters, max_size: event.target.value })} />
            <input placeholder={t("after ISO time")} value={filters.after} onChange={(event) => setFilters({ ...filters, after: event.target.value })} />
            <input placeholder={t("before ISO time")} value={filters.before} onChange={(event) => setFilters({ ...filters, before: event.target.value })} />
          </div>
        </details>
      </div>
      <div className="toolbar workbenchActions">
        <button disabled={!scopeId || !items.length} onClick={submitPreviews}>{t("提交预览任务")}</button>
        <button disabled={!scopeId || !items.length} onClick={snapshot}>{t("固定选择/查询快照")}</button>
        <button disabled={!selected.size} onClick={() => sendToOperationsCopy([...selected])}>{t("批量复制")}</button>
        <button disabled={!selected.size} onClick={() => sendToPresets([...selected])}>{t("生成派生")}</button>
        <button disabled={!scopeId && !projectId} onClick={refreshReports}>{t("刷新重复与容量")}</button>
        <span className="segmented" aria-label={t("显示模式")}>
          <button className={viewMode === "grid" ? "active" : "secondary"} onClick={() => setViewMode("grid")}>{t("网格")}</button>
          <button className={viewMode === "list" ? "active" : "secondary"} onClick={() => setViewMode("list")}>{t("列表")}</button>
        </span>
      </div>
      {error && <div className="notice error">{translateMessage(error)}</div>}
      {snapshotMessage && <div className="notice">{translateMessage(snapshotMessage)}<button onClick={() => setSnapshotMessage("")}>×</button></div>}
      {loading && <div className="notice">{t("加载中")}</div>}
      {!loading && items.length === 0 && <Empty text={t("当前 scope 没有对象。提交扫描任务后这里会显示真实索引结果。")} />}
      <div className="assetWorkspace">
        <div className="assetContent">
          <ComparePanel request={request} scopeId={scopeId} assets={items.filter((item) => selected.has(item.id)).slice(0, 2)} />
          <div className={`assetGrid ${viewMode}`}>
            {items.map((item) => {
              const props = item.properties || {};
              const ready = item.preview_state === "ready";
              const open = () => setDetail(item);
              return (
                <article
                  className={selected.has(item.id) ? "asset selected" : "asset"}
                  key={item.id}
                  role="button"
                  tabIndex={0}
                  aria-label={t("打开对象详情 {name}", { name: item.key_display || item.key })}
                  onClick={open}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      open();
                    }
                  }}
                >
                  <label title={t("选择 {name}", { name: item.key_display || item.key })} onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}><input aria-label={t("选择 {name}", { name: item.key_display || item.key })} type="checkbox" checked={selected.has(item.id)} onChange={(event) => setSelected(toggle(selected, item.id, event.target.checked))} /></label>
                  <div className="thumb">{ready ? <PreviewImage src={`${API}/scopes/${scopeId}/objects/${item.id}/preview`} alt={item.key} /> : <span>{String(props.decode_status || item.preview_state || "not_ready")}</span>}</div>
                  <div className="assetMeta">
                    <strong title={item.key}>{item.key_display || item.key}</strong>
                    <p>{String(props.format || "unknown")} · {String(props.width || "?")}×{String(props.height || "?")} · {formatBytes(item.size_bytes)}</p>
                    <small title={t("对象代际：{revision}", { revision: item.revision })}>{item.reference_status && item.reference_status !== "unknown" ? t("引用：{status}", { status: item.reference_status }) : t("引用状态未知")}</small>
                  </div>
                </article>
              );
            })}
          </div>
          {cursor && <button className="loadMore" onClick={() => load(true)}>{t("加载下一页")}</button>}
        </div>
        <aside className="assetAside">
          <ReportBox title={t("任务")} data={jobs.slice(0, 6)} />
          <ReportBox title={t("重复/容量")} data={{ duplicates, capacity }} />
        </aside>
      </div>
      {detail && detail.scope_id === scopeId && <Detail request={request} asset={detail} scopeId={scopeId} close={() => setDetail(null)} />}
    </section>
  );
}

function ComparePanel({ request, scopeId, assets }: { request: Requester; scopeId: string; assets: Asset[] }) {
  async function buildPreviews() {
    await request(`/scopes/${scopeId}/previews/batch`, { method: "POST", body: JSON.stringify({ object_ids: assets.map((asset) => asset.id) }) });
  }
  if (assets.length < 2) return null;
  return (
    <section className="comparePanel" aria-label={t("并排对比")}>
      <div className="toolbar"><strong>{t("并排对比")}</strong><button onClick={buildPreviews}>{t("为所选对象提交预览")}</button></div>
      <div className="compareGrid">
        {assets.map((asset) => <div key={asset.id} className="compareItem"><strong>{asset.key_display || asset.key}</strong>{asset.preview_state === "ready" ? <img src={`${API}/scopes/${scopeId}/objects/${asset.id}/preview`} /> : <span>preview {asset.preview_state || "not_ready"}</span>}</div>)}
      </div>
    </section>
  );
}

function PreviewImage({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => { setFailed(false); }, [src]);
  return failed ? <span role="status">{t("预览暂不可用，请刷新")}</span> : <img src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />;
}

function Detail({ request, asset, scopeId, close }: { request: Requester; asset: Asset; scopeId: string; close: () => void }) {
  const props = asset.properties || {};
  const [tags, setTags] = useState("");
  const [assetInfo, setAssetInfo] = useState<unknown>(null);
  const [versions, setVersions] = useState<Record<string, any>[]>([]);
  const [message, setMessage] = useState("");

  useEffect(() => {
    setTags(((asset as any).tags || []).join(", "));
    setMessage("");
    request(`/scopes/${scopeId}/assets/${asset.asset_id}`).then(setAssetInfo).catch((err) => setAssetInfo({ error: String((err as Error).message) }));
    request<{ items?: unknown[]; versions?: unknown[] }>(`/scopes/${scopeId}/objects/${encodeURIComponent(asset.id)}/versions`)
      .then((value) => setVersions((value.versions || value.items || []).filter(isRecord)))
      .catch((err) => setVersions([{ error: String((err as Error).message) }]));
  }, [asset.id, asset.asset_id, scopeId]);

  async function saveTags() {
    const value = tags.split(",").map((tag) => tag.trim()).filter(Boolean);
    const result = await request(`/scopes/${scopeId}/assets/${asset.asset_id}/tags`, { method: "PUT", body: JSON.stringify({ tags: value }) });
    setMessage(`tags saved: ${value.length}`);
    setAssetInfo(result);
  }

  async function restoreVersion(versionId: string) {
    const targetKey = window.prompt(t("恢复到新 Key（源对象不会被删除或覆盖）"), `${asset.key}.restore`);
    if (!targetKey) return;
    const result = await request(`/scopes/${scopeId}/objects/${encodeURIComponent(asset.id)}/restore`, { method: "POST", body: JSON.stringify({ version_id: versionId, target_key: targetKey }) });
    setMessage(`restore submitted: ${targetKey}`);
    setAssetInfo(result);
  }

  return (
    <aside className="drawer">
      <button className="drawerClose" onClick={close}>×</button>
      <h2>{asset.key_display || asset.key}</h2>
      <div className="previewLarge">{asset.preview_state === "ready" ? <img src={`${API}/scopes/${scopeId}/objects/${asset.id}/preview`} /> : <span>{String(props.decode_status || "not_ready")}</span>}</div>
      <dl>
        <dt>{t("对象 ID")}</dt><dd>{asset.id}</dd>
        <dt>{t("Asset")}</dt><dd>{asset.asset_id}</dd>
        <dt>{t("Revision")}</dt><dd>{asset.revision}</dd>
        <dt>{t("Checksum")}</dt><dd>{asset.checksum || "unknown"}</dd>
        <dt>{t("Content-Type")}</dt><dd>{asset.content_type || "unknown"}</dd>
        <dt>{t("引用")}</dt><dd>{asset.reference_status || "unknown"}</dd>
      </dl>
      <h3>{t("Asset tags")}</h3>
      <label className="field">{t("逗号分隔标签")}<input value={tags} onChange={(event) => setTags(event.target.value)} /></label>
      <button onClick={() => saveTags().catch((err) => setMessage(String((err as Error).message)))}>{t("保存标签")}</button>
      {message && <div className="notice">{translateMessage(message)}</div>}
      <h3>{t("版本下载")}</h3>
      <p className="hint">{t("下载会带上 opaque version_id；null 版本以字面量 null 查询。DeleteMarker 不提供下载。")}</p>
      <DataTable columns={[t("Version ID"), t("状态"), t("大小"), t("修改时间"), t("动作")]} rows={versions.map((version) => {
        if (version.error) return [<code>{t("error")}</code>, t("unknown"), t("unknown"), t("unknown"), String(version.error)];
        const rawVersionId = String(version.version_id ?? version.VersionId ?? "null");
        const isDeleteMarker = Boolean(version.is_delete_marker ?? version.IsDeleteMarker ?? version.delete_marker);
        return [
          <code>{rawVersionId}</code>,
          isDeleteMarker ? t("DeleteMarker") : String(version.is_latest ?? version.IsLatest ? t("latest") : version.state || t("version")),
          String(version.size ?? version.Size ?? "unknown"),
          String(version.last_modified ?? version.LastModified ?? "unknown"),
          <span className="actions">
            {isDeleteMarker ? <button disabled>{t("下载")}</button> : <a className="buttonLink" href={`${API}/scopes/${scopeId}/objects/${encodeURIComponent(asset.id)}/download?version_id=${encodeURIComponent(rawVersionId)}`}>{t("下载")}</a>}
            <button disabled={isDeleteMarker} onClick={() => restoreVersion(rawVersionId).catch((err) => setMessage(String((err as Error).message)))}>{t("恢复到新 Key")}</button>
          </span>
        ];
      })} />
      <h3>{t("关系来源")}</h3>
      <EvidenceDetails title={t("Asset evidence")} data={assetInfo} />
    </aside>
  );
}

function Scans({ request, scopeId, onChanged }: { request: Requester; scopeId: string; onChanged: () => void }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const token = useRef(0);

  function clearState() {
    token.current += 1;
    setJobs([]);
    setMessage("");
    setError("");
  }

  async function refresh(requestScopeId = scopeId, current = token.current) {
    if (!requestScopeId) {
      clearState();
      return;
    }
    const result = await request<{ items: Job[] }>(`/jobs?scope_id=${encodeURIComponent(requestScopeId)}`);
    if (token.current !== current || requestScopeId !== scopeId) return;
    setJobs(result.items || []);
  }

  async function submit(kind: "scan" | "checksum") {
    const requestScopeId = scopeId;
    const current = token.current;
    if (!requestScopeId) return;
    setError("");
    try {
      const path = kind === "scan" ? `/scopes/${requestScopeId}/scans` : `/scopes/${requestScopeId}/checksum-scans`;
      const result = await request<Job>(path, { method: "POST", body: JSON.stringify({}) });
      if (token.current !== current || requestScopeId !== scopeId) return;
      setMessage(`${kind} 任务已提交：${result.id}`);
      await refresh(requestScopeId, current);
      onChanged();
    } catch (err) {
      if (token.current === current && requestScopeId === scopeId) setError(String((err as Error).message));
    }
  }

  async function control(job: Job, action: string) {
    const requestScopeId = scopeId;
    const current = token.current;
    if (!requestScopeId) return;
    await request(`/jobs/${job.id}/${action}`, { method: "POST", body: "{}" });
    await refresh(requestScopeId, current);
  }

  useEffect(() => {
    const requestScopeId = scopeId;
    const current = ++token.current;
    setJobs([]);
    setMessage("");
    setError("");
    if (!requestScopeId) return;
    refresh(requestScopeId, current).catch(() => undefined);
    const id = setInterval(() => refresh(requestScopeId, current).catch(() => undefined), 5000);
    return () => clearInterval(id);
  }, [scopeId]);

  return (
    <section className="panel imageWorkbench">
      <div className="workbenchHeader">
        <div>
          <p className="eyebrow">{t("Project Scope")}</p>
          <h2>{t("任务与扫描")}</h2>
          <p className="hint">{t("提交扫描、checksum 任务，并观察当前 Scope 的进度。")}</p>
        </div>
        <div className="summaryGrid compactMetrics">
          <Metric label={t("任务")} value={jobs.length} />
          <Metric label={t("运行中")} value={jobs.filter((job) => ["running", "queued"].includes(job.state)).length} />
          <Metric label={t("错误")} value={jobs.reduce((count, job) => count + (Number(job.errors) || 0), 0)} />
        </div>
      </div>
      <div className="workbenchSplit">
      <div className="workbenchCard">
        <h2>{t("提交任务")}</h2>
        <div className="toolbar">
          <button disabled={!scopeId} onClick={() => submit("scan")}>{t("提交扫描")}</button>
          <button disabled={!scopeId} onClick={() => submit("checksum")}>{t("提交 checksum")}</button>
        </div>
        {message && <div className="notice">{translateMessage(message)}</div>}
        {error && <div className="notice error">{translateMessage(error)}</div>}
      </div>
      <div className="workbenchCard flush">
      <DataTable columns={[t("类型"), t("状态"), t("控制"), t("进度"), t("错误"), t("动作")]} rows={jobs.map((job) => [
        job.kind,
        <StatusPill text={job.state} />,
        job.control_request,
        job.total == null ? t("已处理 {processed}，总数未知", { processed: job.processed }) : `${job.processed}/${job.total}`,
        job.errors,
        <span className="actions"><button onClick={() => control(job, "pause")}>{t("暂停")}</button><button onClick={() => control(job, "cancel")}>{t("取消")}</button><button onClick={() => control(job, "retry")}>{t("重试")}</button></span>
      ])} />
      </div>
      </div>
    </section>
  );
}

function Diagnostics({ request, scopeId }: { request: Requester; scopeId: string }) {
  const [objectId, setObjectId] = useState("");
  const [origin, setOrigin] = useState("http://localhost:5173");
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState("");
  const token = useRef(0);

  async function run(path: string, body: Record<string, unknown>) {
    const requestScopeId = scopeId;
    const current = token.current;
    if (!requestScopeId) return;
    setError("");
    try {
      const value = await request(path, { method: "POST", body: JSON.stringify(body) });
      if (token.current === current && requestScopeId === scopeId) setResult(value);
    } catch (err) {
      if (token.current === current && requestScopeId === scopeId) setError(String((err as Error).message));
    }
  }

  useEffect(() => {
    token.current += 1;
    setObjectId("");
    setResult(null);
    setError("");
  }, [scopeId]);

  return (
    <section className="panel imageWorkbench">
      <div className="workbenchHeader">
        <div>
          <p className="eyebrow">{t("Project Scope")}</p>
          <h2>{t("诊断证据")}</h2>
          <p className="hint">{t("验证对象访问、预览和 CORS；切换 Scope 后重新选择对象。")}</p>
        </div>
        <div className="summaryGrid compactMetrics">
          <Metric label={t("结果")} value={result ? t("已读取") : t("未读取")} />
          <Metric label={t("对象 ID")} value={objectId || t("未选择")} />
        </div>
      </div>
      <div className="workbenchSplit">
      <div className="workbenchCard">
        <h2>{t("对象访问诊断")}</h2>
        <label className="field">{t("对象 ID")}<input value={objectId} onChange={(event) => setObjectId(event.target.value)} /></label>
        <button disabled={!scopeId || !objectId} onClick={() => run(`/scopes/${scopeId}/diagnostics/access`, { object_id: objectId })}>{t("运行 HEAD/GET/预览诊断")}</button>
        <h2>{t("CORS L1")}</h2>
        <label className="field">{t("业务 Origin")}<input value={origin} onChange={(event) => setOrigin(event.target.value)} /></label>
        <button disabled={!scopeId} onClick={() => run(`/scopes/${scopeId}/diagnostics/cors-read`, { origin })}>{t("读取 CORS 证据")}</button>
      </div>
      <div className="workbenchCard">
        {error && <div className="notice error">{translateMessage(error)}</div>}
        {result ? <EvidenceDetails title={t("诊断原始证据")} data={result} open /> : <Empty text={t("诊断结果会保留执行位置、时间和证据等级。未验证浏览器 trace 时保持 unknown。")} />}
      </div>
      </div>
    </section>
  );
}

function Operations({ request, scopeId, scope, sourceObjectIds, initialTab }: { request: Requester; scopeId: string; scope?: Scope; sourceObjectIds: string[]; initialTab: string }) {
  const [tab, setTab] = useState(sourceObjectIds.length ? initialTab : "upload");
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [upload, setUpload] = useState({ key: "", metadata: "{}", uploadId: "", parts: "[]" });
  const [file, setFile] = useState<File | null>(null);
  const [partNumber, setPartNumber] = useState("1");
  const [copy, setCopy] = useState({ object_id: "", target_key: "", metadata: "{}" });
  const [copyBatch, setCopyBatch] = useState({ object_ids: "", target_scope_id: "", target_prefix: "", target_keys: "", batch_id: "", idempotency_key: "" });
  const [objectOps, setObjectOps] = useState({ object_id: "", version_id: "", target_key: "", tags: "{}", metadata: "{}", content_type: "", retention: '{"Mode":"GOVERNANCE","RetainUntilDate":"2027-01-01T00:00:00Z"}', legal_hold: '{"Status":"ON"}' });
  const [objectVersions, setObjectVersions] = useState<Record<string, any>[]>([]);
  const [bucket, setBucket] = useState({ kind: "cors", config: "{}", expected_current_hash: "", snapshot_id: "", exclusive_writer_ack: false });
  const [manifest, setManifest] = useState({ manifest_id: "", entries: '[{"key":""}]', declared_hash: "", finalize: true });
  const [capacity, setCapacity] = useState({ thresholds: '{"source_bytes":10737418240}' });
  const [trash, setTrash] = useState({ variant_id: "", reason: "manual review" });
  const token = useRef(0);
  const scopeWritable = Boolean(scope?.writable);
  const scopeCanManageBucket = Boolean(scope?.manage_bucket);

  async function run(label: string, fn: () => Promise<unknown>, apply?: (value: any) => void) {
    const requestScopeId = scopeId;
    const current = token.current;
    setError("");
    try {
      const value = await fn();
      if (token.current !== current || requestScopeId !== scopeId) return;
      apply?.(value);
      setResult({ label, value });
    } catch (err) {
      if (token.current === current && requestScopeId === scopeId) setError(String((err as Error).message));
    }
  }

  async function initUpload() {
    const requestScopeId = scopeId;
    const current = token.current;
    const value = await request<any>(`/scopes/${requestScopeId}/uploads/multipart`, { method: "POST", body: JSON.stringify({ key: upload.key, metadata: parseJsonObject(upload.metadata, "metadata") }) });
    if (token.current === current && requestScopeId === scopeId && value?.id) setUpload((old) => ({ ...old, uploadId: value.id }));
    return value;
  }

  async function uploadPart() {
    if (!file) throw new Error("请选择要上传的 part 文件。");
    return request(`/scopes/${scopeId}/uploads/multipart/${encodeURIComponent(upload.uploadId)}/parts?part_number=${encodeURIComponent(partNumber)}`, { method: "POST", body: file });
  }

  useEffect(() => {
    if (sourceObjectIds.length) {
      setCopyBatch((old) => ({ ...old, object_ids: sourceObjectIds.join("\n"), target_scope_id: old.target_scope_id || scopeId }));
      setCopy((old) => ({ ...old, object_id: sourceObjectIds[0] }));
      setObjectOps((old) => ({ ...old, object_id: sourceObjectIds[0] }));
      setObjectVersions([]);
    }
  }, [sourceObjectIds, scopeId]);

  const copyBatchItems = useMemo(() => {
    const objectIds = lines(copyBatch.object_ids);
    const targetKeys = lines(copyBatch.target_keys);
    const prefix = copyBatch.target_prefix.trim();
    return objectIds.map((object_id, index) => ({
      object_id,
      target_scope_id: copyBatch.target_scope_id || scopeId,
      target_key: `${prefix}${targetKeys[index] || ""}`
    })).filter((item) => item.object_id || item.target_key);
  }, [copyBatch.object_ids, copyBatch.target_keys, copyBatch.target_prefix, copyBatch.target_scope_id, scopeId]);

  function submitCopyBatch() {
    const items = copyBatchItems;
    if (!items.length) throw new Error("请至少填写一个 object_id。");
    if (items.length > 60) throw new Error("批量复制最多 60 个对象。");
    if (items.some((item) => !item.object_id || !item.target_scope_id || !item.target_key)) throw new Error("每个 object_id 都必须有 target_scope_id 和 target_key。");
    const headers = copyBatch.idempotency_key.trim() ? { "Idempotency-Key": copyBatch.idempotency_key.trim() } : undefined;
    return request(`/scopes/${scopeId}/objects/copy-batches`, { method: "POST", headers, body: JSON.stringify({ items }) });
  }

  async function loadObjectVersions() {
    const requestScopeId = scopeId;
    const current = token.current;
    const value = await request(`/scopes/${requestScopeId}/objects/${encodeURIComponent(objectOps.object_id)}/versions`);
    if (token.current === current && requestScopeId === scopeId) setObjectVersions(objectVersionRows(value));
    return value;
  }

  useEffect(() => {
    token.current += 1;
    setResult(null);
    setError("");
    setFile(null);
    setObjectVersions([]);
    setUpload((old) => ({ ...old, uploadId: "", parts: "[]" }));
    setCopy((old) => ({ ...old, object_id: "", target_key: "" }));
    setCopyBatch((old) => ({ ...old, object_ids: "", target_scope_id: scopeId, target_keys: "", batch_id: "", idempotency_key: "" }));
    setObjectOps((old) => ({ ...old, object_id: "", version_id: "", target_key: "" }));
    setBucket((old) => ({ ...old, expected_current_hash: "", snapshot_id: "", exclusive_writer_ack: false }));
    setManifest((old) => ({ ...old, manifest_id: "" }));
    setTrash((old) => ({ ...old, variant_id: "" }));
  }, [scopeId]);

  return (
    <section className="panel imageWorkbench">
      <div className="workbenchHeader">
        <div>
          <p className="eyebrow">{t("Project Scope")}</p>
          <h2>{t("受控操作")}</h2>
          <p className="hint">{t("执行上传、复制、版本、桶配置和容量操作；禁用项会显示原因。")}</p>
        </div>
        <div className="summaryGrid compactMetrics">
          <Metric label={t("writable")} value={scopeWritable ? t("yes") : t("no")} />
          <Metric label={t("manage_bucket")} value={scopeCanManageBucket ? t("yes") : t("no")} />
          <Metric label={t("结果")} value={result ? t("已读取") : t("未读取")} />
        </div>
      </div>
      <div className="tabs">
        {[
          ["upload", "分片上传"], ["copy", "复制/移动"], ["object", "版本/标签/保留"],
          ["bucket", "桶配置"], ["manifest", "引用 Manifest"], ["ops", "审计/缓存/容量"]
        ].map(([id, label]) => <button key={id} className={tab === id ? "active" : "secondary"} onClick={() => setTab(id)}>{t(label)}</button>)}
      </div>
      {!scopeId && <div className="notice error">{t("请先选择 scope。")}</div>}
      {tab === "upload" && <div className="two"><div>
        <h2>{t("multipart 上传")}</h2>
        <label className="field">{t("目标 Key")}<input value={upload.key} onChange={(event) => setUpload({ ...upload, key: event.target.value })} placeholder="prefix/new-image.webp" /></label>
        <label className="field">{t("Metadata JSON")}<textarea value={upload.metadata} onChange={(event) => setUpload({ ...upload, metadata: event.target.value })} /></label>
        <button disabled={!scopeId || !upload.key} onClick={() => run("multipart.init", initUpload)}>{t("初始化")}</button>
        <label className="field">{t("Upload ID")}<input value={upload.uploadId} onChange={(event) => setUpload({ ...upload, uploadId: event.target.value })} /></label>
        <label className="field">{t("Part Number")}<input type="number" min="1" value={partNumber} onChange={(event) => setPartNumber(event.target.value)} /></label>
        <label className="field">{t("Part 文件")}<input type="file" onChange={(event) => setFile(event.target.files?.[0] || null)} /></label>
        <div className="toolbar">
          <button disabled={!scopeId || !upload.uploadId || !file} onClick={() => run("multipart.part", uploadPart)}>{t("上传 part")}</button>
          <button disabled={!scopeId || !upload.uploadId} onClick={() => run("multipart.complete", () => request(`/scopes/${scopeId}/uploads/multipart/${encodeURIComponent(upload.uploadId)}/complete`, { method: "POST", body: JSON.stringify({ parts: parseJsonArray(upload.parts, "parts") }) }))}>{t("Complete")}</button>
          <button disabled={!scopeId || !upload.uploadId} onClick={() => run("multipart.abort", () => request(`/scopes/${scopeId}/uploads/multipart/${encodeURIComponent(upload.uploadId)}/abort`, { method: "POST", body: "{}" }))}>{t("Abort")}</button>
        </div>
        <label className="field">{t("Complete parts JSON")}<textarea value={upload.parts} onChange={(event) => setUpload({ ...upload, parts: event.target.value })} placeholder='[{"PartNumber":1,"ETag":"..."}]' /></label>
      </div><ResultPane error={error} result={result} /></div>}
      {tab === "copy" && <div className="two"><div>
        <h2>{t("复制对象")}</h2>
        <label className="field">{t("源对象 ID")}<input value={copy.object_id} onChange={(event) => setCopy({ ...copy, object_id: event.target.value })} /></label>
        <label className="field">{t("目标 Key")}<input value={copy.target_key} onChange={(event) => setCopy({ ...copy, target_key: event.target.value })} /></label>
        <label className="field">{t("Metadata JSON")}<textarea value={copy.metadata} onChange={(event) => setCopy({ ...copy, metadata: event.target.value })} /></label>
        <div className="toolbar">
          <button disabled={!scopeId || !scopeWritable || !copy.object_id || !copy.target_key} title={scopeWritable ? "" : t("当前 Scope 未开启 writable")} onClick={() => run("object.copy", () => request(`/scopes/${scopeId}/objects/copy`, { method: "POST", body: JSON.stringify({ object_id: copy.object_id, target_key: copy.target_key, metadata: parseJsonObject(copy.metadata, "metadata") }) }))}>{t("复制并校验")}</button>
          <button disabled title={t("后端当前固定返回 PROTECTED_BY_REFERENCE，客户端 guard 不能授权删除源对象。")}>{t("移动源对象（受保护）")}</button>
        </div>
        <p className="hint">{t("移动需要服务端引用保护 adapter，当前 UI 明确禁用。")}</p>
        <h2>{t("批量复制")}</h2>
        <p className="hint">{t("从资产页选择对象后可带入 object_id；目标 key 必须逐行确认。复制只创建目标对象，不删除源对象。")}</p>
        <div className="toolbar">
          <button disabled={!sourceObjectIds.length} onClick={() => setCopyBatch({ ...copyBatch, object_ids: sourceObjectIds.join("\n"), target_scope_id: copyBatch.target_scope_id || scopeId })}>{t("载入当前选择")}</button>
          <button onClick={() => setCopyBatch({ ...copyBatch, idempotency_key: copyBatch.idempotency_key || crypto.randomUUID() })}>{t("生成幂等 Key")}</button>
        </div>
        <label className="field">{t("目标 Scope ID")}<input value={copyBatch.target_scope_id || scopeId} onChange={(event) => setCopyBatch({ ...copyBatch, target_scope_id: event.target.value })} /></label>
        <label className="field">{t("目标 Key 前缀（可选，原样拼接）")}<input value={copyBatch.target_prefix} onChange={(event) => setCopyBatch({ ...copyBatch, target_prefix: event.target.value })} placeholder="copied/" /></label>
        <label className="field">{t("Object IDs（每行一个，最多 60）")}<textarea value={copyBatch.object_ids} onChange={(event) => setCopyBatch({ ...copyBatch, object_ids: event.target.value })} /></label>
        <label className="field">{t("目标 Keys（每行一个，与 object_id 行号对应）")}<textarea value={copyBatch.target_keys} onChange={(event) => setCopyBatch({ ...copyBatch, target_keys: event.target.value })} placeholder="gallery/copy-001.jpg" /></label>
        <label className="field">{t("Idempotency-Key")}<input value={copyBatch.idempotency_key} onChange={(event) => setCopyBatch({ ...copyBatch, idempotency_key: event.target.value })} /></label>
        <DataTable columns={["#", t("object_id"), t("target_scope_id"), t("target_key")]} rows={copyBatchItems.slice(0, 60).map((item, index) => [index + 1, <code>{item.object_id || t("missing")}</code>, <code>{item.target_scope_id || t("missing")}</code>, <code>{item.target_key || t("missing")}</code>])} />
        <div className="toolbar" style={{ marginTop: 10 }}>
          <button disabled={!scopeId || !scopeWritable || copyBatchItems.length === 0 || copyBatchItems.length > 60 || copyBatchItems.some((item) => !item.object_id || !item.target_scope_id || !item.target_key)} title={scopeWritable ? "" : t("当前 Scope 未开启 writable")} onClick={() => run("copy.batch", submitCopyBatch)}>{t("提交批量复制")}</button>
          <label className="field compact">{t("Batch ID")}<input value={copyBatch.batch_id} onChange={(event) => setCopyBatch({ ...copyBatch, batch_id: event.target.value })} /></label>
          <button disabled={!scopeId || !copyBatch.batch_id} onClick={() => run("copy.batch.get", () => request(`/scopes/${scopeId}/objects/copy-batches/${encodeURIComponent(copyBatch.batch_id)}`))}>{t("读取批次详情")}</button>
        </div>
      </div><ResultPane error={error} result={result} /></div>}
      {tab === "object" && <div className="two"><div>
        <h2>{t("对象版本、标签、保留策略")}</h2>
        <label className="field">{t("对象 ID")}<input value={objectOps.object_id} onChange={(event) => setObjectOps({ ...objectOps, object_id: event.target.value })} /></label>
        <label className="field">{t("Version ID")}<input value={objectOps.version_id} onChange={(event) => setObjectOps({ ...objectOps, version_id: event.target.value })} placeholder={t("可留空读取当前版本")} /></label>
        <label className="field">{t("恢复目标 Key")}<input value={objectOps.target_key} onChange={(event) => setObjectOps({ ...objectOps, target_key: event.target.value })} /></label>
        <ObjectVersionTable versions={objectVersions} selectVersion={(versionId) => setObjectOps((old) => ({ ...old, version_id: versionId }))} />
        <div className="toolbar">
          <button disabled={!scopeId || !objectOps.object_id} onClick={() => run("versions.list", loadObjectVersions)}>{t("读取版本")}</button>
          <button disabled={!scopeId || !scopeWritable || !objectOps.object_id || !objectOps.version_id || !objectOps.target_key} title={scopeWritable ? "" : t("当前 Scope 未开启 writable")} onClick={() => run("versions.restore", () => request(`/scopes/${scopeId}/objects/${encodeURIComponent(objectOps.object_id)}/restore`, { method: "POST", body: JSON.stringify({ version_id: objectOps.version_id, target_key: objectOps.target_key }) }))}>{t("恢复到新 Key")}</button>
          <button disabled={!scopeId || !objectOps.object_id} onClick={() => run("tags.get", () => request(`/scopes/${scopeId}/objects/${encodeURIComponent(objectOps.object_id)}/tags${objectOps.version_id ? `?version_id=${encodeURIComponent(objectOps.version_id)}` : ""}`))}>{t("读取标签")}</button>
        </div>
        <label className="field">{t("标签 JSON")}<textarea value={objectOps.tags} onChange={(event) => setObjectOps({ ...objectOps, tags: event.target.value })} /></label>
        <button disabled={!scopeId || !scopeWritable || !objectOps.object_id} title={scopeWritable ? "" : t("当前 Scope 未开启 writable")} onClick={() => run("tags.put", () => request(`/scopes/${scopeId}/objects/${encodeURIComponent(objectOps.object_id)}/tags`, { method: "PUT", body: JSON.stringify({ version_id: objectOps.version_id || undefined, tags: parseJsonObject(objectOps.tags, "tags") }) }))}>{t("写入标签")}</button>
        <h2>{t("Metadata copy")}</h2>
        <p className="hint">{t("S3 metadata 修改通过复制到新 key 实现，源对象会先做 revision/ETag/size/checksum 防护校验。")}</p>
        <label className="field">{t("Metadata JSON")}<textarea value={objectOps.metadata} onChange={(event) => setObjectOps({ ...objectOps, metadata: event.target.value })} /></label>
        <label className="field">{t("Content-Type")}<input value={objectOps.content_type} onChange={(event) => setObjectOps({ ...objectOps, content_type: event.target.value })} placeholder={t("留空保持后端默认")} /></label>
        <div className="toolbar">
          <button disabled={!scopeId || !objectOps.object_id} onClick={() => run("metadata.get", () => request(`/scopes/${scopeId}/objects/${encodeURIComponent(objectOps.object_id)}/metadata`))}>{t("读取 Metadata")}</button>
          <button disabled={!scopeId || !scopeWritable || !objectOps.object_id || !objectOps.target_key} title={scopeWritable ? "" : t("当前 Scope 未开启 writable")} onClick={() => run("metadata.put", () => request(`/scopes/${scopeId}/objects/${encodeURIComponent(objectOps.object_id)}/metadata`, { method: "PUT", body: JSON.stringify({ target_key: objectOps.target_key, metadata: parseJsonObject(objectOps.metadata, "metadata"), content_type: objectOps.content_type || undefined }) }))}>{t("复制写入 Metadata")}</button>
        </div>
        <div className="toolbar" style={{ marginTop: 10 }}>
          <button disabled={!scopeId || !objectOps.object_id} onClick={() => run("retention.get", () => request(`/scopes/${scopeId}/objects/${encodeURIComponent(objectOps.object_id)}/retention${objectOps.version_id ? `?version_id=${encodeURIComponent(objectOps.version_id)}` : ""}`))}>{t("读取保留/Legal Hold")}</button>
        </div>
        <label className="field">{t("Retention JSON")}<textarea value={objectOps.retention} onChange={(event) => setObjectOps({ ...objectOps, retention: event.target.value })} /></label>
        <label className="field">{t("Legal Hold JSON")}<textarea value={objectOps.legal_hold} onChange={(event) => setObjectOps({ ...objectOps, legal_hold: event.target.value })} /></label>
        <div className="toolbar">
          <button disabled={!scopeId || !scopeWritable || !objectOps.object_id || !objectOps.version_id} title={scopeWritable ? "" : t("当前 Scope 未开启 writable")} onClick={() => run("retention.put", () => request(`/scopes/${scopeId}/objects/${encodeURIComponent(objectOps.object_id)}/retention`, { method: "PUT", body: JSON.stringify({ version_id: objectOps.version_id, retention: parseJsonObject(objectOps.retention, "retention") }) }))}>{t("写 Retention")}</button>
          <button disabled={!scopeId || !scopeWritable || !objectOps.object_id || !objectOps.version_id} title={scopeWritable ? "" : t("当前 Scope 未开启 writable")} onClick={() => run("legal-hold.put", () => request(`/scopes/${scopeId}/objects/${encodeURIComponent(objectOps.object_id)}/legal-hold`, { method: "PUT", body: JSON.stringify({ version_id: objectOps.version_id, legal_hold: parseJsonObject(objectOps.legal_hold, "legal_hold") }) }))}>{t("写 Legal Hold")}</button>
        </div>
      </div><ResultPane error={error} result={result} /></div>}
      {tab === "bucket" && <div className="two"><div>
        <h2>{t("桶配置 JSON 编辑器")}</h2>
        <p className="hint">{t("仅整桶 manage_bucket scope 可用。后端不支持 S3 原生 CAS，保存/回滚前必须由操作者确认当前没有其他写入者。")}</p>
        <label className="field">{t("类型")}<select value={bucket.kind} onChange={(event) => setBucket({ ...bucket, kind: event.target.value, exclusive_writer_ack: false })}><option value="cors">CORS</option><option value="lifecycle">{t("Lifecycle")}</option><option value="policy">{t("Policy")}</option></select></label>
        <label className="field">{t("expected_current_hash")}<input value={bucket.expected_current_hash} onChange={(event) => setBucket({ ...bucket, expected_current_hash: event.target.value })} placeholder={t("先读取当前配置获得 current_hash")} /></label>
        <label className="field">{t("rollback snapshot_id")}<input value={bucket.snapshot_id} onChange={(event) => setBucket({ ...bucket, snapshot_id: event.target.value })} placeholder={t("保存结果中的 before_snapshot_id")} /></label>
        <label className="field">{t("配置 JSON")}<textarea value={bucket.config} onChange={(event) => setBucket({ ...bucket, config: event.target.value })} /></label>
        <label className="ack"><input type="checkbox" checked={bucket.exclusive_writer_ack} onChange={(event) => setBucket({ ...bucket, exclusive_writer_ack: event.target.checked })} /> {t("我确认当前没有其他控制台或脚本同时修改该 bucket 配置")}</label>
        <div className="toolbar">
          <button disabled={!scopeId} onClick={() => run("bucket.get", () => request<any>(`/scopes/${scopeId}/bucket/${bucket.kind}`), (value) => {
            setBucket((old) => ({ ...old, config: JSON.stringify(value.config ?? {}, null, 2), expected_current_hash: value.current_hash || "", exclusive_writer_ack: false }));
          })}>{t("读取当前配置")}</button>
          <button disabled={!scopeId || !scopeCanManageBucket || !bucket.expected_current_hash || !bucket.exclusive_writer_ack} title={scopeCanManageBucket ? "" : t("当前 Scope 未开启 manage_bucket")} onClick={() => run("bucket.put", () => request(`/scopes/${scopeId}/bucket/${bucket.kind}`, { method: "POST", body: JSON.stringify({ config: parseJsonObject(bucket.config, "config"), expected_current_hash: bucket.expected_current_hash, exclusive_writer_ack: bucket.exclusive_writer_ack }) }))}>{t("保存配置")}</button>
          <button disabled={!scopeId || !scopeCanManageBucket || !bucket.snapshot_id || !bucket.expected_current_hash || !bucket.exclusive_writer_ack} title={scopeCanManageBucket ? "" : t("当前 Scope 未开启 manage_bucket")} onClick={() => run("bucket.rollback", () => request(`/scopes/${scopeId}/bucket/${bucket.kind}/rollback`, { method: "POST", body: JSON.stringify({ snapshot_id: bucket.snapshot_id, expected_current_hash: bucket.expected_current_hash, exclusive_writer_ack: bucket.exclusive_writer_ack }) }))}>{t("回滚到快照")}</button>
        </div>
      </div><ResultPane error={error} result={result} /></div>}
      {tab === "manifest" && <div className="two"><div>
        <h2>{t("引用 Manifest 导入")}</h2>
        <label className="field">{t("Manifest ID（追加/Finalize 用）")}<input value={manifest.manifest_id} onChange={(event) => setManifest({ ...manifest, manifest_id: event.target.value })} /></label>
        <label className="field">{t("Declared Hash")}<input value={manifest.declared_hash} onChange={(event) => setManifest({ ...manifest, declared_hash: event.target.value })} /></label>
        <label><input type="checkbox" checked={manifest.finalize} onChange={(event) => setManifest({ ...manifest, finalize: event.target.checked })} /> {t("创建后立即 finalize")}</label>
        <label className="field">{t("Entries JSON")}<textarea value={manifest.entries} onChange={(event) => setManifest({ ...manifest, entries: event.target.value })} /></label>
        <div className="toolbar">
          <button disabled={!scopeId} onClick={() => run("manifest.create", () => request(`/scopes/${scopeId}/reference-manifests`, { method: "POST", body: JSON.stringify({ entries: parseJsonArray(manifest.entries, "entries"), declared_hash: manifest.declared_hash || undefined, finalize: manifest.finalize }) }))}>{t("创建/导入")}</button>
          <button disabled={!scopeId || !manifest.manifest_id} onClick={() => run("manifest.entries", () => request(`/scopes/${scopeId}/reference-manifests/${encodeURIComponent(manifest.manifest_id)}/entries`, { method: "POST", body: JSON.stringify({ entries: parseJsonArray(manifest.entries, "entries") }) }))}>{t("追加 entries")}</button>
          <button disabled={!scopeId || !manifest.manifest_id} onClick={() => run("manifest.finalize", () => request(`/scopes/${scopeId}/reference-manifests/${encodeURIComponent(manifest.manifest_id)}/finalize`, { method: "POST", body: "{}" }))}>{t("Finalize")}</button>
        </div>
      </div><ResultPane error={error} result={result} /></div>}
      {tab === "ops" && <div className="two"><div>
        <h2>{t("审计、缓存与容量")}</h2>
        <div className="toolbar">
          <button disabled={!scopeId} onClick={() => run("audits", () => request(`/scopes/${scopeId}/audits`))}>{t("读取审计")}</button>
          <button onClick={() => run("cache.evict", () => request(`/cache/evict`, { method: "POST", body: JSON.stringify({ max_bytes: null }) }))}>{t("执行缓存回收")}</button>
          <button disabled={!scopeId} onClick={() => run("capacity.trends", () => request(`/scopes/${scopeId}/capacity-trends`))}>{t("容量趋势")}</button>
        </div>
        <p className="hint">{t("容量趋势来自扫描样本和手动采样记录；物理占用、版本占用未知时显示为 unknown，不由前端估算。")}</p>
        <button disabled={!scopeId} onClick={() => run("capacity.snapshot", () => request(`/scopes/${scopeId}/capacity-snapshots`, { method: "POST", body: "{}" }))}>{t("按当前索引记录容量样本")}</button>
        <label className="field">{t("阈值 JSON")}<textarea value={capacity.thresholds} onChange={(event) => setCapacity({ ...capacity, thresholds: event.target.value })} /></label>
        <button disabled={!scopeId} onClick={() => run("capacity.thresholds", () => request(`/scopes/${scopeId}/capacity-thresholds`, { method: "POST", body: JSON.stringify({ thresholds: parseJsonObject(capacity.thresholds, "thresholds") }) }))}>{t("检查阈值")}</button>
        <h2>{t("面板自有派生逻辑回收")}</h2>
        <p className="hint">{t("仅标记面板自有派生对象；后端返回 physical_delete_enabled:false 时表示不会物理删除。")}</p>
        <label className="field">{t("Variant ID")}<input value={trash.variant_id} onChange={(event) => setTrash({ ...trash, variant_id: event.target.value })} /></label>
        <label className="field">{t("Reason")}<input value={trash.reason} onChange={(event) => setTrash({ ...trash, reason: event.target.value })} /></label>
        <div className="toolbar">
          <button disabled={!scopeId} onClick={() => run("trash.preview", () => request(`/scopes/${scopeId}/owned-objects/trash-preview`))}>{t("读取回收预览")}</button>
          <button disabled={!scopeId || !trash.variant_id} onClick={() => run("trash.mark", () => request(`/scopes/${scopeId}/owned-objects/${encodeURIComponent(trash.variant_id)}/trash`, { method: "POST", body: JSON.stringify({ reason: trash.reason }) }))}>{t("标记逻辑回收")}</button>
          <button disabled={!scopeId || !trash.variant_id} onClick={() => run("trash.restore", () => request(`/scopes/${scopeId}/owned-objects/${encodeURIComponent(trash.variant_id)}/restore`, { method: "POST", body: "{}" }))}>{t("恢复")}</button>
        </div>
      </div><ResultPane error={error} result={result} /></div>}
    </section>
  );
}

function Presets({ request, projectId, scopeId, initialObjectIds }: { request: Requester; projectId: string; scopeId: string; initialObjectIds: string[] }) {
  const [items, setItems] = useState<Preset[]>([]);
  const [groups, setGroups] = useState<any[]>([]);
  const [variants, setVariants] = useState<any[]>([]);
  const [health, setHealth] = useState<unknown>(null);
  const [name, setName] = useState("gallery-thumb");
  const [preset, setPreset] = useState({ mode: "fit", width: "320", height: "320", quality: "85", format: "webp", focalX: "0.5", focalY: "0.5" });
  const [derive, setDerive] = useState({ object_id: "", preset_id: "", output_key: "", output_scope_id: "", object_ids: "", snapshot_id: "", batch_id: "", idempotency_key: "" });
  const [batchPresetIds, setBatchPresetIds] = useState<Set<string>>(new Set());
  const [group, setGroup] = useState({ name: "manual-selection", object_id: "", group_id: "" });
  const [groupMembers, setGroupMembers] = useState<unknown>(null);
  const [result, setResult] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [handoffOpen, setHandoffOpen] = useState(false);
  const token = useRef(0);
  const previousProject = useRef(projectId);

  async function refresh(requestProjectId = projectId, requestScopeId = scopeId, current = token.current) {
    if (!requestProjectId) return;
    const [presetResult, groupResult] = await Promise.all([
      request<Preset[] | { items: Preset[] }>(`/projects/${requestProjectId}/presets`),
      request<{ items: any[] }>(`/projects/${requestProjectId}/groups`).catch(() => ({ items: [] }))
    ]);
    if (token.current !== current || requestProjectId !== projectId || requestScopeId !== scopeId) return;
    const nextPresets = Array.isArray(presetResult) ? presetResult : presetResult.items || [];
    const nextGroups = groupResult.items || [];
    setItems(nextPresets);
    setGroups(nextGroups);
    setDerive((old) => ({ ...old, preset_id: nextPresets.some((item) => item.id === old.preset_id) ? old.preset_id : nextPresets[0]?.id || "" }));
    setBatchPresetIds((old) => {
      const validIds = new Set(nextPresets.map((item) => item.id));
      const next = new Set([...old].filter((id) => validIds.has(id)));
      if (next.size === 0 && nextPresets[0]?.id) next.add(nextPresets[0].id);
      return next;
    });
    setGroup((old) => ({ ...old, group_id: nextGroups.some((item) => item.id === old.group_id) ? old.group_id : nextGroups[0]?.id || "" }));
    if (requestScopeId) {
      const [variantResult, healthResult] = await Promise.all([
        request<{ items: any[] }>(`/scopes/${requestScopeId}/derived-variants`).catch(() => ({ items: [] })),
        request(`/scopes/${requestScopeId}/derived-variants/health`).catch((err) => ({ error: String((err as Error).message) }))
      ]);
      if (token.current !== current || requestProjectId !== projectId || requestScopeId !== scopeId) return;
      setVariants(variantResult.items || []);
      setHealth(healthResult);
    }
  }

  async function run(label: string, fn: () => Promise<unknown>, after = true, apply?: (value: any) => void) {
    const requestProjectId = projectId;
    const requestScopeId = scopeId;
    const current = token.current;
    setError("");
    try {
      const value = await fn();
      if (token.current !== current || requestProjectId !== projectId || requestScopeId !== scopeId) return;
      apply?.(value);
      setResult({ label, value });
      if (after) await refresh(requestProjectId, requestScopeId, current);
    } catch (err) {
      if (token.current === current && requestProjectId === projectId && requestScopeId === scopeId) setError(String((err as Error).message));
    }
  }

  function presetParams() {
    const params: Record<string, unknown> = {
      mode: preset.mode,
      width: numberValue(preset.width),
      height: numberValue(preset.height),
      quality: numberValue(preset.quality),
      format: preset.format,
      alpha_policy: "preserve",
      orientation: "auto",
      color_policy: "srgb",
      metadata_policy: "strip"
    };
    if (preset.mode === "fill") params.focal_point = { x: Number(preset.focalX), y: Number(preset.focalY) };
    return params;
  }

  useEffect(() => {
    const current = ++token.current;
    const projectChanged = previousProject.current !== projectId;
    previousProject.current = projectId;
    setVariants([]);
    setHealth(null);
    setGroupMembers(null);
    setResult(null);
    setError("");
    setDerive((old) => ({ ...old, object_id: "", output_key: "", output_scope_id: "", object_ids: "", snapshot_id: "", batch_id: "", idempotency_key: "", preset_id: projectChanged ? "" : old.preset_id }));
    setGroup((old) => ({ ...old, object_id: "", group_id: projectChanged ? "" : old.group_id }));
    if (projectChanged) {
      setItems([]);
      setGroups([]);
      setBatchPresetIds(new Set());
    }
    refresh(projectId, scopeId, current).catch(() => undefined);
  }, [projectId, scopeId]);

  useEffect(() => {
    if (initialObjectIds.length) {
      setDerive((old) => ({ ...old, object_ids: initialObjectIds.join("\n"), snapshot_id: "" }));
      setHandoffOpen(true);
    }
  }, [initialObjectIds.join("|")]);

  const batchObjectIds = derive.object_ids.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
  const selectedPresetIds = [...batchPresetIds];
  const derivePresetValid = Boolean(derive.preset_id && items.some((item) => item.id === derive.preset_id));
  const groupIdValid = Boolean(group.group_id && groups.some((item) => item.id === group.group_id));
  const plannedBatchCount = (batchObjectIds.length || (derive.snapshot_id ? 1 : 0)) * selectedPresetIds.length;

  function submitBatch() {
    const body = {
      object_ids: batchObjectIds.length ? batchObjectIds : undefined,
      snapshot_id: batchObjectIds.length ? undefined : derive.snapshot_id || undefined,
      preset_ids: selectedPresetIds,
      output_scope_id: derive.output_scope_id || undefined,
      idempotency_key: derive.idempotency_key || undefined
    };
    return request(`/scopes/${scopeId}/derived-variant-batches`, { method: "POST", body: JSON.stringify(body) });
  }

  return (
    <section className="panel imageWorkbench">
      <div className="workbenchHeader">
        <div>
          <p className="eyebrow">{t("Project Scope")}</p>
          <h2>{t("图片规格")}</h2>
          <p className="hint">{t("管理规格、派生批次、分组和健康证据。")}</p>
        </div>
        <div className="summaryGrid compactMetrics">
          <Metric label={t("规格")} value={items.length} />
          <Metric label={t("派生记录")} value={variants.length} />
          <Metric label={t("分组")} value={groups.length} />
          <Metric label={t("预计任务")} value={plannedBatchCount || t("unknown")} />
        </div>
      </div>
      <div className="workbenchSplit">
      <div className="workbenchCard">
        <h2>{t("规格库")}</h2>
        <label className="field">{t("名称")}<input value={name} onChange={(event) => setName(event.target.value)} /></label>
        <div className="miniGrid">
          <label className="field">{t("mode")}<select value={preset.mode} onChange={(event) => setPreset({ ...preset, mode: event.target.value })}><option value="fit">{t("fit")}</option><option value="fill">{t("fill")}</option><option value="stretch">{t("stretch")}</option></select></label>
          <label className="field">{t("width")}<input type="number" value={preset.width} onChange={(event) => setPreset({ ...preset, width: event.target.value })} /></label>
          <label className="field">{t("height")}<input type="number" value={preset.height} onChange={(event) => setPreset({ ...preset, height: event.target.value })} /></label>
          <label className="field">{t("quality")}<input type="number" min="1" max="100" value={preset.quality} onChange={(event) => setPreset({ ...preset, quality: event.target.value })} /></label>
          <label className="field">{t("format")}<select value={preset.format} onChange={(event) => setPreset({ ...preset, format: event.target.value })}><option value="webp">{t("webp")}</option><option value="jpeg">{t("jpeg")}</option><option value="png">{t("png")}</option></select></label>
          <label className="field">{t("focal x")}<input value={preset.focalX} onChange={(event) => setPreset({ ...preset, focalX: event.target.value })} /></label>
          <label className="field">{t("focal y")}<input value={preset.focalY} onChange={(event) => setPreset({ ...preset, focalY: event.target.value })} /></label>
        </div>
        <button disabled={!projectId || !name} onClick={() => run("preset.create", () => request(`/projects/${projectId}/presets`, { method: "POST", body: JSON.stringify({ name, params: presetParams() }) }))}>{t("创建不可变规格版本")}</button>
        <h2>{t("派生任务")}</h2>
        <label className="field">{t("对象 ID")}<input value={derive.object_id} onChange={(event) => setDerive({ ...derive, object_id: event.target.value })} /></label>
        <label className="field">{t("Preset")}<select value={derive.preset_id} onChange={(event) => setDerive({ ...derive, preset_id: event.target.value })}>{items.map((item) => <option key={item.id} value={item.id}>{item.name} v{item.version}</option>)}</select></label>
        <label className="field">{t("输出 Key")}<input value={derive.output_key} onChange={(event) => setDerive({ ...derive, output_key: event.target.value })} /></label>
        <label className="field">{t("输出 Scope ID")}<input value={derive.output_scope_id} onChange={(event) => setDerive({ ...derive, output_scope_id: event.target.value })} placeholder={t("留空使用当前 scope")} /></label>
        <button disabled={!scopeId || !derive.object_id || !derivePresetValid} onClick={() => run("variant.submit", () => request(`/scopes/${scopeId}/derived-variants`, { method: "POST", body: JSON.stringify({ object_id: derive.object_id, preset_id: derive.preset_id, output_key: derive.output_key || undefined, output_scope_id: derive.output_scope_id || undefined }) }))}>{t("提交派生任务")}</button>
        <details className="workbenchDetails" open={handoffOpen || Boolean(batchObjectIds.length || derive.snapshot_id)}>
          <summary>{t("批量派生")}</summary>
          <p className="hint">{t("已知选中对象直接提交 object_ids；全查询范围必须先在资产页固定 snapshot，再填 snapshot_id。预计提交数只按已知对象数或 snapshot×preset 数计算。")}</p>
          <label className="field">{t("对象 ID 列表")}<textarea value={derive.object_ids} onChange={(event) => { setDerive({ ...derive, object_ids: event.target.value, snapshot_id: "" }); setHandoffOpen(true); }} placeholder={t("每行一个 object_id，可由资产页“生成派生”带入")} /></label>
          <label className="field">{t("Snapshot ID")}<input value={derive.snapshot_id} onChange={(event) => { setDerive({ ...derive, snapshot_id: event.target.value, object_ids: "" }); setHandoffOpen(true); }} placeholder={t("批量全查询时使用固定快照")} /></label>
          <div className="presetChecks" aria-label={t("批量 preset")}>
            {items.map((item) => <label key={item.id}><input type="checkbox" checked={batchPresetIds.has(item.id)} onChange={(event) => setBatchPresetIds(toggle(batchPresetIds, item.id, event.target.checked))} /> {item.name} v{item.version}</label>)}
          </div>
          <label className="field">{t("Idempotency Key")}<input value={derive.idempotency_key} onChange={(event) => setDerive({ ...derive, idempotency_key: event.target.value })} placeholder={t("可选，重试时复用")} /></label>
          <div className="summaryGrid">
            <Metric label={t("对象来源")} value={batchObjectIds.length ? `${batchObjectIds.length} selected` : derive.snapshot_id ? "snapshot" : "none"} />
            <Metric label={t("Preset 数")} value={selectedPresetIds.length} />
            <Metric label={t("预计任务")} value={plannedBatchCount || t("unknown")} />
          </div>
          <button disabled={!scopeId || (!batchObjectIds.length && !derive.snapshot_id) || selectedPresetIds.length === 0} onClick={() => run("variant.batch.submit", submitBatch)}>{t("提交批量派生")}</button>
          <label className="field">{t("Batch ID")}<input value={derive.batch_id} onChange={(event) => setDerive({ ...derive, batch_id: event.target.value })} placeholder={t("批量提交返回的 id")} /></label>
          <button disabled={!scopeId || !derive.batch_id} onClick={() => run("variant.batch.get", () => request(`/scopes/${scopeId}/derived-variant-batches/${encodeURIComponent(derive.batch_id)}`), false)}>{t("读取批次详情")}</button>
        </details>
        <details className="workbenchDetails">
          <summary>{t("分组")}</summary>
          <label className="field">{t("组名")}<input value={group.name} onChange={(event) => setGroup({ ...group, name: event.target.value })} /></label>
          <button disabled={!projectId || !group.name} onClick={() => run("group.create", () => request(`/projects/${projectId}/groups`, { method: "POST", body: JSON.stringify({ name: group.name, source: "manual" }) }))}>{t("创建组")}</button>
          <label className="field">{t("Group")}<select value={group.group_id} onChange={(event) => setGroup({ ...group, group_id: event.target.value })}>{groups.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          <label className="field">{t("对象 ID")}<input value={group.object_id} onChange={(event) => setGroup({ ...group, object_id: event.target.value })} /></label>
          <button disabled={!projectId || !groupIdValid || !group.object_id} onClick={() => run("group.member.add", () => request(`/projects/${projectId}/groups/${encodeURIComponent(group.group_id)}/members`, { method: "POST", body: JSON.stringify({ object_id: group.object_id, source: "manual" }) }))}>{t("加入组")}</button>
          <button disabled={!projectId || !groupIdValid} onClick={() => run("group.members", () => request(`/projects/${projectId}/groups/${encodeURIComponent(group.group_id)}/members`), false, setGroupMembers)}>{t("读取成员")}</button>
        </details>
        {error && <div className="notice error">{translateMessage(error)}</div>}
      </div>
      <div className="stack workbenchCard flush">
        <DataTable columns={[t("名称"), t("版本"), t("模式"), t("尺寸"), t("格式")]} rows={items.map((item) => [item.name, item.version, String(item.params?.mode || "fit"), `${String(item.params?.width || "?")}×${String(item.params?.height || "?")}`, String(item.params?.format || "webp")])} />
        <DerivedHealthPanel data={health} refresh={() => refresh().catch(() => undefined)} rebuildDisabled={!scopeId || (!batchObjectIds.length && !derive.snapshot_id) || selectedPresetIds.length === 0} rebuild={() => run("variant.health.rebuild", submitBatch)} />
        <ReportBox title={t("派生记录")} data={variants.slice(0, 12)} />
        <ReportBox title={t("分组")} data={groups.slice(0, 12)} />
        <ReportBox title={t("分组成员")} data={groupMembers} />
        <ResultPane error="" result={result} />
      </div>
      </div>
    </section>
  );
}

function Settings({ request, projects, connections, managementConnections, refresh, refreshManagementConnections, projectId, setProjectId }: { request: Requester; projects: Project[]; connections: Connection[]; managementConnections: ManagementConnection[]; refresh: () => Promise<void>; refreshManagementConnections: () => Promise<void>; projectId: string; setProjectId: (value: string) => void }) {
  const [conn, setConn] = useState({ display_name: "SeaweedFS test", endpoint_url: "http://127.0.0.1:8333", region: "us-east-1", secret_ref: "server-test", addressing_style: "path", verify_tls: false });
  const [project, setProject] = useState({ project_key: "images", display_name: "Images", description: "" });
  const [scope, setScope] = useState({ connection_id: "", display_name: "Images scope", bucket: "archive", prefix: "", allow_preview: true, allow_original_download: true, writable: false, manage_bucket: false });
  const [tab, setTab] = useState<"management" | "images">("management");
  const [error, setError] = useState("");

  async function save(fn: () => Promise<void>) {
    setError("");
    try {
      await fn();
      await refresh();
    } catch (err) {
      setError(String((err as Error).message));
    }
  }

  return (
    <section className="panel">
      <div className="tabs">
        <button className={tab === "management" ? "active" : "secondary"} onClick={() => setTab("management")}>{t("Management Connection")}</button>
        <button className={tab === "images" ? "active" : "secondary"} onClick={() => setTab("images")}>{t("Project Scope")}</button>
      </div>
      {tab === "management" && <ManagementSettings request={request} s3Connections={connections} managementConnections={managementConnections} refreshConnections={refreshManagementConnections} />}
      {tab === "images" && <div className="grid3">
        <div>
          <h2>{t("S3 连接")}</h2>
          <label className="field">{t("名称")}<input value={conn.display_name} onChange={(event) => setConn({ ...conn, display_name: event.target.value })} /></label>
          <label className="field">{t("Endpoint")}<input value={conn.endpoint_url} onChange={(event) => setConn({ ...conn, endpoint_url: event.target.value })} /></label>
          <label className="field">{t("Secret Ref")}<input value={conn.secret_ref} onChange={(event) => setConn({ ...conn, secret_ref: event.target.value })} /></label>
          <label className="field">{t("Region")}<input value={conn.region} onChange={(event) => setConn({ ...conn, region: event.target.value })} /></label>
          <label><input type="checkbox" checked={conn.verify_tls} onChange={(event) => setConn({ ...conn, verify_tls: event.target.checked })} /> {t("校验 TLS")}</label>
          <button onClick={() => save(async () => { await request("/connections", { method: "POST", body: JSON.stringify(conn) }); })}>{t("添加连接")}</button>
          <DataTable columns={[t("名称"), t("Endpoint"), t("版本")]} rows={connections.map((item) => [item.display_name, item.endpoint_url, item.server_version || "unknown"])} />
        </div>
        <div>
          <h2>{t("项目")}</h2>
          <label className="field">{t("Key")}<input value={project.project_key} onChange={(event) => setProject({ ...project, project_key: event.target.value })} /></label>
          <label className="field">{t("名称")}<input value={project.display_name} onChange={(event) => setProject({ ...project, display_name: event.target.value })} /></label>
          <button onClick={() => save(async () => { const created = await request<Project>("/projects", { method: "POST", body: JSON.stringify(project) }); setProjectId(created.id); })}>{t("添加项目")}</button>
          <DataTable columns={[t("Key"), t("名称")]} rows={projects.map((item) => [item.project_key || item.id, item.display_name])} />
        </div>
        <div>
          <h2>{t("Scope")}</h2>
          <label className="field">{t("连接")}<select value={scope.connection_id} onChange={(event) => setScope({ ...scope, connection_id: event.target.value })}>{connections.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label>
          <label className="field">{t("显示名")}<input value={scope.display_name} onChange={(event) => setScope({ ...scope, display_name: event.target.value })} /></label>
          <label className="field">{t("Bucket")}<input value={scope.bucket} onChange={(event) => setScope({ ...scope, bucket: event.target.value })} /></label>
          <label className="field">{t("Prefix")}<input value={scope.prefix} onChange={(event) => setScope({ ...scope, prefix: event.target.value })} /></label>
          <label><input type="checkbox" checked={scope.allow_preview} onChange={(event) => setScope({ ...scope, allow_preview: event.target.checked })} /> {t("允许预览")}</label>
          <label><input type="checkbox" checked={scope.allow_original_download} onChange={(event) => setScope({ ...scope, allow_original_download: event.target.checked })} /> {t("允许原图下载")}</label>
          <label><input type="checkbox" checked={scope.writable} onChange={(event) => setScope({ ...scope, writable: event.target.checked })} /> {t("可写 scope")}</label>
          <label><input type="checkbox" checked={scope.manage_bucket} onChange={(event) => setScope({ ...scope, manage_bucket: event.target.checked })} /> {t("管理桶配置")}</label>
          <button disabled={!projectId || connections.length === 0} onClick={() => save(async () => {
            await request(`/projects/${projectId}/scopes`, { method: "POST", body: JSON.stringify({ connection_id: scope.connection_id || connections[0]?.id, display_name: scope.display_name, bucket: scope.bucket, prefix: scope.prefix, scope_kind: "source", writable: scope.writable, manage_bucket: scope.manage_bucket, scope_policy: { allow_preview: scope.allow_preview, allow_original_download: scope.allow_original_download }, overlap_ack: true }) });
          })}>{t("添加范围")}</button>
          <p className="hint">{t("Secret Ref 只保存服务端引用；前端不会接收或展示密钥。")}</p>
        </div>
      </div>}
      {error && <div className="notice error">{translateMessage(error)}</div>}
    </section>
  );
}

function ResultPane({ error, result }: { error: string; result: unknown }) {
  const state = isRecord(result) ? String(result.state || result.status || "") : "";
  return <div>{error && <div className="notice error">{translateMessage(error)}</div>}{state === "needs_review" && <div className="notice">{t("操作返回 needs_review：请核查 readback/状态，不会自动重发。")}</div>}{result ? <ResultSummary title={t("执行结果")} data={result} /> : <Empty text={t("执行结果会显示在这里；不支持或权限不足时保留后端错误码。")} />}</div>;
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
  const parsed = parseJson(value || "{}", label);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error(`${label} 必须是 JSON object。`);
  return parsed as Record<string, unknown>;
}

function parseJsonArray(value: string, label: string): unknown[] {
  const parsed = parseJson(value || "[]", label);
  if (!Array.isArray(parsed)) throw new Error(`${label} 必须是 JSON array。`);
  return parsed;
}

function parseJson(value: string, label: string): unknown {
  try {
    return JSON.parse(value);
  } catch (err) {
    throw new Error(`${label} JSON 解析失败：${String((err as Error).message)}`);
  }
}

function lines(value: string) {
  return value.split(/\r?\n|,/).map((item) => item.trim()).filter(Boolean);
}

function numberValue(value: string) {
  const next = Number(value);
  if (!Number.isFinite(next) || next < 0) throw new Error(`数值无效：${value}`);
  return next;
}

function DataTable({ columns, rows }: { columns: string[]; rows: React.ReactNode[][] }) {
  if (rows.length === 0) return <Empty text={t("暂无记录。")} />;
  return <div className="tableWrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div>;
}

function Empty({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

function StatusPill({ text }: { text: string }) {
  return <span className={`pill ${text.includes("fail") || text.includes("denied") || text.includes("error") ? "bad" : text.includes("ready") || text.includes("succeeded") || text.includes("supported") ? "good" : ""}`}>{text}</span>;
}

function DerivedHealthPanel({ data, refresh, rebuild, rebuildDisabled }: { data: unknown; refresh: () => void; rebuild: () => void; rebuildDisabled: boolean }) {
  const value = isRecord(data) && "value" in data ? data.value : data;
  const items = isRecord(value) && Array.isArray(value.items) ? value.items.filter(isRecord) : [];
  const statusCounts = { healthy: 0, missing: 0, corrupt: 0, outdated: 0, unknown: 0 };
  for (const item of items) {
    const status = String(item.health || item.status || item.state || "unknown");
    if (status in statusCounts) statusCounts[status as keyof typeof statusCounts] += 1;
    else if (status === "succeeded") statusCounts.healthy += 1;
    else statusCounts.unknown += 1;
  }
  if (!items.length && isRecord(value)) {
    statusCounts.healthy = Number(value.healthy ?? value.succeeded ?? 0);
    statusCounts.missing = Number(value.missing ?? 0);
    statusCounts.corrupt = Number(value.corrupt ?? value.failed ?? 0);
    statusCounts.outdated = Number(value.outdated ?? value.planned ?? 0);
    statusCounts.unknown = Number(value.unknown ?? (value.unknown_external_relations ? 1 : 0));
  }
  return (
    <div>
      <h2>{t("派生健康")}</h2>
      <div className="summaryGrid">
        <Metric label="healthy" value={statusCounts.healthy} />
        <Metric label="missing" value={statusCounts.missing} />
        <Metric label="corrupt" value={statusCounts.corrupt} />
        <Metric label="outdated" value={statusCounts.outdated} />
        <Metric label="unknown" value={statusCounts.unknown} />
      </div>
      <div className="toolbar" style={{ marginTop: 10 }}>
        <button onClick={refresh}>{t("刷新健康")}</button>
        <button disabled={rebuildDisabled} onClick={rebuild}>{t("按当前批量输入补建")}</button>
      </div>
      <p className="hint">{t("发布/CDN 未接入时保持 unknown；补建只提交新的派生批次，不移动或清理源图。")}</p>
      <ApiStructuredResult title={t("派生健康")} data={data} kind="variant-health" />
    </div>
  );
}

function AppIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7.5c0-2.2 3.6-4 8-4s8 1.8 8 4-3.6 4-8 4-8-1.8-8-4Z" /><path d="M4 7.5v4c0 2.2 3.6 4 8 4s8-1.8 8-4v-4" /><path d="M4 11.5v5c0 2.2 3.6 4 8 4s8-1.8 8-4v-5" /></svg>;
}

function Icon({ name }: { name: string }) {
  const common = { fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  const paths: Record<string, React.ReactNode> = {
    dashboard: <><rect x="3" y="3" width="7" height="8" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="15" width="7" height="6" rx="1.5" /></>,
    topology: <><circle cx="12" cy="5" r="2" /><circle cx="5" cy="18" r="2" /><circle cx="19" cy="18" r="2" /><path d="M12 7v4M12 11H5v5M12 11h7v5" /></>,
    storage: <><ellipse cx="12" cy="5" rx="7" ry="3" /><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5" /><path d="M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" /></>,
    bucket: <><path d="M5 6h14l-1.5 14h-11L5 6Z" /><path d="M8 6a4 4 0 0 1 8 0" /></>,
    files: <><path d="M5 4h5l2 2h7v14H5z" /><path d="M8 11h8M8 15h5" /></>,
    objects: <><path d="M4 7h16v10H4z" /><path d="M8 7V5h8v2" /><circle cx="8" cy="12" r="1" /><path d="M11 12h5M11 15h7" /></>,
    key: <><circle cx="8" cy="12" r="3" /><path d="M11 12h9M16 12v3M19 12v2" /></>,
    wrench: <><path d="M14.5 5.5a4 4 0 0 0 4.9 5.1L11 19l-4-4 8.4-8.4Z" /><path d="m5 21-2-2 4-4 2 2-4 4Z" /></>,
    plug: <><path d="M8 2v6M16 2v6M6 8h12v4a6 6 0 0 1-12 0V8Z" /><path d="M12 18v4" /></>,
    images: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m7 15 3-3 2 2 3-4 3 5" /><circle cx="8" cy="9" r="1" /></>,
    activity: <><path d="M4 12h4l2-6 4 12 2-6h4" /></>,
    shield: <><path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6l-7-3Z" /><path d="m9 12 2 2 4-4" /></>,
    workflow: <><circle cx="6" cy="6" r="2" /><circle cx="18" cy="6" r="2" /><circle cx="12" cy="18" r="2" /><path d="M8 6h8M7.5 7.5 11 16M16.5 7.5 13 16" /></>,
    sliders: <><path d="M4 7h10" /><path d="M18 7h2" /><circle cx="16" cy="7" r="2" /><path d="M4 17h2" /><path d="M10 17h10" /><circle cx="8" cy="17" r="2" /></>,
    settings: <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M4.9 4.9 7 7M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1 7 17M17 7l2.1-2.1" /></>
  };
  return <svg className="navIcon" viewBox="0 0 24 24" aria-hidden="true" {...common}>{paths[name]}</svg>;
}

function ReportBox({ title, data }: { title: string; data: unknown }) {
  return <ResultSummary title={title} data={data} />;
}

function EvidenceDetails({ title, data, open = false }: { title: string; data: unknown; open?: boolean }) {
  return <details className="evidence" open={open}><summary>{title}</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>;
}

function ObjectVersionTable({ versions, selectVersion }: { versions: Record<string, any>[]; selectVersion: (versionId: string) => void }) {
  if (!versions.length) return <Empty text={t("读取版本后可在这里选择 Version ID。")} />;
  return <DataTable columns={[t("Version ID"), t("Latest"), t("DeleteMarker"), t("Size"), t("Modified"), t("Action")]} rows={versions.map((version) => {
    const versionId = objectVersionId(version);
    return [
      <code>{versionId}</code>,
      displayBool(version.is_latest ?? version.IsLatest),
      displayBool(version.delete_marker ?? version.is_delete_marker ?? version.DeleteMarker ?? version.IsDeleteMarker),
      formatOptionalBytes(version.size ?? version.Size ?? version.size_bytes),
      String(version.last_modified ?? version.LastModified ?? version.modified ?? version.mtime ?? t("未知")),
      <button onClick={() => selectVersion(versionId)}>{t("使用此版本")}</button>
    ];
  })} />;
}

function objectVersionRows(value: unknown): Record<string, any>[] {
  if (Array.isArray(value)) return value.filter(isRecord);
  if (!isRecord(value)) return [];
  for (const key of ["versions", "items", "Versions", "object_versions"]) {
    const items = value[key];
    if (Array.isArray(items)) return items.filter(isRecord);
  }
  return [];
}

function objectVersionId(version: Record<string, any>) {
  const value = version.version_id ?? version.versionId ?? version.VersionId ?? version.id;
  return value === null || value === undefined || value === "" ? "null" : String(value);
}

function displayBool(value: unknown) {
  if (value === true) return "yes";
  if (value === false) return "no";
  return t("未知");
}

function formatOptionalBytes(value: unknown) {
  const number = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(number) ? formatBytes(number) : t("未知");
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return <div className="summaryCard"><span>{label}</span><strong>{value}</strong></div>;
}

function isRecord(value: unknown): value is Record<string, any> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function normalizeProjects(raw: any[]): Project[] {
  return raw.map((item) => ({
    id: item.id || item.project_id,
    project_key: item.project_key || item.project_id,
    display_name: item.display_name || item.project_key || item.project_id,
    description: item.description || "",
    scopes: (item.scopes || []).map((scope: any) => ({
      id: scope.id || scope.scope_id,
      display_name: scope.display_name || scope.scope_name,
      allow_preview: Boolean(scope.allow_preview),
      allow_original_download: Boolean(scope.allow_original_download),
      bucket: scope.bucket,
      prefix: scope.prefix,
      connection_id: scope.connection_id,
      writable: Boolean(scope.writable),
      manage_bucket: Boolean(scope.manage_bucket),
      authz_epoch: scope.authz_epoch
    }))
  }));
}

function toggle(set: Set<string>, id: string, on: boolean) {
  const next = new Set(set);
  if (on) next.add(id);
  else next.delete(id);
  return next;
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

const INTENT_STORAGE_KEY = "seaweedfs-console.management-intents";

function pendingFingerprint(method: string, path: string, bodyFingerprint: unknown) {
  return `pending:${hashString(`${method} ${path} ${JSON.stringify(bodyFingerprint)}`)}`;
}

function intentFingerprint(method: string, path: string, bodyFingerprint: unknown) {
  return `intent:${hashString(`${method} ${path} ${JSON.stringify(bodyFingerprint)}`)}`;
}

function isDurableManagementMutation(method: string, path: string) {
  if (method === "GET") return false;
  const cleanPath = path.split("?")[0];
  if (/^\/management\/connections(?:\/|$)/.test(cleanPath)) return false;
  if (!/^\/management\/[^/]+\//.test(cleanPath)) return false;
  if (/^\/management\/[^/]+\/objects\/select$/.test(cleanPath)) return false;
  if (/^\/management\/[^/]+\/modules\/s3-tables\/table-preview$/.test(cleanPath)) return false;
  if (/^\/management\/[^/]+\/iam\/policies\/validate$/.test(cleanPath)) return false;
  return true;
}

function supportsBodyIdempotency(method: string, path: string) {
  const cleanPath = path.split("?")[0];
  return (
    /^\/management\/[^/]+\/files\/(?:mkdir|delete|rename)$/.test(cleanPath) ||
    /^\/management\/[^/]+\/maintenance\/actions$/.test(cleanPath) ||
    /^\/management\/[^/]+\/volumes\/[^/]+\/actions$/.test(cleanPath) ||
    /^\/management\/[^/]+\/modules\/mq\//.test(cleanPath) ||
    (/^\/management\/[^/]+\/modules\/s3-tables\//.test(cleanPath) && !/^\/management\/[^/]+\/modules\/s3-tables\/table-preview$/.test(cleanPath))
  );
}

function supportsQueryIdempotency(method: string, path: string) {
  return /^\/management\/[^/]+\/files\/upload$/.test(path.split("?")[0]);
}

function loadStoredIntentKeys() {
  const map = new Map<string, string>();
  try {
    const raw = typeof sessionStorage === "undefined" ? "" : sessionStorage.getItem(INTENT_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      for (const [fingerprint, key] of Object.entries(parsed)) if (typeof key === "string") map.set(fingerprint, key);
    }
  } catch {
    // Intent keys are an optimization for replay safety; in-memory fallback remains valid.
  }
  return map;
}

function saveStoredIntentKeys(map: Map<string, string>) {
  try {
    if (typeof sessionStorage !== "undefined") sessionStorage.setItem(INTENT_STORAGE_KEY, JSON.stringify(Object.fromEntries(map)));
  } catch {
    // Ignore storage failures and keep current-page protection.
  }
}

function intentKeyFor(map: Map<string, string>, fingerprint: string) {
  const existing = map.get(fingerprint);
  if (existing) return existing;
  const key = randomIntentKey();
  map.set(fingerprint, key);
  saveStoredIntentKeys(map);
  return key;
}

function removeIntentKey(map: Map<string, string>, fingerprint: string) {
  if (!map.delete(fingerprint)) return;
  saveStoredIntentKeys(map);
}

async function readResponseText(response: Response, durable: boolean) {
  try {
    return await response.text();
  } catch (err) {
    if (durable) throw new Error(MANAGEMENT_RECEIPT_UNCERTAIN_MESSAGE);
    throw err;
  }
}

function parseResponseText(text: string, accept: (value: any) => void) {
  if (!text) return false;
  try {
    accept(JSON.parse(text));
    return true;
  } catch {
    return false;
  }
}

function classifyDurableManagementReceipt(value: unknown, parsedJson: boolean) {
  if (!parsedJson || !isRecord(value)) return "malformed";
  const state = durableReceiptState(value);
  if (state === "malformed") return "malformed";
  if (["needs_review", "unknown", "pending", "partial", "partially_failed"].includes(state)) return "uncertain";
  if (!["confirmed", "failed"].includes(state)) return "malformed";
  if (typeof value.operation_id !== "string" || !value.operation_id.trim() || typeof value.replayed !== "boolean") return "malformed";
  if (value.local_index_status != null) {
    if (typeof value.local_index_status !== "string") return "malformed";
    if (value.local_index_status.toLowerCase() === "needs_review") return "uncertain";
  }
  return "terminal";
}

function durableReceiptState(value: Record<string, any>) {
  const states = ["status", "journal_status", "state"].flatMap((key) => {
    const candidate = value[key];
    if (candidate == null) return [];
    return typeof candidate === "string" ? [candidate.toLowerCase()] : ["malformed"];
  });
  if (!states.length || states.includes("malformed")) return "malformed";
  return states.every((state) => state === states[0]) ? states[0] : "malformed";
}

function randomIntentKey() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `intent-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function hashString(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function title(route: string) {
  if (isManagementRoute(route)) return managementTitle(route);
  return ({ assets: t("资产检索"), scans: t("任务与扫描"), diagnostics: t("诊断证据"), operations: t("受控操作"), presets: t("图片规格"), settings: t("设置向导") } as Record<string, string>)[route] || t("工作台");
}

function isImageRoute(route: string) {
  return ["assets", "scans", "diagnostics", "operations", "presets"].includes(route);
}

createRoot(document.getElementById("root")!).render(<App />);

