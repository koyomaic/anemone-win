#!/usr/bin/env bash
# chatx MCP 团队安装（方案1：local 桥接）。幂等，不含任何密钥。
set -euo pipefail
WORKSPACE="${WORKSPACE:-/mnt/vol-eltaah12/workspace}"
SHARED="$WORKSPACE/shared"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE_DST="$SHARED/tools/chatx-mcp-bridge.py"
ENTRY_DST="$SHARED/mcp/chatx.json"
ENV_FILE="$SHARED/secrets/chatx.env"

mkdir -p "$SHARED/tools" "$SHARED/mcp"
install -m 0755 "$HERE/assets/chatx-mcp-bridge.py" "$BRIDGE_DST"

cat > "$ENTRY_DST" <<JSON
{
  "type": "local",
  "command": ["/usr/bin/python3", "$BRIDGE_DST"],
  "enabled": true
}
JSON

echo "已安装桥接: $BRIDGE_DST"
echo "已注册条目: $ENTRY_DST"
if [ ! -f "$ENV_FILE" ]; then
  echo "⚠️ 缺 $ENV_FILE（需含 CHATX_MCP_TOKEN=...，向 chambroad 申请）。无 token 调用会 401。"
else
  chmod 600 "$ENV_FILE" 2>/dev/null || true
fi
echo "下一个 relay 任务即自动挂载 mcp__chatx__*（无需重启 relay）。"
