# Management connection and selection state

Connection switches remount management forms, confirmations, receipts and settings. Filer target changes invalidate delete previews. Object selections are bound to the connection, Scope and query, and reversed responses cannot replace the latest selection. Bucket details disappear while the new target loads. The parent object selection supports clearing stale selections.

The six browser cases cover connection switches for Filer and object deletion, edited Filer targets, stale Scope selection, reversed object selection responses, and pending bucket policy reads. Explicit Operations handoff is covered by its separate change.

## Verification

```sh
npm run build --prefix frontend
npm run test:i18n --prefix frontend
node scripts/verify_management_context_regressions.cjs
```

The standalone browser runner defaults to `/usr/bin/chromium` and the execution environment's Playwright module. Override `SWC_CHROME_PATH` and `SWC_PLAYWRIGHT_MODULE` on other machines. Chromium's sandbox is enabled. All HTTP data is synthetic and served on loopback; these checks do not write to SeaweedFS.

2026-10-11: production build passed; 1,432 bilingual messages and 50 SSR views passed; the scoped browser cases passed in actual sandboxed Chromium. The command sandbox blocked Chromium's SUID helper, so approved execution outside that command sandbox was used without changing Chromium security flags or host settings. The external Windows documentation vault is unavailable in this environment.

For a composed build that also includes `fix/operations-selection-handoff`, run `SWC_VERIFY_OPERATIONS_HANDOFF=1 node scripts/verify_management_context_regressions.cjs`. This preserves the original explicit latest-object handoff assertion: click Object operations after reversed selection responses and verify that the destination form receives the latest object ID. The independent default runs six management cases without requiring the separate handoff repair.
