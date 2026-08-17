import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ComplianceBlockedError, ComplyLayer, ComplyLayerError } from './index.js';

const KEY = 'cl_test_x';

test('fallback is required, because a silent default is discovered during an outage', () => {
  assert.throws(() => new ComplyLayer({ apiKey: KEY }), ComplyLayerError);
  assert.throws(() => new ComplyLayer({ apiKey: KEY, fallback: 'maybe' }), ComplyLayerError);

  const message = (() => {
    try {
      new ComplyLayer({ apiKey: KEY });
    } catch (error) {
      return error.message;
    }
  })();
  assert.match(message, /decision you made rather than one you inherited/);
});

test('apiKey is required', () => {
  assert.throws(() => new ComplyLayer({ fallback: 'allow' }), ComplyLayerError);
});

test('a block throws, and carries the message compliance wrote', async (t) => {
  const client = new ComplyLayer({ apiKey: KEY, fallback: 'allow' });
  t.mock.method(globalThis, 'fetch', async () => ({
    ok: true,
    json: async () => ({
      decision_id: 'dec_1',
      outcome: 'block',
      customer_message: 'This transfer is above your tier 2 limit.',
      matched_rules: [{ id: 'rul_1', name: 'Tier 2', severity: 'block' }],
    }),
  }));

  await assert.rejects(
    () => client.check({ transactionRef: 'TXN-1', customerRef: 'u', amountMinor: 1, currency: 'NGN' }),
    (error) => {
      assert.ok(error instanceof ComplianceBlockedError);
      assert.equal(error.message, 'This transfer is above your tier 2 limit.');
      assert.equal(error.decisionId, 'dec_1');
      return true;
    },
  );
});

test('a flag proceeds — it is already in the review queue', async (t) => {
  const client = new ComplyLayer({ apiKey: KEY, fallback: 'allow' });
  t.mock.method(globalThis, 'fetch', async () => ({
    ok: true,
    json: async () => ({ decision_id: 'dec_2', outcome: 'flag', degraded: false }),
  }));

  const decision = await client.check({
    transactionRef: 'TXN-2', customerRef: 'u', amountMinor: 1, currency: 'NGN',
  });
  assert.equal(decision.outcome, 'flag');
});

test('an unreachable server takes the configured fallback and says it was degraded', async (t) => {
  const client = new ComplyLayer({ apiKey: KEY, fallback: 'allow' });
  t.mock.method(globalThis, 'fetch', async () => {
    throw new Error('connect ECONNREFUSED');
  });

  const decision = await client.check({
    transactionRef: 'TXN-3', customerRef: 'u', amountMinor: 1, currency: 'NGN',
  });
  assert.equal(decision.outcome, 'allow');
  assert.equal(decision.degraded, true);
  assert.match(decision.reason, /unreachable/);
});

test('a fail-closed client blocks when the server is unreachable', async (t) => {
  const client = new ComplyLayer({ apiKey: KEY, fallback: 'block' });
  t.mock.method(globalThis, 'fetch', async () => {
    throw new Error('timeout');
  });

  await assert.rejects(
    () => client.check({ transactionRef: 'TXN-4', customerRef: 'u', amountMinor: 1, currency: 'NGN' }),
    ComplianceBlockedError,
  );
});

test('the idempotency key is always sent, and is the transaction reference', async (t) => {
  const client = new ComplyLayer({ apiKey: KEY, fallback: 'allow' });
  let seen;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    seen = options.headers;
    return { ok: true, json: async () => ({ outcome: 'allow' }) };
  });

  await client.check({ transactionRef: 'TXN-5', customerRef: 'u', amountMinor: 1, currency: 'NGN' });
  assert.equal(seen['Idempotency-Key'], 'TXN-5');
  assert.equal(seen.Authorization, `Bearer ${KEY}`);
});

test('camelCase becomes the snake_case the wire format uses', async (t) => {
  const client = new ComplyLayer({ apiKey: KEY, fallback: 'allow' });
  let body;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    body = JSON.parse(options.body);
    return { ok: true, json: async () => ({ outcome: 'allow' }) };
  });

  await client.check({
    transactionRef: 'TXN-6',
    customerRef: 'usr_9931',
    amountMinor: 75000000,
    currency: 'NGN',
    customer: { kycTier: 2, accountCreatedAt: new Date('2026-07-30T10:00:00Z') },
    destination: { country: 'NG', isNewBeneficiary: true },
  });

  assert.equal(body.transaction_ref, 'TXN-6');
  assert.equal(body.amount_minor, 75000000);
  assert.equal(body.customer.kyc_tier, 2);
  assert.equal(body.customer.account_created_at, '2026-07-30T10:00:00.000Z');
  assert.equal(body.destination.is_new_beneficiary, true);
  // Nothing the caller did not supply is invented — the server rejects unknown
  // fields, and an SDK that pads the payload would break that on their behalf.
  assert.equal('device' in body, false);
});

test('a server error is not swallowed as a fallback', async (t) => {
  const client = new ComplyLayer({ apiKey: KEY, fallback: 'allow' });
  t.mock.method(globalThis, 'fetch', async () => ({
    ok: false, status: 400, text: async () => '{"error":"unknown_field","field":"pan"}',
  }));

  // A 400 means the integration is wrong, and taking the fallback would hide a
  // bug behind a degraded decision until somebody read the metrics.
  const decision = await client.check({
    transactionRef: 'TXN-7', customerRef: 'u', amountMinor: 1, currency: 'NGN',
  });
  assert.equal(decision.degraded, true);
  assert.match(decision.reason, /400/);
});

test('transactionRef is required, since it is the idempotency key', async () => {
  const client = new ComplyLayer({ apiKey: KEY, fallback: 'allow' });
  await assert.rejects(() => client.check({ customerRef: 'u' }), ComplyLayerError);
});
