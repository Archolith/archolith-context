"""Tests for admin configuration endpoint."""

import pytest


@pytest.mark.asyncio
async def test_update_config_bool_coercion_true(client):
    """PATCH /admin/config correctly coerces string 'true' to bool True."""
    response = await client.patch(
        "/admin/config",
        json={"embedding_enabled": "true"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["updated"]["embedding_enabled"] is True
    assert len(data["rejected"]) == 0


@pytest.mark.asyncio
async def test_update_config_bool_coercion_false(client):
    """PATCH /admin/config correctly coerces string 'false' to bool False."""
    response = await client.patch(
        "/admin/config",
        json={"embedding_enabled": "false"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["updated"]["embedding_enabled"] is False
    assert len(data["rejected"]) == 0


@pytest.mark.asyncio
async def test_update_config_bool_coercion_1(client):
    """PATCH /admin/config correctly coerces string '1' to bool True."""
    response = await client.patch(
        "/admin/config",
        json={"embedding_enabled": "1"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["updated"]["embedding_enabled"] is True


@pytest.mark.asyncio
async def test_update_config_bool_coercion_0(client):
    """PATCH /admin/config correctly coerces string '0' to bool False."""
    response = await client.patch(
        "/admin/config",
        json={"embedding_enabled": "0"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["updated"]["embedding_enabled"] is False


@pytest.mark.asyncio
async def test_update_config_bool_invalid_string(client):
    """PATCH /admin/config rejects invalid boolean strings."""
    response = await client.patch(
        "/admin/config",
        json={"embedding_enabled": "invalid"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "embedding_enabled" in data["rejected"]
    assert "Cannot parse" in data["rejected"]["embedding_enabled"]


@pytest.mark.asyncio
async def test_update_config_float_preserved(client):
    """PATCH /admin/config preserves float types correctly."""
    response = await client.patch(
        "/admin/config",
        json={"assembly_min_savings_ratio": 0.5}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["updated"]["assembly_min_savings_ratio"] == 0.5
    assert isinstance(data["updated"]["assembly_min_savings_ratio"], float)


@pytest.mark.asyncio
async def test_update_config_rejects_deprecated_synthetic_tools_toggle(client):
    """PATCH /admin/config no longer re-enables the deprecated synthetic tools feature."""
    response = await client.patch(
        "/admin/config",
        json={"synthetic_tools_enabled": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert "synthetic_tools_enabled" not in data["updated"]
    assert data["rejected"]["synthetic_tools_enabled"] == "not a tunable field"


@pytest.mark.asyncio
async def test_retry_promotion_returns_501(client, app):
    """POST /promotions/retry/{id} returns 501 when the promotion is found (stub)."""
    from types import SimpleNamespace

    # Inject a promotion service whose audit trail contains the requested id so
    # the endpoint reaches the found-but-not-implemented (501) path rather than
    # the 503 (no service) / 404 (not found) early returns.
    app.state.promotion_service = SimpleNamespace(
        audit_trail=[SimpleNamespace(promotion_id="known-id")]
    )
    try:
        response = await client.post("/promotions/retry/known-id")
        assert response.status_code == 501
        data = response.json()
        assert data.get("error") == "Not Implemented"
    finally:
        app.state.promotion_service = None


@pytest.mark.asyncio
async def test_retry_promotion_unknown_id_returns_404(client, app):
    """POST /promotions/retry/{id} returns 404 when the id is not in the audit trail."""
    from types import SimpleNamespace

    app.state.promotion_service = SimpleNamespace(audit_trail=[])
    try:
        response = await client.post("/promotions/retry/nonexistent-id")
        assert response.status_code == 404
        assert "not found" in response.json().get("error", "").lower()
    finally:
        app.state.promotion_service = None
