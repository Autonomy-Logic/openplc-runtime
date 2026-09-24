# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Modbus Master plugin connection management utilities."""

import time
from typing import Literal, Optional, Union

from pymodbus.client import ModbusTcpClient, ModbusSerialClient

TransportType = Literal["tcp", "rtu"]
ParityType = Literal["N", "E", "O"]


class ModbusConnectionManager:  # pylint: disable=too-many-instance-attributes
    """Manages Modbus TCP and RTU connections with retry logic."""

    def __init__(
        self,
        transport: TransportType = "tcp",
        # TCP parameters
        host: str = "127.0.0.1",
        port: int = 502,
        # RTU parameters
        serial_port: str = "",
        baud_rate: int = 9600,
        parity: ParityType = "N",
        stop_bits: int = 1,
        data_bits: int = 8,
        # Common parameters
        timeout_ms: int = 1000,
        slave_id: int = 1,
    ):
        self.transport = transport
        self.timeout = timeout_ms / 1000.0  # Convert to seconds
        self.slave_id = slave_id  # Unit/Slave ID for Modbus operations

        # TCP configuration
        self.host = host
        self.port = port

        # RTU configuration
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.parity = parity
        self.stop_bits = stop_bits
        self.data_bits = data_bits

        # Retry configuration
        self.retry_delay_base = 2.0  # initial delay between attempts (seconds)
        self.retry_delay_max = 30.0  # maximum delay between attempts (seconds)
        self.retry_delay_current = self.retry_delay_base

        # Connection state - is_connected is the authoritative flag for connection health
        # It is set to False when any error occurs, forcing reconnection on next cycle
        self.client: Optional[Union[ModbusTcpClient, ModbusSerialClient]] = None
        self.is_connected = False

    def _create_client(self) -> Union[ModbusTcpClient, ModbusSerialClient]:
        """Create the appropriate Modbus client based on transport type."""
        if self.transport == "tcp":
            return ModbusTcpClient(
                host=self.host,
                port=self.port,
                timeout=self.timeout
            )
        else:  # RTU
            return ModbusSerialClient(
                port=self.serial_port,
                baudrate=self.baud_rate,
                parity=self.parity,
                stopbits=self.stop_bits,
                bytesize=self.data_bits,
                timeout=self.timeout
            )

    def get_connection_info(self) -> str:
        """Return human-readable connection information."""
        if self.transport == "tcp":
            return f"TCP {self.host}:{self.port}"
        else:
            return f"RTU {self.serial_port}@{self.baud_rate}"

    def connect_with_retry(self, stop_event=None) -> bool:
        """
        Attempts to connect to Modbus device with infinite retry.

        Args:
            stop_event: Optional threading.Event to allow early termination

        Returns:
            True if connected successfully, False if interrupted
        """
        retry_count = 0
        conn_info = self.get_connection_info()

        while stop_event is None or not stop_event.is_set():
            try:
                # Create new client if necessary
                if self.client is None or not self.client.connected:
                    if self.client:
                        try:
                            self.client.close()
                        except Exception:
                            pass
                    self.client = self._create_client()

                # Attempt to connect
                if self.client.connect():
                    print(
                        f"(PASS) Connected to {conn_info} (attempt {retry_count + 1})"
                    )
                    self.is_connected = True
                    self.retry_delay_current = self.retry_delay_base  # Reset delay
                    return True

            except Exception as e:
                print(f"(FAIL) Connection attempt {retry_count + 1} to {conn_info} failed: {e}")

            # Increment counter and calculate delay
            retry_count += 1

            # Attempt logging
            if retry_count == 1:
                print(f"Failed to connect to {conn_info}, starting retry attempts...")
            elif retry_count % 10 == 0:  # Log every 10 attempts
                print(f"Connection attempt {retry_count} to {conn_info} failed, continuing retries...")

            # Wait with increasing delay (limited exponential backoff)
            delay = min(self.retry_delay_current, self.retry_delay_max)

            # Sleep in small increments to allow quick stop
            sleep_increments = int(delay * 10)  # 0.1s increments
            for _ in range(sleep_increments):
                if stop_event and stop_event.is_set():
                    return False
                time.sleep(0.1)

            # Increase delay for next attempt (maximum of retry_delay_max)
            self.retry_delay_current = min(self.retry_delay_current * 1.5, self.retry_delay_max)

        return False

    def ensure_connection(self, stop_event=None) -> bool:
        """
        Ensures there is a valid connection, reconnecting if necessary.

        This method checks BOTH the pymodbus client state AND our is_connected flag.
        The is_connected flag is set to False when any communication error occurs,
        which forces a full reconnection even if pymodbus thinks the socket is still open.

        Args:
            stop_event: Optional threading.Event to allow early termination

        Returns:
            True if connection is available, False if interrupted
        """
        # Check if already connected - must check BOTH conditions:
        # 1. is_connected flag (set to False on any error)
        # 2. client.connected (pymodbus internal socket state)
        if self.is_connected and self.client and self.client.connected:
            return True

        # Connection is broken or marked as unhealthy - force full reconnection
        # First, close any existing client to clean up dead sockets
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass  # Ignore errors during cleanup
            self.client = None

        self.is_connected = False

        # Try to reconnect with infinite retry
        return self.connect_with_retry(stop_event)

    def mark_disconnected(self):
        """
        Mark the connection as broken, forcing reconnection on next ensure_connection() call.

        This should be called when any communication error occurs (timeout, broken pipe,
        ModbusIOException, etc.) to ensure the connection is properly re-established.
        """
        self.is_connected = False
        conn_info = self.get_connection_info()
        print(
            f"Connection to {conn_info} marked as disconnected, "
            "will reconnect on next cycle"
        )

    def disconnect(self):
        """Close the connection and clean up resources."""
        conn_info = self.get_connection_info()
        try:
            if self.client:
                self.client.close()
                self.client = None
            self.is_connected = False
            print(f"Disconnected from {conn_info}")
        except Exception as e:
            print(f"(FAIL) Error disconnecting from {conn_info}: {e}")

    def is_healthy(self) -> bool:
        """
        Check if the connection is healthy.

        Returns:
            True if connection is active and healthy
        """
        return self.client is not None and self.client.connected and self.is_connected
