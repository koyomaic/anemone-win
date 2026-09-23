#!/usr/bin/env python3
"""chatx MCP stdio<->HTTP 桥接（方案1）。

claude --mcp-config 以 local stdio 方式拉起本进程：stdin 逐行读 JSON-RPC 2.0
请求，转发到 chatx 无状态 JSON-RPC 入口 /mcp/http（注入 Authorization），把响应
原样单行写回 stdout。通知（无 id）不写回。

凭据只从 shared/secrets/chatx.env 读，绝不硬编码、绝不回显。
"""
import json
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://chatx.chambroad.com/mcp/http"
ENV_FILE = "/mnt/vol-eltaah12/workspace/shared/secrets/chatx.env"
TIMEOUT = 120


def load_token():
    tok = os.environ.get("CHATX_MCP_TOKEN", "")
    if tok:
        return tok.strip()
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("CHATX_MCP_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def scrub(text, tok):
    return text.replace(tok, "***") if tok else text


def parse_body(raw, ctype):
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    if "text/event-stream" in (ctype or "") or text.startswith("event:") or "\ndata:" in text or text.startswith("data:"):
        for ln in text.splitlines():
            ln = ln.strip()
            if ln.startswith("data:"):
                ln = ln[5:].strip()
                if ln and ln != "[DONE]":
                    try:
                        return json.loads(ln)
                    except ValueError:
                        continue
        return None
    try:
        return json.loads(text)
    except ValueError:
        return {"jsonrpc": "2.0", "error": {"code": -32603,
                "message": "upstream non-JSON response"}, "id": None}


def forward(msg, tok):
    body = json.dumps(msg).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + tok,
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            return parse_body(resp.read(), ctype)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {
            "code": -32000 - int(e.code or 0), "message": scrub("HTTP %s: %s" % (e.code, detail), tok)}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {
            "code": -32603, "message": scrub("bridge error: %r" % (e,), tok)}}


def main():
    tok = load_token()
    if not tok:
        sys.stderr.write("chatx-bridge: CHATX_MCP_TOKEN 未找到（检查 %s）\n" % ENV_FILE)
        return 1
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            sys.stderr.write("chatx-bridge: 跳过非 JSON 行\n")
            continue
        is_notification = "id" not in msg
        resp = forward(msg, tok)
        if is_notification:
            continue
        if resp is None:
            resp = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
