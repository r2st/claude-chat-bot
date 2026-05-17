#!/usr/bin/env node

const { execSync, spawn } = require("child_process");
const { existsSync, writeFileSync } = require("fs");
const path = require("path");
const readline = require("readline");
const https = require("https");

const PYPI_PACKAGE = "telechatai";
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

function openUrl(url) {
  try {
    const cmd = process.platform === "darwin" ? "open"
      : process.platform === "win32" ? "start"
      : "xdg-open";
    execSync(`${cmd} "${url}"`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function httpGet(url) {
  return new Promise((resolve) => {
    https.get(url, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try { resolve({ status: res.statusCode, data: JSON.parse(data) }); }
        catch { resolve({ status: res.statusCode, data }); }
      });
    }).on("error", () => resolve(null));
  });
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
    options.forEach((o, i) => console.log(`  ${i + 1}) ${o.label}${o.hint ? `  — ${o.hint}` : ""}`));
    rl.question(`Choose [1-${options.length}]: `, (answer) => {
      const idx = parseInt(answer, 10) - 1;
      resolve(options[idx >= 0 && idx < options.length ? idx : 0].value);
    });
  });
}

function spin(msg) {
  const frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  let i = 0;
  const id = setInterval(() => {
    process.stdout.write(`\r  ${frames[i++ % frames.length]} ${msg}`);
  }, 80);
  return {
    ok(text) { clearInterval(id); process.stdout.write(`\r  ✓ ${text}\n`); },
    fail(text) { clearInterval(id); process.stdout.write(`\r  ✗ ${text}\n`); },
  };
}

async function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ─── Token validators ────────────────────────────────────────────────────────

async function validateTelegramToken(token) {
  const res = await httpGet(`https://api.telegram.org/bot${token}/getMe`);
  if (!res || res.status !== 200 || !res.data?.ok) return null;
  return res.data.result;
}

async function validateGreenApi(instanceId, token) {
  const url = `https://api.green-api.com/waInstance${instanceId}/getStateInstance/${token}`;
  const res = await httpGet(url);
  if (!res || res.status !== 200) return null;
  return res.data;
}

async function waitForTelegramMessage(token, timeoutSec) {
  const deadline = Date.now() + timeoutSec * 1000;
  let offset = 0;
  while (Date.now() < deadline) {
    const res = await httpGet(
      `https://api.telegram.org/bot${token}/getUpdates?offset=${offset}&timeout=3&allowed_updates=["message"]`
    );
    if (res?.data?.ok && res.data.result?.length) {
      for (const u of res.data.result) {
        offset = u.update_id + 1;
        if (u.message?.text === "/start" || u.message?.text) {
          return u.message.from;
        }
      }
    }
    await sleep(1000);
  }
  return null;
}

// ─── Setup wizard ─────────────────────────────────────────────────────────────

async function setup() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

  console.log(`
┌──────────────────────────────────────────────┐
│          telechat ${NPM_VERSION} — setup wizard           │
│   Claude AI bot for Telegram / WhatsApp / Slack   │
└──────────────────────────────────────────────┘
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
    console.log("⚠ Claude CLI not found (needed for CLI mode)");
    console.log("  Install: npm install -g @anthropic-ai/claude-code");
    console.log("  Then run: claude auth login\n");
  }

  // ── Platform ──
  const platform = await pick(rl, "Which platform(s)?", [
    { label: "Telegram",               value: "telegram",  hint: "easiest to set up" },
    { label: "WhatsApp",               value: "whatsapp",  hint: "via Green API" },
    { label: "Slack",                   value: "slack",     hint: "Socket Mode" },
    { label: "Telegram + WhatsApp",     value: "telegram,whatsapp" },
    { label: "All three",              value: "all" },
  ]);

  const env = { BOT_MODE: platform };
  const platforms = platform === "all" ? ["telegram", "whatsapp", "slack"]
    : platform.split(",").map((s) => s.trim());

  // ── Telegram setup ──
  if (platforms.includes("telegram")) {
    console.log("\n── Telegram setup ──\n");
    console.log("  I'll open BotFather in Telegram where you can create a bot.");
    console.log("  Send /newbot → pick a name → pick a username → copy the token.\n");

    const opened = openUrl("https://t.me/BotFather");
    if (opened) {
      console.log("  ✓ Opened BotFather in your browser\n");
    } else {
      console.log("  Open this link: https://t.me/BotFather\n");
    }

    await ask(rl, "  Press Enter when you have the token...");

    let botInfo = null;
    while (!botInfo) {
      const token = await ask(rl, "  Paste your bot token: ");
      if (!token) {
        console.log("  ⚠ Skipped — set TELEGRAM_BOT_TOKEN in .env later");
        break;
      }

      const s = spin("Validating token...");
      botInfo = await validateTelegramToken(token);
      if (botInfo) {
        s.ok(`Bot verified: @${botInfo.username} (${botInfo.first_name})`);
        env.TELEGRAM_BOT_TOKEN = token;
      } else {
        s.fail("Invalid token. Check and try again (or press Enter to skip).");
      }
    }

    // Auto-detect user ID
    if (env.TELEGRAM_BOT_TOKEN) {
      console.log("\n  -- Access control --");
      console.log("  To restrict the bot to only you, I need your Telegram user ID.");
      const idMethod = await pick(rl, "  How to set access control?", [
        { label: "Auto-detect", hint: "send any message to your new bot" },
        { label: "Enter manually", hint: "if you know your numeric ID" },
        { label: "Skip", hint: "allow anyone to use the bot" },
      ]);

      if (idMethod === "Auto-detect") {
        console.log(`\n  Open your bot: https://t.me/${botInfo.username}`);
        openUrl(`https://t.me/${botInfo.username}`);
        console.log("  Send any message to the bot (e.g. \"hi\").");

        // flush old updates first
        await httpGet(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates?offset=-1`);

        const s = spin("Waiting for your message (60s)...");
        const sender = await waitForTelegramMessage(env.TELEGRAM_BOT_TOKEN, 60);
        if (sender) {
          s.ok(`Got it! User ID: ${sender.id} (${sender.first_name})`);
          env.TELEGRAM_ALLOWED_USER_IDS = String(sender.id);
        } else {
          s.fail("Timed out. You can set TELEGRAM_ALLOWED_USER_IDS in .env later.");
        }
      } else if (idMethod === "Enter manually") {
        const uid = await ask(rl, "  Your Telegram user ID: ");
        if (uid) env.TELEGRAM_ALLOWED_USER_IDS = uid;
      }
    }
  }

  // ── WhatsApp setup ──
  if (platforms.includes("whatsapp")) {
    console.log("\n── WhatsApp setup (Green API — free tier) ──\n");
    console.log("  I'll open the Green API console where you create a free instance.");
    console.log("  Steps:");
    console.log("    1. Sign up / log in");
    console.log("    2. Create instance → Developer plan (free)");
    console.log("    3. Scan QR code with your WhatsApp phone");
    console.log("    4. Copy Instance ID and API Token from the dashboard\n");

    const opened = openUrl("https://console.green-api.com");
    if (opened) {
      console.log("  ✓ Opened Green API console in your browser\n");
    } else {
      console.log("  Open: https://console.green-api.com\n");
    }

    await ask(rl, "  Press Enter when you have your Instance ID and Token...");

    let validated = false;
    while (!validated) {
      const instanceId = await ask(rl, "  Instance ID: ");
      const apiToken = await ask(rl, "  API Token: ");

      if (!instanceId || !apiToken) {
        console.log("  ⚠ Skipped — set GREEN_API_INSTANCE_ID and GREEN_API_TOKEN in .env later");
        break;
      }

      const s = spin("Validating Green API credentials...");
      const state = await validateGreenApi(instanceId, apiToken);
      if (state) {
        const stateStr = state.stateInstance || "unknown";
        if (stateStr === "authorized") {
          s.ok(`Connected! WhatsApp instance is authorized.`);
        } else {
          s.ok(`Credentials valid (instance state: ${stateStr}).`);
          if (stateStr === "notAuthorized") {
            console.log("  ⚠ Scan the QR code in Green API console to authorize WhatsApp.");
          }
        }
        env.GREEN_API_INSTANCE_ID = instanceId;
        env.GREEN_API_TOKEN = apiToken;
        validated = true;
      } else {
        s.fail("Invalid credentials. Check and try again (or press Enter to skip).");
      }
    }

    if (env.GREEN_API_INSTANCE_ID) {
      const waNum = await ask(rl, "  Your WhatsApp number (without +, e.g. 919876543210, blank=allow all): ");
      if (waNum) env.WHATSAPP_ALLOWED_NUMBERS = waNum;
    }
  }

  // ── Slack setup ──
  if (platforms.includes("slack")) {
    console.log("\n── Slack setup (Socket Mode) ──\n");
    console.log("  I'll open the Slack app creation page.\n");
    console.log("  Steps:");
    console.log("    1. Create New App → From scratch");
    console.log("    2. Enable Socket Mode → create App-Level Token (connections:write)");
    console.log("    3. OAuth & Permissions → add bot scopes:");
    console.log("       chat:write, channels:history, im:history, im:write,");
    console.log("       app_mentions:read, reactions:write");
    console.log("    4. Event Subscriptions → subscribe to:");
    console.log("       message.im, message.channels, app_mention");
    console.log("    5. Install to workspace\n");

    const opened = openUrl("https://api.slack.com/apps");
    if (opened) {
      console.log("  ✓ Opened Slack API console in your browser\n");
    } else {
      console.log("  Open: https://api.slack.com/apps\n");
    }

    await ask(rl, "  Press Enter when you have your tokens...");
    env.SLACK_BOT_TOKEN = await ask(rl, "  Slack Bot Token (xoxb-...): ");
    env.SLACK_APP_TOKEN = await ask(rl, "  Slack App Token (xapp-...): ");
    const slackUser = await ask(rl, "  Your Slack member ID (blank=allow all): ");
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

  const python = findPython();
  if (!python) {
    console.error("Error: Python 3.9+ is required.\nInstall from https://python.org");
    process.exit(1);
  }

  if (cmd === "setup" || (!cmd && !existsSync(ENV_FILE))) {
    await setup();
    if (!isPyPkgInstalled(python)) {
      if (!installPyPkg(python)) process.exit(1);
    }
    console.log("\n✓ Ready! Starting telechat...\n");
  }

  if (cmd === "--install") {
    if (!installPyPkg(python)) process.exit(1);
    console.log("Done.");
    process.exit(0);
  }

  if (!isPyPkgInstalled(python)) {
    if (!installPyPkg(python)) process.exit(1);
  }

  if (!existsSync(ENV_FILE)) {
    console.error("No .env file found. Run: telechat setup");
    process.exit(1);
  }

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
