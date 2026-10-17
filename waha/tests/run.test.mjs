import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";
import { buildEnvironment, run } from "../run.mjs";

const apiKey = "test-only-secret-123456";

function fixture({ options = { api_key: apiKey }, env = {}, discoveryError, readError } = {}) {
  const processImpl = new EventEmitter();
  processImpl.env = { SUPERVISOR_TOKEN: "test-supervisor-token", ...env };
  const exits = [];
  processImpl.exit = (code) => exits.push(code);
  const calls = [];
  const directories = [];
  const logs = [];
  const reads = [];
  const discoveries = [];
  const runtime = run({
    processImpl,
    spawnImpl(command, args, config) {
      const child = new EventEmitter();
      child.signals = [];
      child.kill = (signal) => {
        child.signals.push(signal);
        queueMicrotask(() => {
          child.emit("exit", 0, signal);
          child.emit("close", 0, signal);
        });
      };
      calls.push({ command, args, config, child });
      return child;
    },
    mkdirImpl: (...args) => directories.push(args),
    readFileImpl: (...args) => {
      reads.push(args);
      if (readError) throw readError;
      return JSON.stringify(options);
    },
    registerDiscoveryImpl: async (config) => {
      discoveries.push(config);
      if (discoveryError) throw discoveryError;
      return true;
    },
    logger: Object.fromEntries(["log", "error", "warn"].map((level) => [level, (...args) => logs.push(args.join(" "))])),
  });
  return { runtime, processImpl, exits, calls, directories, logs, reads, discoveries };
}

test("environment preserves persistent GOWS storage and disables media by default", () => {
  const env = buildEnvironment({ api_key: apiKey }, { WAHA_EVENTS_DOWNLOAD_MEDIA: "true", WAHA_API_DOWNLOAD_MEDIA: "true", WAHA_BASE_URL: "https://untrusted.example/path", HOSTNAME: "addon_abc_ha_waha" });
  assert.equal(env.HOSTNAME, "addon_abc_ha_waha");
  assert.equal(env.WHATSAPP_DEFAULT_ENGINE, "GOWS");
  assert.equal(env.WAHA_LOCAL_STORE_BASE_DIR, "/data/sessions");
  assert.equal(env.WAHA_NAMESPACE, "all");
  assert.equal(env.WHATSAPP_FILES_FOLDER, "/data/media");
  assert.equal(env.WAHA_BASE_URL, "http://addon-abc-ha-waha:3000");
  for (const key of ["WHATSAPP_DOWNLOAD_MEDIA", "WAHA_EVENTS_DOWNLOAD_MEDIA", "WAHA_API_DOWNLOAD_MEDIA"]) assert.equal(env[key], "false");
  assert.equal(env.WHATSAPP_RESTART_ALL_SESSIONS, "true");
  assert.equal(env.HA_WAHA_SESSION_NAME, "default");
});

test("environment applies explicit media, session, device, and logging options", () => {
  const env = buildEnvironment({ api_key: apiKey, download_media: true, session_name: "home_1", device_name: "HA living room", log_level: "debug", auto_create_session: true });
  for (const key of ["WHATSAPP_DOWNLOAD_MEDIA", "WAHA_EVENTS_DOWNLOAD_MEDIA", "WAHA_API_DOWNLOAD_MEDIA"]) assert.equal(env[key], "true");
  assert.equal(env.HA_WAHA_SESSION_NAME, "home_1");
  assert.equal(env.HA_WAHA_AUTO_CREATE, "true");
  assert.equal(env.WAHA_CLIENT_DEVICE_NAME, "HA living room");
  assert.equal(env.WAHA_LOG_LEVEL, "debug");
  assert.equal(env.WAHA_HTTP_LOG_LEVEL, "debug");
});

test("environment supplies auth/dashboard credentials without startup fallback generation", () => {
  const env = buildEnvironment({ api_key: apiKey });
  for (const key of ["WAHA_API_KEY", "WAHA_DASHBOARD_PASSWORD", "WHATSAPP_SWAGGER_PASSWORD"]) assert.equal(env[key], apiKey);
  assert.equal(env.WAHA_DASHBOARD_ENABLED, "true");
  assert.equal(env.WAHA_DASHBOARD_USERNAME, "admin");
  assert.equal(env.WHATSAPP_SWAGGER_USERNAME, "admin");
  assert.equal(env.WHATSAPP_SWAGGER_ENABLED, "false");
  assert.equal(env.WAHA_PRINT_QR, "false");
  assert.equal(env.WHATSAPP_API_HOSTNAME, "0.0.0.0");
  assert.equal(env.WHATSAPP_API_PORT, "3000");
  assert.equal(env.WAHA_BASE_URL, "http://127.0.0.1:3000");
});

test("environment rejects an unsafe discovery hostname before startup writes", () => {
  assert.throws(
    () => buildEnvironment({ api_key: apiKey }, { HOSTNAME: "waha:3000/path" }),
    /HOSTNAME must be a valid internal DNS hostname/,
  );
  const f = fixture({ env: { HOSTNAME: "waha:3000/path" } });
  assert.deepEqual(f.exits, [1]);
  assert.equal(f.calls.length, 0);
  assert.equal(f.directories.length, 0);
  assert.ok(!f.logs.join("\n").includes("waha:3000/path"));
});

test("wrapper starts the upstream entrypoint and control child with configured environment", async () => {
  const f = fixture({ env: { OPTIONS_PATH: "/test/options.json" } });
  assert.deepEqual(f.reads, [["/test/options.json", "utf8"]]);
  assert.deepEqual(f.directories, [["/data/sessions", { recursive: true }], ["/data/media", { recursive: true }]]);
  assert.deepEqual(f.calls.map(({ command, args }) => [command, args]), [["/entrypoint.sh", []], ["node", ["/ha/control.mjs"]]]);
  assert.ok(f.calls.every(({ config }) => config.stdio === "inherit" && config.env.WAHA_API_KEY === apiKey));
  assert.deepEqual(f.discoveries, [{ apiKey, sessionName: "default" }]);
  f.processImpl.emit("SIGTERM");
  await new Promise(setImmediate);
  assert.deepEqual(f.exits, [0]);
  assert.ok(f.calls.every(({ child }) => child.signals[0] === "SIGTERM"));
  assert.ok(!f.logs.join("\n").includes(apiKey));
});

test("invalid options fail before filesystem writes or child startup and do not echo values", () => {
  for (const options of [{ api_key: "short" }, { api_key: apiKey, session_name: apiKey + "/bad" }, { api_key: apiKey, device_name: "" }, { api_key: apiKey, log_level: apiKey }]) {
    const f = fixture({ options });
    assert.deepEqual(f.exits, [1]);
    assert.equal(f.calls.length, 0);
    assert.equal(f.directories.length, 0);
    assert.ok(!f.logs.join("\n").includes(apiKey));
  }
  const f = fixture({ readError: new SyntaxError(`Invalid JSON near ${apiKey}`) });
  assert.deepEqual(f.exits, [1]);
  assert.deepEqual(f.reads, [["/data/options.json", "utf8"]]);
  assert.ok(!f.logs.join("\n").includes(apiKey));
});

test("unexpected child exit terminates siblings and exits unsuccessfully", async () => {
  const f = fixture();
  f.calls[0].child.emit("exit", 7, null);
  await new Promise(setImmediate);
  assert.deepEqual(f.exits, [7]);
  assert.deepEqual(f.calls[1].child.signals, ["SIGTERM"]);
});

test("wrapper redacts known credentials in discovery and spawn errors", async () => {
  const f = fixture({ discoveryError: new Error(`${apiKey} test-supervisor-token`) });
  await new Promise(setImmediate);
  f.calls[0].child.emit("error", new Error(`spawn failed ${apiKey}`));
  await new Promise(setImmediate);
  assert.deepEqual(f.exits, [1]);
  const output = f.logs.join("\n");
  assert.ok(output.includes("[redacted]"));
  assert.ok(!output.includes(apiKey));
  assert.ok(!output.includes("test-supervisor-token"));
});

test("failed asynchronous spawn closes without exit and still exits nonzero", async () => {
  const f = fixture();
  const failed = f.calls[0].child;
  failed.kill = () => false;
  failed.emit("error", new Error("spawn /entrypoint.sh ENOENT"));
  failed.emit("close", -2, null);
  await new Promise(setImmediate);
  assert.deepEqual(f.exits, [1]);
  assert.deepEqual(f.calls[1].child.signals, ["SIGTERM"]);
});
