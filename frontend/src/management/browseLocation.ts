export type ObjectBrowseTarget = { managementId: string; bucket: string; key?: string; scopeId?: string; prefix?: string };
export type FileBrowseTarget = { managementId: string; path: string };

export function readBrowseTarget(hash: string): ObjectBrowseTarget | undefined {
  if (!["objects", "settings"].includes(hash.replace(/^#/, "").split("?")[0])) return undefined;
  const params = new URLSearchParams(hash.split("?")[1] || "");
  if (!["management", "bucket", "scope", "prefix", "key"].some((key) => params.has(key))) return undefined;
  const managementId = params.get("management") || "";
  const bucket = params.get("bucket") || "";
  return { managementId, bucket, ...(params.has("key") ? { key: params.get("key")! } : {}), ...(params.has("scope") ? { scopeId: params.get("scope")! } : {}), ...(params.has("prefix") ? { prefix: params.get("prefix")! } : {}) };
}

export function objectBrowseHref(target: ObjectBrowseTarget, setup = false) {
  const params = new URLSearchParams({ management: target.managementId, bucket: target.bucket });
  if (target.key !== undefined) params.set("key", target.key);
  if (target.scopeId !== undefined && !setup) params.set("scope", target.scopeId);
  if (target.prefix !== undefined) params.set("prefix", target.prefix);
  if (setup) params.set("setup", "scope");
  return `#${setup ? "settings" : "objects"}?${params.toString()}`;
}

export function readFileBrowseTarget(hash: string): FileBrowseTarget | undefined {
  if (hash.replace(/^#/, "").split("?")[0] !== "files") return undefined;
  const params = new URLSearchParams(hash.split("?")[1] || "");
  if (!params.has("management") && !params.has("path")) return undefined;
  const managementId = params.get("management") || "";
  return { managementId, path: params.get("path") ?? "/" };
}

export function fileBrowseHref(target: FileBrowseTarget) {
  return `#files?${new URLSearchParams({ management: target.managementId, path: target.path })}`;
}

function canonicalFilerPath(raw: string): string | undefined {
  if (raw.includes("\0") || raw.includes("\\") || !raw.trim().startsWith("/")) return undefined;
  const parts = raw.trim().split("/").filter((part) => part !== "" && part !== ".");
  // Decode traversal/separator bytes without rejecting ordinary literal percent names.
  const decodeBytes = (value: string) => value.replace(/%([0-9a-f]{2})/gi, (_match, hex: string) => String.fromCharCode(parseInt(hex, 16)));
  for (const part of parts) {
    const decoded = decodeBytes(decodeBytes(part));
    if (part === ".." || decoded === "." || decoded === ".." || decoded.includes("/") || decoded.includes("\\")) return undefined;
  }
  return "/" + parts.join("/");
}

export function filerLocation(rawPath: string | undefined, configuredRoots: unknown) {
  const values = configuredRoots === undefined ? ["/"] : Array.isArray(configuredRoots) ? configuredRoots : [];
  const roots = values.filter((value): value is string => typeof value === "string").map(canonicalFilerPath).filter((value): value is string => value !== undefined);
  const requested = rawPath ?? roots[0] ?? "/";
  const canonical = canonicalFilerPath(requested);
  const path = canonical ?? requested;
  const root = [...roots].sort((a, b) => b.length - a.length).find((value) => path === value || path.startsWith(value.replace(/\/$/, "") + "/"));
  const protectedPath = protectedFilerPath(path);
  return { path, roots, root, allowed: canonical !== undefined && root !== undefined && !protectedPath };
}

export function protectedFilerPath(path: string) {
  return ["/.etc", "/.system", "/etc", "/topics", "/filer-store"].some((value) => path === value || path.startsWith(value + "/"));
}

export function replaceBrowseHash(hash: string) {
  if (location.hash === hash) return;
  history.replaceState(null, "", hash);
  dispatchEvent(new HashChangeEvent("hashchange"));
}

export function directoryAncestors(value: string, root: string) {
  if (!value.startsWith(root)) return [];
  const ancestors = [root];
  for (let index = root.length; index < value.length; index += 1) {
    if (value[index] === "/" && index + 1 < value.length) ancestors.push(value.slice(0, index + 1));
  }
  if (value !== root) ancestors.push(value);
  return [...new Set(ancestors)];
}

export function filerObjectTarget(managementId: string, identity: unknown): ObjectBrowseTarget | undefined {
  if (!identity || typeof identity !== "object" || Array.isArray(identity)) return undefined;
  const { bucket, key } = identity as Record<string, unknown>;
  if (typeof bucket !== "string" || !bucket || bucket.includes("/") || typeof key !== "string" || !key) return undefined;
  return { managementId, bucket, key };
}
