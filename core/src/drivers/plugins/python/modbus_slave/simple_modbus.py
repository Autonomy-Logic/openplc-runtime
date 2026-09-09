# pylint: disable=C0103,C0301,C0302,C0413,W0107,W0602,W0621,C0415,R0913,R0914,R0917
# C0103: Method/variable naming (getValues/setValues required by pymodbus API)
# C0301: Line too long (some lines exceed 100 chars)
# C0302: Too many lines in module (complex Modbus implementation)
# C0413: Import position (shared module import must be after sys.path modification)
# W0107: Unnecessary pass (used for read-only setValues methods)
# W0602: Global variable not assigned (threading.Event uses methods, not reassignment)
# W0621: Redefining name from outer scope (runtime_args parameter shadows global)
# C0415: Import outside toplevel (traceback imported in exception handlers)
# R0913: Too many arguments (required for segmented data block configuration)
# R0914: Too many local variables (complex address segmentation logic)
# R0917: Too many positional arguments (required for segmented data block configuration)

import asyncio
import os
import sys
import threading

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusServerContext,
    ModbusSparseDataBlock,
)
from pymodbus.server import ServerStop
from pymodbus.server.server import ModbusTcpServer

MAX_BITS = 8

# BUFFER_SIZE used to live here as `1024  # Must match BUFFER_SIZE in
# image_tables.h`, and that comment was the whole problem: a copy of a number
# owned by the runtime, kept in step by hand. It is gone, and the runtime's
# actual `buffer_size` is used instead (RTOP-284).
#
# The runtime no longer HAS a fixed image. It allocates one per program load,
# sized from what the project needs, so a copy here could not be right for more
# than one program at a time -- and being wrong is invisible: the exposed
# register block would be declared wider than the image, every address in the
# gap would pass pymodbus's validate(), fail the buffer read, and answer zero.
# A SCADA reading %QW2000 would get a plausible, wrong value indistinguishable
# from a real zero.
#
# Clamping to the runtime's size instead makes the declared block match the
# image exactly, and pymodbus then answers anything beyond it with exception 02
# (Illegal Data Address) out of its own validate(). That is the client being
# told, by the protocol, in the standard way, rather than being handed a
# fabricated value.

# Default segmentation configuration (matches v3 behavior)
DEFAULT_HOLDING_REG_CONFIG = {
    "qw_count": 1024,  # %QW - int_output (addresses 0-1023)
    "mw_count": 1024,  # %MW - int_memory (addresses 1024-2047)
    "md_count": 1024,  # %MD - dint_memory (addresses 2048-4095, 2 regs per value)
    "ml_count": 1024,  # %ML - lint_memory (addresses 4096-8191, 4 regs per value)
}

DEFAULT_COILS_CONFIG = {
    "qx_bits": 8192,  # %QX - bool_output
    "mx_bits": 0,  # %MX - bool_memory (disabled by default for backward compat)
}

DEFAULT_DISCRETE_INPUTS_CONFIG = {
    "ix_bits": 8192,  # %IX - bool_input
}

DEFAULT_INPUT_REGISTERS_CONFIG = {
    "iw_count": 1024,  # %IW - int_input
}

# Add the parent directory to Python path to find shared module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import the correct type definitions (must be after sys.path modification)
from shared import (  # noqa: E402
    PluginLogger,
    SafeBufferAccess,
    safe_extract_runtime_args_from_capsule,
)


class OpenPLCDeviceContext(ModbusDeviceContext):
    """
    Custom Modbus device context that correctly handles FC5/FC6 response echo.

    Per Modbus specification, FC5 (Write Single Coil) and FC6 (Write Single Register)
    responses should echo the requested value, not the current buffer state.

    pymodbus implements FC5/FC6 by calling setValues() then getValues() and using
    the read-back value in the response. However, OpenPLC uses a journal-based write
    system where writes are queued and applied at the next PLC scan cycle. This means
    getValues() may return the old value before the journal write is applied.

    This class caches values written via FC5/FC6 and returns them on the subsequent
    getValues() call, ensuring the response correctly echoes the requested value.

    The cache uses asyncio task ID to isolate concurrent requests, preventing race
    conditions when multiple FC5/FC6 requests arrive simultaneously.
    """

    # Function codes that require echo response (write single operations)
    _ECHO_FUNCTION_CODES = (5, 6)  # FC5: Write Single Coil, FC6: Write Single Register

    def __init__(self, *args, **kwargs):
        """Initialize with an empty pending writes cache."""
        super().__init__(*args, **kwargs)
        # Cache for FC5/FC6 echo values: (task_id, func_code, address) -> value
        self._pending_writes = {}

    def setValues(self, func_code, address, values):
        """
        Set values in the datastore and cache for FC5/FC6 echo response.

        For FC5/FC6, the written values are cached with the current asyncio task ID
        so that the subsequent getValues() call can return the correct echo value.
        """
        # Cache values for FC5/FC6 echo response
        if func_code in self._ECHO_FUNCTION_CODES:
            try:
                task_id = id(asyncio.current_task())
                for i, val in enumerate(values):
                    cache_key = (task_id, func_code, address + i)
                    self._pending_writes[cache_key] = val
            except RuntimeError:
                # No running event loop - skip caching (shouldn't happen in normal operation)
                pass

        return super().setValues(func_code, address, values)

    def getValues(self, func_code, address, count=1):
        """
        Get values from the datastore, using cached values for FC5/FC6 echo response.

        For FC5/FC6, if cached values exist for this task, they are returned and
        removed from the cache. This ensures the response echoes the requested value
        per Modbus specification.
        """
        # For FC5/FC6, return cached echo values if available
        if func_code in self._ECHO_FUNCTION_CODES:
            try:
                task_id = id(asyncio.current_task())
                results = []
                all_cached = True

                for i in range(count):
                    cache_key = (task_id, func_code, address + i)
                    if cache_key in self._pending_writes:
                        results.append(self._pending_writes.pop(cache_key))
                    else:
                        all_cached = False
                        break

                if all_cached:
                    return results

                # If not all values were cached, clean up any partial cache entries
                # and fall through to normal read
                for i in range(len(results), count):
                    cache_key = (task_id, func_code, address + i)
                    self._pending_writes.pop(cache_key, None)

            except RuntimeError:
                # No running event loop - fall through to normal read
                pass

        return super().getValues(func_code, address, count)


class OpenPLCCoilsDataBlock(ModbusSparseDataBlock):
    """Custom Modbus coils data block that mirrors OpenPLC bool_output using SafeBufferAccess"""

    def __init__(self, runtime_args, num_coils=64):
        self.runtime_args = runtime_args
        self.num_coils = num_coils

        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.warn(
                    f"Failed to create safe buffer access for coils: "
                    f"{self.safe_buffer_access.error_msg}"
                )

        # Initialize with zeros
        super().__init__([0] * num_coils)

    def getValues(self, address, count=1):
        """Get coil values from OpenPLC bool_output using SafeBufferAccess"""
        address = address - 1  # Modbus addresses are 0-based

        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.error(f"Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count

        # Ensure thread-safe access
        self.safe_buffer_access.acquire_mutex()

        values = []
        for i in range(count):
            coil_addr = address + i

            if coil_addr < self.num_coils:
                # Map coil address to buffer and bit indices
                buffer_idx = coil_addr // MAX_BITS  # 8 bits per buffer
                bit_idx = coil_addr % MAX_BITS  # bit within buffer

                value, error_msg = self.safe_buffer_access.read_bool_output(
                    buffer_idx, bit_idx, thread_safe=False
                )
                if error_msg == "Success":
                    values.append(1 if value else 0)
                else:
                    if logger:
                        logger.error(f"Error reading coil {coil_addr}: {error_msg}")
                    values.append(0)
            else:
                values.append(0)

        # Release mutex after access
        self.safe_buffer_access.release_mutex()

        return values

    def setValues(self, address, values):
        """Set coil values to OpenPLC bool_output using SafeBufferAccess.

        Note: Writes go through the journal buffer system which is internally
        thread-safe, so no mutex is needed for write operations.
        """
        address = address - 1  # Modbus addresses are 0-based

        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.error(f"Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return

        # Journal writes are thread-safe, no mutex needed
        for i, value in enumerate(values):
            coil_addr = address + i

            if coil_addr < self.num_coils:
                # Map coil address to buffer and bit indices
                buffer_idx = coil_addr // MAX_BITS  # 8 bits per buffer
                bit_idx = coil_addr % MAX_BITS  # bit within buffer

                _, error_msg = self.safe_buffer_access.write_bool_output(
                    buffer_idx, bit_idx, bool(value)
                )
                if error_msg != "Success":
                    if logger:
                        logger.error(f"Error setting coil {coil_addr}: {error_msg}")


class OpenPLCDiscreteInputsDataBlock(ModbusSparseDataBlock):
    """Custom Modbus discrete inputs data block that mirrors OpenPLC bool_input."""

    def __init__(self, runtime_args, num_inputs=64):
        self.runtime_args = runtime_args
        self.num_inputs = num_inputs

        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.warn(
                    f"Failed to create safe buffer access for "
                    f"discrete inputs: {self.safe_buffer_access.error_msg}"
                )

        # Initialize with zeros
        super().__init__([0] * num_inputs)

    def getValues(self, address, count=1):
        """Get discrete input values from OpenPLC bool_input using SafeBufferAccess"""
        address = address - 1  # Modbus addresses are 0-based

        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.error(f"Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count

        # Ensure thread-safe access
        self.safe_buffer_access.acquire_mutex()

        values = []
        for i in range(count):
            input_addr = address + i

            if input_addr < self.num_inputs:
                # Map input address to buffer and bit indices
                buffer_idx = input_addr // MAX_BITS  # 8 bits per buffer
                bit_idx = input_addr % MAX_BITS  # bit within buffer

                value, error_msg = self.safe_buffer_access.read_bool_input(
                    buffer_idx, bit_idx, thread_safe=False
                )
                if error_msg == "Success":
                    values.append(1 if value else 0)
                else:
                    if logger:
                        logger.error(f"Error reading discrete input {input_addr}: {error_msg}")
                    values.append(0)
            else:
                values.append(0)

        # Release mutex after access
        self.safe_buffer_access.release_mutex()

        return values

    def setValues(self, address, values):
        """Discrete inputs are read-only, this method should not be called"""
        pass  # Silently ignore writes to read-only inputs


class OpenPLCInputRegistersDataBlock(ModbusSparseDataBlock):
    """Custom Modbus input registers data block that mirrors OpenPLC analog inputs."""

    def __init__(self, runtime_args, num_registers=32):
        self.runtime_args = runtime_args
        self.num_registers = num_registers

        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.warn(
                    f"Failed to create safe buffer access for "
                    f"input registers: {self.safe_buffer_access.error_msg}"
                )

        # Initialize with zeros
        super().__init__([0] * num_registers)

    def getValues(self, address, count=1):
        """Get input register values from OpenPLC int_input using SafeBufferAccess"""
        address = address - 1  # Modbus addresses are 0-based

        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.error(f"Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count

        # Ensure buffer mutex
        self.safe_buffer_access.acquire_mutex()

        values = []
        for i in range(count):
            reg_addr = address + i

            if reg_addr < self.num_registers:
                value, error_msg = self.safe_buffer_access.read_int_input(
                    reg_addr, thread_safe=False
                )
                if error_msg == "Success":
                    values.append(value)
                else:
                    if logger:
                        logger.error(f"Error reading input register {reg_addr}: {error_msg}")
                    values.append(0)
            else:
                values.append(0)

        # Release mutex after access
        self.safe_buffer_access.release_mutex()

        return values

    def setValues(self, address, values):
        """Input registers are read-only, this method should not be called"""
        pass  # Silently ignore writes to read-only registers


class OpenPLCHoldingRegistersDataBlock(ModbusSparseDataBlock):
    """Custom Modbus holding registers data block that mirrors OpenPLC analog outputs."""

    def __init__(self, runtime_args, num_registers=32):
        self.runtime_args = runtime_args
        self.num_registers = num_registers

        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.warn(
                    f"Failed to create safe buffer access for "
                    f"holding registers: {self.safe_buffer_access.error_msg}"
                )

        # Initialize with zeros
        super().__init__([0] * num_registers)

    def getValues(self, address, count=1):
        """Get holding register values from OpenPLC int_output using SafeBufferAccess"""
        address = address - 1  # Modbus addresses are 0-based

        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.error(f"Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count

        # Ensure buffer mutex
        self.safe_buffer_access.acquire_mutex()

        values = []
        for i in range(count):
            reg_addr = address + i

            if reg_addr < self.num_registers:
                value, error_msg = self.safe_buffer_access.read_int_output(
                    reg_addr, thread_safe=False
                )
                if error_msg == "Success":
                    values.append(value)
                else:
                    if logger:
                        logger.error(f"Error reading holding register {reg_addr}: {error_msg}")
                    values.append(0)
            else:
                values.append(0)

        # Release mutex after access
        self.safe_buffer_access.release_mutex()
        return values

    def setValues(self, address, values):
        """Set holding register values to OpenPLC int_output using SafeBufferAccess.

        Note: Writes go through the journal buffer system which is internally
        thread-safe, so no mutex is needed for write operations.
        """
        address = address - 1  # Modbus addresses are 0-based

        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.error(f"Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return

        # Journal writes are thread-safe, no mutex needed
        for i, value in enumerate(values):
            reg_addr = address + i

            if reg_addr < self.num_registers:
                _, error_msg = self.safe_buffer_access.write_int_output(reg_addr, value)
                if error_msg != "Success":
                    if logger:
                        logger.error(f"Error setting holding register {reg_addr}: {error_msg}")


class OpenPLCSegmentedCoilsDataBlock(ModbusSparseDataBlock):
    """
    Segmented Modbus coils data block supporting both %QX (bool_output) and %MX (bool_memory).

    Address segmentation:
    - Addresses 0 to qx_bits-1: %QX (bool_output)
    - Addresses qx_bits to qx_bits+mx_bits-1: %MX (bool_memory)
    """

    def __init__(self, runtime_args, qx_bits=8192, mx_bits=0):
        self.runtime_args = runtime_args
        self.qx_bits = qx_bits
        self.mx_bits = mx_bits
        self.total_bits = qx_bits + mx_bits

        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.warn(
                    f"Failed to create safe buffer access for segmented coils: "
                    f"{self.safe_buffer_access.error_msg}"
                )

        # Initialize with zeros
        super().__init__([0] * self.total_bits)
        if logger:
            logger.info(
                f"Segmented coils: %QX={qx_bits} bits, %MX={mx_bits} bits, total={self.total_bits}"
            )

    def _get_segment_info(self, coil_addr):
        """
        Determine which segment a coil address belongs to.
        Returns: (segment_type, buffer_idx, bit_idx) or (None, None, None) if out of range
        """
        if coil_addr < 0:
            return None, None, None

        if coil_addr < self.qx_bits:
            # %QX segment (bool_output)
            buffer_idx = coil_addr // MAX_BITS
            bit_idx = coil_addr % MAX_BITS
            return "qx", buffer_idx, bit_idx

        mx_offset = coil_addr - self.qx_bits
        if mx_offset < self.mx_bits:
            # %MX segment (bool_memory)
            buffer_idx = mx_offset // MAX_BITS
            bit_idx = mx_offset % MAX_BITS
            return "mx", buffer_idx, bit_idx

        return None, None, None

    def getValues(self, address, count=1):
        """Get coil values from appropriate OpenPLC buffer based on address segmentation"""
        address = address - 1  # Modbus addresses are 1-based

        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.error(f"Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count

        self.safe_buffer_access.acquire_mutex()
        try:
            values = []
            for i in range(count):
                coil_addr = address + i
                segment, buffer_idx, bit_idx = self._get_segment_info(coil_addr)

                if segment == "qx":
                    value, error_msg = self.safe_buffer_access.read_bool_output(
                        buffer_idx, bit_idx, thread_safe=False
                    )
                    if error_msg == "Success":
                        values.append(1 if value else 0)
                    else:
                        if logger:
                            logger.error(f"Error reading coil %QX{coil_addr}: {error_msg}")
                        values.append(0)
                elif segment == "mx":
                    mx_addr = coil_addr - self.qx_bits
                    value, error_msg = self.safe_buffer_access.read_bool_memory(
                        buffer_idx, bit_idx, thread_safe=False
                    )
                    if error_msg == "Success":
                        values.append(1 if value else 0)
                    else:
                        if logger:
                            logger.error(f"Error reading coil %MX{mx_addr}: {error_msg}")
                        values.append(0)
                else:
                    values.append(0)

            return values
        finally:
            self.safe_buffer_access.release_mutex()

    def setValues(self, address, values):
        """Set coil values to appropriate OpenPLC buffer based on address segmentation.

        Note: Writes go through the journal buffer system which is internally
        thread-safe, so no mutex is needed for write operations.
        """
        address = address - 1  # Modbus addresses are 1-based

        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.error(f"Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return

        # Journal writes are thread-safe, no mutex needed
        for i, value in enumerate(values):
            coil_addr = address + i
            segment, buffer_idx, bit_idx = self._get_segment_info(coil_addr)

            if segment == "qx":
                _, error_msg = self.safe_buffer_access.write_bool_output(
                    buffer_idx, bit_idx, bool(value)
                )
                if error_msg != "Success":
                    if logger:
                        logger.error(f"Error setting coil %QX{coil_addr}: {error_msg}")
            elif segment == "mx":
                mx_addr = coil_addr - self.qx_bits
                _, error_msg = self.safe_buffer_access.write_bool_memory(
                    buffer_idx, bit_idx, bool(value)
                )
                if error_msg != "Success":
                    if logger:
                        logger.error(f"Error setting coil %MX{mx_addr}: {error_msg}")


class OpenPLCSegmentedHoldingRegistersDataBlock(ModbusSparseDataBlock):
    """
    Segmented Modbus holding registers data block supporting %QW, %MW, %MD, and %ML.

    Address segmentation (matching v3 behavior):
    - Addresses 0 to qw_count-1: %QW (int_output, 16-bit)
    - Addresses qw_count to qw_count+mw_count-1: %MW (int_memory, 16-bit)
    - Addresses qw_count+mw_count to qw_count+mw_count+md_count*2-1: %MD (dint_memory, 32-bit, 2 regs)
    - Addresses qw_count+mw_count+md_count*2 to end: %ML (lint_memory, 64-bit, 4 regs)

    Word order for multi-register values:
    - high_word_first (default, v3 compatible): High word at lower address
    - low_word_first: Low word at lower address
    """

    def __init__(
        self,
        runtime_args,
        qw_count=1024,
        mw_count=1024,
        md_count=1024,
        ml_count=1024,
        word_order="high_word_first",
    ):
        self.runtime_args = runtime_args
        self.qw_count = qw_count
        self.mw_count = mw_count
        self.md_count = md_count
        self.ml_count = ml_count
        self.word_order = word_order

        # Calculate segment boundaries
        self.qw_end = qw_count
        self.mw_start = self.qw_end
        self.mw_end = self.mw_start + mw_count
        self.md_start = self.mw_end
        self.md_end = self.md_start + (md_count * 2)  # 2 registers per 32-bit value
        self.ml_start = self.md_end
        self.ml_end = self.ml_start + (ml_count * 4)  # 4 registers per 64-bit value

        self.total_registers = self.ml_end

        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.warn(
                    f"Failed to create safe buffer access for segmented holding registers: "
                    f"{self.safe_buffer_access.error_msg}"
                )

        # Initialize with zeros
        super().__init__([0] * self.total_registers)
        if logger:
            logger.info(
                f"Segmented holding registers: %QW=0-{self.qw_end - 1}, "
                f"%MW={self.mw_start}-{self.mw_end - 1}, %MD={self.md_start}-{self.md_end - 1}, "
                f"%ML={self.ml_start}-{self.ml_end - 1}, word_order={word_order}"
            )

    def _get_segment_info(self, reg_addr):
        """
        Determine which segment a register address belongs to.
        Returns: (segment_type, value_index, word_offset) or (None, None, None) if out of range

        For 16-bit values: word_offset is always 0
        For 32-bit values: word_offset is 0 or 1 (which word within the DINT)
        For 64-bit values: word_offset is 0, 1, 2, or 3 (which word within the LINT)
        """
        if reg_addr < 0:
            return None, None, None

        if reg_addr < self.qw_end:
            # %QW segment (int_output)
            return "qw", reg_addr, 0

        if reg_addr < self.mw_end:
            # %MW segment (int_memory)
            return "mw", reg_addr - self.mw_start, 0

        if reg_addr < self.md_end:
            # %MD segment (dint_memory) - 2 registers per value
            offset = reg_addr - self.md_start
            value_idx = offset // 2
            word_offset = offset % 2
            return "md", value_idx, word_offset

        if reg_addr < self.ml_end:
            # %ML segment (lint_memory) - 4 registers per value
            offset = reg_addr - self.ml_start
            value_idx = offset // 4
            word_offset = offset % 4
            return "ml", value_idx, word_offset

        return None, None, None

    def _split_dint_to_words(self, value):
        """Split a 32-bit value into two 16-bit words based on word order."""
        value = value & 0xFFFFFFFF  # Ensure 32-bit
        high_word = (value >> 16) & 0xFFFF
        low_word = value & 0xFFFF
        if self.word_order == "high_word_first":
            return [high_word, low_word]
        else:
            return [low_word, high_word]

    def _combine_words_to_dint(self, words):
        """Combine two 16-bit words into a 32-bit value based on word order."""
        if self.word_order == "high_word_first":
            return ((words[0] & 0xFFFF) << 16) | (words[1] & 0xFFFF)
        else:
            return ((words[1] & 0xFFFF) << 16) | (words[0] & 0xFFFF)

    def _split_lint_to_words(self, value):
        """Split a 64-bit value into four 16-bit words based on word order."""
        value = value & 0xFFFFFFFFFFFFFFFF  # Ensure 64-bit
        words = [
            (value >> 48) & 0xFFFF,  # Highest word
            (value >> 32) & 0xFFFF,
            (value >> 16) & 0xFFFF,
            value & 0xFFFF,  # Lowest word
        ]
        if self.word_order == "high_word_first":
            return words
        else:
            return list(reversed(words))

    def _combine_words_to_lint(self, words):
        """Combine four 16-bit words into a 64-bit value based on word order."""
        if self.word_order != "high_word_first":
            words = list(reversed(words))
        return (
            ((words[0] & 0xFFFF) << 48)
            | ((words[1] & 0xFFFF) << 32)
            | ((words[2] & 0xFFFF) << 16)
            | (words[3] & 0xFFFF)
        )

    def getValues(self, address, count=1):
        """Get holding register values from appropriate OpenPLC buffer based on address segmentation"""
        address = address - 1  # Modbus addresses are 1-based

        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.error(f"Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count

        self.safe_buffer_access.acquire_mutex()
        try:
            values = []
            for i in range(count):
                reg_addr = address + i
                segment, value_idx, word_offset = self._get_segment_info(reg_addr)

                if segment == "qw":
                    value, error_msg = self.safe_buffer_access.read_int_output(
                        value_idx, thread_safe=False
                    )
                    if error_msg == "Success":
                        values.append(value & 0xFFFF)
                    else:
                        if logger:
                            logger.error(f"Error reading %QW{value_idx}: {error_msg}")
                        values.append(0)

                elif segment == "mw":
                    value, error_msg = self.safe_buffer_access.read_int_memory(
                        value_idx, thread_safe=False
                    )
                    if error_msg == "Success":
                        values.append(value & 0xFFFF)
                    else:
                        if logger:
                            logger.error(f"Error reading %MW{value_idx}: {error_msg}")
                        values.append(0)

                elif segment == "md":
                    dint_value, error_msg = self.safe_buffer_access.read_dint_memory(
                        value_idx, thread_safe=False
                    )
                    if error_msg == "Success":
                        words = self._split_dint_to_words(dint_value)
                        values.append(words[word_offset])
                    else:
                        if logger:
                            logger.error(f"Error reading %MD{value_idx}: {error_msg}")
                        values.append(0)

                elif segment == "ml":
                    lint_value, error_msg = self.safe_buffer_access.read_lint_memory(
                        value_idx, thread_safe=False
                    )
                    if error_msg == "Success":
                        words = self._split_lint_to_words(lint_value)
                        values.append(words[word_offset])
                    else:
                        if logger:
                            logger.error(f"Error reading %ML{value_idx}: {error_msg}")
                        values.append(0)

                else:
                    values.append(0)

            return values
        finally:
            self.safe_buffer_access.release_mutex()

    def setValues(self, address, values):
        """Set holding register values to appropriate OpenPLC buffer based on address segmentation.

        Note: While journal writes are thread-safe, this method still uses mutex because
        partial DINT/LINT updates require read-modify-write (RMW) operations. The mutex
        ensures consistency between the read and subsequent write for multi-word values.
        Simple QW/MW writes don't strictly need the mutex, but we keep it for RMW safety.
        """
        address = address - 1  # Modbus addresses are 1-based

        if not self.safe_buffer_access.is_valid:
            if logger:
                logger.error(f"Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return

        # Mutex needed for read-modify-write consistency on partial DINT/LINT updates
        self.safe_buffer_access.acquire_mutex()
        try:
            # For multi-word values, we need to handle partial writes carefully
            # Build a map of pending multi-word updates
            pending_dint = {}  # value_idx -> [words]
            pending_lint = {}  # value_idx -> [words]

            for i, value in enumerate(values):
                reg_addr = address + i
                segment, value_idx, word_offset = self._get_segment_info(reg_addr)

                if segment == "qw":
                    # Simple 16-bit write - journal handles thread safety
                    _, error_msg = self.safe_buffer_access.write_int_output(
                        value_idx, value & 0xFFFF, thread_safe=False
                    )
                    if error_msg != "Success":
                        if logger:
                            logger.error(f"Error setting %QW{value_idx}: {error_msg}")

                elif segment == "mw":
                    # Simple 16-bit write - journal handles thread safety
                    _, error_msg = self.safe_buffer_access.write_int_memory(
                        value_idx, value & 0xFFFF, thread_safe=False
                    )
                    if error_msg != "Success":
                        if logger:
                            logger.error(f"Error setting %MW{value_idx}: {error_msg}")

                elif segment == "md":
                    # Partial 32-bit write - need RMW for consistency
                    if value_idx not in pending_dint:
                        # Read current value to preserve unchanged words
                        current, _ = self.safe_buffer_access.read_dint_memory(
                            value_idx, thread_safe=False
                        )
                        pending_dint[value_idx] = self._split_dint_to_words(
                            current if current else 0
                        )
                    pending_dint[value_idx][word_offset] = value & 0xFFFF

                elif segment == "ml":
                    # Partial 64-bit write - need RMW for consistency
                    if value_idx not in pending_lint:
                        # Read current value to preserve unchanged words
                        current, _ = self.safe_buffer_access.read_lint_memory(
                            value_idx, thread_safe=False
                        )
                        pending_lint[value_idx] = self._split_lint_to_words(
                            current if current else 0
                        )
                    pending_lint[value_idx][word_offset] = value & 0xFFFF

            # Write pending DINT values (RMW complete, write to journal)
            for value_idx, words in pending_dint.items():
                dint_value = self._combine_words_to_dint(words)
                _, error_msg = self.safe_buffer_access.write_dint_memory(
                    value_idx, dint_value, thread_safe=False
                )
                if error_msg != "Success":
                    if logger:
                        logger.error(f"Error setting %MD{value_idx}: {error_msg}")

            # Write pending LINT values (RMW complete, write to journal)
            for value_idx, words in pending_lint.items():
                lint_value = self._combine_words_to_lint(words)
                _, error_msg = self.safe_buffer_access.write_lint_memory(
                    value_idx, lint_value, thread_safe=False
                )
                if error_msg != "Success":
                    if logger:
                        logger.error(f"Error setting %ML{value_idx}: {error_msg}")
        finally:
            self.safe_buffer_access.release_mutex()


def _log_clamped_segments(config_map, buffer_config, buffer_size):
    """Say what the image could not hold, once, at startup.

    The clamp itself is already honest to a Modbus client -- anything past the
    declared block gets exception 02 from pymodbus. But the person who
    configured the server is not the client: they set 1024 registers in the
    editor and would otherwise have to notice, from the other end of a network,
    that only some of them answer. This is the line they can find on the device.

    Only reports segments that actually shrank. On the normal path nothing did,
    because the editor sizes the image from the exposure it was asked for, and
    the interesting case is precisely the one where that did not happen: an
    upload without image.conf, or a device provisioned by some other route.
    """
    requested = config_map.get("buffer_mapping", {})
    if not requested:
        return

    pairs = [
        ("holding_registers", "qw_count", buffer_size),
        ("holding_registers", "mw_count", buffer_size),
        ("holding_registers", "md_count", buffer_size),
        ("holding_registers", "ml_count", buffer_size),
        ("coils", "qx_bits", buffer_size * MAX_BITS),
        ("coils", "mx_bits", buffer_size * MAX_BITS),
        ("discrete_inputs", "ix_bits", buffer_size * MAX_BITS),
        ("input_registers", "iw_count", buffer_size),
    ]

    shrunk = []
    for section, key, limit in pairs:
        asked = requested.get(section, {}).get(key)
        if isinstance(asked, int) and asked > limit:
            shrunk.append(f"{key} {asked} -> {limit}")

    if shrunk:
        logger.warn(
            "Modbus exposure reduced to fit the I/O image ("
            + ", ".join(shrunk)
            + "). Addresses beyond the reduced range answer Illegal Data Address. "
            "This means the image sizes did not reach this device: check that the "
            "program was uploaded by a current editor."
        )


def parse_buffer_mapping_config(config_map, buffer_size):
    """
    Parse buffer_mapping configuration from JSON config.
    Supports both legacy format (max_coils, etc.) and new segmented format.

    `buffer_size` is the runtime's ACTUAL image size for the program now
    loaded, from ``runtime_args.safe_access_buffer_size()``. Every count is
    clamped to it (RTOP-284) -- this used to clamp to a copy of the runtime's
    old fixed 1024, kept in step by hand and unable to be right for more than
    one program at a time.

    THE CLAMP IS WHAT MAKES OUT-OF-RANGE HONEST, not just tidy. Each data block
    declares itself as wide as the counts returned here, and pymodbus's own
    validate() answers exception 02 (Illegal Data Address) for anything past
    that. Declare a block wider than the image and the addresses in the gap
    pass validate, fail the buffer read, and answer ZERO -- a plausible, wrong
    value that a client cannot tell from a real zero, logged once per read at
    scan rate. Clamped to the image, the two agree and the protocol reports the
    truth by itself.

    Note this also settles the case where the user configured nothing: the
    editor materialises its defaults (1024 registers, 8192 coils) into
    modbus_slave.json even when the project never opened the Modbus screen, so
    the common project would otherwise declare 1024 registers over an image of
    eight.

    Returns a dict with parsed configuration for each data block type.
    """
    buffer_mapping = config_map.get("buffer_mapping", {})

    # Check for new segmented format
    if "holding_registers" in buffer_mapping and isinstance(
        buffer_mapping["holding_registers"], dict
    ):
        # New segmented format
        hr_config = buffer_mapping.get("holding_registers", {})
        coils_config = buffer_mapping.get("coils", {})
        di_config = buffer_mapping.get("discrete_inputs", {})
        ir_config = buffer_mapping.get("input_registers", {})

        return {
            "format": "segmented",
            "holding_registers": {
                "qw_count": min(
                    hr_config.get("qw_count", DEFAULT_HOLDING_REG_CONFIG["qw_count"]), buffer_size
                ),
                "mw_count": min(
                    hr_config.get("mw_count", DEFAULT_HOLDING_REG_CONFIG["mw_count"]), buffer_size
                ),
                "md_count": min(
                    hr_config.get("md_count", DEFAULT_HOLDING_REG_CONFIG["md_count"]), buffer_size
                ),
                "ml_count": min(
                    hr_config.get("ml_count", DEFAULT_HOLDING_REG_CONFIG["ml_count"]), buffer_size
                ),
            },
            "coils": {
                "qx_bits": min(
                    coils_config.get("qx_bits", DEFAULT_COILS_CONFIG["qx_bits"]),
                    buffer_size * MAX_BITS,
                ),
                "mx_bits": min(
                    coils_config.get("mx_bits", DEFAULT_COILS_CONFIG["mx_bits"]),
                    buffer_size * MAX_BITS,
                ),
            },
            "discrete_inputs": {
                "ix_bits": min(
                    di_config.get("ix_bits", DEFAULT_DISCRETE_INPUTS_CONFIG["ix_bits"]),
                    buffer_size * MAX_BITS,
                ),
            },
            "input_registers": {
                "iw_count": min(
                    ir_config.get("iw_count", DEFAULT_INPUT_REGISTERS_CONFIG["iw_count"]),
                    buffer_size,
                ),
            },
            "word_order": config_map.get("word_order", "high_word_first"),
        }

    # Legacy format (max_coils, max_discrete_inputs, etc.)
    # Convert to segmented format with no memory location support
    max_coils = buffer_mapping.get("max_coils", 8192)
    max_discrete_inputs = buffer_mapping.get("max_discrete_inputs", 8192)
    max_holding_registers = buffer_mapping.get("max_holding_registers", 1024)
    max_input_registers = buffer_mapping.get("max_input_registers", 1024)

    return {
        "format": "legacy",
        "holding_registers": {
            "qw_count": min(max_holding_registers, buffer_size),
            "mw_count": 0,  # No memory support in legacy mode
            "md_count": 0,
            "ml_count": 0,
        },
        "coils": {
            "qx_bits": min(max_coils, buffer_size * MAX_BITS),
            "mx_bits": 0,  # No memory support in legacy mode
        },
        "discrete_inputs": {
            "ix_bits": min(max_discrete_inputs, buffer_size * MAX_BITS),
        },
        "input_registers": {
            "iw_count": min(max_input_registers, buffer_size),
        },
        "word_order": "high_word_first",
    }


# Global variables for plugin lifecycle
server_task = None
server_context = None
runtime_args = None
running = False
logger: PluginLogger = None
server_loop = None  # Reference to the server's event loop for cross-thread operations
server_started_event = threading.Event()  # Signals successful server startup
server_error = None  # Stores any startup error message
gIp = "172.29.65.104"  # Default values
gPort = 5020

# Retry configuration for server restart
RETRY_DELAY_BASE = 2.0  # Initial delay between restart attempts (seconds)
RETRY_DELAY_MAX = 30.0  # Maximum delay between restart attempts (seconds)


def init(args_capsule):
    """Initialize the Modbus plugin"""
    global runtime_args, logger

    # Initialize logger early (without runtime_args for now)
    logger = PluginLogger("MODBUS_SLAVE", None)
    logger.info("Python plugin 'simple_modbus' initializing...")

    try:
        # Print structure validation info for debugging
        logger.debug("Validating plugin structure alignment...")

        # Extract runtime args from capsule using safe method
        if hasattr(args_capsule, "__class__") and "PyCapsule" in str(type(args_capsule)):
            # This is a PyCapsule from C - use safe extraction
            runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
            if runtime_args is None:
                logger.error(f"Failed to extract runtime args: {error_msg}")
                return False

            # Re-initialize logger with runtime_args for central logging
            logger = PluginLogger("MODBUS_SLAVE", runtime_args)
            logger.info("Runtime arguments extracted successfully")
        else:
            # This is a direct object (for testing)
            runtime_args = args_capsule
            logger.info("Using direct runtime args for testing")

        return True

    except Exception as e:
        logger.error(f"Plugin initialization failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def start_loop():
    """Start the Modbus server with automatic restart on failure."""
    global server_task, running, server_loop, server_started_event, server_error
    global server_context, gIp, gPort

    if runtime_args is None:
        logger.error("Plugin not initialized")
        return False

    # THE IMAGE SIZE COMES FIRST, because every count parsed below is clamped
    # to it. The runtime allocates the image per program load (RTOP-284), so
    # this is the size for the program running right now -- not a constant this
    # plugin can keep a copy of.
    buffer_size, size_error = runtime_args.safe_access_buffer_size()
    if buffer_size == -1:
        logger.error(f"Failed to access buffer size: {size_error}")
        return False

    # Load configuration and create data blocks
    try:
        # Try to load configuration from plugin_specific_config_file_path
        config_map = None
        buffer_config = None
        try:
            config_map, status = SafeBufferAccess(runtime_args).get_config_file_args_as_map()
            if status == "Success" and config_map:
                # Try to extract network configuration
                network_config = config_map.get("network_configuration", {})
                if network_config and "host" in network_config and "port" in network_config:
                    gIp = str(network_config["host"])
                    gPort = int(network_config["port"])
                    logger.info(f"Configuration loaded - Host: {gIp}, Port: {gPort}")
                else:
                    logger.warn(
                        "Config file loaded but network_configuration section missing or "
                        "incomplete - using defaults"
                    )
                    logger.debug(f"Available config sections: {list(config_map.keys())}")

                # Parse buffer mapping configuration
                buffer_config = parse_buffer_mapping_config(config_map, buffer_size)
                logger.info(f"Buffer mapping format: {buffer_config['format']}")
                _log_clamped_segments(config_map, buffer_config, buffer_size)
            else:
                logger.warn(f"Failed to load configuration file: {status} - using defaults")
        except Exception as config_error:
            logger.warn(f"Exception while loading config: {config_error} - using defaults")
            import traceback

            traceback.print_exc()

        # Use default configuration if not loaded from file
        if buffer_config is None:
            buffer_config = parse_buffer_mapping_config({}, buffer_size)
            logger.info("Using default buffer mapping configuration")

        # Create OpenPLC-connected data blocks based on configuration
        hr_config = buffer_config["holding_registers"]
        coils_cfg = buffer_config["coils"]
        di_config = buffer_config["discrete_inputs"]
        ir_config = buffer_config["input_registers"]
        word_order = buffer_config["word_order"]

        # Use segmented data blocks for holding registers and coils (supports memory locations)
        coils_block = OpenPLCSegmentedCoilsDataBlock(
            runtime_args, qx_bits=coils_cfg["qx_bits"], mx_bits=coils_cfg["mx_bits"]
        )
        discrete_inputs_block = OpenPLCDiscreteInputsDataBlock(
            runtime_args, num_inputs=di_config["ix_bits"]
        )
        input_registers_block = OpenPLCInputRegistersDataBlock(
            runtime_args, num_registers=ir_config["iw_count"]
        )
        holding_registers_block = OpenPLCSegmentedHoldingRegistersDataBlock(
            runtime_args,
            qw_count=hr_config["qw_count"],
            mw_count=hr_config["mw_count"],
            md_count=hr_config["md_count"],
            ml_count=hr_config["ml_count"],
            word_order=word_order,
        )

        # Create device context with all OpenPLC-connected data blocks
        # Uses OpenPLCDeviceContext for correct FC5/FC6 response echo handling
        device = OpenPLCDeviceContext(
            di=discrete_inputs_block,  # Discrete Inputs -> bool_input (%IX)
            co=coils_block,  # Coils -> bool_output (%QX) + bool_memory (%MX)
            ir=input_registers_block,  # Input Registers -> int_input (%IW)
            hr=holding_registers_block,  # Holding Registers -> %QW, %MW, %MD, %ML
        )
        server_context = ModbusServerContext(devices={1: device}, single=False)

        logger.info(f"Plugin initialized successfully - Host: {gIp}, Port: {gPort}")

    except Exception as e:
        logger.error(f"Configuration loading failed: {e}")
        import traceback

        traceback.print_exc()
        return False

    # Prevent double-start
    if server_task is not None and server_task.is_alive():
        logger.warn("Server already running")
        return True

    running = True
    server_started_event.clear()
    server_error = None

    def run_server():
        """Server thread with automatic restart on failure (never give up)."""
        global server_loop, server_error
        backoff = RETRY_DELAY_BASE
        first_attempt = True

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server_loop = loop

        async def server_runner():
            """Main server coroutine with restart logic."""
            global server_error
            nonlocal backoff, first_attempt

            while running:
                # Check if cleanup has been called
                if server_context is None:
                    break

                try:
                    # Create and start the server
                    server = ModbusTcpServer(context=server_context, address=(gIp, gPort))

                    # serve_forever with background=True returns after successful bind
                    await server.serve_forever(background=True)

                    # If we get here, server is listening
                    if first_attempt:
                        logger.info(f"Server listening on {gIp}:{gPort}")
                        server_started_event.set()
                        first_attempt = False

                    backoff = RETRY_DELAY_BASE  # Reset backoff on success

                    # Keep server running until stop is requested
                    while running and server_context is not None:
                        await asyncio.sleep(1)

                    # Graceful shutdown
                    await server.shutdown()
                    break

                except Exception as e:
                    error_msg = str(e)
                    server_error = error_msg

                    if first_attempt:
                        # Signal startup failure on first attempt
                        logger.error(f"Failed to start server on {gIp}:{gPort}: {error_msg}")
                        server_started_event.set()  # Unblock start_loop

                    if not running:
                        break  # Stop requested, don't retry

                    logger.warn(f"Server error, will retry in {backoff:.1f}s: {error_msg}")

                    # Wait before retry (check running flag periodically)
                    wait_time = 0
                    while wait_time < backoff and running:
                        await asyncio.sleep(0.5)
                        wait_time += 0.5

                    # Increase backoff for next attempt (capped at max)
                    backoff = min(backoff * 1.5, RETRY_DELAY_MAX)
                    first_attempt = False

        try:
            loop.run_until_complete(server_runner())
        except Exception as e:
            logger.error(f"Fatal error in server thread: {e}")
        finally:
            server_loop = None
            loop.close()

    server_task = threading.Thread(target=run_server, daemon=False)
    server_task.start()

    # Wait for server to start (or fail) with timeout
    startup_timeout = 5.0
    if server_started_event.wait(timeout=startup_timeout):
        if server_error is not None:
            logger.error(f"Server startup failed: {server_error}")
            return False
        return True
    else:
        logger.error(f"Timeout waiting for server to start on {gIp}:{gPort}")
        return False


def _cancel_all_tasks(loop):
    """Cancel all running tasks on the event loop."""
    for task in asyncio.all_tasks(loop):
        task.cancel()


def stop_loop():
    """Stop the Modbus server gracefully.

    Uses a two-phase approach:
    1. Graceful (10s): call ServerStop and wait for the thread to finish.
    2. Forced (5s): cancel all asyncio tasks on the event loop and wait again.

    Returns:
        True if the server thread stopped, False if it survived both phases.
    """
    global server_task, running

    running = False

    if server_task:
        # Phase 1: Graceful stop via pymodbus ServerStop
        try:
            ServerStop()
        except RuntimeError as e:
            # Server may not be running or already stopped
            logger.warn(f"ServerStop warning: {e}")

        server_task.join(timeout=10.0)

        if server_task.is_alive():
            # Phase 2: Force-cancel the event loop
            logger.warn("Graceful stop timed out, forcing event loop cancellation...")
            loop = server_loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(_cancel_all_tasks, loop)
            server_task.join(timeout=5.0)

        if server_task.is_alive():
            logger.error("Server thread did not stop after forced cancellation")
            server_task = None
            return False

        server_task = None

    logger.info("Server stopped")
    return True


def cleanup():
    """Cleanup plugin resources"""
    global server_context, runtime_args

    server_context = None
    runtime_args = None

    logger.info("Plugin cleaned up")
    return True


async def main():
    """Standalone server for testing"""
    # Create a proper mock runtime args that inherits from PluginRuntimeArgs

    # Create a mock that has the required methods
    class MockArgs:
        def __init__(self):
            self.buffer_size = 1
            self.bits_per_buffer = 8
            # Create simple boolean list for testing
            self.bool_data = [[False] * 8]  # 1 buffer, 8 booleans
            self.bool_output = self.bool_data  # Simple reference
            self.image_lock = None
            self.image_unlock = None

        def safe_access_buffer_size(self):
            """Mock implementation of safe_access_buffer_size"""
            return self.buffer_size, "Success"

        def validate_pointers(self):
            """Mock implementation of validate_pointers"""
            return True, "Mock validation passed"

        def __str__(self):
            return (
                f"MockArgs(buffer_size={self.buffer_size}, bits_per_buffer={self.bits_per_buffer})"
            )

    mock_args = MockArgs()

    # Initialize and start
    if init(mock_args):
        if start_loop():
            print(f"Modbus server running on {gIp}:{gPort}")
            print("Press Ctrl+C to stop...")

            try:
                # Keep server running
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                print("\nStopping server...")
                stop_loop()
                cleanup()
        else:
            print("Failed to start server")
    else:
        print("Failed to initialize plugin")


if __name__ == "__main__":
    asyncio.run(main())
