"""Fenced synthetic refund verification on fresh and upgraded PostgreSQL."""

import asyncio
import os
from contextlib import asynccontextmanager
from hmac import digest
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncTransaction
from synthetic_refund_reconciliation import RefundReconciler
from test_durable_execution_schema import IMAGE
from test_durable_execution_schema import sandbox as sandbox
from test_execution_worker_postgres import expire_synthetic_lease, snapshot
from test_synthetic_refund_postgres import current_owner, effect, scenario, services
from test_synthetic_refund_postgres import upgraded as upgraded

from gateway.execution_contracts import ExecutionState
from gateway.execution_store import CommandStoreError
from gateway.execution_worker_store import synthetic_evidence_bytes
from shared.database import execute

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)


class SimulatedCrash(Exception):
    pass


def attest(evidence, key):
    return evidence.model_copy(
        update={
            "attestation": digest(key, synthetic_evidence_bytes(evidence), "sha256")
        }
    )


@asynccontextmanager
async def parked(upgraded, monkeypatch, *, seed=True, committed=True):
    async with scenario(upgraded, seed=seed) as (db, store, ledger, flow, ident, keys):
        _, approvals, _, _ = services(db, keys)
        lookup = ledger.evidence
        intents = []

        async def crash_after_effect(intent):
            intents.append(intent)
            raise SimulatedCrash

        async def crash_before_effect():
            raise SimulatedCrash

        with monkeypatch.context() as patch:
            patch.setattr(ledger, "evidence", crash_after_effect)
            with pytest.raises(SimulatedCrash):
                if committed:
                    await flow.run_once(uuid4())
                else:
                    await flow.run_once(uuid4(), after_consumption=crash_before_effect)
        sender = await current_owner(db, ident)
        assert not await store.recover_expired_synthetic_refund(sender)
        await expire_synthetic_lease(db, ident)
        assert await store.recover_expired_synthetic_refund(sender)
        assert not await store.recover_expired_synthetic_refund(sender)
        state, [send] = await snapshot(db, ident)
        assert state["status"] == state["state"] == "UNKNOWN"
        assert state["command_row"]["next_check_at"]
        assert send["kind"] == "SEND" and send["outcome"] == "UNKNOWN"
        assert send["send_marked_at"] is not None
        assert state["receipts"] == 0
        yield db, store, ledger, approvals, flow, sender, send, lookup, intents, keys


async def check_terminal(db, sender, send, expected):
    state, attempts = await snapshot(db, sender.execution_id)
    assert state["status"] == state["state"] == expected
    assert state["lease_owner"] is None
    assert state["command_row"]["next_check_at"] is None
    assert attempts[0] == send
    assert all(
        attempt["kind"] == "VERIFY" and attempt["send_marked_at"] is None
        for attempt in attempts[1:]
    )
    assert attempts[-1]["outcome"] == expected
    assert sum(attempt["outcome"] == expected for attempt in attempts) == 1
    assert state["receipts"] == 1
    receipt = state["receipt_row"]
    assert receipt["generation"] == attempts[-1]["generation"]
    assert receipt["outcome"] == expected
    assert receipt["external_reference"] == f"synthetic:{sender.execution_id}"
    assert receipt["evidence_source"] == "LOOKUP"


async def make_due(db, ident):
    async with db.begin() as conn:
        await execute(
            conn,
            """
            UPDATE execution_commands SET next_check_at=clock_timestamp()
            WHERE execution_id=:id AND next_check_at IS NOT NULL
            """,
            id=ident,
        )


@pytest.mark.parametrize(
    ("seed", "expected"),
    [(True, ExecutionState.SUCCEEDED), (False, ExecutionState.FAILED)],
)
def test_reconciles_terminal_ledger_evidence(upgraded, monkeypatch, seed, expected):
    async def check():
        async with parked(upgraded, monkeypatch, seed=seed) as fixture:
            db, store, _, approvals, flow, sender, send, lookup, intents, _ = fixture
            ident = sender.execution_id
            assert intents[0].owner == sender
            reconciler = RefundReconciler(approvals, store, lookup)
            assert await reconciler.run_once(ident, uuid4()) is expected
            assert await reconciler.run_once(ident, uuid4()) is None
            assert await flow.run_once(uuid4()) is None
            await check_terminal(db, sender, send, expected)
            assert await effect(db, ident) == ((1, 123, 377) if seed else (1, 0, None))

    asyncio.run(check())


def test_missing_then_late_evidence(upgraded, monkeypatch):
    async def check():
        async with parked(upgraded, monkeypatch, committed=False) as fixture:
            db, store, ledger, approvals, flow, sender, send, lookup, _, _ = fixture
            ident = sender.execution_id
            observed = []

            async def observe(intent):
                observed.append(intent)
                return await lookup(intent)

            reconciler = RefundReconciler(approvals, store, observe)
            assert await reconciler.run_once(ident, uuid4()) is ExecutionState.UNKNOWN
            state, [original, verify] = await snapshot(db, ident)
            assert original == send and verify["outcome"] == "UNKNOWN"
            assert state["command_row"]["next_check_at"] and state["receipts"] == 0
            assert state["lease_owner"] is None
            assert await reconciler.run_once(ident, uuid4()) is None
            assert await flow.run_once(uuid4()) is None
            assert await effect(db, ident) == (0, None, 500)
            # Deliver the already-authorized original request after the first lookup.
            assert observed[0].owner == sender
            await ledger.invoke(observed[0])
            await make_due(db, ident)
            assert await reconciler.run_once(ident, uuid4()) is ExecutionState.SUCCEEDED
            assert observed == [observed[0], observed[0]]
            assert await effect(db, ident) == (1, 123, 377)
            await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)

    asyncio.run(check())


def test_competing_claims_and_expired_owner(upgraded, monkeypatch):
    async def check():
        async with parked(upgraded, monkeypatch) as fixture:
            db, store, _, _, _, sender, send, lookup, intents, keys = fixture
            ident = sender.execution_id
            barrier = asyncio.Barrier(4)

            async def claim():
                await barrier.wait()
                return await store.claim_synthetic_refund_verification(ident, uuid4())

            claims = await asyncio.gather(*(claim() for _ in range(4)))
            [expired] = [item for item in claims if item is not None]
            evidence = await lookup(intents[0])
            assert evidence is not None
            assert (
                expired.sender == sender
                and expired.intent_binding == intents[0].binding
            )

            async def at_lease_expiry(_conn):
                return expired.lease.expires_at

            with monkeypatch.context() as patch:
                patch.setattr(store, "_clock", at_lease_expiry)
                assert (
                    await store.finish_synthetic_refund_verification(
                        expired, evidence=evidence
                    )
                    is None
                )
            await expire_synthetic_lease(db, ident)
            replacement = await store.claim_synthetic_refund_verification(
                ident, expired.lease.owner.worker_id
            )
            assert replacement is not None
            assert (
                replacement.lease.owner.generation == expired.lease.owner.generation + 1
            )
            conflicting = attest(
                evidence.model_copy(update={"result": "DEFINITIVE_NO_EFFECT"}),
                keys[-1],
            )
            outcomes = await asyncio.gather(
                store.finish_synthetic_refund_verification(
                    expired, evidence=conflicting
                ),
                store.finish_synthetic_refund_verification(
                    replacement, evidence=evidence
                ),
                store.finish_synthetic_refund_verification(
                    replacement, evidence=evidence
                ),
            )
            assert outcomes.count(ExecutionState.SUCCEEDED) == 1
            assert outcomes.count(None) == 2
            assert (
                await store.read_completed_synthetic_refund_verification(
                    replacement, conflicting
                )
                is None
            )
            assert (
                await store.read_completed_synthetic_refund_verification(
                    expired, evidence
                )
                is None
            )
            await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
            assert await effect(db, ident) == (1, 123, 377)

    asyncio.run(check())


@pytest.mark.parametrize("fault", ["crash", "commit-ack", "rollback"])
def test_verification_crash_or_ambiguous_finalize(upgraded, monkeypatch, fault):
    async def check():
        async with parked(upgraded, monkeypatch) as fixture:
            db, store, _, approvals, _, sender, send, lookup, _, _ = fixture
            ident = sender.execution_id
            reconciler = RefundReconciler(approvals, store, lookup)
            original_finish = store.finish_synthetic_refund_verification
            original_exit = AsyncTransaction.__aexit__

            async def fail_exit(tx, *failure):
                assert failure[0] is None
                if fault == "commit-ack":
                    await original_exit(tx, *failure)
                else:
                    await tx.rollback()
                raise OSError("fixture transaction fault")

            async def interrupt_finish(*args, **kwargs):
                if fault == "crash":
                    raise SimulatedCrash
                with monkeypatch.context() as patch:
                    patch.setattr(AsyncTransaction, "__aexit__", fail_exit)
                    return await original_finish(*args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(
                    store, "finish_synthetic_refund_verification", interrupt_finish
                )
                if fault == "commit-ack":
                    assert (
                        await reconciler.run_once(ident, uuid4())
                        is ExecutionState.SUCCEEDED
                    )
                else:
                    error = SimulatedCrash if fault == "crash" else CommandStoreError
                    with pytest.raises(error):
                        await reconciler.run_once(ident, uuid4())

            if fault != "commit-ack":
                state, [original, verify] = await snapshot(db, ident)
                assert original == send and verify["outcome"] is None
                assert state["status"] == state["state"] == "UNKNOWN"
                assert state["receipts"] == 0
                assert await reconciler.run_once(ident, uuid4()) is None
                await expire_synthetic_lease(db, ident)
                assert (
                    await reconciler.run_once(ident, uuid4())
                    is ExecutionState.SUCCEEDED
                )
            await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
            assert await effect(db, ident) == (1, 123, 377)

    asyncio.run(check())


@pytest.mark.parametrize(
    "fault", ["wrapping-key", "intent", "operation", "attestation"]
)
def test_invalid_lookup_defers_without_terminal_receipt(upgraded, monkeypatch, fault):
    async def check():
        async with parked(upgraded, monkeypatch) as fixture:
            db, store, _, approvals, _, sender, send, lookup, _, keys = fixture
            ident = sender.execution_id
            wrong_approvals = services(db, [os.urandom(32), *keys[1:]])[1]
            calls = []

            async def bad_lookup(intent):
                calls.append(intent)
                evidence = await lookup(intent)
                assert evidence is not None
                update = {
                    "intent": {"intent_binding": bytes(32)},
                    "operation": {"operation_id": uuid4()},
                    "attestation": {"attestation": bytes(32)},
                }
                invalid = evidence.model_copy(update=update[fault])
                return invalid if fault == "attestation" else attest(invalid, keys[-1])

            reconciler = RefundReconciler(
                wrong_approvals if fault == "wrapping-key" else approvals,
                store,
                bad_lookup,
            )
            assert await reconciler.run_once(ident, uuid4()) is ExecutionState.UNKNOWN
            assert len(calls) == (0 if fault == "wrapping-key" else 1)
            state, [original, verify] = await snapshot(db, ident)
            assert original == send and verify["outcome"] == "UNKNOWN"
            assert state["receipts"] == 0 and state["lease_owner"] is None
            assert state["command_row"]["next_check_at"]
            reason = "UNAVAILABLE" if fault == "wrapping-key" else "MISMATCH"
            assert (
                state["audit_rows"][-1]["action"]
                == f"SYNTHETIC_VERIFY_DEFERRED_{reason}"
            )
            await make_due(db, ident)
            good = RefundReconciler(approvals, store, lookup)
            assert await good.run_once(ident, uuid4()) is ExecutionState.SUCCEEDED
            await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
            assert await effect(db, ident) == (1, 123, 377)

    asyncio.run(check())
