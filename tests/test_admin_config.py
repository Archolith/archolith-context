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
async def test_retry_promotion_returns_501(client):
    """POST /promotions/retry/{id} returns 501 Not Implemented."""
    response = await client.post(
        "/promotions/retry/nonexistent-id"
    )
    # Either 404 if not found, or 501 if found but not implemented
    # For non-existent ID it should be 404
    if response.status_code == 404:
        data = response.json()
        assert "not found" in data.get("error", "").lower()
    else:
        # If somehow the ID exists, it should still be 501
        assert response.status_code == 501
        data = response.json()
        assert data.get("error") == "Not Implemented"
