// The orchestrator's runtime probe (concern 81).
//
// This is the orchestrator's own program, not a project's. It is copied into
// the run's worktree, executed inside the worker by `node`, and prints exactly
// one machine-readable line; the orchestrator parses that line and makes every
// decision itself. The probe asserts nothing.
//
// Two modes, because the orchestrator needs to interleave liveness checks with
// readiness polling and only it can see the managed background process:
//
//   node runtime_probe.mjs ready   <config.json>   bounded HTTP readiness poll
//   node runtime_probe.mjs observe <config.json>   load one page, report facts
//
// No dependencies. Node 22's built-in `fetch` and `WebSocket` are enough to
// drive Chromium over the DevTools Protocol, which is why the worker image
// needs nothing installed and the orchestrator needs no browser library. The
// abstraction is deliberately narrow: navigate, watch responses, read the
// rendered text, collect console and page errors. There is no clicking, no
// typing, no selector language, no screenshot and no second page.

import { spawn } from "node:child_process";
import { readFileSync, rmSync, mkdtempSync, readFile } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const SENTINEL = "__ORCHESTRATOR_RUNTIME__";

function emit(payload) {
  // One line, last line, on stdout. Anything else the probe or Chromium wrote
  // is diagnostic noise the orchestrator keeps as evidence but does not parse.
  process.stdout.write("\n" + SENTINEL + " " + JSON.stringify(payload) + "\n");
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --------------------------------------------------------------- readiness

async function pollReady(config) {
  const deadline = Date.now() + Math.max(1, config.readiness_budget_seconds) * 1000;
  let attempts = 0;
  let lastError = "";
  let lastStatus = null;
  while (Date.now() < deadline) {
    attempts += 1;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(config.readiness_url, {
        signal: controller.signal,
        redirect: "manual",
      });
      lastStatus = response.status;
      // V1 readiness: the application answered with a non-server-error status.
      // "Up" is not "correct" -- correctness is what the assertions are for --
      // and a 404 from a dev server that is serving means it is serving.
      if (response.status < 500) {
        return { ready: true, status: response.status, attempts, error: "" };
      }
      lastError = "HTTP " + response.status;
    } catch (error) {
      lastError = String(error && error.message ? error.message : error);
    } finally {
      clearTimeout(timer);
    }
    await sleep(Math.min(500, Math.max(0, deadline - Date.now())));
  }
  return { ready: false, status: lastStatus, attempts, error: lastError };
}

// ----------------------------------------------------------------- chromium

function devToolsEndpoint(userDataDir, deadline, dead) {
  // Chromium writes the port it actually bound into this file, which is why
  // the probe can ask for port 0 and never collide with the application it is
  // there to observe.
  //
  // `dead()` is why this is not a plain poll: a browser that could not be
  // spawned at all, or that died on startup, is knowable immediately, and
  // waiting out the whole start timeout to report it would make an
  // infrastructure problem look like a slow one.
  const path = join(userDataDir, "DevToolsActivePort");
  return new Promise((resolve, reject) => {
    const attempt = () => {
      readFile(path, "utf8", (error, text) => {
        const lines = (text || "").split("\n");
        if (!error && lines.length >= 2 && lines[0].trim()) {
          resolve("ws://127.0.0.1:" + lines[0].trim() + lines[1].trim());
          return;
        }
        const reason = dead();
        if (reason) {
          reject(new Error("Chromium did not start: " + reason));
          return;
        }
        if (Date.now() > deadline) {
          reject(new Error("Chromium did not report a DevTools port"));
          return;
        }
        setTimeout(attempt, 100);
      });
    };
    attempt();
  });
}

function launchChromium(config, userDataDir) {
  const args = [
    "--headless",
    // The probe runs inside the worker's own Docker isolation, which is the
    // boundary that matters; Chromium's internal sandbox cannot initialise
    // under `no-new-privileges`. The worker image's launcher already passes
    // this for the same reason, and passing it twice is harmless.
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-translate",
    "--disable-background-networking",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0",
    "--user-data-dir=" + userDataDir,
    "about:blank",
  ];
  const errors = [];
  for (const binary of config.chromium_candidates) {
    try {
      const child = spawn(binary, args, { stdio: ["ignore", "pipe", "pipe"] });
      let failed = null;
      child.on("error", (error) => {
        failed = error;
      });
      const noise = [];
      const keep = (chunk) => {
        if (noise.join("").length < 4000) noise.push(String(chunk));
      };
      child.stdout.on("data", keep);
      child.stderr.on("data", keep);
      return { child, binary, noise, failure: () => failed };
    } catch (error) {
      errors.push(binary + ": " + error.message);
    }
  }
  const error = new Error(
    "no Chromium executable could be started (" + errors.join("; ") + ")",
  );
  error.infrastructure = true;
  throw error;
}

// ----------------------------------------------------- devtools protocol

class Session {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.sessionId = null;
    this.handlers = [];
    socket.addEventListener("message", (event) => this._receive(event.data));
  }

  _receive(raw) {
    let message;
    try {
      message = JSON.parse(String(raw));
    } catch {
      return;
    }
    if (message.id && this.pending.has(message.id)) {
      const { resolve, reject } = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message || "devtools error"));
      else resolve(message.result || {});
      return;
    }
    if (message.method) {
      for (const handler of this.handlers) handler(message.method, message.params || {});
    }
  }

  on(handler) {
    this.handlers.push(handler);
  }

  send(method, params = {}, { scoped = true, timeout = 30000 } = {}) {
    const id = this.nextId++;
    const payload = { id, method, params };
    if (scoped && this.sessionId) payload.sessionId = this.sessionId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(method + " timed out"));
      }, timeout);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
      this.socket.send(JSON.stringify(payload));
    });
  }
}

function connect(endpoint, timeout) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(endpoint);
    const timer = setTimeout(() => reject(new Error("DevTools connection timed out")), timeout);
    socket.addEventListener("open", () => {
      clearTimeout(timer);
      resolve(socket);
    });
    socket.addEventListener("error", () => {
      clearTimeout(timer);
      reject(new Error("DevTools connection failed"));
    });
  });
}

function describeArgument(argument) {
  if (argument === undefined || argument === null) return "";
  if ("value" in argument) return typeof argument.value === "string" ? argument.value : JSON.stringify(argument.value);
  return argument.description || argument.unserializableValue || argument.type || "";
}

async function observe(config) {
  const userDataDir = mkdtempSync(join(tmpdir(), "orchestrator-browser-"));
  let launched = null;
  let socket = null;
  const responses = [];
  const consoleErrors = [];
  const pageErrors = [];
  let omitted = 0;
  let loaded = false;
  let loadError = "";
  let lastResponseAt = 0;

  try {
    launched = launchChromium(config, userDataDir);
    const endpoint = await devToolsEndpoint(
      userDataDir,
      Date.now() + Math.max(5, config.browser_start_timeout_seconds) * 1000,
      () => {
        const failure = launched.failure();
        if (failure) return failure.message;
        if (launched.child.exitCode !== null) {
          return "it exited with code " + launched.child.exitCode;
        }
        return "";
      },
    ).catch((error) => {
      const failure = launched.failure();
      const detail = failure ? failure.message : launched.noise.join("").slice(-1500);
      const wrapped = new Error(error.message + (detail ? ": " + detail : ""));
      wrapped.infrastructure = true;
      throw wrapped;
    });

    socket = await connect(endpoint, 15000).catch((error) => {
      error.infrastructure = true;
      throw error;
    });
    const session = new Session(socket);
    session.on((method, params) => {
      if (method === "Network.responseReceived") {
        const response = params.response || {};
        const headers = response.headers || {};
        let contentType = "";
        for (const key of Object.keys(headers)) {
          if (key.toLowerCase() === "content-type") contentType = String(headers[key]);
        }
        lastResponseAt = Date.now();
        if (responses.length < config.max_responses) {
          responses.push({
            url: String(response.url || ""),
            status: Number(response.status || 0),
            content_type: contentType || String(response.mimeType || ""),
          });
        } else {
          omitted += 1;
        }
      } else if (method === "Runtime.consoleAPICalled") {
        if (params.type === "error" && consoleErrors.length < config.max_console) {
          consoleErrors.push(
            (params.args || []).map(describeArgument).filter(Boolean).join(" ").slice(0, 1000)
              || "console.error",
          );
        }
      } else if (method === "Runtime.exceptionThrown") {
        if (pageErrors.length < config.max_console) {
          const details = params.exceptionDetails || {};
          const exception = details.exception || {};
          pageErrors.push(
            String(exception.description || details.text || "uncaught error").slice(0, 1000),
          );
        }
      } else if (method === "Page.loadEventFired") {
        loaded = true;
      }
    });

    const target = await session.send("Target.createTarget", { url: "about:blank" }, { scoped: false });
    const attached = await session.send(
      "Target.attachToTarget",
      { targetId: target.targetId, flatten: true },
      { scoped: false },
    );
    session.sessionId = attached.sessionId;

    await session.send("Page.enable");
    await session.send("Runtime.enable");
    await session.send("Network.enable");

    const navigation = await session.send("Page.navigate", { url: config.page });
    if (navigation.errorText) loadError = String(navigation.errorText);

    const loadDeadline = Date.now() + Math.max(1, config.page_timeout_seconds) * 1000;
    while (!loaded && !loadError && Date.now() < loadDeadline) await sleep(100);
    if (!loaded && !loadError) {
      loadError = "the page did not finish loading within " + config.page_timeout_seconds + "s";
    }

    if (loaded) {
      // A single-page application issues its API calls *after* the load event,
      // so stopping there would observe none of them. Bounded network quiet is
      // the smallest thing that catches them: wait until nothing has arrived
      // for `settle_ms`, and never longer than `settle_max_ms`.
      const settleDeadline = Date.now() + Math.max(0, config.settle_max_ms);
      lastResponseAt = lastResponseAt || Date.now();
      while (Date.now() < settleDeadline && Date.now() - lastResponseAt < config.settle_ms) {
        await sleep(50);
      }
    }

    let pageText = "";
    if (loaded) {
      const evaluated = await session
        .send("Runtime.evaluate", {
          expression: "document.body ? document.body.innerText : document.documentElement.innerText",
          returnByValue: true,
          awaitPromise: false,
        })
        .catch(() => null);
      const value = evaluated && evaluated.result ? evaluated.result.value : "";
      pageText = typeof value === "string" ? value.slice(0, config.max_text_chars) : "";
    }

    return {
      ok: true,
      infrastructure_error: null,
      observation: {
        page_url: config.page,
        loaded: loaded && !loadError,
        load_error: loadError,
        page_text: pageText,
        responses,
        responses_omitted: omitted,
        console_errors: consoleErrors,
        page_errors: pageErrors,
      },
    };
  } finally {
    // Finally-style, unconditionally: a browser left behind would hold the
    // worktree and the port, and the orchestrator promises neither happens.
    if (socket) {
      try {
        socket.close();
      } catch {
        /* closing a broken socket is not a failure */
      }
    }
    if (launched && launched.child) {
      try {
        launched.child.kill("SIGTERM");
        const stopDeadline = Date.now() + 5000;
        while (launched.child.exitCode === null && Date.now() < stopDeadline) await sleep(50);
        if (launched.child.exitCode === null) launched.child.kill("SIGKILL");
      } catch {
        /* already gone */
      }
    }
    try {
      rmSync(userDataDir, { recursive: true, force: true });
    } catch {
      /* a tmpfs discarded with the worker anyway */
    }
  }
}

// --------------------------------------------------------------------- main

const [mode, configPath] = process.argv.slice(2);
let config;
try {
  config = JSON.parse(readFileSync(configPath, "utf8"));
} catch (error) {
  emit({ ok: false, infrastructure_error: "unreadable probe config: " + error.message });
  process.exit(0);
}

try {
  if (mode === "ready") {
    emit({ ok: true, infrastructure_error: null, readiness: await pollReady(config) });
  } else if (mode === "observe") {
    emit(await observe(config));
  } else {
    emit({ ok: false, infrastructure_error: "unknown probe mode " + mode });
  }
} catch (error) {
  emit({
    ok: false,
    infrastructure_error: String(error && error.message ? error.message : error),
    infrastructure: Boolean(error && error.infrastructure),
  });
}
// Exit 0 whatever happened: the verdict is in the payload, and a non-zero code
// would make the orchestrator classify a reported failure twice.
process.exit(0);
