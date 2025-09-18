import asyncio
import ctypes
import threading
import time
import sys
import os
from pymodbus.server import StartAsyncTcpServer, ServerStop
from pymodbus.datastore import (
    ModbusSparseDataBlock,
    ModbusDeviceContext,
    ModbusServerContext,
)

# Add the parent directory to Python path to find shared module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import the correct type definitions
from shared.python_plugin_types import (
    PluginRuntimeArgs, 
    safe_extract_runtime_args_from_capsule,
    SafeBufferAccess,
    PluginStructureValidator
)

class OpenPLCModbusDataBlock(ModbusSparseDataBlock):
    """Custom Modbus data block that mirrors OpenPLC bool_output using SafeBufferAccess"""
    
    def __init__(self, runtime_args, buffer_index=0, num_coils=64):
        self.runtime_args = runtime_args
        self.buffer_index = buffer_index
        self.num_coils = num_coils
        
        # Create safe buffer access wrapper
        self.safe_buffer_access = SafeBufferAccess(runtime_args)
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Warning: Failed to create safe buffer access: {self.safe_buffer_access.error_msg}")
        
        # Initialize with zeros
        super().__init__([0] * num_coils)
    
    def getValues(self, address, count=1):
        """Get coil values from OpenPLC bool_output using SafeBufferAccess"""
        print(f"[MODBUS] getValues called: address={address}, count={count}")
        address = address - 1
        
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Error: Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return [0] * count
        
        values = []
        for i in range(count):
            coil_addr = address + i
            
            # Use SafeBufferAccess to safely read the boolean value
            if coil_addr < self.num_coils:
                # Map coil address to buffer and bit indices
                # For now, use buffer_index and coil_addr as bit_idx
                if coil_addr < 8:  # 8 boolean values per buffer
                    value, error_msg = self.safe_buffer_access.read_bool_output(self.buffer_index, coil_addr)
                    if error_msg == "Success":
                        values.append(1 if value else 0)
                        print(f"[MODBUS] Read coil {coil_addr}: {value}")
                    else:
                        print(f"[MODBUS] Error reading coil {coil_addr}: {error_msg}")
                        values.append(0)
                else:
                    values.append(0)
            else:
                values.append(0)
        
        return values
    
    def setValues(self, address, values):
        """Set coil values to OpenPLC bool_output using SafeBufferAccess"""
        print(f"[MODBUS] setValues called: address={address}, values={values}")
        
        if not self.safe_buffer_access.is_valid:
            print(f"[MODBUS] Error: Safe buffer access not valid: {self.safe_buffer_access.error_msg}")
            return
        
        for i, value in enumerate(values):
            coil_addr = address + i
            
            # Use SafeBufferAccess to safely write the boolean value
            if coil_addr < self.num_coils:
                # Map coil address to buffer and bit indices
                # For now, use buffer_index and coil_addr as bit_idx
                if coil_addr < 8:  # 8 boolean values per buffer
                    success, error_msg = self.safe_buffer_access.write_bool_output(self.buffer_index, coil_addr, bool(value))
                    if error_msg == "Success":
                        print(f"[MODBUS] Set coil {coil_addr}: {bool(value)}")
                    else:
                        print(f"[MODBUS] Error setting coil {coil_addr}: {error_msg}")

# Global variables for plugin lifecycle
server_task = None
server_context = None
runtime_args = None
update_thread = None
running = False
gIp = "172.29.65.104"
gPort = 5020

def init(args_capsule, host="172.29.65.104", port=5020):
    """Initialize the Modbus plugin"""
    global runtime_args, server_context, gIp, gPort
    gIp = host
    gPort = port

    print("[MODBUS] Python plugin 'simple_modbus' initializing...")
    
    try:
        # Print structure validation info for debugging
        print("[MODBUS] Validating plugin structure alignment...")
        PluginStructureValidator.print_structure_info()
        
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
        
        # Safely access buffer size using validation
        buffer_size, size_error = runtime_args.safe_access_buffer_size()
        if buffer_size == -1:
            print(f"[MODBUS] ✗ Failed to access buffer size: {size_error}")
            return False
        
        print(f"[MODBUS]   Buffer size: {buffer_size}")
        print(f"[MODBUS]   Bits per buffer: {runtime_args.bits_per_buffer}")
        print(f"[MODBUS]   Structure details: {runtime_args}")
        
        # Create OpenPLC-connected coils data block
        coils_block = OpenPLCModbusDataBlock(runtime_args, buffer_index=0, num_coils=64)
        
        # Standard data blocks for other Modbus types
        di = ModbusSparseDataBlock([0] * 64)   # Discrete Inputs
        ir = ModbusSparseDataBlock([0] * 32)   # Input Registers (16-bit)
        hr = ModbusSparseDataBlock([0] * 32)   # Holding Registers (16-bit)

        # Create device context with OpenPLC-connected coils
        print(f"[MODBUS] coils_block created with {coils_block} coils")
        device = ModbusDeviceContext(di=di, co=coils_block, ir=ir, hr=hr)
        server_context = ModbusServerContext(devices={1: device}, single=False)
        
        print(f"[MODBUS] ✓ Plugin initialized successfully - Host: {host}, Port: {port}")
        return True
        
    except Exception as e:
        print(f"[MODBUS] ✗ Plugin initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def start_loop():
    """Start the Modbus server"""
    global server_task, running, update_thread, gIp, gPort
    
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
    global server_task, running, update_thread
    
    running = False
    
    if update_thread:
        update_thread.join(timeout=1.0)
        update_thread = None
    
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
