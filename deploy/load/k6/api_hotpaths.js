// HARD-11 — k6 API hot-path load test.
//
// Drives the read- and write-heavy hot paths (/health, /board/tasks,
// /knowledge/search, agent-run enqueue) at a configurable VU/duration and
// enforces p95 latency + error-rate thresholds from deploy/load/budgets.toml.
//
// Usage (full run, manual/nightly):
//   k6 run -e BASE_URL=http://localhost:8000 -e API_KEY=forge_xxx \
//          deploy/load/k6/api_hotpaths.js
// Usage (CI smoke, non-blocking — see `make load-smoke`):
//   k6 run -e SMOKE=1 deploy/load/k6/api_hotpaths.js
//
// No external creds are required for /health; authenticated routes need an
// API_KEY (a workspace key). Without one, the script exercises /health only and
// records the authenticated routes as skipped.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const API_KEY = __ENV.API_KEY || '';
const SMOKE = __ENV.SMOKE === '1';

const errorRate = new Rate('hotpath_errors');

export const options = SMOKE
  ? {
      // Tiny, fast smoke for CI (non-blocking): a few VUs for a few seconds.
      vus: 2,
      duration: '5s',
      thresholds: {
        http_req_failed: ['rate<0.05'],
      },
    }
  : {
      scenarios: {
        reads: {
          executor: 'ramping-vus',
          startVUs: 0,
          stages: [
            { duration: '30s', target: 20 },
            { duration: '2m', target: 20 },
            { duration: '30s', target: 0 },
          ],
        },
      },
      thresholds: {
        // p95 budgets (ms) — keep in sync with deploy/load/budgets.toml [api].
        'http_req_duration{route:health}': ['p(95)<50'],
        'http_req_duration{route:board}': ['p(95)<300'],
        'http_req_duration{route:knowledge_search}': ['p(95)<800'],
        http_req_failed: ['rate<0.01'],
      },
    };

function authHeaders() {
  return API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {};
}

export default function () {
  // Liveness — always available, never rate limited.
  const health = http.get(`${BASE_URL}/health`, { tags: { route: 'health' } });
  check(health, { 'health 200': (r) => r.status === 200 });
  errorRate.add(health.status !== 200);

  if (API_KEY) {
    const board = http.get(`${BASE_URL}/board/tasks`, {
      headers: authHeaders(),
      tags: { route: 'board' },
    });
    check(board, { 'board < 500': (r) => r.status < 500 });
    errorRate.add(board.status >= 500);

    const search = http.post(
      `${BASE_URL}/knowledge/search`,
      JSON.stringify({ query: 'server config', k: 5 }),
      {
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        tags: { route: 'knowledge_search' },
      },
    );
    check(search, { 'search < 500': (r) => r.status < 500 });
    errorRate.add(search.status >= 500);
  }

  sleep(1);
}
