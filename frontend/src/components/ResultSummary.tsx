import "./result-summary.css";
import { formatDate, formatNumber, t, useLocale } from "../i18n";

export type ResultSummaryKind =
  | "auto"
  | "jobs"
  | "capacity"
  | "duplicates"
  | "variant-health"
  | "generic";

export type ResultSummaryProps = {
  title?: string;
  data: unknown;
  kind?: ResultSummaryKind;
  maxRows?: number;
  rawInitiallyOpen?: boolean;
  emptyText?: string;
};

type RowValue = string | number | boolean | null | undefined;
type Row = Record<string, unknown>;
const STATUS_VALUE_FIELDS = new Set(["control_request", "health", "reference_state", "state", "status"]);

const STATE_LABELS: Record<string, string> = {
  alerting: "Alerting",
  cancelled: "Cancelled",
  corrupt: "Corrupt",
  denied: "Denied",
  error: "Failed",
  failed: "Failed",
  needs_review: "Needs review",
  none: "None",
  ok: "OK",
  partially_failed: "Partially failed",
  paused: "Paused",
  planned: "Planned",
  queued: "Queued",
  ready: "Ready",
  running: "Running",
  succeeded: "Succeeded",
  supported: "Supported",
  healthy: "Healthy",
  missing: "Missing",
  outdated: "Outdated",
  unknown: "Unknown",
  unsupported: "Unsupported",
};

const FIELD_LABELS: Record<string, string> = {
  action: "Action",
  algorithm: "Algorithm",
  bucket: "Bucket",
  by_bucket: "By bucket",
  by_format: "By format",
  checksum: "Checksum",
  control_request: "Control request",
  coverage: "Coverage",
  covered_count: "Covered count",
  created_at: "Created at",
  current_logical_bytes: "Current logical bytes",
  derived_bytes: "Derived bytes",
  eligible_count: "Eligible count",
  errors: "Errors",
  failed: "Failed",
  id: "ID",
  key: "Key",
  kind: "Kind",
  known_relations_only: "Known relations only",
  logical_total_bytes: "Logical total bytes",
  name: "Name",
  needs_review: "Needs review",
  object_count: "Object count",
  observed_at: "Observed at",
  physical_disk_bytes: "Physical disk bytes",
  planned: "Planned",
  private_cache_bytes: "Private cache bytes",
  processed: "Processed",
  reference_state: "Reference state",
  sample_complete: "Sample complete",
  scope_id: "Scope",
  source: "Source",
  source_bytes: "Source bytes",
  state: "Status",
  status: "Status",
  succeeded: "Succeeded",
  temporary_bytes: "Temporary bytes",
  total: "Total",
  unscanned_count: "Unscanned count",
  unknown_bytes: "Unknown bytes",
  unknown_external_relations: "Unknown external relations",
  updated_at: "Updated at",
  version: "Version",
  version_bytes: "Version bytes",
};

export function ResultSummary({
  title,
  data,
  kind = "auto",
  maxRows = 8,
  rawInitiallyOpen = false,
  emptyText = "No results.",
}: ResultSummaryProps) {
  useLocale();
  const isEmpty = isEmptyResult(data);
  const summaryTitle = title ? t(title) : undefined;

  return (
    <section className="summary-card" aria-label={summaryTitle || t("Result summary")}>
      {title ? (
        <header className="summary-header">
          <h2>{summaryTitle}</h2>
        </header>
      ) : null}
      {isEmpty ? (
        <EmptyState text={emptyText} />
      ) : (
        <StructuredResult data={data} kind={kind} maxRows={maxRows} />
      )}
      {!isEmpty ? (
        <details className="summary-raw" open={rawInitiallyOpen}>
          <summary>{t("Raw API evidence")}</summary>
          <pre>{safeJson(data)}</pre>
        </details>
      ) : null}
    </section>
  );
}

export function StructuredResult({
  data,
  title,
  kind = "auto",
  maxRows = 8,
}: ResultSummaryProps) {
  useLocale();
  const resolved = kind === "auto" ? detectKind(data) : kind;

  if (isRecord(data) && ("duplicates" in data || "capacity" in data)) {
    return (
      <div className="summary-stack">
        {"capacity" in data ? (
          <StructuredResult title="Capacity" data={data.capacity} kind="capacity" maxRows={maxRows} />
        ) : null}
        {"duplicates" in data ? (
          <StructuredResult title="Duplicate candidates" data={data.duplicates} kind="duplicates" maxRows={maxRows} />
        ) : null}
      </div>
    );
  }

  return (
    <div className="summary-content">
      {title ? <h3 className="summary-subtitle">{t(title)}</h3> : null}
      {resolved === "jobs" ? <JobsSummary data={data} maxRows={maxRows} /> : null}
      {resolved === "capacity" ? <CapacitySummary data={data} maxRows={maxRows} /> : null}
      {resolved === "duplicates" ? <DuplicatesSummary data={data} maxRows={maxRows} /> : null}
      {resolved === "variant-health" ? <VariantHealthSummary data={data} /> : null}
      {resolved === "generic" ? <GenericSummary data={data} maxRows={maxRows} /> : null}
    </div>
  );
}

function JobsSummary({ data, maxRows }: { data: unknown; maxRows: number }) {
  useLocale();
  const rows = asArray(data).filter(isRecord).slice(0, maxRows);
  if (!rows.length) return <EmptyState text="No job records." />;

  return (
    <div className="summary-table-wrap">
      <table className="summary-table">
        <caption>{t("Recent jobs")}</caption>
        <thead>
          <tr>
            <th scope="col">{t("Type")}</th>
            <th scope="col">{t("Status")}</th>
            <th scope="col">{t("Progress")}</th>
            <th scope="col">{t("Errors")}</th>
            <th scope="col">{t("Control")}</th>
            <th scope="col">{t("Updated at")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const state = text(row.state ?? row.status ?? "unknown");
            const total = row.total;
            return (
              <tr key={stableKey(row, index)}>
                <td><CodeText value={row.kind ?? row.action ?? row.id} /></td>
                <td><StatusBadge state={state} /></td>
                <td>{formatProgress(row.processed, total)}</td>
                <td>{formatValue(row.errors ?? 0)}</td>
                <td>{formatStatusValue(row.control_request ?? "none")}</td>
                <td>{formatMaybeDate(row.updated_at ?? row.created_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CapacitySummary({ data, maxRows }: { data: unknown; maxRows: number }) {
  useLocale();
  if (!isRecord(data)) return <GenericSummary data={data} maxRows={maxRows} />;

  const metrics = [
    ["current_logical_bytes", data.current_logical_bytes],
    ["object_count", data.object_count],
    ["private_cache_bytes", data.private_cache_bytes],
    ["version_bytes", data.version_bytes],
    ["physical_disk_bytes", data.physical_disk_bytes],
    ["coverage", data.coverage],
    ["reference_state", data.reference_state],
    ["observed_at", data.observed_at ?? data.created_at],
  ] as const;

  return (
    <div className="summary-stack">
      <DefinitionGrid rows={metrics} />
      <MapTable title="By format" data={data.by_format} valueLabel="Bytes" maxRows={maxRows} />
      <MapTable title="By bucket" data={data.by_bucket} valueLabel="Bytes" maxRows={maxRows} />
      {Array.isArray(data.unmeasured) && data.unmeasured.length ? (
        <p className="summary-note">{t("Unmeasured fields: {fields}", { fields: data.unmeasured.map((field) => formatFieldName(text(field))).join(t(", ")) })}</p>
      ) : null}
    </div>
  );
}

function DuplicatesSummary({ data, maxRows }: { data: unknown; maxRows: number }) {
  useLocale();
  if (!isRecord(data)) return <GenericSummary data={data} maxRows={maxRows} />;
  const groups = asArray(data.groups).filter(isRecord).slice(0, maxRows);

  return (
    <div className="summary-stack">
      <DefinitionGrid
        rows={[
          ["algorithm", data.algorithm],
          ["source", data.source],
          ["covered_count", data.covered_count],
          ["eligible_count", data.eligible_count],
          ["unscanned_count", data.unscanned_count],
          ["reference_state", data.reference_state],
        ]}
      />
      {groups.length ? (
        <div className="summary-table-wrap">
          <table className="summary-table">
            <caption>{t("Exact checksum duplicate candidates")}</caption>
            <thead>
              <tr>
                <th scope="col">SHA256</th>
                <th scope="col">{t("Object count")}</th>
                <th scope="col">{t("Sample objects")}</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group, index) => {
                const objects = asArray(group.objects).filter(isRecord);
                return (
                  <tr key={stableKey(group, index)}>
                    <td><CodeText value={group.sha256 ?? group.checksum} /></td>
                    <td>{formatValue(group.count ?? objects.length)}</td>
                    <td title={objects.map((item) => text(item.key_display ?? item.key ?? item.id)).join(", ")}>
                      {objects.slice(0, 3).map((item) => text(item.key_display ?? item.key ?? item.id)).join(" · ") || t("Not provided")}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState text="No exact checksum duplicate candidates. Unscanned objects are not treated as zero duplicates." />
      )}
    </div>
  );
}

function VariantHealthSummary({ data }: { data: unknown }) {
  useLocale();
  if (!isRecord(data)) return <GenericSummary data={data} maxRows={8} />;
  const rows = [
    ["succeeded", data.succeeded],
    ["planned", data.planned],
    ["needs_review", data.needs_review],
    ["failed", data.failed],
    ["known_relations_only", data.known_relations_only],
    ["unknown_external_relations", data.unknown_external_relations],
    ["scope_id", data.scope_id],
  ] as const;

  return (
    <div className="summary-stack">
      <div className="summary-status-grid" aria-label={t("Variant health")}>
        {["succeeded", "planned", "needs_review", "failed"].map((field) => (
          <div className="summary-stat" key={field}>
            <span>{formatFieldName(field)}</span>
            <strong>{formatValue(data[field])}</strong>
          </div>
        ))}
      </div>
      <DefinitionGrid rows={rows} />
      {data.unknown_external_relations === true ? (
        <p className="summary-note">{t("External relations are not connected; do not show unknown relations as unreferenced.")}</p>
      ) : null}
    </div>
  );
}

function GenericSummary({ data, maxRows }: { data: unknown; maxRows: number }) {
  useLocale();
  if (Array.isArray(data)) return <ArrayTable data={data} maxRows={maxRows} />;
  if (!isRecord(data)) {
    return (
      <dl className="summary-defs">
        <div>
          <dt>{t("Result")}</dt>
          <dd>{formatValue(data)}</dd>
        </div>
      </dl>
    );
  }

  const primitiveRows = Object.entries(data).filter(([, value]) => isDisplayValue(value));
  const nestedArrays = Object.entries(data).filter((entry): entry is [string, unknown[]] => Array.isArray(entry[1]));
  const nestedObjects = Object.entries(data).filter((entry): entry is [string, Row] => isRecord(entry[1]));

  return (
    <div className="summary-stack">
      {primitiveRows.length ? <DefinitionGrid rows={primitiveRows.slice(0, 16)} /> : null}
      {nestedArrays.slice(0, 2).map(([name, value]) => (
        <ArrayTable key={name} title={formatFieldName(name)} data={value} maxRows={maxRows} />
      ))}
      {nestedObjects.slice(0, 2).map(([name, value]) => (
        <MapTable key={name} title={formatFieldName(name)} data={value} maxRows={maxRows} />
      ))}
      {!primitiveRows.length && !nestedArrays.length && !nestedObjects.length ? (
        <EmptyState text="The result is empty or has no displayable fields." />
      ) : null}
    </div>
  );
}

function DefinitionGrid({ rows }: { rows: ReadonlyArray<readonly [string, unknown]> }) {
  useLocale();
  const visible = rows.filter(([, value]) => value !== undefined);
  if (!visible.length) return <EmptyState text="No structured fields." />;

  return (
    <dl className="summary-defs">
      {visible.map(([name, value]) => (
        <div key={name}>
          <dt>{formatFieldName(name)}</dt>
          <dd title={plainValue(value)}>{formatMaybeBytes(name, value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function MapTable({
  title,
  data,
  valueLabel = "Value",
  maxRows,
}: {
  title: string;
  data: unknown;
  valueLabel?: string;
  maxRows: number;
}) {
  useLocale();
  if (!isRecord(data) || !Object.keys(data).length) return null;
  const rows = Object.entries(data).slice(0, maxRows);

  return (
    <div className="summary-table-wrap">
      <table className="summary-table">
        <caption>{t(title)}</caption>
        <thead>
          <tr>
            <th scope="col">{t("Name")}</th>
            <th scope="col">{t(valueLabel)}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, value]) => (
            <tr key={name}>
              <td title={name}><CodeText value={name} /></td>
              <td>{formatMaybeBytes(name, value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ArrayTable({ title = "Records", data, maxRows }: { title?: string; data: unknown[]; maxRows: number }) {
  useLocale();
  const rows = data.filter(isRecord).slice(0, maxRows);
  if (!rows.length) return <EmptyState text="No records." />;

  const columns = pickColumns(rows);
  return (
    <div className="summary-table-wrap">
      <table className="summary-table">
        <caption>{t(title)}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column} scope="col">{formatFieldName(column)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={stableKey(row, index)}>
              {columns.map((column) => (
                <td key={column} title={plainValue(row[column])}>
                  {column === "state" || column === "status" ? (
                    <StatusBadge state={text(row[column] ?? "unknown")} />
                  ) : (
                    formatMaybeBytes(column, row[column])
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatusBadge({ state }: { state: string }) {
  useLocale();
  const normalized = state.toLowerCase();
  const tone = ["succeeded", "ok", "ready", "supported"].includes(normalized)
    ? "good"
    : ["failed", "denied", "error", "corrupt", "alerting"].includes(normalized)
      ? "bad"
      : ["needs_review", "partially_failed", "unknown", "unsupported"].includes(normalized)
        ? "warn"
        : "neutral";
  return (
    <span className={`summary-status summary-status-${tone}`}>
      {STATE_LABELS[normalized] ? t(STATE_LABELS[normalized]) : state}
    </span>
  );
}

function EmptyState({ text }: { text: string }) {
  useLocale();
  return <p className="summary-empty">{t(text)}</p>;
}

function CodeText({ value }: { value: unknown }) {
  useLocale();
  const rendered = formatValue(value);
  return <code className="summary-code" title={plainValue(value)}>{rendered}</code>;
}

function detectKind(data: unknown): ResultSummaryKind {
  if (Array.isArray(data) && data.some((item) => isRecord(item) && ("state" in item || "kind" in item) && ("processed" in item || "errors" in item || "total" in item))) {
    return "jobs";
  }
  if (isRecord(data)) {
    if ("groups" in data && ("covered_count" in data || "eligible_count" in data || "unscanned_count" in data)) return "duplicates";
    if ("current_logical_bytes" in data || "private_cache_bytes" in data || "by_format" in data || "by_bucket" in data) return "capacity";
    if ("unknown_external_relations" in data && ("planned" in data || "succeeded" in data || "needs_review" in data)) return "variant-health";
  }
  return "generic";
}

function pickColumns(rows: Row[]) {
  const preferred = ["kind", "state", "status", "id", "name", "version", "key", "bucket", "processed", "errors", "updated_at", "created_at"];
  const found = new Set<string>();
  for (const column of preferred) {
    if (rows.some((row) => isDisplayValue(row[column]))) found.add(column);
  }
  for (const row of rows) {
    for (const [key, value] of Object.entries(row)) {
      if (found.size >= 6) break;
      if (isDisplayValue(value)) found.add(key);
    }
  }
  return [...found].slice(0, 6);
}

function formatProgress(processed: unknown, total: unknown) {
  const done = typeof processed === "number" ? processed : Number(processed);
  const hasDone = Number.isFinite(done);
  if (total === null || total === undefined || total === "") {
    return hasDone ? t("{done} / Unknown", { done: formatNumber(done) }) : t("Unknown");
  }
  const all = typeof total === "number" ? total : Number(total);
  if (!Number.isFinite(all)) return hasDone ? t("{done} / Unknown", { done: formatNumber(done) }) : t("Unknown");
  return t("{done} / {total}", { done: formatNumber(hasDone ? done : 0), total: formatNumber(all) });
}

function formatMaybeBytes(name: string, value: unknown) {
  if (isDateField(name) && (typeof value === "string" || typeof value === "number")) return formatMaybeDate(value);
  if (STATUS_VALUE_FIELDS.has(name)) return formatStatusValue(value);
  if (typeof value === "number" && /bytes$|_bytes$|size/i.test(name)) return formatBytes(value);
  return formatValue(value);
}

function formatMaybeDate(value: unknown) {
  if (value === null || value === undefined || value === "") return t("Unknown");
  if ((typeof value === "string" || typeof value === "number") && isValidDateValue(value)) return formatDate(value);
  return formatValue(value);
}

function formatValue(value: unknown): string {
  if (value === null) return t("Unmeasured");
  if (value === undefined || value === "") return t("Unknown");
  if (typeof value === "boolean") return value ? t("Yes") : t("No");
  if (typeof value === "number") return Number.isFinite(value) ? formatNumber(value) : t("Unknown");
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return t("{count} items", { count: formatNumber(value.length) });
  if (isRecord(value)) return t("Structured object");
  return String(value);
}

function formatStatusValue(value: unknown) {
  if (typeof value !== "string") return formatValue(value);
  const normalized = value.toLowerCase();
  return STATE_LABELS[normalized] ? t(STATE_LABELS[normalized]) : value;
}

function plainValue(value: unknown) {
  if (isDisplayValue(value)) return formatValue(value);
  return safeJson(value);
}

function formatFieldName(name: string) {
  return t(FIELD_LABELS[name] || titleCase(name.replaceAll("_", " ")));
}

function formatBytes(value: number) {
  if (!Number.isFinite(value)) return t("Unknown");
  if (value < 1024) return `${formatNumber(value)} B`;
  if (value < 1024 * 1024) return `${formatNumber(Number((value / 1024).toFixed(1)))} KiB`;
  if (value < 1024 * 1024 * 1024) return `${formatNumber(Number((value / 1024 / 1024).toFixed(1)))} MiB`;
  return `${formatNumber(Number((value / 1024 / 1024 / 1024).toFixed(2)))} GiB`;
}

function isDateField(name: string) {
  return /(^|_)(created|updated|observed|modified)_at$/.test(name) || name === "last_modified";
}

function isValidDateValue(value: string | number) {
  if (typeof value === "number") return Number.isFinite(value);
  return !Number.isNaN(Date.parse(value));
}

function titleCase(value: string) {
  return value.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function stableKey(row: Row, index: number) {
  return text(row.id ?? row.job_id ?? row.sha256 ?? row.checksum ?? index);
}

function text(value: unknown) {
  return typeof value === "string" ? value : String(value ?? "");
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function isDisplayValue(value: unknown): value is RowValue {
  return value === null || value === undefined || ["string", "number", "boolean"].includes(typeof value);
}

function isRecord(value: unknown): value is Row {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isEmptyResult(data: unknown) {
  if (data === null || data === undefined || data === "") return true;
  if (Array.isArray(data)) return data.length === 0;
  if (isRecord(data)) return Object.keys(data).length === 0;
  return false;
}

function safeJson(data: unknown) {
  try {
    return JSON.stringify(data, null, 2);
  } catch {
    return String(data);
  }
}

export default ResultSummary;
