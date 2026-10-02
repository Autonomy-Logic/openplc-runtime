# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Unit tests for EtherCAT discovery module."""

from webserver.discovery.ethercat_discovery import (
    EtherCATDevice,
    EtherCATValidationResult,
    validate_config,
)


class TestValidateConfig:
    """Tests for validate_config function."""

    def test_valid_config(self, sample_valid_config):
        """Test validation of a valid configuration."""
        result = validate_config(sample_valid_config)

        assert result.valid is True
        assert len(result.errors) == 0

    def test_missing_interface(self, sample_invalid_config_missing_interface):
        """Test validation catches missing interface."""
        result = validate_config(sample_invalid_config_missing_interface)

        assert result.valid is False
        assert any("interface" in err.lower() for err in result.errors)

    def test_missing_slaves(self, sample_invalid_config_missing_slaves):
        """Test validation catches missing slaves."""
        result = validate_config(sample_invalid_config_missing_slaves)

        assert result.valid is False
        assert any("slaves" in err.lower() for err in result.errors)

    def test_invalid_position(self, sample_invalid_config_bad_position):
        """Test validation catches invalid position."""
        result = validate_config(sample_invalid_config_bad_position)

        assert result.valid is False
        assert any("position" in err.lower() for err in result.errors)

    def test_empty_slaves_warning(self):
        """Test validation warns about empty slaves list."""
        config = {"interface": "eth0", "slaves": []}
        result = validate_config(config)

        assert result.valid is True  # Valid but with warning
        assert any("no slaves" in warn.lower() for warn in result.warnings)

    def test_missing_vendor_id_warning(self):
        """Test validation warns about missing vendor_id."""
        config = {
            "interface": "eth0",
            "slaves": [{"position": 1, "product_code": 123}],
        }
        result = validate_config(config)

        assert result.valid is True  # Valid but with warning
        assert any("vendor_id" in warn.lower() for warn in result.warnings)

    def test_invalid_cycle_time(self):
        """Test validation catches invalid cycle_time_ms."""
        config = {
            "interface": "eth0",
            "slaves": [{"position": 1}],
            "cycle_time_ms": -1,
        }
        result = validate_config(config)

        assert result.valid is False
        assert any("cycle_time_ms" in err.lower() for err in result.errors)

    def test_very_low_cycle_time_warning(self):
        """Test validation warns about very low cycle time."""
        config = {
            "interface": "eth0",
            "slaves": [{"position": 1}],
            "cycle_time_ms": 0.5,
        }
        result = validate_config(config)

        assert result.valid is True
        assert any("preempt_rt" in warn.lower() for warn in result.warnings)

    def test_pdo_mapping_missing_address(self):
        """Test validation catches missing address in PDO mapping."""
        config = {
            "interface": "eth0",
            "slaves": [
                {
                    "position": 1,
                    "pdo_mapping": {
                        "inputs": [{"index": 0x6000}],  # Missing 'address'
                    },
                }
            ],
        }
        result = validate_config(config)

        assert result.valid is False
        assert any("address" in err.lower() for err in result.errors)


class TestDataClasses:
    """Tests for data classes."""

    def test_ethercat_device_defaults(self):
        """Test EtherCATDevice default values."""
        device = EtherCATDevice(position=1, name="Test")

        assert device.position == 1
        assert device.name == "Test"
        assert device.vendor_id == 0
        assert device.state == "UNKNOWN"
        assert device.has_coe is False

    def test_ethercat_validation_result(self):
        """Test EtherCATValidationResult."""
        result = EtherCATValidationResult(
            valid=False,
            errors=["Error 1"],
            warnings=["Warning 1"],
        )

        assert result.valid is False
        assert len(result.errors) == 1
        assert len(result.warnings) == 1
