"""Read-only fixed-version Admin schema probe; no upstream secrets in evidence."""
import json
from pathlib import Path
import re
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
SENSITIVE = {"password", "secretkey", "secretaccesskey", "csrf_token", "csrftoken", "sessiontoken", "authorization", "cookie", "token"}


def redact(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if re.sub(r"[-_]", "", key).lower() in {re.sub(r"[-_]", "", key).lower() for key in SENSITIVE} else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and any(part in value.lower() for part in ("x-amz-signature=", "x-amz-credential=")):
        return "[redacted signed URL]"
    return value


def main():
    entry = json.loads((ROOT / "output" / "server-secrets.json").read_text())["server-admin"]
    base = entry["allowed_endpoint_url"]
    evidence = {"protocol_baseline": "4.48", "source_sha": "530be3e37337488ecc34d58441e0bc476e121c93", "endpoint": base, "routes": {}}
    with httpx.Client(base_url=base, timeout=10, follow_redirects=False, trust_env=False) as client:
        login = client.get("/login")
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', login.text)
        if not match:
            raise RuntimeError("Official Admin login CSRF field not found")
        signed_in = client.post("/login", data={"username": entry["username"], "password": entry["password"], "csrf_token": match.group(1)})
        if signed_in.status_code not in (302, 303):
            raise RuntimeError(f"Official Admin login refused ({signed_in.status_code})")
        routes = ["/health", "/api/admin", "/api/cluster/topology", "/api/cluster/masters", "/api/cluster/volumes",
                  "/api/config", "/api/s3/buckets", "/api/users", "/api/groups", "/api/service-accounts", "/api/object-store/policies",
                  "/api/volumes/export", "/api/plugin/status", "/api/plugin/workers", "/api/plugin/jobs",
                  "/api/plugin/lanes", "/api/plugin/job-types", "/api/plugin/scheduler-status", "/api/plugin/activities"]
        for route in routes:
            response = client.get(route)
            item = {"status": response.status_code, "content_type": response.headers.get("content-type"), "bytes": len(response.content)}
            if "json" in response.headers.get("content-type", ""):
                item["data"] = redact(response.json())
            elif "csv" in response.headers.get("content-type", ""):
                item["csv_header"] = response.text.splitlines()[0] if response.text else None
                item["csv_rows"] = response.text.splitlines()[1:6]
            evidence["routes"][route] = item
            print(f"{route} {item['status']} {item['content_type']}")
    path = ROOT / "output" / "official-admin-read-probe.json"
    path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Read-only redacted evidence saved; no management mutation issued.")


if __name__ == "__main__":
    main()
