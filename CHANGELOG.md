# Changelog

Versions follow [semantic versioning](https://semver.org). The Python package,
the Node SDK and the container image share one version and are released from one
tag, so a client never has to be matched against a server by hand.

`0.x` means the HTTP contract is not frozen yet. The decision envelope
(`decision`, `reasons`, `rule_version`, `decision_id`) is stable and is what the
SDKs depend on; everything around it may still move.

## 0.1.0

First release. Nine phases, 1015 tests, and a decision path that has been run
rather than only tested.

### The engine

- A rule DSL that is parsed and walked, never `eval`ed — enforced at build time
  by `scripts/no_eval_guard.py` and explained in ADR-0001. Every addition to
  `ALLOWED_NODES` or `ALLOWED_FUNCTIONS` needs a matching entry in the escape
  corpus, which is a blocking gate.
- Deterministic evaluation: the same facts and the same `rule_version` give the
  same decision, asserted by `tests/test_determinism.py`.
- Velocity rules backed by Redis, with the provider injected so the evaluation
  stage stays pure.
- Rule changes go through a proposed/approved lifecycle with a diff that is
  shown in currency units rather than as a text diff.

### Tenancy

- Postgres row-level security on every table carrying a `tenant_id`, with
  `FORCE` so the owner is bound too, and a non-superuser application role.
- `tests/test_rls_every_table.py` compares the models that have a tenant against
  the policies in the migrations, so a new table cannot be added without one.
- Two tables establish tenancy and so cannot be scoped by it — API keys and
  dashboard users. Both resolve through a `SET`-scoped policy flag and a
  resolver function rather than `SECURITY DEFINER`, which `FORCE` would defeat.

### Audit

- An append-only decision log: `UPDATE` and `DELETE` are refused by trigger, not
  by convention.
- A SHA-256 hash chain anchored by Ed25519-signed checkpoints, so the chain is
  keyed rather than merely self-consistent. The canonical form includes the
  chain length, so truncation is detected as well as mutation.

### Interfaces

- A decision API authenticated by Argon2id-hashed keys, cached on a digest of
  the whole presented key.
- A dashboard with TOTP second factor, and a rule builder.
- A Node SDK (`@complylayer/node`) that fails closed.

### Operations

- The image runs as an unprivileged user, is built on Debian 13, applies
  Debian's security updates at build time, and is scanned twice: a blocking scan
  for anything with a fix available, and a reporting scan that keeps the unfixed
  findings visible rather than dropping them.
- Every GitHub Action is pinned to a commit. The release pipeline can be
  rehearsed by `workflow_dispatch`, which is unconditionally a dry run —
  publishing is gated on a tag push at every step that uploads, signs or logs
  in, and `tests/test_release_workflow.py` fails the build if a new publishing
  step loses that gate.
