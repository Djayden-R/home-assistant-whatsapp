import { spawn } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

import { registerDiscoveryWithRetry } from "./discovery.mjs";

const DNS_LABEL = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/;

function mediaBaseUrl(hostname) {
  if (hostname == null || hostname === "") return "http://127.0.0.1:3000";
  if (typeof hostname !== "string") {
    throw new Error("HOSTNAME must be a valid internal DNS hostname");
  }
  const host = hostname.replaceAll("_", "-");
  const labels = host.split(".");
  if (
    host.length > 253 ||
    labels.some((label) => label.length > 63 || !DNS_LABEL.test(label))
  ) {
    throw new Error("HOSTNAME must be a valid internal DNS hostname");
  }
  return `http://${host}:3000`;
}

export function buildEnvironment(options, inherited = {}) {
  const apiKey = String(options.api_key || "");
  const sessionName = String(options.session_name || "default");
  const deviceName = String(options.device_name ?? "Home Assistant");
  const logLevel = String(options.log_level || "info");

  if (apiKey.length < 16) {
    throw new Error("api_key must be at least 16 characters long");
  }
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(sessionName)) {
    throw new Error(
      "session_name may contain only letters, numbers, underscores, and hyphens",
    );
  }
  if (deviceName.length < 1 || deviceName.length > 64) {
    throw new Error("device_name must contain between 1 and 64 characters");
  }
  if (!["error", "warn", "info", "debug"].includes(logLevel)) {
    throw new Error("log_level must be error, warn, info, or debug");
  }

  return {
    ...inherited,
    WAHA_API_KEY: apiKey,
    WAHA_DASHBOARD_ENABLED: "true",
    WAHA_DASHBOARD_USERNAME: "admin",
    WAHA_DASHBOARD_PASSWORD: apiKey,
    WHATSAPP_SWAGGER_ENABLED: "false",
    // WAHA's entrypoint generates and prints missing Swagger credentials even
    // when Swagger is disabled. Supplying them prevents the API key from being
    // echoed into the app logs by that fallback path.
    WHATSAPP_SWAGGER_USERNAME: "admin",
    WHATSAPP_SWAGGER_PASSWORD: apiKey,
    WHATSAPP_DEFAULT_ENGINE: "GOWS",
    WHATSAPP_API_HOSTNAME: "0.0.0.0",
    WHATSAPP_API_PORT: "3000",
    // WAHA uses this public-facing base to generate media.url. Its listener
    // host is 0.0.0.0 and must never leak into media retrieval URLs. Match the
    // private DNS hostname published through Supervisor discovery instead.
    WAHA_BASE_URL: mediaBaseUrl(inherited.HOSTNAME),
    WAHA_LOCAL_STORE_BASE_DIR: "/data/sessions",
    WAHA_NAMESPACE: "all",
    WHATSAPP_FILES_FOLDER: "/data/media",
    WHATSAPP_DOWNLOAD_MEDIA: options.download_media ? "true" : "false",
    // WAHA 2026.8.2+ separates event and API download defaults. Keep the
    // deprecated fallback too, while explicitly applying the app option to both.
    WAHA_EVENTS_DOWNLOAD_MEDIA: options.download_media ? "true" : "false",
    WAHA_API_DOWNLOAD_MEDIA: options.download_media ? "true" : "false",
    WHATSAPP_FILES_LIFETIME: "86400",
    WHATSAPP_RESTART_ALL_SESSIONS: "true",
    WAHA_PRINT_QR: "false",
    WAHA_CLIENT_DEVICE_NAME: deviceName,
    WAHA_CLIENT_BROWSER_NAME: "Desktop",
    WAHA_LOG_FORMAT: "PRETTY",
    WAHA_LOG_LEVEL: logLevel,
    // Keep request logs below the normal INFO threshold. They become visible
    // automatically when the user temporarily enables DEBUG logging.
    WAHA_HTTP_LOG_LEVEL: "debug",
    HA_WAHA_SESSION_NAME: sessionName,
    HA_WAHA_AUTO_CREATE: options.auto_create_session ? "true" : "false",
    HA_WAHA_API_URL: "http://127.0.0.1:3000",
    HA_WAHA_CONTROL_PORT: "8099",
    HA_WAHA_CONTROL_HTML: "/ha/control.html",
  };
}

export function run({
  processImpl = process,
  spawnImpl = spawn,
  mkdirImpl = mkdirSync,
  readFileImpl = readFileSync,
  registerDiscoveryImpl = registerDiscoveryWithRetry,
  logger = console,
} = {}) {
  const optionsPath = processImpl.env.OPTIONS_PATH || "/data/options.json";
  let options;
  try {
    options = JSON.parse(readFileImpl(optionsPath, "utf8"));
  } catch {
    // JSON parse errors can quote the source, which includes the API key.
    logger.error("[WAHA app] Unable to read valid app options");
    processImpl.exit(1);
    return;
  }
  let env;
  try {
    env = buildEnvironment(options, processImpl.env);
  } catch (error) {
    logger.error(`[WAHA app] ${error.message}`);
    processImpl.exit(1);
    return;
  }
  mkdirImpl("/data/sessions", { recursive: true });
  mkdirImpl("/data/media", { recursive: true });
  Object.assign(processImpl.env, env);
  const apiKey = env.WAHA_API_KEY;
  const sessionName = env.HA_WAHA_SESSION_NAME;

  function safeError(error) {
    let message = String(error.message || error);
    for (const secret of [apiKey, env.SUPERVISOR_TOKEN]) {
      if (secret) message = message.replaceAll(secret, "[redacted]");
    }
    return message;
  }

  logger.log(`[WAHA app] Starting WAHA (GOWS) with session '${sessionName}'`);

  const children = new Set();
  let shuttingDown = false;

  function start(command, args, label) {
    const child = spawnImpl(command, args, {
      env: processImpl.env,
      stdio: "inherit",
    });
    children.add(child);
    // `close` follows both normal exit and a failed asynchronous spawn. A
    // failed spawn may never emit `exit`, so use close for lifecycle cleanup.
    child.once("close", () => children.delete(child));
    child.once("exit", (code, signal) => {
      if (!shuttingDown) {
        logger.error(
          `[WAHA app] ${label} exited unexpectedly (${signal || code || 0})`,
        );
        shutdown(code || 1);
      }
    });
    child.once("error", (error) => {
      logger.error(`[WAHA app] ${label} could not start: ${safeError(error)}`);
      shutdown(1);
    });
    return child;
  }

  function shutdown(exitCode = 0) {
    if (shuttingDown) return;
    shuttingDown = true;
    const timer = setTimeout(() => {
      for (const child of children) child.kill("SIGKILL");
      processImpl.exit(exitCode);
    }, 10_000);
    // Keep the deadline referenced: an unresolved promise alone cannot keep
    // Node alive long enough to preserve a failed startup's nonzero exit.
    const closed = Promise.all(
      [...children].map(
        (child) => new Promise((resolve) => child.once("close", resolve)),
      ),
    );
    for (const child of children) child.kill("SIGTERM");
    closed.then(() => {
      clearTimeout(timer);
      processImpl.exit(exitCode);
    });
  }

  processImpl.on("SIGTERM", () => shutdown(0));
  processImpl.on("SIGINT", () => shutdown(0));

  start("/entrypoint.sh", [], "WAHA");
  start("node", ["/ha/control.mjs"], "control panel");

  registerDiscoveryImpl({
    apiKey,
    sessionName,
  })
    .then((registered) => {
      if (registered) {
        logger.log("[WAHA app] Published Home Assistant integration discovery");
      }
    })
    .catch((error) => {
      logger.warn(`[WAHA app] Integration discovery failed: ${safeError(error)}`);
    });
  return { shutdown };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  run();
}
