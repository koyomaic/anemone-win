#!/usr/bin/env bash
# chatx MCP 桥接自测：喂 initialize + tools/list，确认网关可达与工具数。
set -euo pipefail
WORKSPACE="${WORKSPACE:-/mnt/vol-eltaah12/workspace}"
BRIDGE="$WORKSPACE/shared/tools/chatx-mcp-bridge.py"
[ -f "$BRIDGE" ] || { echo "桥接未安装，先跑 scripts/install.sh"; exit 1; }
printf '%s\n%s\n%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
 | /usr/bin/python3 "$BRIDGE"
