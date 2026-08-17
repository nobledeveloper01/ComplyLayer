// The §11.9 load target: 2,000 decisions per second with 100 active rules,
// asserting p99 under 100 ms.
//
// Not run in CI. A p99 assertion on a shared runner flakes, and a flaky blocking
// gate is one somebody comments out within a month — the evaluation-stage
// benchmark in `tests/test_latency_benchmark.py` is what blocks a pull request.
// This runs nightly on dedicated hardware, where the number means something.
//
//   k6 run -e BASE_URL=https://... -e API_KEY=cl_live_... deploy/k6/decisions.js

import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const API_KEY = __ENV.API_KEY || '';

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: 2000,
      timeUnit: '1s',
      duration: '5m',
      preAllocatedVUs: 200,
      maxVUs: 800,
    },
  },
  thresholds: {
    // The product promise, as a build condition.
    'http_req_duration{expected_response:true}': ['p(99)<100', 'p(50)<20'],
    // §11.3's degraded-decision SLO. Treated as availability rather than
    // quality: a degraded decision is a compliance control that did not run.
    'checks{check:not degraded}': ['rate>0.9995'],
    http_req_failed: ['rate<0.0005'],
  },
};

export default function () {
  // A distinct customer per virtual user, so velocity windows are realistic
  // rather than every request hammering one customer's sorted set.
  const customer = `usr_${__VU}`;
  const reference = `TXN-${__VU}-${__ITER}`;

  const response = http.post(
    `${BASE_URL}/v1/decisions`,
    JSON.stringify({
      transaction_ref: reference,
      customer_ref: customer,
      amount_minor: 1000000 + ((__ITER * 7919) % 90000000),
      currency: 'NGN',
      transaction_type: 'transfer',
      channel: 'mobile',
      customer: { kyc_tier: (__VU % 3) + 1, country: 'NG' },
      destination: { country: 'NG', bank_code: '058', is_new_beneficiary: __ITER % 11 === 0 },
    }),
    {
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': reference,
        Authorization: `Bearer ${API_KEY}`,
      },
    },
  );

  const body = response.status === 200 ? response.json() : {};
  check(response, {
    'decided': (r) => r.status === 200,
    'has an outcome': () => ['allow', 'flag', 'block'].includes(body.outcome),
    'not degraded': () => body.degraded === false,
  });
}
