"""Signed checkpoints: the anchor the hash chain does not have on its own.

§8.3 lists four mechanisms and this is the honest hole in them. Grants stop the
application writing; the trigger stops anyone at a psql prompt; the chain catches
a record altered in place. None of them catch the case that matters most to an
auditor: somebody with write access who edits a record **and recomputes every
hash after it**. The chain is unkeyed SHA-256, so recomputing it is arithmetic,
and the result verifies perfectly.

A checkpoint closes that by signing the chain head with a key that is not in the
database. An attacker who rewrites history can produce a consistent chain; they
cannot produce a signature over it without the private key, and the mismatch is
what an auditor sees.

Three things this deliberately does not do:

- **It does not sign every record.** One signature per checkpoint over the head
  hash covers everything before it, because the head hash already covers
  everything before it. Signing each record would be the same guarantee at
  thousands of times the cost.
- **It does not hold the private key.** The signer takes it as an argument. The
  key belongs in a secret manager or an HSM, and a module that reaches for a
  global is a module that ends up with the key in a database.
- **It does not pretend to be anchored when it is not.** With no key configured,
  `verify_checkpoint` returns UNANCHORED rather than True. A verification that
  cannot fail is a verification that means nothing, and this one goes to a
  customer's auditor.

The interval between checkpoints is the window an attacker can rewrite
undetected. That is a real limit and it is stated rather than papered over: a
tamper at 10:05 against a chain last signed at 10:00 is invisible until the next
checkpoint runs, at which point it is permanent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class Anchoring(StrEnum):
    """What a verification can actually say about the chain being anchored."""

    SIGNED = "signed"
    UNANCHORED = "unanchored"
    BROKEN = "broken"


@dataclass(frozen=True)
class CheckpointResult:
    anchoring: Anchoring
    checked_at: datetime | None = None
    chain_length: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        """UNANCHORED is not ok. It is the absence of the guarantee, and an
        auditor reading `ok` must not be told yes because nothing was checked."""
        return self.anchoring is Anchoring.SIGNED


def generate_key() -> tuple[str, str]:
    """Return (private_pem, public_pem). Run once, keep the private half out of
    the database and out of the repository."""
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def canonical_checkpoint(
    *, tenant_id: str, chain_length: int, head_hash: str, signed_at: datetime
) -> bytes:
    """The exact bytes the signature covers.

    Sorted keys and explicit separators, for the reason `canonical_payload` in
    chain.py gives: a signature whose input depends on dict ordering verifies
    today and fails after a Python upgrade.

    `chain_length` is in here as well as the head hash. Without it, a chain
    truncated back to an earlier signed head would verify — the attacker simply
    deletes everything after a checkpoint and presents an older signature as
    current.
    """
    return json.dumps(
        {
            "tenant_id": tenant_id,
            "chain_length": chain_length,
            "head_hash": head_hash,
            "signed_at": signed_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def sign(private_pem: str, **fields: Any) -> str:
    """Sign a chain head. Returns the signature, hex encoded."""
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("the checkpoint signing key must be Ed25519")
    return key.sign(canonical_checkpoint(**fields)).hex()


def verify_signature(public_pem: str, signature: str, **fields: Any) -> bool:
    key = serialization.load_pem_public_key(public_pem.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("the checkpoint verifying key must be Ed25519")
    try:
        key.verify(bytes.fromhex(signature), canonical_checkpoint(**fields))
    except (InvalidSignature, ValueError):
        return False
    return True
