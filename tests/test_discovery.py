import csv
import io
import os
import time

import httpx
import pytest

from benchmarks.dataset import corpus, fingerprint, labeled


def test_reproducible_split_fingerprint():
    documents = corpus(600, 42)
    tuning = labeled(documents, 40, 42, "tuning")
    heldout = labeled(documents, 40, 42, "test")
    assert {key for case in tuning for key in case.expected}.isdisjoint(
        key for case in heldout for key in case.expected
    )
    assert fingerprint(documents, heldout) == fingerprint(
        corpus(600, 42), labeled(corpus(600, 42), 40, 42, "test")
    )
    assert fingerprint(documents, tuning) != fingerprint(documents, heldout)


@pytest.mark.skipif(
    not os.getenv("TOOLMESH_TEST_API"), reason="requires configured Compose sandbox"
)
def test_rank_prefilter_and_persisted_evaluation():
    origin = os.environ["TOOLMESH_TEST_API"]
    with httpx.Client(base_url=f"{origin}/api/v1", timeout=10) as client:

        def auth(user, key, fallback):
            response = client.post(
                "/auth/login",
                json={"username": user, "password": os.getenv(key, fallback)},
            )
            response.raise_for_status()
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        admin = auth(
            "platform-admin", "TOOLMESH_ADMIN_PASSWORD", "local_admin_password"
        )
        scoped = auth(
            "payments-agent", "TOOLMESH_PAYMENTS_PASSWORD", "local_payments_password"
        )
        assert client.get("/evaluations", headers=scoped).status_code == 403
        tool = "payments.check_duplicate_payment"
        release = client.post(
            f"/tools/{tool}/publish",
            headers=admin,
            json={"version": 1, "reason": "Test discovery"},
        )
        assert release.status_code == 200, release.text
        program = client.get("/evaluations", headers=admin).json()
        has_model = program["model"]["status"] == "ready"
        if os.getenv("TOOLMESH_TEST_REQUIRE_MODEL") == "1":
            assert has_model, program

        for strategy in ("keyword", "vector", "hybrid"):
            response = client.post(
                "/discovery/search",
                headers=scoped,
                json={"query": "duplicate capture", "strategy": strategy},
            )
            if not has_model and strategy != "keyword":
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "EMBEDDING_MODEL_UNAVAILABLE"
                continue
            assert response.status_code == 200, response.text
            candidates = response.json()
            assert candidates and candidates[0]["id"] == tool, candidates
            assert all(
                item["service_id"] == "payments" and item["risk"] == "read"
                for item in candidates
            )
            assert [item["rank"] for item in candidates] == list(
                range(1, len(candidates) + 1)
            )
            denied = client.post(
                "/discovery/search",
                headers=scoped,
                json={
                    "query": "ticket search",
                    "service_id": "support",
                    "strategy": strategy,
                },
            )
            assert denied.json() == []
        no_runtime = client.get(
            "/discovery/tools/bench.payments.fake.0/schema", headers=admin
        )
        assert no_runtime.status_code == 404
        benchmark = client.post(
            "/evaluations/runs",
            headers=admin,
            json={"catalog_size": 60, "case_count": 10},
        )
        if not has_model:
            assert benchmark.status_code == 503
            return
        assert benchmark.status_code == 202, benchmark.text
        run = benchmark.json()["id"]
        for _ in range(35):
            detail = client.get(f"/evaluations/runs/{run}", headers=admin).json()
            if detail["run"]["status"] not in {"RUNNING", "QUEUED"}:
                break
            time.sleep(0.4)
        assert detail["run"]["status"] == "SUCCEEDED", detail
        summary = detail["run"]["summary"]
        assert set(summary["strategies"]) == {"keyword", "vector", "hybrid"}
        assert summary["all_allowed_schema_bytes"] > 0
        assert summary["full_schema_llm_baseline"] == "not_run"
        exported = client.get(f"/evaluations/runs/{run}/export", headers=admin).json()
        cases = exported["cases"]
        assert len(cases) == 10
        for case in cases:
            if case["kind"] == "denied":
                assert all(
                    not identifier.startswith(f"bench.{case['domain']}.")
                    for ranked in case["rankings"].values()
                    for identifier in ranked
                )
        csv_export = client.get(
            f"/evaluations/runs/{run}/export?format=csv", headers=admin
        )
        assert len(list(csv.DictReader(io.StringIO(csv_export.text)))) == 30
