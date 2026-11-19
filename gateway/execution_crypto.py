"""Authenticated payload envelopes and domain-separated command digests."""

import json
from dataclasses import dataclass
from hashlib import sha256
from hmac import digest
from os import urandom
from typing import Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gateway.execution_contracts import ProtectedIntentScope, SealedPayload

NONCE_BYTES = 12
KEY_BYTES = 32


class PayloadUnavailable(Exception):
    """Payload or key unavailable, without sensitive diagnostic details."""


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class PayloadContext:
    record_type: str
    execution_id: UUID
    scope: ProtectedIntentScope
    schema_version: int = 1

    def associated_data(self) -> bytes:
        return canonical_json(
            [
                "toolmesh.execution.payload",
                self.record_type,
                str(self.execution_id),
                self.scope.model_dump(mode="json"),
                self.schema_version,
            ]
        )


class WrappingKeyProvider(Protocol):
    """Wrap data keys without requiring export of the provider's wrapping key."""

    def wrap(self, data_key: bytes, context: bytes) -> tuple[int, bytes]: ...

    def unwrap(self, version: int, wrapped_key: bytes, context: bytes) -> bytes: ...


class SandboxWrappingKeys:
    """In-memory provider. The sandbox caller injects keys from outside the database."""

    def __init__(self, keys: dict[int, bytes], active_version: int):
        if (
            active_version not in keys
            or any(
                version < 1 or len(key) != KEY_BYTES for version, key in keys.items()
            )
            or len(set(keys.values())) != len(keys)
        ):
            raise ValueError("invalid sandbox wrapping-key configuration")
        self._keys = dict(keys)
        self._active_version = active_version

    @staticmethod
    def _binding(version: int, context: bytes) -> bytes:
        return f"toolmesh.execution.dek:{version}:".encode() + context

    def wrap(self, data_key: bytes, context: bytes) -> tuple[int, bytes]:
        version = self._active_version
        nonce = urandom(NONCE_BYTES)
        wrapped = AESGCM(self._keys[version]).encrypt(
            nonce, data_key, self._binding(version, context)
        )
        return version, nonce + wrapped

    def unwrap(self, version: int, wrapped_key: bytes, context: bytes) -> bytes:
        try:
            return AESGCM(self._keys[version]).decrypt(
                wrapped_key[:NONCE_BYTES],
                wrapped_key[NONCE_BYTES:],
                self._binding(version, context),
            )
        except (KeyError, InvalidTag, ValueError):
            raise PayloadUnavailable("EXECUTION_PAYLOAD_UNAVAILABLE") from None


class PayloadProtection:
    def __init__(self, provider: WrappingKeyProvider):
        self._provider = provider

    def seal(self, plaintext: bytes, context: PayloadContext) -> SealedPayload:
        aad = context.associated_data()
        data_key = AESGCM.generate_key(bit_length=256)
        nonce = urandom(NONCE_BYTES)
        version, wrapped_key = self._provider.wrap(data_key, aad)
        return SealedPayload(
            ciphertext=AESGCM(data_key).encrypt(nonce, plaintext, aad),
            wrapped_data_key=wrapped_key,
            nonce=nonce,
            key_version=version,
        )

    def open(self, payload: SealedPayload, context: PayloadContext) -> bytes:
        aad = context.associated_data()
        key = self._provider.unwrap(payload.key_version, payload.wrapped_data_key, aad)
        return self._decrypt(payload, aad, key)

    @staticmethod
    def _decrypt(payload: SealedPayload, aad: bytes, key: bytes) -> bytes:
        try:
            if len(key) != KEY_BYTES:
                raise ValueError
            return AESGCM(key).decrypt(payload.nonce, payload.ciphertext, aad)
        except (InvalidTag, ValueError):
            raise PayloadUnavailable("EXECUTION_PAYLOAD_UNAVAILABLE") from None

    def rewrap(self, payload: SealedPayload, context: PayloadContext) -> SealedPayload:
        aad = context.associated_data()
        key = self._provider.unwrap(payload.key_version, payload.wrapped_data_key, aad)
        self._decrypt(payload, aad, key)
        version, wrapped_key = self._provider.wrap(key, aad)
        return SealedPayload(
            ciphertext=payload.ciphertext,
            nonce=payload.nonce,
            wrapped_data_key=wrapped_key,
            key_version=version,
        )


class CommandDigests:
    """One sandbox digest key set. Rotation requires a separate reservation migration."""

    version = 1

    def __init__(self, lookup_key: bytes, intent_key: bytes):
        if (
            len(lookup_key) != KEY_BYTES
            or len(intent_key) != KEY_BYTES
            or lookup_key == intent_key
        ):
            raise ValueError("independent 256-bit digest keys required")
        self._lookup_key = lookup_key
        self._intent_key = intent_key

    def lookup(self, scope: ProtectedIntentScope, key: str) -> bytes:
        return digest(
            self._lookup_key,
            canonical_json(["toolmesh.execution.key.v1", scope.model_dump(), key]),
            sha256,
        )

    def legacy_alias(self, requester: str, tool_id: str, key: str) -> bytes:
        return digest(
            self._lookup_key,
            canonical_json(
                ["toolmesh.execution.legacy-key.v1", requester, tool_id, key]
            ),
            sha256,
        )

    def intent(self, canonical_intent: bytes) -> bytes:
        return digest(
            self._intent_key,
            b"toolmesh.execution.intent.v1\0" + canonical_intent,
            sha256,
        )
