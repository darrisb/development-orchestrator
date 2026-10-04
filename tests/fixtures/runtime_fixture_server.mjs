// The runtime-contract fixture application (concern 81).
//
// Forty lines of dependency-free Node serving one page and one API route. It is
// deliberately not Angular, React or Vue: the orchestrator's runtime
// verification knows nothing about any framework, and a fixture that needed one
// would hide that.
//
// FIXTURE_MODE selects the behaviour under test, and the *contract* declares it,
// which is also how the `env` field earns its place:
//
//   json           everything works
//   spa_fallback   /api/items answers 200 with the index HTML -- the regression
//   no_request     the page never calls the API
//   server_error   /api/items answers 500
//   other_heading  the page renders different text
//   console_error  the page calls console.error
//   console_log    the page calls console.log only (must NOT fail)
//   page_error     the page throws an uncaught error
//   exit_on_start  the process dies before it can listen
//   never_listens  the process stays up but never serves

import { createServer } from "node:http";

const mode = process.env.FIXTURE_MODE || "json";
const port = Number(process.env.FIXTURE_PORT || "7391");

if (mode === "exit_on_start") {
  process.stderr.write("FATAL: port already in use\n");
  process.exit(3);
}
if (mode === "never_listens") {
  // Never listens: the readiness poll must bound itself and give up.
  setInterval(() => {}, 1000);
} else {
  const script = {
    "console_error": 'console.error("TypeError: boom from the fixture");',
    "console_log": 'console.log("ordinary progress message");',
    "page_error": 'setTimeout(() => { throw new Error("uncaught from the fixture"); }, 0);',
  }[mode] || "";
  const heading = mode === "other_heading" ? "Widgets" : "Items";
  const fetches = mode === "no_request" ? "false" : "true";
  const index = `<!doctype html><html><head><title>Fixture</title></head><body>
<h1>${heading}</h1><div id="out">loading</div>
<script>
${script}
(async () => {
  if (!${fetches}) return;
  const response = await fetch("/api/items");
  const body = await response.text();
  try {
    document.getElementById("out").textContent =
      JSON.parse(body).map((entry) => entry.name).join(",");
  } catch (error) {
    document.getElementById("out").textContent = "not json";
  }
})();
</script></body></html>`;

  createServer((request, response) => {
    const url = new URL(request.url, "http://localhost");
    if (url.pathname === "/api/items") {
      if (mode === "spa_fallback") {
        response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        response.end(index);
        return;
      }
      if (mode === "server_error") {
        response.writeHead(500, { "Content-Type": "application/json" });
        response.end('{"error":"nope"}');
        return;
      }
      response.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      response.end('[{"name":"alpha"}]');
      return;
    }
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    response.end(index);
  }).listen(port, "127.0.0.1", () => process.stdout.write("listening on " + port + "\n"));
}
