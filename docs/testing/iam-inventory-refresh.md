# IAM inventory refresh and language

Successful IAM mutations refresh users, groups, service accounts, policies and principals. Read-only validation does not refresh inventories. Empty access-key and group cells use the current interface language.

The four browser cases cover user mutations, group/policy mutations, service-account mutations, and populated IAM rows switching English/Chinese.

## Verification

```sh
npm run build --prefix frontend
npm run test:i18n --prefix frontend
node scripts/verify_iam_inventory_regressions.cjs
```

The standalone browser runner defaults to `/usr/bin/chromium` and the execution environment's Playwright module. Override `SWC_CHROME_PATH` and `SWC_PLAYWRIGHT_MODULE` on other machines. Chromium's sandbox is enabled. All HTTP data is synthetic and served on loopback; these checks do not write to SeaweedFS.

2026-10-11: production build passed; 1,432 bilingual messages and 50 SSR views passed; the scoped browser cases passed in actual sandboxed Chromium. The command sandbox blocked Chromium's SUID helper, so approved execution outside that command sandbox was used without changing Chromium security flags or host settings. The external Windows documentation vault is unavailable in this environment.
