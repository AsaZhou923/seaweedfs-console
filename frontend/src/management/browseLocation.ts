export type ObjectBrowseTarget = { managementId: string; bucket: string; key?: string };

export function readBrowseTarget(hash: string): ObjectBrowseTarget | undefined {
  const params = new URLSearchParams(hash.split("?")[1] || "");
  const managementId = params.get("management");
  const bucket = params.get("bucket");
  if (!managementId || !bucket) return undefined;
  return { managementId, bucket, ...(params.has("key") ? { key: params.get("key")! } : {}) };
}

export function objectBrowseHref(target: ObjectBrowseTarget, setup = false) {
  const params = new URLSearchParams({ management: target.managementId, bucket: target.bucket });
  if (target.key !== undefined) params.set("key", target.key);
  if (setup) params.set("setup", "scope");
  return `#${setup ? "settings" : "objects"}?${params.toString()}`;
}

export function filerObjectTarget(managementId: string, path: string): ObjectBrowseTarget | undefined {
  if (!path.startsWith("/buckets/")) return undefined;
  const remainder = path.slice("/buckets/".length);
  const separator = remainder.indexOf("/");
  if (separator < 1 || separator === remainder.length - 1) return undefined;
  return { managementId, bucket: remainder.slice(0, separator), key: remainder.slice(separator + 1) };
}
