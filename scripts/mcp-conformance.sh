#!/usr/bin/env bash
# Run the published MCP conformance suite's server scenarios against an SCF MCP endpoint.
#
#   SCF_MCP_TOKEN=<SCF API key> scripts/mcp-conformance.sh http://localhost:8000/mcp [suite options]
#
# The suite runs exactly the server scenarios that REVISION requires, at one pinned
# SUITE_VERSION, and scripts/mcp-conformance-baseline.yaml lists the failures SCF
# expects, each with its reason. The suite sends no credentials, so a loopback proxy
# started here adds "Authorization: Bearer $SCF_MCP_TOKEN" to every request it
# forwards; it listens on 127.0.0.1 only and stops when the script exits.
#
# Needs Node 20 or later and access to the npm registry. Exit status is the suite's:
# 0 when only baselined failures occur. See docs/integrations/mcp-conformance.md.
set -euo pipefail

SUITE_VERSION="0.2.0-alpha.12"
REVISION="2026-07-28"
BASELINE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mcp-conformance-baseline.yaml"
PROXY_PORT="${SCF_MCP_PROXY_PORT:-8765}"

url="${1:?usage: SCF_MCP_TOKEN=<api key> $0 <mcp endpoint url> [suite options]}"
shift
: "${SCF_MCP_TOKEN:?set SCF_MCP_TOKEN to an SCF API key}"
command -v node >/dev/null || { echo "mcp-conformance: node is required" >&2; exit 2; }
command -v curl >/dev/null || { echo "mcp-conformance: curl is required" >&2; exit 2; }

# The proxy answers READY_PATH itself with 204, so the probe below cannot mistake
# another listener on the port for it.
READY_PATH="/.scf-mcp-proxy-ready"
# shellcheck disable=SC2016 # the ${...} below are JavaScript template literals
SCF_MCP_URL="$url" PROXY_PORT="$PROXY_PORT" READY_PATH="$READY_PATH" node -e '
const http = require("http");
const target = new URL(process.env.SCF_MCP_URL);
const client = require(target.protocol === "https:" ? "https" : "http");
const authorization = `Bearer ${process.env.SCF_MCP_TOKEN}`;
http.createServer((req, res) => {
  if (req.url === process.env.READY_PATH) { res.writeHead(204); res.end(); return; }
  const headers = { ...req.headers, host: target.host, authorization };
  const upstream = client.request(target, { method: req.method, headers }, (reply) => {
    res.writeHead(reply.statusCode, reply.headers);
    reply.pipe(res);
  });
  upstream.on("error", (err) => { res.writeHead(502); res.end(String(err)); });
  req.pipe(upstream);
}).on("error", (err) => {
  console.error(`mcp-conformance: proxy: ${err.message}`);
  process.exit(1);
}).listen(Number(process.env.PROXY_PORT), "127.0.0.1");
' &
proxy=$!
trap 'kill "$proxy" 2>/dev/null || true' EXIT

ready=""
for _ in $(seq 50); do
  kill -0 "$proxy" 2>/dev/null || break
  status="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PROXY_PORT}${READY_PATH}" || true)"
  if [ "$status" = 204 ]; then ready=1; break; fi
  sleep 0.1
done
if [ -z "$ready" ] || ! kill -0 "$proxy" 2>/dev/null; then
  echo "mcp-conformance: the proxy did not start on 127.0.0.1:${PROXY_PORT};" \
    "set SCF_MCP_PROXY_PORT to a free port" >&2
  exit 2
fi

# Only the proxy needs the key: npx and the install scripts it runs do not get it.
env -u SCF_MCP_TOKEN npx --yes "@modelcontextprotocol/conformance@${SUITE_VERSION}" server \
  --url "http://127.0.0.1:${PROXY_PORT}/mcp" \
  --requirements "${REVISION}" \
  --expected-failures "${BASELINE}" \
  "$@"
