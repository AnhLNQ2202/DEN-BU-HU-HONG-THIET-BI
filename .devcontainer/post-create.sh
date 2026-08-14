#!/usr/bin/env bash
set -euo pipefail

python -m venv /home/vscode/.venv
/home/vscode/.venv/bin/python -m pip install --upgrade pip
/home/vscode/.venv/bin/python -m pip install -e ".[dev,email]"

sudo corepack enable
sudo corepack prepare pnpm@11.19.0 --activate
sudo mkdir -p /workspace/frontend/node_modules
sudo chown -R "$(id -u):$(id -g)" /workspace/frontend/node_modules
pnpm --dir frontend install --frozen-lockfile

/home/vscode/.venv/bin/python -m asset_compensation.cli init --demo
