"""Unit tests for EtherCAT discovery module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from webserver.discovery.ethercat_discovery import (
    DiscoveryStatus,
    EtherCATDevice,
    EtherCATScanResult,
    EtherCATValidationResult,
    list_network_interfaces,
    scan_network,
    validate_config,
)
from webserver.discovery.ethercat_discovery import (
    test_connection as ethercat_test_connection,
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


class TestScanNetwork:
    """Tests for scan_network function."""

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    @patch("webserver.discovery.ethercat_discovery._run_discovery_script")
    def test_scan_no_slaves(self, mock_run_script, mock_available):
        """Test scan with no slaves found."""
        mock_available.return_value = True
        mock_run_script.return_value = {
            "status": "success",
            "devices": [],
            "message": "No EtherCAT slaves found on the network",
            "scan_time_ms": 150,
            "interface": "eth0",
        }

        result = scan_network("eth0")

        assert result.status == DiscoveryStatus.SUCCESS
        assert len(result.devices) == 0
        assert "no" in result.message.lower()

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    @patch("webserver.discovery.ethercat_discovery._run_discovery_script")
    def test_scan_with_slaves(self, mock_run_script, mock_available):
        """Test scan with slaves found."""
        mock_available.return_value = True
        mock_run_script.return_value = {
            "status": "success",
            "devices": [
                {
                    "position": 1,
                    "name": "EK1100",
                    "vendor_id": 2,
                    "product_code": 72100946,
                    "revision": 1114112,
                    "serial_number": 0,
                    "config_address": 4097,
                    "alias": 0,
                    "state": "INIT",
                    "al_status_code": 0,
                    "has_coe": True,
                    "input_bytes": 0,
                    "output_bytes": 0,
                },
                {
                    "position": 2,
                    "name": "EL1008",
                    "vendor_id": 2,
                    "product_code": 66387026,
                    "revision": 1048576,
                    "serial_number": 0,
                    "config_address": 4098,
                    "alias": 0,
                    "state": "INIT",
                    "al_status_code": 0,
                    "has_coe": True,
                    "input_bytes": 1,
                    "output_bytes": 0,
                },
            ],
            "message": "Found 2 EtherCAT slave(s)",
            "scan_time_ms": 250,
            "interface": "eth0",
        }

        result = scan_network("eth0")

        assert result.status == DiscoveryStatus.SUCCESS
        assert len(result.devices) == 2
        assert result.devices[0].name == "EK1100"
        assert result.devices[0].vendor_id == 2
        assert result.devices[1].name == "EL1008"

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    @patch("webserver.discovery.ethercat_discovery._run_discovery_script")
    def test_scan_permission_denied(self, mock_run_script, mock_available):
        """Test scan with permission denied."""
        mock_available.return_value = True
        mock_run_script.return_value = {
            "status": "permission_denied",
            "devices": [],
            "message": "Permission denied. Run with CAP_NET_RAW capability or as root.",
            "scan_time_ms": 5,
            "interface": "eth0",
        }

        result = scan_network("eth0")

        assert result.status == DiscoveryStatus.PERMISSION_DENIED
        assert "permission" in result.message.lower()

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    @patch("webserver.discovery.ethercat_discovery._run_discovery_script")
    def test_scan_interface_not_found(self, mock_run_script, mock_available):
        """Test scan with interface not found."""
        mock_available.return_value = True
        mock_run_script.return_value = {
            "status": "interface_not_found",
            "devices": [],
            "message": "Network interface 'eth99' not found",
            "scan_time_ms": 3,
            "interface": "eth99",
        }

        result = scan_network("eth99")

        assert result.status == DiscoveryStatus.INTERFACE_NOT_FOUND
        assert "not found" in result.message.lower()

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    def test_scan_discovery_not_available(self, mock_available):
        """Test scan when discovery venv is not available."""
        mock_available.return_value = False

        result = scan_network("eth0")

        assert result.status == DiscoveryStatus.NOT_AVAILABLE
        assert "not available" in result.message.lower()

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    @patch("webserver.discovery.ethercat_discovery._run_discovery_script")
    def test_scan_returns_device_info(self, mock_run_script, mock_available):
        """Test that scan returns complete device information."""
        mock_available.return_value = True
        mock_run_script.return_value = {
            "status": "success",
            "devices": [
                {
                    "position": 1,
                    "name": "EK1100",
                    "vendor_id": 2,
                    "product_code": 72100946,
                    "revision": 1114112,
                    "serial_number": 0,
                    "config_address": 4097,
                    "alias": 0,
                    "state": "INIT",
                    "al_status_code": 0,
                    "has_coe": True,
                    "input_bytes": 0,
                    "output_bytes": 0,
                },
            ],
            "message": "Found 1 EtherCAT slave(s)",
            "scan_time_ms": 200,
            "interface": "eth0",
        }

        result = scan_network("eth0")

        device = result.devices[0]
        assert device.position == 1
        assert device.name == "EK1100"
        assert device.vendor_id == 2
        assert device.product_code == 72100946
        assert device.state == "INIT"
        assert device.has_coe is True


class TestTestConnection:
    """Tests for ethercat_test_connection function."""

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    @patch("webserver.discovery.ethercat_discovery._run_discovery_script")
    def test_connection_success(self, mock_run_script, mock_available):
        """Test successful connection to a slave."""
        mock_available.return_value = True
        mock_run_script.return_value = {
            "status": "success",
            "connected": True,
            "device": {
                "position": 1,
                "name": "EK1100",
                "vendor_id": 2,
                "product_code": 72100946,
                "revision": 0,
                "serial_number": 0,
                "config_address": 4097,
                "alias": 0,
                "state": "INIT",
                "al_status_code": 0,
                "has_coe": True,
                "input_bytes": 0,
                "output_bytes": 0,
            },
            "message": "Successfully connected to EK1100 at position 1",
            "response_time_ms": 120,
        }

        result = ethercat_test_connection("eth0", 1)

        assert result.status == DiscoveryStatus.SUCCESS
        assert result.connected is True
        assert result.device is not None
        assert result.device.name == "EK1100"

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    @patch("webserver.discovery.ethercat_discovery._run_discovery_script")
    def test_connection_device_not_found(self, mock_run_script, mock_available):
        """Test connection to non-existent device position."""
        mock_available.return_value = True
        mock_run_script.return_value = {
            "status": "error",
            "connected": False,
            "device": None,
            "message": "No device at position 5. Found 2 slave(s).",
            "response_time_ms": 150,
        }

        result = ethercat_test_connection("eth0", 5)

        assert result.status == DiscoveryStatus.ERROR
        assert result.connected is False
        assert result.device is None

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    def test_connection_invalid_position(self, mock_available):
        """Test connection with invalid position."""
        mock_available.return_value = True

        result = ethercat_test_connection("eth0", 0)

        assert result.status == DiscoveryStatus.ERROR
        assert "position" in result.message.lower()

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    def test_connection_discovery_not_available(self, mock_available):
        """Test connection when discovery venv is not available."""
        mock_available.return_value = False

        result = ethercat_test_connection("eth0", 1)

        assert result.status == DiscoveryStatus.NOT_AVAILABLE


class TestListNetworkInterfaces:
    """Tests for list_network_interfaces function."""

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    @patch("webserver.discovery.ethercat_discovery._run_discovery_script")
    def test_list_interfaces_success(self, mock_run_script, mock_available):
        """Test listing network interfaces."""
        mock_available.return_value = True
        mock_run_script.return_value = {
            "status": "success",
            "interfaces": [
                {"name": "eth0", "description": "Ethernet adapter"},
                {"name": "enp3s0", "description": "PCI Ethernet"},
            ],
            "message": "Found 2 network interface(s)",
        }

        result = list_network_interfaces()

        assert result["status"] == "success"
        assert len(result["interfaces"]) == 2
        assert result["interfaces"][0]["name"] == "eth0"

    @patch("webserver.discovery.ethercat_discovery.is_discovery_available")
    def test_list_interfaces_not_available(self, mock_available):
        """Test listing interfaces when discovery not available."""
        mock_available.return_value = False

        result = list_network_interfaces()

        assert result["status"] == "not_available"


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

    def test_ethercat_scan_result_defaults(self):
        """Test EtherCATScanResult default values."""
        result = EtherCATScanResult(status=DiscoveryStatus.SUCCESS)

        assert result.status == DiscoveryStatus.SUCCESS
        assert result.devices == []
        assert result.message == ""

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
