#!/usr/bin/env node

const { execSync, spawn } = require("child_process");
const { existsSync, readFileSync, writeFileSync } = require("fs");
const path = require("path");
const readline = require("readline");

const PYPI_PACKAGE = "telechat";
const NPM_VERSION = require("../package.json").version;
const ENV_FILE = path.join(process.cwd(), ".env");

// ─── Helpers ──────────────────────────────────────────────────────────────────

function findPython() {
  for (const cmd of ["python3", "python"]) {
    try {
      const v = execSync(`${cmd} --version 2>&1`, { encoding: "utf8" });
      if (v.includes("Python 3")) return cmd;
    } catch {}
  }
  return null;
}

function isPyPkgInstalled(python) {
  try {
    execSync(`${python} -c "import telechat_pkg"`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function claudeCliInstalled() {
  try {
    execSync("claude --version 2>&1", { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function ask(rl, question, fallback) {
  return new Promise((resolve) => {
    rl.question(question, (answer) => {
      resolve(answer.trim() || fallback || "");
    });
  });
}

function pick(rl, question, options) {
  return new Promise((resolve) => {
    console.log(`\n${question}`);
    options.forEach((o, i) => console.log(`  ${i + 1}) ${o.label}${o.hint ? ` — ${o.hint}` : ""}`));
    rl.question(`Choose [1-${options.length}]: `, (answer) => {
      const idx = parseInt(answer, 10) - 1;
      resolve(options[idx >= 0 && idx < options.length ? idx : 0].value);
    });
  });
}

// ─── Setup wizard ─────────────────────────────────────────────────────────────

async function setup() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

  console.log(`
┌──────────────────────────────────────────┐
│         telechat ${NPM_VERSION} — setup wizard        │
│  Claude AI bot for Telegram / WhatsApp / Slack  │
└──────────────────────────────────────────┘
`);

  // ── Check Python ──
  const python = findPython();
  if (!python) {
    console.error("✗ Python 3.9+ not found. Install from https://python.org");
    rl.close();
    process.exit(1);
  }
  const pyVer = execSync(`${python} --version`, { encoding: "utf8" }).trim();
  console.log(`✓ ${pyVer}`);

  // ── Check Claude CLI ──
  const hasClaude = claudeCliInstalled();
  if (hasClaude) {
    console.log("✓ Claude CLI installed");
  } else {
    console.log("✗ Claude CLI not found (needed for CLI mode)");
    console.log("  Install: npm install -g @anthropic-ai/claude-code");
    console.log("  Then run: claude auth login\n");
  }

  // ── Platform ──
  const platform = await pick(rl, "Which platform(s)?", [
    { label: "Telegram",               value: "telegram",  hint: "easiest to set up" },
    { label: "WhatsApp",               value: "whatsapp",  hint: "via Green API" },
    { label: "Slack",                   value: "slack",     hint: "Socket Mode" },
    { label: "Telegram + WhatsApp",     value: "telegram,whatsapp", hint: "" },
    { label: "All three",              value: "all",       hint: "" },
  ]);

  const env = { BOT_MODE: platform };

  // ── Platform tokens ──
  const platforms = platform === "all" ? ["telegram", "whatsapp", "slack"]
    : platform === "both" ? ["telegram", "whatsapp"]
    : platform.split(",").map((s) => s.trim());

  if (platforms.includes("telegram")) {
    console.log("\n── Telegram setup ──");
    console.log("  1. Open Telegram → search @BotFather");
    console.log("  2. Send /newbot and follow the prompts");
    console.log("  3. Copy the token\n");
    env.TELEGRAM_BOT_TOKEN = await ask(rl, "Telegram bot token: ");
    if (!env.TELEGRAM_BOT_TOKEN) {
      console.log("  ⚠ Skipped — set TELEGRAM_BOT_TOKEN in .env later");
    }
    const userId = await ask(rl, "Your Telegram user ID (for access control, leave blank to allow all): ");
    if (userId) env.ALLOWED_USER_IDS = userId;
  }

  if (platforms.includes("whatsapp")) {
    console.log("\n── WhatsApp setup (Green API — free tier) ──");
    console.log("  1. Sign up at https://console.green-api.com");
    console.log("  2. Create instance → Developer plan (free)");
    console.log("  3. Scan QR with your WhatsApp phone\n");
    env.GREEN_API_INSTANCE_ID = await ask(rl, "Green API Instance ID: ");
    env.GREEN_API_TOKEN = await ask(rl, "Green API Token: ");
    const waNum = await ask(rl, "Your WhatsApp number (without +, e.g. 919876543210): ");
    if (waNum) env.WHATSAPP_ALLOWED_NUMBERS = waNum;
  }

  if (platforms.includes("slack")) {
    console.log("\n── Slack setup (Socket Mode) ──");
    console.log("  1. Go to https://api.slack.com/apps → Create New App");
    console.log("  2. Enable Socket Mode → create App-Level Token (connections:write)");
    console.log("  3. Add bot scopes: chat:write, channels:history, im:history, im:write, app_mentions:read, reactions:write");
    console.log("  4. Subscribe to events: message.im, message.channels, app_mention");
    console.log("  5. Install to workspace\n");
    env.SLACK_BOT_TOKEN = await ask(rl, "Slack Bot Token (xoxb-...): ");
    env.SLACK_APP_TOKEN = await ask(rl, "Slack App Token (xapp-...): ");
    const slackUser = await ask(rl, "Your Slack member ID (leave blank to allow all): ");
    if (slackUser) env.SLACK_ALLOWED_USER_IDS = slackUser;
  }

  // ── Claude mode ──
  const claudeMode = await pick(rl, "How should telechat connect to Claude?", hasClaude
    ? [
        { label: "CLI mode", value: "cli", hint: "free with Claude subscription" },
        { label: "API mode", value: "api", hint: "requires ANTHROPIC_API_KEY" },
      ]
    : [
        { label: "API mode", value: "api", hint: "requires ANTHROPIC_API_KEY" },
        { label: "CLI mode", value: "cli", hint: "install Claude CLI first" },
      ]
  );
  env.CLAUDE_MODE = claudeMode;

  if (claudeMode === "api") {
    env.ANTHROPIC_API_KEY = await ask(rl, "Anthropic API key (sk-ant-...): ");
    if (!env.ANTHROPIC_API_KEY) {
      console.log("  ⚠ Skipped — set ANTHROPIC_API_KEY in .env later");
    }
  } else if (!hasClaude) {
    console.log("\n  ⚠ Claude CLI is required for CLI mode.");
    console.log("    npm install -g @anthropic-ai/claude-code && claude auth login\n");
  }

  // ── Write .env ──
  const envContent = Object.entries(env)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n") + "\n";

  if (existsSync(ENV_FILE)) {
    const overwrite = await ask(rl, "\n.env already exists. Overwrite? (y/N): ");
    if (overwrite.toLowerCase() !== "y") {
      console.log("  Kept existing .env");
      rl.close();
      return;
    }
  }

  writeFileSync(ENV_FILE, envContent);
  console.log(`\n✓ Wrote ${ENV_FILE}`);
  rl.close();
}

// ─── Install Python package ──────────────────────────────────────────────────

function installPyPkg(python) {
  console.log(`Installing ${PYPI_PACKAGE} from PyPI...`);
  try {
    execSync(`${python} -m pip install --upgrade ${PYPI_PACKAGE}`, { stdio: "inherit" });
    return true;
  } catch {
    console.error(
      `\nFailed to install from PyPI. You can install manually:\n` +
      `  ${python} -m pip install ${PYPI_PACKAGE}\n\n` +
      `Or clone and run from source:\n` +
      `  git clone https://github.com/telechatai/telechat.git\n` +
      `  cd telechat && ./scripts/install.sh && ./scripts/start.sh`
    );
    return false;
  }
}

// ─── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const args = process.argv.slice(2);
  const cmd = args[0];

  // Info commands — no deps needed
  if (cmd === "--help" || cmd === "-h") {
    console.log(`telechat ${NPM_VERSION} — Claude AI messenger bot (Telegram, WhatsApp, Slack)

Usage:
  telechat              Start the bot (runs setup wizard if no .env found)
  telechat setup        Interactive setup wizard
  telechat start        Start the bot (skip setup)
  telechat --version    Show version

Docs: https://github.com/telechatai/telechat`);
    process.exit(0);
  }

  if (cmd === "--version" || cmd === "-v") {
    console.log(`telechat ${NPM_VERSION}`);
    process.exit(0);
  }

  // Check Python
  const python = findPython();
  if (!python) {
    console.error("Error: Python 3.9+ is required.\nInstall from https://python.org");
    process.exit(1);
  }

  // Setup wizard
  if (cmd === "setup" || (!cmd && !existsSync(ENV_FILE))) {
    await setup();
    if (!isPyPkgInstalled(python)) {
      if (!installPyPkg(python)) process.exit(1);
    }
    console.log("\n✓ Ready! Starting telechat...\n");
  }

  // Explicit install
  if (cmd === "--install") {
    if (!installPyPkg(python)) process.exit(1);
    console.log("Done.");
    process.exit(0);
  }

  // Ensure Python package
  if (!isPyPkgInstalled(python)) {
    if (!installPyPkg(python)) process.exit(1);
  }

  // Check .env
  if (!existsSync(ENV_FILE)) {
    console.error("No .env file found. Run: telechat setup");
    process.exit(1);
  }

  // Start
  const child = spawn(python, ["-m", "telechat_pkg.main"], {
    stdio: "inherit",
    cwd: process.cwd(),
  });

  child.on("exit", (code) => process.exit(code || 0));
  child.on("error", (err) => {
    console.error("Failed to start:", err.message);
    process.exit(1);
  });
}

main();
