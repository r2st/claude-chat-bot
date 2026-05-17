#!/usr/bin/env node

const { execSync, spawn } = require("child_process");

const PYPI_PACKAGE = "telechat";
const NPM_VERSION = require("../package.json").version;

function findPython() {
  for (const cmd of ["python3", "python"]) {
    try {
      const version = execSync(`${cmd} --version 2>&1`, { encoding: "utf8" });
      if (version.includes("Python 3")) return cmd;
    } catch {}
  }
  return null;
}

function isInstalled(python) {
  try {
    execSync(`${python} -c "import telechat_pkg"`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function main() {
  const args = process.argv.slice(2);

  if (args[0] === "--help" || args[0] === "-h") {
    console.log(`telechat ${NPM_VERSION} — Claude AI messenger bot (Telegram, WhatsApp, Slack)

Usage:
  telechat              Start the bot (reads .env in current directory)
  telechat --install    Install/upgrade the Python package
  telechat --version    Show version

Requirements:
  - Python 3.9+
  - A .env file with your bot tokens (see README)

Docs: https://github.com/telechatai/telechat`);
    process.exit(0);
  }

  if (args[0] === "--version" || args[0] === "-v") {
    console.log(`telechat ${NPM_VERSION}`);
    process.exit(0);
  }

  const python = findPython();
  if (!python) {
    console.error("Error: Python 3.9+ is required but not found.");
    console.error("Install Python from https://python.org and try again.");
    process.exit(1);
  }

  if (args[0] === "--install" || !isInstalled(python)) {
    if (!isInstalled(python)) {
      console.log(`Python package not found. Installing ${PYPI_PACKAGE} from PyPI...`);
    } else {
      console.log(`Upgrading ${PYPI_PACKAGE}...`);
    }
    try {
      execSync(`${python} -m pip install --upgrade ${PYPI_PACKAGE}`, {
        stdio: "inherit",
      });
    } catch {
      console.error(
        `\nFailed to install from PyPI. You can install manually:\n` +
        `  ${python} -m pip install ${PYPI_PACKAGE}\n\n` +
        `Or clone and run from source:\n` +
        `  git clone https://github.com/telechatai/telechat.git\n` +
        `  cd telechat && ./scripts/install.sh && ./scripts/start.sh`
      );
      process.exit(1);
    }
    if (args[0] === "--install") {
      console.log("Done.");
      process.exit(0);
    }
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
