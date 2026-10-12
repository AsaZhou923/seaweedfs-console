# Preserve Operations selection handoff within its Scope

Import the selected object after the initial Scope reset, and ignore the previous selection when Scope changes. Fresh same-Scope selection continues to populate Operations.

Verification (2026-10-11), independent baseline `d721727d7450aea7f8b7fbececb94df824ce2fa8`:

- Four browser scenarios cover initial handoff, a new same-Scope selection, Scope-change clearing, and a fresh selection in the new Scope. Baseline fails because initial Object ID is empty.
- Real sandboxed Chromium, synthetic component request recorder; no backend or SeaweedFS writes.
- `npm run build --prefix frontend`: PASS (existing chunk-size warning).
- `npm run test:i18n --prefix frontend`: PASS, 1,432 bilingual messages and 50 SSR views.
- `git diff --check`: PASS.

Run focused browser regression after installing the locked frontend dependencies; Playwright is an external test tool, not an application dependency:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright-core CHROMIUM_PATH=/usr/bin/chromium node scripts/verify_operations_selection_handoff.cjs
```

The fixture exports Operations only in Vite’s test transform and never changes production exports. Browser sandboxing remains enabled. RTK is unavailable in this environment, so native commands were used. The external development-plan vault is unavailable; this record is local test evidence rather than a delivery-ledger update.

Merge note: the independent object-version-identity fix modifies the same selection effect. When combining, keep this effect after the Scope reset and retain its sameScope guard, but call changeObjectId(sourceObjectIds[0]) instead of the baseline setObjectOps updater.
