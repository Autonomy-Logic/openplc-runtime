import asyncio
import ctypes
from operator import add
import threading
import time
import sys
import os
import json
from pymodbus.server import StartAsyncTcpServer, ServerStop
from pymodbus.datastore import (
    ModbusSparseDataBlock,
    ModbusDeviceContext,
    ModbusServerContext,
)

MAX_BITS = 8

# Add the parent directory to Python path to find shared module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import the correct type definitions
from shared.python_plugin_types import (
    PluginRuntimeArgs, 
    safe_extract_runtime_args_from_capsule,
    SafeBufferAccess,
    PluginStructureValidator
)

class OpenPLCCoilsDataBlock(ModbusSparseDataBlock):
    """Custom Modbus coils data block that mirrors OpenPLC bool_output using SafeBufferAccess"""
    
    def __init__(self, runtime_args, num_coils=64):
        self.runtime_args = runtime_args
        self.num_coils = num_coils
        
        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Warning: Failed to create safe buffer access for coils: {self.safe_buffer_access.error_msg}")
        
        # Initialize with zeros
        super().__init__([0] * num_coils)
    
    def getValues(self, address, count=1):
        address = address - 1  # Modbus addresses are 0-based

        """Get coil values from OpenPLC bool_output using SafeBufferAccess"""
        print(f"[MODBUS] Coils getValues called: address={address}, count={count}")
        
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Error: Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count
        
        # Ensure thread-safe access
        self.safe_buffer_access.acquire_mutex()

        values = []
        for i in range(count):
            coil_addr = address + i
            
            if coil_addr < self.num_coils:
                # Map coil address to buffer and bit indices
                buffer_idx = coil_addr // MAX_BITS  # 8 bits per buffer
                bit_idx = coil_addr % MAX_BITS      # bit within buffer

                value, error_msg = self.safe_buffer_access.read_bool_output(buffer_idx, bit_idx, thread_safe=False)
                if error_msg == "Success":
                    values.append(1 if value else 0)
                    print(f"[MODBUS] Read coil {coil_addr} (buf:{buffer_idx}, bit:{bit_idx}): {value}")
                else:
                    print(f"[MODBUS] Error reading coil {coil_addr}: {error_msg}")
                    values.append(0)
            else:
                values.append(0)
        
        # Release mutex after access
        self.safe_buffer_access.release_mutex()

        return values
    
    def setValues(self, address, values):
        address = address - 1  # Modbus addresses are 0-based
        """Set coil values to OpenPLC bool_output using SafeBufferAccess"""
        print(f"[MODBUS] Coils setValues called: address={address}, values={values}")
        
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Error: Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return
        
        # Ensure thread-safe access
        self.safe_buffer_access.acquire_mutex()
        
        for i, value in enumerate(values):
            coil_addr = address + i
            
            if coil_addr < self.num_coils:
                # Map coil address to buffer and bit indices
                buffer_idx = coil_addr // MAX_BITS  # 8 bits per buffer
                bit_idx = coil_addr % MAX_BITS      # bit within buffer

                success, error_msg = self.safe_buffer_access.write_bool_output(buffer_idx, bit_idx, bool(value), thread_safe=False)
                if error_msg == "Success":
                    print(f"[MODBUS] Set coil {coil_addr} (buf:{buffer_idx}, bit:{bit_idx}): {bool(value)}")
                else:
                    print(f"[MODBUS] Error setting coil {coil_addr}: {error_msg}")
        
        # Release mutex after access
        self.safe_buffer_access.release_mutex()


class OpenPLCDiscreteInputsDataBlock(ModbusSparseDataBlock):
    """Custom Modbus discrete inputs data block that mirrors OpenPLC bool_input using SafeBufferAccess"""
    
    def __init__(self, runtime_args, num_inputs=64):
        self.runtime_args = runtime_args
        self.num_inputs = num_inputs
        
        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Warning: Failed to create safe buffer access for discrete inputs: {self.safe_buffer_access.error_msg}")
        
        # Initialize with zeros
        super().__init__([0] * num_inputs)
    
    def getValues(self, address, count=1):
        address = address - 1  # Modbus addresses are 0-based
        """Get discrete input values from OpenPLC bool_input using SafeBufferAccess"""
        print(f"[MODBUS] Discrete Inputs getValues called: address={address}, count={count}")
        
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Error: Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count
        
        # Ensure thread-safe access
        self.safe_buffer_access.acquire_mutex()
        
        values = []
        for i in range(count):
            input_addr = address + i
            
            if input_addr < self.num_inputs:
                # Map input address to buffer and bit indices
                buffer_idx = input_addr // MAX_BITS  # 8 bits per buffer
                bit_idx = input_addr % MAX_BITS      # bit within buffer

                value, error_msg = self.safe_buffer_access.read_bool_input(buffer_idx, bit_idx, thread_safe=False)
                if error_msg == "Success":
                    values.append(1 if value else 0)
                    print(f"[MODBUS] Read discrete input {input_addr} (buf:{buffer_idx}, bit:{bit_idx}): {value}")
                else:
                    print(f"[MODBUS] Error reading discrete input {input_addr}: {error_msg}")
                    values.append(0)
            else:
                values.append(0)
        
        # Release mutex after access
        self.safe_buffer_access.release_mutex()

        return values
    
    def setValues(self, address, values):
        """Discrete inputs are read-only, this method should not be called"""
        print(f"[MODBUS] Warning: Attempt to write to read-only discrete inputs at address {address}")


class OpenPLCInputRegistersDataBlock(ModbusSparseDataBlock):
    """Custom Modbus input registers data block that mirrors OpenPLC analog inputs using SafeBufferAccess"""
    
    def __init__(self, runtime_args, num_registers=32):
        self.runtime_args = runtime_args
        self.num_registers = num_registers
        
        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Warning: Failed to create safe buffer access for input registers: {self.safe_buffer_access.error_msg}")
        
        # Initialize with zeros
        super().__init__([0] * num_registers)
    
    def getValues(self, address, count=1):
        address = address - 1  # Modbus addresses are 0-based
        """Get input register values from OpenPLC int_input using SafeBufferAccess"""
        print(f"[MODBUS] Input Registers getValues called: address={address}, count={count}")
        
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Error: Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count
        
        # Ensure buffer mutex
        self.safe_buffer_access.acquire_mutex()

        values = []
        for i in range(count):
            reg_addr = address + i
            
            if reg_addr < self.num_registers:
                value, error_msg = self.safe_buffer_access.read_int_input(reg_addr, thread_safe=False)
                if error_msg == "Success":
                    values.append(value)
                    print(f"[MODBUS] Read input register {reg_addr}: {value}")
                else:
                    print(f"[MODBUS] Error reading input register {reg_addr}: {error_msg}")
                    values.append(0)
            else:
                values.append(0)
        
        # Release mutex after access
        self.safe_buffer_access.release_mutex()

        return values
    
    def setValues(self, address, values):
        """Input registers are read-only, this method should not be called"""
        print(f"[MODBUS] Warning: Attempt to write to read-only input registers at address {address}")


class OpenPLCHoldingRegistersDataBlock(ModbusSparseDataBlock):
    """Custom Modbus holding registers data block that mirrors OpenPLC analog outputs using SafeBufferAccess"""
    
    def __init__(self, runtime_args, num_registers=32):
        self.runtime_args = runtime_args
        self.num_registers = num_registers
        
        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Warning: Failed to create safe buffer access for holding registers: {self.safe_buffer_access.error_msg}")
        
        # Initialize with zeros
        super().__init__([0] * num_registers)
    
    def getValues(self, address, count=1):
        address = address - 1  # Modbus addresses are 0-based
        """Get holding register values from OpenPLC int_output using SafeBufferAccess"""
        print(f"[MODBUS] Holding Registers getValues called: address={address}, count={count}")
        
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Error: Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count
        
        # Ensure buffer mutex
        self.safe_buffer_access.acquire_mutex() 

        values = []
        for i in range(count):
            reg_addr = address + i
            
            if reg_addr < self.num_registers:
                value, error_msg = self.safe_buffer_access.read_int_output(reg_addr, thread_safe=False)
                if error_msg == "Success":
                    values.append(value)
                    print(f"[MODBUS] Read holding register {reg_addr}: {value}")
                else:
                    print(f"[MODBUS] Error reading holding register {reg_addr}: {error_msg}")
                    values.append(0)
            else:
                values.append(0)
        
        # Release mutex after access
        self.safe_buffer_access.release_mutex()
        return values
    
    def setValues(self, address, values):
        address = address - 1  # Modbus addresses are 0-based
        """Set holding register values to OpenPLC int_output using SafeBufferAccess"""
        print(f"[MODBUS] Holding Registers setValues called: address={address}, values={values}")
        
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Error: Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return
        
        # Ensure buffer mutex
        self.safe_buffer_access.acquire_mutex()
        
        for i, value in enumerate(values):
            reg_addr = address + i
            
            if reg_addr < self.num_registers:
                success, error_msg = self.safe_buffer_access.write_int_output(reg_addr, value, thread_safe=False)
                if error_msg == "Success":
                    print(f"[MODBUS] Set holding register {reg_addr}: {value}")
                else:
                    print(f"[MODBUS] Error setting holding register {reg_addr}: {error_msg}")

        # Release mutex after access
        self.safe_buffer_access.release_mutex()

# Global variables for plugin lifecycle
server_task = None
server_context = None
runtime_args = None
running = False
gIp = "172.29.65.104"  # Default values
gPort = 5020

def init(args_capsule):
    """Initialize the Modbus plugin"""
    global runtime_args, server_context, gIp, gPort

    print("[MODBUS] Python plugin 'simple_modbus' initializing...")
    
    try:
        # Print structure validation info for debugging
        print("[MODBUS] Validating plugin structure alignment...")
        # PluginStructureValidator.print_structure_info()
        
        # Extract runtime args from capsule using safe method
        if hasattr(args_capsule, '__class__') and 'PyCapsule' in str(type(args_capsule)):
            # This is a PyCapsule from C - use safe extraction
            runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
            if runtime_args is None:
                print(f"[MODBUS] ✗ Failed to extract runtime args: {error_msg}")
                return False
            
            print(f"[MODBUS] ✓ Runtime arguments extracted successfully")
        else:
            # This is a direct object (for testing)
            runtime_args = args_capsule
            print(f"[MODBUS] ✓ Using direct runtime args for testing")
        
        # Try to load configuration from plugin_specific_config_file_path
        try:
            config_map, status = SafeBufferAccess(runtime_args).get_config_file_args_as_map()
            if status == "Success" and config_map:
                # Try to extract network configuration
                network_config = config_map.get('network_configuration', {})
                if network_config and 'host' in network_config and 'port' in network_config:
                    gIp = str(network_config['host'])
                    gPort = int(network_config['port'])
                    print(f"[MODBUS] ✓ Configuration loaded - Host: {gIp}, Port: {gPort}")
                else:
                    print(f"[MODBUS] ⚠ Config file loaded but network_configuration section missing or incomplete - using defaults")
                    print(f"[MODBUS] Available config sections: {list(config_map.keys())}")
            else:
                print(f"[MODBUS] ✗ Failed to load configuration file: {status} - using defaults")
        except Exception as config_error:
            print(f"[MODBUS] ⚠ Exception while loading config: {config_error} - using defaults")
            import traceback
            traceback.print_exc()

        # Safely access buffer size using validation
        buffer_size, size_error = runtime_args.safe_access_buffer_size()
        if buffer_size == -1:
            print(f"[MODBUS] ✗ Failed to access buffer size: {size_error}")
            return False
        
        # print(f"[MODBUS]   Buffer size: {buffer_size}")
        # print(f"[MODBUS]   Bits per buffer: {runtime_args.bits_per_buffer}")
        # print(f"[MODBUS]   Structure details: {runtime_args}")
        
        # Create OpenPLC-connected data blocks for all Modbus types
        coils_block = OpenPLCCoilsDataBlock(runtime_args, num_coils=64)
        discrete_inputs_block = OpenPLCDiscreteInputsDataBlock(runtime_args, num_inputs=64)
        input_registers_block = OpenPLCInputRegistersDataBlock(runtime_args, num_registers=32)
        holding_registers_block = OpenPLCHoldingRegistersDataBlock(runtime_args, num_registers=32)

        # Create device context with all OpenPLC-connected data blocks
        # print(f"[MODBUS] Created data blocks:")
        # print(f"[MODBUS]   - Coils (bool_output): {coils_block.num_coils} coils")
        # print(f"[MODBUS]   - Discrete Inputs (bool_input): {discrete_inputs_block.num_inputs} inputs")
        # print(f"[MODBUS]   - Input Registers (int_input): {input_registers_block.num_registers} registers")
        # print(f"[MODBUS]   - Holding Registers (int_output): {holding_registers_block.num_registers} registers")
        
        device = ModbusDeviceContext(
            di=discrete_inputs_block,      # Discrete Inputs -> bool_input
            co=coils_block,                # Coils -> bool_output
            ir=input_registers_block,      # Input Registers -> int_input
            hr=holding_registers_block     # Holding Registers -> int_output
        )
        server_context = ModbusServerContext(devices={1: device}, single=False)
        
        print(f"[MODBUS] ✓ Plugin initialized successfully - Host: {gIp}, Port: {gPort}")
        return True
        
    except Exception as e:
        print(f"[MODBUS] ✗ Plugin initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def start_loop():
    """Start the Modbus server"""
    global server_task, running, gIp, gPort
    
    if server_context is None:
        print("[MODBUS] Error: Plugin not initialized")
        return False
    
    print("[MODBUS] Server context is valid, proceeding with startup...")
    print(f"[MODBUS] Server context created successfully")
    
    running = True
    
    # Start server in separate thread with proper asyncio handling
    def run_server():
        try:
            print("[MODBUS] Creating new event loop for server thread...")
            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            print("[MODBUS] Event loop created successfully")
            
            # Start the server and keep it running
            async def start_server():
                try:
                    print(f"[MODBUS] Attempting to start TCP server on {gIp}:{gPort}...")
                    try:
                        server = await StartAsyncTcpServer(
                            context=server_context,
                            address=(gIp, gPort)
                        )
                        print(f"[MODBUS] Server successfully bound to {gIp}:{gPort}")
                    except Exception as bind_error:
                        print(f"[MODBUS] Failed to bind to {gIp}:{gPort}: {bind_error}")
                        print(f"[MODBUS] Attempting to bind to 0.0.0.0:{gPort} as fallback...")
                        server = await StartAsyncTcpServer(
                            context=server_context,
                            address=("0.0.0.0", gPort)
                        )
                        print(f"[MODBUS] Server successfully bound to 0.0.0.0:{gPort} (fallback)")
                    
                    # Keep the server running
                    try:
                        print("[MODBUS] Server is now running and accepting connections")
                        while running:
                            await asyncio.sleep(1)
                    except asyncio.CancelledError:
                        print("[MODBUS] Server cancelled")
                    finally:
                        print("[MODBUS] Shutting down server...")
                        if hasattr(server, 'close'):
                            server.close()
                            if hasattr(server, 'wait_closed'):
                                await server.wait_closed()
                        print("[MODBUS] Server shutdown complete")
                        
                except Exception as server_error:
                    print(f"[MODBUS] Error in start_server async function: {server_error}")
                    import traceback
                    print(f"[MODBUS] Traceback: {traceback.format_exc()}")
                    raise
            
            # Run the server
            print("[MODBUS] Running server event loop...")
            loop.run_until_complete(start_server())
            
        except Exception as e:
            print(f"[MODBUS] Error in run_server thread: {e}")
            import traceback
            print(f"[MODBUS] Full traceback: {traceback.format_exc()}")
        finally:
            print("[MODBUS] Closing event loop...")
            loop.close()
            print("[MODBUS] Event loop closed")
    
    server_task = threading.Thread(target=run_server, daemon=False)
    server_task.start()

    print(f"[MODBUS] Server thread started on {gIp}:{gPort}")
    return True

def stop_loop():
    """Stop the Modbus server"""
    global server_task, running
    
    running = False
    
    if server_task:
        # Stop the asyncio server
        try:
            asyncio.run(ServerStop())
        except:
            pass
        
        server_task.join(timeout=2.0)
        server_task = None
    
    print("[MODBUS] Server stopped")
    return True

def cleanup():
    """Cleanup plugin resources"""
    global server_context, runtime_args
    
    server_context = None
    runtime_args = None
    
    print("[MODBUS] Plugin cleaned up")
    return True

async def main():
    """Standalone server for testing"""
    # Create a proper mock runtime args that inherits from PluginRuntimeArgs
    import ctypes
    
    # Create a mock that has the required methods
    class MockArgs:
        def __init__(self):
            self.buffer_size = 1
            self.bits_per_buffer = 8
            # Create simple boolean list for testing
            self.bool_data = [[False] * 8]  # 1 buffer, 8 booleans
            self.bool_output = self.bool_data  # Simple reference
            self.mutex_take = None
            self.mutex_give = None
            self.buffer_mutex = None
        
        def safe_access_buffer_size(self):
            """Mock implementation of safe_access_buffer_size"""
            return self.buffer_size, "Success"
        
        def validate_pointers(self):
            """Mock implementation of validate_pointers"""
            return True, "Mock validation passed"
        
        def __str__(self):
            return f"MockArgs(buffer_size={self.buffer_size}, bits_per_buffer={self.bits_per_buffer})"
    
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
