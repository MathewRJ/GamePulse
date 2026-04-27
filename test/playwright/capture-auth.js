#!/usr/bin/env node
/*
 * One-time browser-auth capture for Kibana. Opens a headed Chromium
 * window, lets the user log in (including MFA / OTP), and writes a
 * Playwright storage-state JSON file to --state-file.
 *
 * The state file is gitignored. Refresh it whenever Elastic Cloud
 * invalidates the session.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const readline = require("readline");

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
      case "--state-file": args.stateFile = next(); break;
      default: throw new Error(`Unknown argument: ${k}`);
    }
  }
  return args;
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

function waitForEnter(prompt) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question(prompt, () => { rl.close(); resolve(); });
  });
}

async function run() {
  const args = parseArgs(process.argv.slice(2));
  for (const k of ["baseUrl", "stateFile"]) {
    if (!args[k]) throw new Error(`Missing required arg: --${k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`);
  }
  fs.mkdirSync(path.dirname(args.stateFile), { recursive: true });

  const { chromium } = loadPlaywright();
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();

  console.log(`Opening ${args.baseUrl}`);
  console.log("Log in in the Chromium window (including MFA / OTP).");
  console.log("Once the Kibana home / dashboard list is loaded, come back here.");
  await page.goto(args.baseUrl, { waitUntil: "domcontentloaded" });

  await waitForEnter("Press Enter once you're logged in and the Kibana UI is visible... ");

  await context.storageState({ path: args.stateFile });
  console.log(`Wrote ${args.stateFile}`);

  await context.close().catch(() => {});
  await browser.close().catch(() => {});
}

run().catch((err) => {
  console.error(err.stack || err.message);
  process.exit(1);
});
