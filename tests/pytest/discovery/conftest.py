"""Fixtures for Discovery Service tests."""

from typing import Any

import pytest


@pytest.fixture
def sample_valid_config() -> dict[str, Any]:
    """Sample valid EtherCAT configuration."""
    return {
        "interface": "eth0",
        "cycle_time_ms": 4,
        "slaves": [
            {
                "position": 1,
                "vendor_id": 2,
                "product_code": 72100946,
                "pdo_mapping": {
                    "inputs": [{"address": "%IW0", "index": 0x6000, "subindex": 1}],
                    "outputs": [{"address": "%QW0", "index": 0x7000, "subindex": 1}],
                },
            }
        ],
    }


@pytest.fixture
def sample_invalid_config_missing_interface() -> dict[str, Any]:
    """Sample invalid config missing interface."""
    return {
        "slaves": [{"position": 1}],
    }


@pytest.fixture
def sample_invalid_config_missing_slaves() -> dict[str, Any]:
    """Sample invalid config missing slaves."""
    return {
        "interface": "eth0",
    }


@pytest.fixture
def sample_invalid_config_bad_position() -> dict[str, Any]:
    """Sample invalid config with bad position."""
    return {
        "interface": "eth0",
        "slaves": [{"position": 0}],  # Invalid: must be >= 1
    }
