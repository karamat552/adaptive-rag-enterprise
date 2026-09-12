// k6 Load Test — Adaptive RAG Enterprise (P3)
// 5→10 virtual users, p95 latency + error rates for the resume baseline.
// Prereq: k6 installed (winget install grafana.k6 or brew install k6)
// Run:    k6 run scripts/load_test.js
//
// This test measures the SERVER (concurrency, pool, auth), not the LLM —
// cache-hit requests are the 0-token path. For full-pipeline load, use
// unique phrasings (consumes Groq TPD).

import http from "k6/http";
import { check, sleep } from "k6";
import { Rate, Trend } from "k6/metrics";

const BASE = __ENV.BASE_URL || "https://adaptive-rag-enterprise.onrender.com";
const API_KEY = __ENV.QUERY_API_KEY || "";
const HEADERS = { "Content-Type": "application/json" };
if (API_KEY) HEADERS["X-API-Key"] = API_KEY;

const errorRate = new Rate("query_errors");
const queryLatency = new Trend("query_latency_ms", true);

export const options = {
  stages: [
    { duration: "30s", target: 5 },   // ramp to 5 VUs
    { duration: "60s", target: 5 },   // hold at 5
    { duration: "30s", target: 10 },  // spike to 10
    { duration: "60s", target: 10 },  // hold at 10
    { duration: "30s", target: 0 },   // ramp down
  ],
  thresholds: {
    http_req_duration: ["p(95)<5000"], // p95 under 5s (cache-hit path)
    query_errors: ["rate<0.1"],        // error rate under 10%
  },
};

const QUESTIONS = [
  "What was Tesla total automotive revenues in Q4 2023?",
  "What was Apple total net sales in Q4 2023?",
  "What was Meta total revenue in Q4 2023?",
  "What was Reality Labs operating loss in Q4 2023?",
  "What was Tesla diluted EPS in Q4 2023?",
  "What was Apple cash position in Q4 2023?",
  "Did Meta initiate a dividend in Q4 2023?",
  "What was Meta advertising revenue in Q4 2023?",
  "What was Tesla operating margin in Q4 2023?",
  "What was Apple services revenue in Q4 2023?",
];

let qIdx = 0;

export default function () {
  const q = QUESTIONS[qIdx % QUESTIONS.length];
  qIdx++;

  const res = http.post(
    `${BASE}/query`,
    JSON.stringify({ question: q }),
    { headers: HEADERS, timeout: "120s" }
  );

  const ok = check(res, {
    "status is 200": (r) => r.status === 200,
    "has outcome": (r) => {
      try { return JSON.parse(r.body).outcome !== undefined; }
      catch { return false; }
    },
  });
  errorRate.add(!ok);
  queryLatency.add(res.timings.duration);

  sleep(2); // think time between queries
}
