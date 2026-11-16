# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Autonomy®

"""Modbus Master plugin connection management utilities."""

import threading
import time
from typing import Literal, Optional, Union

from pymodbus.client import ModbusTcpClient, ModbusSerialClient
from pymodbus.exceptions import ConnectionException

TransportType = Literal["tcp", "rtu"]
ParityType = Literal["N", "E", "O"]
GiveUpReason = Literal["failed first connect", "connection lost"]

GIVE_UP_FAILED_FIRST_CONNECT: GiveUpReason = "failed first connect"
GIVE_UP_CONNECTION_LOST: GiveUpReason = "connection lost"


def is_connection_lost(
    failure: object, client: Optional[Union[ModbusTcpClient, ModbusSerialClient]]
) -> bool:
    """
    Classify a failed request as a lost connection or a failed poll.

    Args:
        failure: The error response returned or the exception raised by the request.
        client: The client the request went through.

    Returns:
        True when the stack reports the socket closed, reset or failing (a
        ConnectionException, a socket OSError, or a client no longer connected);
        False for an exception response, an error response, a timeout or a
        malformed frame on a connected client.
    """
    if isinstance(failure, (ConnectionException, OSError)):
        return True
    return client is None or not client.connected


class ModbusConnectionManager:  # pylint: disable=too-many-instance-attributes
    """Manages Modbus TCP and RTU connections with retry logic."""

    def __init__(
        self,
        transport: TransportType = "tcp",
        host: str = "127.0.0.1",
        port: int = 502,
        serial_port: str = "",
        baud_rate: int = 9600,
        parity: ParityType = "N",
        stop_bits: int = 1,
        data_bits: int = 8,
        timeout_ms: int = 1000,
        slave_id: int = 1,
        reconnect: bool = True,
    ):
        self.transport = transport
        self.timeout = timeout_ms / 1000.0
        self.slave_id = slave_id

        self.host = host
        self.port = port

        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.parity = parity
        self.stop_bits = stop_bits
        self.data_bits = data_bits

        self.retry_delay_base = 2.0
        self.retry_delay_max = 30.0
        self.retry_delay_current = self.retry_delay_base

        self.reconnect = reconnect
        self.give_up_reason: Optional[GiveUpReason] = None

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
        else:
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

    @property
    def given_up(self) -> bool:
        """True once the connection was given up; only a new manager connects again."""
        return self.give_up_reason is not None

    def give_up(self, reason: GiveUpReason) -> None:
        """Close the connection and stop using it for the rest of this manager's life."""
        if self.give_up_reason is None:
            self.give_up_reason = reason
        if self.client:
            try:
                self.client.close()
            except OSError as e:
                print(f"(FAIL) Error closing {self.get_connection_info()}: {e}")
            self.client = None
        self.is_connected = False

    def connect(self, stop_event: Optional[threading.Event] = None) -> bool:
        """
        Establish the first connection according to the reconnect policy.

        With reconnect on, retries until connected or stopped. With it off, makes
        a single attempt and gives up on failure.

        Args:
            stop_event: Optional threading.Event to allow early termination

        Returns:
            True if connected, False if interrupted or given up
        """
        if self.reconnect:
            return self.connect_with_retry(stop_event)
        return self.connect_once()

    def connect_once(self) -> bool:
        """
        Make a single connection attempt; on failure the connection is given up.

        Returns:
            True if connected, False if the attempt failed
        """
        if self.given_up:
            return False
        conn_info = self.get_connection_info()
        try:
            self.client = self._create_client()
            if self.client.connect():
                print(f"(PASS) Connected to {conn_info} (single attempt)")
                self.is_connected = True
                return True
            print(f"(FAIL) Connection attempt to {conn_info} failed")
        except (ConnectionException, OSError) as e:
            print(f"(FAIL) Connection attempt to {conn_info} failed: {e}")
        self.give_up(GIVE_UP_FAILED_FIRST_CONNECT)
        return False

    def handle_request_failure(self, failure: object) -> None:
        """
        Apply the reconnect policy to a failed request.

        With reconnect on, the connection is marked disconnected on every failure.
        With it off, the connection is given up only when the failure is a lost
        connection; any other failure leaves the connection up.

        Args:
            failure: The error response returned or the exception raised by the request.
        """
        if self.reconnect:
            self.mark_disconnected()
        elif is_connection_lost(failure, self.client):
            self.give_up(GIVE_UP_CONNECTION_LOST)

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
                if self.client is None or not self.client.connected:
                    if self.client:
                        try:
                            self.client.close()
                        except Exception:
                            pass
                    self.client = self._create_client()

                if self.client.connect():
                    print(
                        f"(PASS) Connected to {conn_info} (attempt {retry_count + 1})"
                    )
                    self.is_connected = True
                    self.retry_delay_current = self.retry_delay_base
                    return True

            except Exception as e:
                print(f"(FAIL) Connection attempt {retry_count + 1} to {conn_info} failed: {e}")

            retry_count += 1

            if retry_count == 1:
                print(f"Failed to connect to {conn_info}, starting retry attempts...")
            elif retry_count % 10 == 0:
                print(f"Connection attempt {retry_count} to {conn_info} failed, continuing retries...")

            delay = min(self.retry_delay_current, self.retry_delay_max)

            sleep_increments = int(delay * 10)
            for _ in range(sleep_increments):
                if stop_event and stop_event.is_set():
                    return False
                time.sleep(0.1)

            self.retry_delay_current = min(self.retry_delay_current * 1.5, self.retry_delay_max)

        return False

    def ensure_connection(self, stop_event=None) -> bool:
        """
        Ensures there is a valid connection, reconnecting if necessary.

        Checks both the pymodbus client state and the is_connected flag.

        Args:
            stop_event: Optional threading.Event to allow early termination

        Returns:
            True if connection is available, False if interrupted
        """
        if self.is_connected and self.client and self.client.connected:
            return True

        if not self.reconnect:
            self.give_up(GIVE_UP_CONNECTION_LOST)
            return False

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

        self.is_connected = False

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
