# Disable multipart actions for read-only Scopes

Multipart Initialize, Upload part, Complete, and Abort now require the selected Scope to have writable enabled. Disabled controls use the existing bilingual permission explanation. Backend permission enforcement is unchanged.

Verification (2026-10-11), independent baseline `d721727d7450aea7f8b7fbececb94df824ce2fa8`:

- Three browser scenarios cover populated read-only controls with zero dispatches, writable controls dispatching four synthetic requests, and missing Scope controls disabled. Baseline fails because Initialize remains enabled.
- Real sandboxed Chromium, synthetic component request recorder; no backend or SeaweedFS writes.
- `npm run build --prefix frontend`: PASS (existing chunk-size warning).
- `npm run test:i18n --prefix frontend`: PASS, 1,432 bilingual messages and 50 SSR views.
- `git diff --check`: PASS.

Run focused browser regression after installing the locked frontend dependencies; Playwright is an external test tool, not an application dependency:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright-core CHROMIUM_PATH=/usr/bin/chromium node scripts/verify_multipart_readonly_ui.cjs
```

The fixture exports Operations only in Vite’s test transform and never changes production exports. Browser sandboxing remains enabled. RTK is unavailable in this environment, so native commands were used. The external development-plan vault is unavailable; this record is local test evidence rather than a delivery-ledger update.
