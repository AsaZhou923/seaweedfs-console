# Keep project Scope hydration bound to the selected project

Switching projects previously left the previous Scope and object actions available until its replacement list loaded. Delayed bootstrap requests could also switch the visible Scope back to a prior project. Project selection now clears stale context immediately, and Scope hydration ignores responses and failures from obsolete requests.

Run `npm run build --prefix frontend` and `npm run test:i18n --prefix frontend`, then `CHROME_PATH=/usr/bin/chromium SWC_REGRESSION_PROJECT_CONTEXT_ONLY=1 env -u CI node scripts/verify_frontend_review_regressions.cjs`. The Chromium executable must be available and its sandbox supported; no live storage endpoints are used. On Windows set CHROME_PATH with the shell's environment syntax. The runner's default mode also includes these checks in the broader frontend suite.

Five real Chromium scenarios cover delayed and failed project Scope reads, reversed successful and failed requests, and delayed bootstrap connections. This branch's build and bilingual/SSR checks passed with 1,432 messages and 50 views. This branch does not include the separate management selection or object version repairs.

Optional DOM-only fallback: install jsdom@30.1.2 in a separate tools directory, set SWC_JSDOM_ROOT to it, and run `node scripts/verify_project_context_dom.cjs`. The shared helper is test-only and leaves application dependencies unchanged. DOM execution is not browser/layout acceptance.
