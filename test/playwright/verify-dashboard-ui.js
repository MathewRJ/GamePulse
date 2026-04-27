#!/usr/bin/env node
/*
 * Generic Kibana dashboard UI verifier.
 *
 * Reads expected dashboard title and panel titles from the saved object
 * via /api/saved_objects/_export (using ES_API_KEY for that REST call only)
 * and then opens the dashboard in headless Chromium with a real
 * browser-auth storage-state. Asserts:
 *   - the dashboard title is visible in the rendered page,
 *   - every non-blank panel title is visible,
 *   - no Lens / Kibana embeddable failure strings appear in the body,
 *   - a full-page screenshot is saved on success and on failure.
 *
 * Hard requirement: storage-state file from a real browser-auth session.
 * No fallback to ApiKey headers for the browser routes.
 *
 * Adapted from the framework at /home/cachyos/coding/chatgpt-codex-test.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const https = require("https");
const http = require("http");

const FAILURE_NEEDLES = [
  "Cannot read properties",
  "No embeddable factory found",
  "Field not found",
  "Error loading dashboard",
  "Could not load embeddable",
  "Visualization could not be saved",
  "Unable to load",
  "An error occurred",
];

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const k = argv[i];
    const next = () => {
      i += 1;
      if (i >= argv.length) throw new Error(`Missing value for ${k}`);
      return argv[i];
    };
    switch (k) {
      case "--base-url": args.baseUrl = next(); break;
      case "--dashboard-id": args.dashboardId = next(); break;
      case "--storage-state": args.storageState = next(); break;
      case "--artifact-dir": args.artifactDir = next(); break;
      case "--es-api-key": args.esApiKey = next(); break;
      case "--timeout-ms": args.timeoutMs = Number(next()); break;
      default: throw new Error(`Unknown argument: ${k}`);
    }
  }
  return args;
}

function safeFilePart(value) {
  return value.replace(/[^A-Za-z0-9_.-]/g, "_");
}

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (error) {
    const msg = error && error.message ? error.message : String(error);
    throw new Error(
      "Playwright is not installed. Install once with:\n" +
      "  npm install --no-save playwright\n" +
      "  npx playwright install chromium\n" +
      `Underlying error: ${msg}`
    );
  }
}

function postExport(baseUrl, esApiKey, dashboardId) {
  return new Promise((resolve, reject) => {
    const url = new URL(`${baseUrl.replace(/\/$/, "")}/api/saved_objects/_export`);
    const body = JSON.stringify({
      objects: [{ type: "dashboard", id: dashboardId }],
      includeReferencesDeep: false,
      excludeExportDetails: true,
    });
    const lib = url.protocol === "http:" ? http : https;
    const req = lib.request(
      {
        method: "POST",
        hostname: url.hostname,
        port: url.port || (url.protocol === "http:" ? 80 : 443),
        path: url.pathname + url.search,
        headers: {
          "Authorization": `ApiKey ${esApiKey}`,
          "kbn-xsrf": "true",
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => {
          if (res.statusCode && res.statusCode >= 400) {
            reject(new Error(`_export failed: HTTP ${res.statusCode}: ${data.slice(0, 500)}`));
            return;
          }
          resolve(data);
        });
      }
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

function parseExportNdjson(ndjson, dashboardId) {
  const lines = ndjson.split("\n").filter((l) => l.trim().length > 0);
  for (const line of lines) {
    let obj;
    try { obj = JSON.parse(line); } catch (_e) { continue; }
    if (obj.type !== "dashboard" || obj.id !== dashboardId) continue;
    const title = obj.attributes && obj.attributes.title ? obj.attributes.title : null;
    let panels = [];
    if (obj.attributes && typeof obj.attributes.panelsJSON === "string") {
      try {
        panels = JSON.parse(obj.attributes.panelsJSON);
      } catch (_e) { panels = []; }
    }
    return { title, panels };
  }
  throw new Error(`Dashboard ${dashboardId} not found in _export response`);
}

function panelTitles(panels) {
  const out = [];
  for (const p of panels) {
    const ec = p.embeddableConfig || {};
    const t1 = ec.title;
    const t2 = ec.attributes && ec.attributes.title;
    const t3 = ec.config && ec.config.title;
    const title = t1 || t2 || t3 || null;
    if (title && typeof title === "string" && title.trim().length > 0) {
      out.push(title.trim());
    }
  }
  return Array.from(new Set(out));
}

async function waitForVisibleText(page, text, timeout) {
  try {
    await page.getByText(text, { exact: false }).first().waitFor({ state: "visible", timeout });
  } catch (error) {
    const url = page.url();
    const body = await page.locator("body").innerText({ timeout: 5000 }).catch(() => "");
    const head = body.slice(0, 1500).replace(/\s+/g, " ");
    throw new Error(
      `Timed out waiting for text: "${text}"\n` +
      `URL: ${url}\n` +
      `Body head: ${head}`
    );
  }
}

async function checkFailureNeedles(page) {
  const body = await page.locator("body").innerText({ timeout: 30000 }).catch(() => "");
  const hits = FAILURE_NEEDLES.filter((n) => body.includes(n));
  if (hits.length > 0) {
    throw new Error(`Page contains failure text: ${hits.join("; ")}`);
  }
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  for (const k of ["baseUrl", "dashboardId", "storageState", "artifactDir", "esApiKey"]) {
    if (!args[k]) throw new Error(`Missing required arg: --${k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`);
  }
  const timeout = args.timeoutMs && args.timeoutMs > 0 ? args.timeoutMs : 60000;

  if (!fs.existsSync(args.storageState)) {
    throw new Error(`storage-state file not found: ${args.storageState}`);
  }
  fs.mkdirSync(args.artifactDir, { recursive: true });

  // 1. Pull title + panel titles from _export.
  const ndjson = await postExport(args.baseUrl, args.esApiKey, args.dashboardId);
  const { title, panels } = parseExportNdjson(ndjson, args.dashboardId);
  const titles = panelTitles(panels);
  console.log(`Dashboard title:    ${title || "(no title)"}`);
  console.log(`Panels in export:   ${panels.length} (${titles.length} with non-blank titles)`);

  // 2. Open with Playwright + storage-state.
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    storageState: args.storageState,
    viewport: { width: 1440, height: 1000 },
  });
  const page = await context.newPage();
  const url = `${args.baseUrl.replace(/\/$/, "")}/app/dashboards#/view/${args.dashboardId}`;
  const screenshot = path.join(args.artifactDir, `${safeFilePart(args.dashboardId)}.png`);
  const failShot = path.join(args.artifactDir, `${safeFilePart(args.dashboardId)}.failure.png`);

  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout });
    await page.waitForLoadState("networkidle", { timeout }).catch(() => {});

    // If the navigation redirected to /login, fail loudly with MFA guidance.
    const finalUrl = page.url();
    if (/\/login/i.test(finalUrl) || /\/security\/account/i.test(finalUrl)) {
      throw new Error(`Redirected to ${finalUrl} — storage-state is expired. Re-run scripts/capture-kibana-auth.sh.`);
    }

    if (title) {
      await waitForVisibleText(page, title, timeout);
    }

    for (const t of titles) {
      await waitForVisibleText(page, t, timeout);
    }

    await checkFailureNeedles(page);
    await page.screenshot({ path: screenshot, fullPage: true });

    console.log(`OK ui: dashboard title and ${titles.length} panel title(s) rendered`);
    console.log(`OK ui: no failure needles in page body`);
    console.log(`Screenshot: ${screenshot}`);
    console.log(`PASS ${args.dashboardId} (UI)`);
  } catch (error) {
    await page.screenshot({ path: failShot, fullPage: true }).catch(() => {});
    console.error(`Failure screenshot: ${failShot}`);
    throw error;
  } finally {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }
}

run().catch((err) => {
  console.error(err.stack || err.message);
  process.exit(1);
});
