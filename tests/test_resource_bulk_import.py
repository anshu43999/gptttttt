from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from application.resource_pool_service import ResourcePoolService
from infrastructure.repositories.resource_pool_repository import ResourcePoolRepository


def _outlook_line(i: int) -> str:
    return f"user{i}@outlook.com----pass{i}----client-id----refresh-token-{i}"


def test_import_many_3000_outlook_tokens_is_fast(tmp_path: Path) -> None:
    text = "\n".join(_outlook_line(i) for i in range(3000))
    svc = ResourcePoolService(ResourcePoolRepository(tmp_path / "pool.db"))
    import time

    t0 = time.perf_counter()
    count = svc.import_outlook_tokens(text)
    elapsed = time.perf_counter() - t0
    assert count == 3000
    # Pre-fix baseline was ~110s for 3000 rows; bulk path must stay interactive.
    assert elapsed < 5.0, f"bulk import too slow: {elapsed:.3f}s"
    # Existing keys are skipped without re-insert.
    assert svc.import_outlook_tokens(text) == 0


def test_import_resources_file_path(tmp_path: Path) -> None:
    from main import app
    from api.resources import get_resource_pool_service

    db_path = tmp_path / "pool.db"
    svc = ResourcePoolService(ResourcePoolRepository(db_path))
    order = tmp_path / "order.txt"
    order.write_text(
        "\n".join(_outlook_line(i) for i in range(50)) + "\n",
        encoding="utf-8",
    )
    app.dependency_overrides[get_resource_pool_service] = lambda: svc
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/api/resources/import",
                json={
                    "resource_type": "email",
                    "provider": "outlook_token",
                    "file_path": str(order),
                },
            )
    finally:
        app.dependency_overrides.pop(get_resource_pool_service, None)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 50
