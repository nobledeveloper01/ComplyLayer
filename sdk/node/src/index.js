/**
 * ComplyLayer for Node.
 *
 * Two decisions in this file are deliberate and both will annoy somebody:
 *
 * `fallback` is a required constructor argument with no default. Forcing the
 * choice at integration time is better than a silent default whose meaning a
 * fintech discovers during an outage. It is three characters to type and it is
 * the difference between "we chose to let payments through" and "we did not
 * know we had chosen".
 *
 * A `block` throws. Everything else returns. `flag` means proceed — the
 * transaction is already in the compliance review queue — and an SDK that made
 * callers remember which outcomes are fatal would eventually meet one who did
 * not.
 */

export class ComplianceBlockedError extends Error {
  constructor(message, decisionId, matchedRules) {
    super(message);
    this.name = 'ComplianceBlockedError';
    this.decisionId = decisionId;
    this.matchedRules = matchedRules;
  }
}

export class ComplyLayerError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ComplyLayerError';
    this.status = status;
  }
}

const FALLBACKS = new Set(['allow', 'block']);

export class ComplyLayer {
  /**
   * @param {object} options
   * @param {string} options.apiKey
   * @param {'allow'|'block'} options.fallback  Required. What to do when
   *   ComplyLayer cannot be reached. There is no default, on purpose.
   * @param {string} [options.baseUrl]
   * @param {number} [options.timeout] Milliseconds. Defaults to 150, matching
   *   the server's own hard timeout — a client that waits longer than the
   *   server will is a client that waits for nothing.
   */
  constructor({ apiKey, fallback, baseUrl = 'https://api.complylayer.dev', timeout = 150 } = {}) {
    if (!apiKey) {
      throw new ComplyLayerError('apiKey is required.');
    }
    if (!FALLBACKS.has(fallback)) {
      throw new ComplyLayerError(
        "fallback is required and must be 'allow' or 'block'. There is no default: " +
          'this is what happens to your transactions when ComplyLayer is unreachable, ' +
          'and it should be a decision you made rather than one you inherited.',
      );
    }

    this.apiKey = apiKey;
    this.fallback = fallback;
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.timeout = timeout;
  }

  /**
   * Decide whether a transaction may proceed.
   *
   * Throws ComplianceBlockedError on a block. Returns the decision otherwise,
   * including on a flag — a flagged transaction proceeds and is reviewed.
   */
  async check(transaction) {
    const reference = transaction.transactionRef;
    if (!reference) {
      throw new ComplyLayerError('transactionRef is required.');
    }

    let decision;
    try {
      decision = await this.#post(reference, toPayload(transaction));
    } catch (error) {
      // The fallback path. Marked so a caller can count these — a sustained
      // degraded rate is an incident, and the server cannot see the ones that
      // never reached it.
      decision = {
        outcome: this.fallback,
        degraded: true,
        reason: `ComplyLayer was unreachable: ${error.message}`,
        matchedRules: [],
        decisionId: null,
      };
    }

    if (decision.outcome === 'block') {
      throw new ComplianceBlockedError(
        decision.customerMessage || 'This transaction cannot be completed.',
        decision.decisionId,
        decision.matchedRules,
      );
    }
    return decision;
  }

  async #post(idempotencyKey, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);

    try {
      const response = await fetch(`${this.baseUrl}/v1/decisions`, {
        method: 'POST',
        signal: controller.signal,
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${this.apiKey}`,
          // Always sent. The server requires it, and a retry that produced a
          // second decision would give one transaction two compliance records.
          'Idempotency-Key': idempotencyKey,
        },
        body: JSON.stringify(body),
      });

      if (!response.ok) {
        const detail = await response.text();
        throw new ComplyLayerError(
          `ComplyLayer returned ${response.status}: ${detail.slice(0, 200)}`,
          response.status,
        );
      }
      return fromPayload(await response.json());
    } finally {
      clearTimeout(timer);
    }
  }
}

/** camelCase in, snake_case out. The wire format is the server's, not JavaScript's. */
function toPayload(transaction) {
  const payload = {
    transaction_ref: transaction.transactionRef,
    customer_ref: transaction.customerRef,
    amount_minor: transaction.amountMinor,
    currency: transaction.currency,
  };
  if (transaction.transactionType) payload.transaction_type = transaction.transactionType;
  if (transaction.channel) payload.channel = transaction.channel;

  if (transaction.customer) {
    payload.customer = {};
    const { kycTier, accountCreatedAt, lastTransactionAt, country } = transaction.customer;
    if (kycTier !== undefined) payload.customer.kyc_tier = kycTier;
    if (accountCreatedAt) payload.customer.account_created_at = asIso(accountCreatedAt);
    if (lastTransactionAt) payload.customer.last_transaction_at = asIso(lastTransactionAt);
    if (country) payload.customer.country = country;
  }

  if (transaction.destination) {
    payload.destination = {};
    const { country, bankCode, isNewBeneficiary } = transaction.destination;
    if (country) payload.destination.country = country;
    if (bankCode) payload.destination.bank_code = bankCode;
    if (isNewBeneficiary !== undefined) payload.destination.is_new_beneficiary = isNewBeneficiary;
  }

  if (transaction.device) {
    payload.device = {};
    if (transaction.device.id) payload.device.id = transaction.device.id;
    if (transaction.device.ipCountry) payload.device.ip_country = transaction.device.ipCountry;
  }

  return payload;
}

function fromPayload(body) {
  return {
    decisionId: body.decision_id,
    outcome: body.outcome,
    reason: body.reason,
    matchedRules: (body.matched_rules || []).map((rule) => ({
      id: rule.id,
      name: rule.name,
      severity: rule.severity,
      regulatoryReference: rule.regulatory_reference,
    })),
    evaluatedRules: body.evaluated_rules,
    rulesetVersion: body.ruleset_version,
    latencyMs: body.latency_ms,
    degraded: body.degraded,
    decidedAt: body.decided_at,
    customerMessage: body.customer_message,
  };
}

function asIso(value) {
  return value instanceof Date ? value.toISOString() : value;
}
