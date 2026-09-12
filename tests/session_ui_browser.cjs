/* Optional local browser check: requires Playwright and Chromium.
 * NODE_PATH=<playwright-install>/node_modules node tests/session_ui_browser.cjs
 * All HTTP/WebSocket traffic is intercepted; no running Hub is required.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "..");
const app = fs.readFileSync(path.join(root, "app/static/app.js"), "utf8")
  .split("(async function boot() {")[0];

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || "/usr/bin/chromium" });
  try {
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport });
      const errors = [], unexpected = [], actions = [];
      page.on("pageerror", e => errors.push(e.message));
      let session = { id: "aaaaaa", name: "Sessione di prova", environment: "PROJECT",
        unix_user: "test-agent", profile_id: "codex", workdir: "/synthetic/project",
        status: "running", alive: true, lifecycle: "NEEDS_HOST_ACTION", lifecycle_reported: true,
        report_summary: "Intervento di prova", messages_total: 0, undelivered: 0 };
      let reports = [{ status: "WAITING_SESSION", summary: "Report storico <prova>",
        waiting_for_session: "bbbbbb", reported_at: "2026-09-10T10:00:00Z", unix_user: "test-agent" }];
      await page.routeWebSocket("**/*", ws => ws.close());
      await page.route("**/*", async route => {
        const url = new URL(route.request().url());
        let data;
        if (url.pathname === "/") return route.fulfill({ contentType: "text/html",
          body: '<!doctype html><html><body><main id="view"></main><div id="toast"></div></body></html>' });
        if (url.pathname === "/api/notifications/read") data = { unread_count: 0 };
        else if (url.pathname === "/api/documents") data = { documents: [] };
        else if (url.pathname === "/api/sessions") data = { sessions: [], unread_count: 0 };
        else if (url.pathname === "/api/sessions/aaaaaa") data = { session, reports };
        else if (url.pathname.endsWith("/state")) data = session;
        else if (url.pathname.endsWith("/runtime")) data = { live: { state: "unsupported" }, capabilities: {} };
        else if (url.pathname.endsWith("/transcript")) return route.fulfill({ contentType: "text/plain", body: "Output sintetico" });
        else if (url.pathname.endsWith("/action")) {
          actions.push({ path: url.pathname, ...route.request().postDataJSON() });
          data = { ok: true };
        } else { unexpected.push(url.pathname); return route.abort(); }
        await route.fulfill({ json: data });
      });
      await page.goto("http://agent-hub.test/");
      for (const file of ["style.css", "vendor/xterm.css"])
        await page.addStyleTag({ path: path.join(root, "app/static", file) });
      for (const file of ["vendor/xterm.js", "vendor/addon-fit.js"])
        await page.addScriptTag({ path: path.join(root, "app/static", file) });
      await page.addScriptTag({ content: app });
      await page.evaluate(() => { BOOT = { profiles: [], catalog: {} }; });
      const render = () => page.evaluate(async () => {
        if (cleanup) cleanup();
        location.hash = "#/session/aaaaaa";
        await viewSession("aaaaaa");
      });
      await render();
      const advanced = page.locator("#session-options");
      const history = page.getByText("Report storico <prova>", { exact: true });
      assert.equal(await advanced.getAttribute("open"), null);
      assert.equal(await history.isVisible(), false);
      assert.equal(await page.locator("#esc-go").isVisible(), true);
      assert.equal(await page.locator("#status-strip .life").isVisible(), true);
      assert.ok((await page.locator("#status-strip").innerText()).includes("Intervento di prova"));
      await advanced.locator(":scope > summary").click();
      assert.equal(await history.isVisible(), true);
      assert.equal(await advanced.locator('a[href="#/session/bbbbbb"]').isVisible(), true);
      assert.equal(await history.locator("prova").count(), 0);
      await advanced.locator(":scope > summary").click();
      assert.equal(await history.isVisible(), false);
      assert.equal(await page.locator("#a-kill-pair").count(), 0);

      session = { ...session, environment: "SERVER", relation_type: "escalation",
        parent_session: "bbbbbb", lifecycle: "COMPLETED" };
      reports = [];
      await render();
      assert.ok((await page.locator("#status-strip").innerText()).includes("Turno completato"));
      const empty = page.getByText("Nessuno stato finale registrato per questa sessione.");
      assert.equal(await empty.isVisible(), false);
      await advanced.locator(":scope > summary").click();
      assert.equal(await empty.isVisible(), true);

      // Cancellation sends nothing; accepted single/pair close retain distinct actions.
      let confirmation = "";
      page.once("dialog", async d => { confirmation = d.message(); await d.dismiss(); });
      await page.getByRole("button", { name: "Chiudi solo escalation", exact: true }).click();
      assert.equal(actions.length, 0);
      assert.ok(confirmation.includes("La sessione chiamante resterà aperta"));
      page.once("dialog", d => d.accept());
      await page.getByRole("button", { name: "Chiudi solo escalation", exact: true }).click();
      await page.waitForFunction(() => location.hash === "#/");
      assert.deepEqual(actions, [{ path: "/api/sessions/aaaaaa/action", action: "kill" }]);
      page.once("dialog", d => d.accept());
      await page.getByRole("button", { name: "Chiudi entrambe", exact: true }).click();
      await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("kill_pair"));
      assert.deepEqual(actions[1], { path: "/api/sessions/aaaaaa/action", action: "kill_pair" });
      session = { ...session, relation_type: "", parent_session: "" };
      await render();
      assert.equal(await page.locator("#a-kill").innerText(), "Kill");
      assert.equal(await page.locator("#a-kill-pair").count(), 0);
      assert.deepEqual(errors, []);
      assert.deepEqual(unexpected, []);
      await page.evaluate(() => cleanup());
      await page.close();
      console.log(`Session UI ${viewport.width}x${viewport.height}: OK`);
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
