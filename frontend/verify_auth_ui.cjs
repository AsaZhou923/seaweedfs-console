const fs = require("fs");
const assert = require("assert");

const source = fs.readFileSync("frontend/src/main.tsx", "utf8");
for (const snippet of [
  "const authGeneration = useRef(0)",
  "const userRef = useRef(\"\")",
  "const sessionInitializedRef = useRef(false)",
  "authGeneration.current !== requestGeneration",
  "INITIAL_UNAUTHENTICATED_MESSAGE",
  "setAuthenticatedSession(username: string, csrfToken: string, advanceGeneration: boolean)",
  "setAuthenticatedSession(me.user.username, me.csrf_token, Boolean(options.bootstrap && !sessionInitializedRef.current))",
]) {
  assert(source.includes(snippet), `main.tsx missing expected auth guard snippet: ${snippet}`);
}

const SESSION_EXPIRED_MESSAGE = "会话到期，请重新登录";
const STALE_AUTH_RESPONSE_MESSAGE = "会话状态已更新，忽略旧请求";
const INITIAL_UNAUTHENTICATED_MESSAGE = "初始会话未登录";

function isUnauthenticatedResponse(status, data = {}) {
  const code = String(data?.error?.code || data?.code || "");
  const message = String(data?.error?.message || data?.message || data?.raw || "");
  return status === 401 || code.includes("UNAUTHENTICATED") || message.includes("UNAUTHENTICATED");
}

function createAuthModel() {
  return {
    epoch: 0,
    user: "",
    sessionInitialized: false,
    startRequest() {
      return this.epoch;
    },
    clearSession(message) {
      this.epoch += 1;
      this.user = "";
      return { kind: "expired", message };
    },
    login(username) {
      this.epoch += 1;
      this.user = username;
      this.sessionInitialized = true;
    },
    applyMe(username, advanceEpoch) {
      if (advanceEpoch) this.epoch += 1;
      this.user = username;
      this.sessionInitialized = true;
    },
    decide({ requestEpoch, path, ok, status, data }) {
      if (this.epoch !== requestEpoch) return { kind: "stale", message: STALE_AUTH_RESPONSE_MESSAGE };
      if (!ok && isUnauthenticatedResponse(status, data)) {
        if (path === "/auth/login") return { kind: "login-error" };
        if (path === "/auth/me" && !this.user && !this.sessionInitialized) {
          return { kind: "initial-unauthenticated", message: INITIAL_UNAUTHENTICATED_MESSAGE };
        }
        return this.clearSession(SESSION_EXPIRED_MESSAGE);
      }
      return ok ? { kind: "ok" } : { kind: "error" };
    },
  };
}

{
  const auth = createAuthModel();
  const oldWrite = auth.startRequest();
  auth.login("admin");
  const decision = auth.decide({ requestEpoch: oldWrite, path: "/management/1/buckets", ok: false, status: 401, data: { error: { code: "UNAUTHENTICATED" } } });
  assert.equal(decision.kind, "stale");
  assert.equal(auth.user, "admin");
  assert.equal(auth.epoch, 1);
}

{
  const auth = createAuthModel();
  const oldRead = auth.startRequest();
  auth.login("admin");
  const decision = auth.decide({ requestEpoch: oldRead, path: "/connections", ok: true, status: 200, data: { items: [] } });
  assert.equal(decision.kind, "stale");
  assert.equal(auth.user, "admin");
  assert.equal(auth.epoch, 1);
}

{
  const auth = createAuthModel();
  auth.login("admin");
  const refreshEpoch = auth.startRequest();
  assert.equal(auth.decide({ requestEpoch: refreshEpoch, path: "/auth/me", ok: true, status: 200, data: {} }).kind, "ok");
  auth.applyMe("admin", false);
  assert.equal(auth.epoch, 1, "ordinary auth/me refresh must not advance epoch");
}

{
  const auth = createAuthModel();
  const initial = auth.startRequest();
  const decision = auth.decide({ requestEpoch: initial, path: "/auth/me", ok: false, status: 401, data: { error: { code: "UNAUTHENTICATED" } } });
  assert.equal(decision.kind, "initial-unauthenticated");
  assert.equal(auth.user, "");
  assert.equal(auth.epoch, 0);
}

{
  const auth = createAuthModel();
  auth.login("admin");
  const me = auth.startRequest();
  const decision = auth.decide({ requestEpoch: me, path: "/auth/me", ok: false, status: 401, data: { error: { code: "UNAUTHENTICATED" } } });
  assert.equal(decision.kind, "expired");
  assert.equal(auth.user, "");
  assert.equal(auth.epoch, 2);
}

console.log("auth UI epoch decision assertions passed");
