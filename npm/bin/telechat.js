#!/usr/bin/env node

const { execSync, spawn } = require("child_process");
const { existsSync } = require("fs");
const path = require("path");

const PYPI_PACKAGE = "telechat";

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
    console.log(`
telechat — Claude AI messenger bot (Telegram, WhatsApp, Slack)

Usage:
  telechat              Start the bot (reads .env in current directory)
  telechat --install    Install/upgrade the Python package
  telechat --version    Show version

Requirements:
  - Python 3.9+
  - A .env file with your bot tokens (see README)

Docs: https://github.com/telechatai/telechat
`);
    process.exit(0);
  }

  const python = findPython();
  if (!python) {
    console.error("Error: Python 3.9+ is required but not found.");
    console.error("Install Python from https://python.org and try again.");
    process.exit(1);
  }

  if (args[0] === "--install" || !isInstalled(python)) {
    console.log(`Installing ${PYPI_PACKAGE} from PyPI...`);
    try {
      execSync(`${python} -m pip install --upgrade ${PYPI_PACKAGE}`, {
        stdio: "inherit",
      });
    } catch (e) {
      console.error("Failed to install. Try: pip install telechat");
      process.exit(1);
    }
    if (args[0] === "--install") {
      console.log("Done.");
      process.exit(0);
    }
  }

  if (args[0] === "--version") {
    try {
      const version = execSync(
        `${python} -c "from telechat_pkg import __version__; print(__version__)"`,
        { encoding: "utf8" }
      ).trim();
      console.log(`telechat ${version}`);
    } catch {
      console.log("telechat (version unknown)");
    }
    process.exit(0);
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
