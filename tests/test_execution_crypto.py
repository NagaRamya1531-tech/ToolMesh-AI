from dataclasses import replace
from os import urandom
from uuid import uuid4

import pytest

from gateway.execution_contracts import ProtectedIntentScope, SealedPayload
from gateway.execution_crypto import (
    CommandDigests,
    PayloadContext,
    PayloadProtection,
    PayloadUnavailable,
    SandboxWrappingKeys,
    canonical_json,
)


def context():
    return PayloadContext(
        "command",
        uuid4(),
        ProtectedIntentScope(
            workspace_id="default",
            requester="finance-agent",
            customer_id="C-102",
            tool_id="payments.issue_refund",
        ),
    )


def test_envelopes_use_independent_random_keys_and_nonces():
    provider = SandboxWrappingKeys({1: urandom(32)}, 1)
    protection = PayloadProtection(provider)
    binding = context()
    opaque = b"synthetic-private-arguments"
    first = protection.seal(opaque, binding)
    second = protection.seal(opaque, binding)
    aad = binding.associated_data()
    assert first.nonce != second.nonce
    assert first.wrapped_data_key[:12] != second.wrapped_data_key[:12]
    assert provider.unwrap(1, first.wrapped_data_key, aad) != provider.unwrap(
        1, second.wrapped_data_key, aad
    )
    assert opaque not in first.ciphertext
    assert protection.open(first, binding) == protection.open(second, binding) == opaque


@pytest.mark.parametrize("field", ["ciphertext", "nonce", "wrapped_data_key"])
def test_authentication_detects_tampering(field):
    provider = SandboxWrappingKeys({1: urandom(32)}, 1)
    protection = PayloadProtection(provider)
    binding = context()
    original = protection.seal(b"synthetic", binding)
    value = getattr(original, field)
    corrupted = SealedPayload.model_validate(
        original.model_dump() | {field: bytes([value[0] ^ 1]) + value[1:]}
    )
    with pytest.raises(PayloadUnavailable):
        protection.open(corrupted, binding)
    with pytest.raises(PayloadUnavailable):
        protection.rewrap(corrupted, binding)


def test_envelopes_reject_other_records_scopes_and_versions():
    protection = PayloadProtection(SandboxWrappingKeys({1: urandom(32)}, 1))
    binding = context()
    envelope = protection.seal(b"synthetic", binding)
    for changed in (
        replace(binding, execution_id=uuid4()),
        replace(binding, record_type="receipt"),
        replace(binding, schema_version=2),
        replace(
            binding, scope=binding.scope.model_copy(update={"customer_id": "C-103"})
        ),
        replace(
            binding, scope=binding.scope.model_copy(update={"workspace_id": "other"})
        ),
    ):
        with pytest.raises(PayloadUnavailable):
            protection.open(envelope, changed)


def test_rewrap_preserves_payload_and_requires_old_key_until_rotated():
    old_key, new_key = urandom(32), urandom(32)
    original = PayloadProtection(SandboxWrappingKeys({1: old_key}, 1))
    rotating = PayloadProtection(SandboxWrappingKeys({1: old_key, 2: new_key}, 2))
    current = PayloadProtection(SandboxWrappingKeys({2: new_key}, 2))
    binding = context()
    old = original.seal(b"synthetic", binding)
    upgraded = rotating.rewrap(old, binding)
    assert upgraded.key_version == 2
    assert upgraded.ciphertext == old.ciphertext
    assert upgraded.nonce == old.nonce
    assert current.open(upgraded, binding) == b"synthetic"
    with pytest.raises(PayloadUnavailable):
        current.open(old, binding)


def test_digest_domains_and_keys_are_independent():
    lookup_key = urandom(32)
    intent_key = urandom(32)
    scope = context().scope
    with pytest.raises(ValueError):
        CommandDigests(lookup_key, lookup_key)
    digests = CommandDigests(lookup_key, intent_key)
    new_intent_key = CommandDigests(lookup_key, urandom(32))
    other_scope = scope.model_copy(update={"customer_id": "C-103"})
    assert digests.lookup(scope, "key") == new_intent_key.lookup(scope, "key")
    assert digests.lookup(scope, "key") != digests.lookup(other_scope, "key")
    assert digests.lookup(scope, "key") != digests.legacy_alias(
        scope.requester, scope.tool_id, "key"
    )
    assert digests.intent(b"intent") != new_intent_key.intent(b"intent")
    assert canonical_json({"z": 1, "a": "x"}) == canonical_json({"a": "x", "z": 1})
