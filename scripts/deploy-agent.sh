#!/usr/bin/env bash
set -euo pipefail

git pull --ff-only
docker compose up -d --build --no-deps whatsapp-agent
docker compose ps whatsapp-agent
