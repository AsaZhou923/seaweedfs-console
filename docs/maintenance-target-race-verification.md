# Maintenance target race verification

Changing the Job type or Job ID while its detail request is pending could populate the new target with the old target's result. A prior JobType configuration could then be copied into the new target's editor.

The maintenance form now invalidates pending detail requests on target and management-connection changes, clears copied configuration and notices, and ignores obsolete successes and errors. Loading configuration requires evidence for the current JobType. Pending mutation notices are also tied to the target generations. Route remount keys are outside this change.

## Validation — 2026-10-11

Independent checkout based on `d721727d7450aea7f8b7fbececb94df824ce2fa8`.

- `npm run build --prefix frontend`: passed.
- `npm run test:i18n --prefix frontend`: passed, 1,432 bilingual messages and 50 SSR views.
- `node scripts/verify_maintenance_target_regressions.cjs`: three scenarios passed in real sandboxed Chromium with synthetic localhost HTTP: reversed JobType responses retain B configuration; changing JobType clears copied configuration; reversed Job ID responses retain B details.
- `git diff --check`: passed.

The runner defaults to externally installed Playwright Core at `/opt/codex/runtimes/cua/lib/node_modules/playwright-core` and `/usr/bin/chromium`. Override `SWC_PLAYWRIGHT_MODULE`, `SWC_CHROME_PATH`, or `SWC_REGRESSION_DIST` for other installations. Chromium sandboxing stays enabled. The initial constrained-command launch aborted on the SUID sandbox helper; the approved execution retry with writable XDG configuration/cache directories passed without changing host security settings.

This is mocked service coverage, not live maintenance write acceptance. No service credentials, real storage writes, application dependencies, or route keys were added. The referenced Windows documentation vault is unavailable here; this scoped record preserves the verification outcome locally.
