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

export function filerObjectTarget(managementId: string, identity: unknown): ObjectBrowseTarget | undefined {
  if (!identity || typeof identity !== "object" || Array.isArray(identity)) return undefined;
  const { bucket, key } = identity as Record<string, unknown>;
  if (typeof bucket !== "string" || !bucket || bucket.includes("/") || typeof key !== "string" || !key) return undefined;
  return { managementId, bucket, key };
}
