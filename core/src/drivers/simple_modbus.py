import asyncio
import ctypes
from ctypes import POINTER, c_bool, c_ubyte, c_uint16, c_uint32, c_uint64, c_int, c_void_p, CFUNCTYPE
import threading
import time
from pymodbus.server import StartAsyncTcpServer, ServerStop
from pymodbus.datastore import (
    ModbusSparseDataBlock,
    ModbusDeviceContext,
    ModbusServerContext,
)

class PluginRuntimeArgs(ctypes.Structure):
    """Python ctypes structure matching plugin_runtime_args_t"""
    _fields_ = [
        # Buffer arrays (using POINTER type for arrays)
        ("bool_input", POINTER(POINTER(c_bool * 8))),   # bool_input[BUFFER_SIZE][8] 
        ("bool_output", POINTER(POINTER(c_bool * 8))),  # bool_output[BUFFER_SIZE][8]
        ("byte_input", POINTER(POINTER(c_ubyte))),      # byte_input[BUFFER_SIZE]
        ("byte_output", POINTER(POINTER(c_ubyte))),     # byte_output[BUFFER_SIZE]
        ("int_input", POINTER(POINTER(c_uint16))),      # int_input[BUFFER_SIZE]
        ("int_output", POINTER(POINTER(c_uint16))),     # int_output[BUFFER_SIZE]
        ("dint_input", POINTER(POINTER(c_uint32))),     # dint_input[BUFFER_SIZE]
        ("dint_output", POINTER(POINTER(c_uint32))),    # dint_output[BUFFER_SIZE]
        ("lint_input", POINTER(POINTER(c_uint64))),     # lint_input[BUFFER_SIZE]
        ("lint_output", POINTER(POINTER(c_uint64))),    # lint_output[BUFFER_SIZE]
        ("int_memory", POINTER(POINTER(c_uint16))),     # int_memory[BUFFER_SIZE]
        ("dint_memory", POINTER(POINTER(c_uint32))),    # dint_memory[BUFFER_SIZE]
        ("lint_memory", POINTER(POINTER(c_uint64))),    # lint_memory[BUFFER_SIZE]
        
        # Mutex function pointers
        ("mutex_take", CFUNCTYPE(c_int, c_void_p)),           # int (*mutex_take)(pthread_mutex_t*)
        ("mutex_give", CFUNCTYPE(c_int, c_void_p)),           # int (*mutex_give)(pthread_mutex_t*)
        ("buffer_mutex", c_void_p),                           # pthread_mutex_t *buffer_mutex
        
        # Buffer size information
        ("buffer_size", c_int),                               # int buffer_size
        ("bits_per_buffer", c_int),                           # int bits_per_buffer
    ]

class OpenPLCModbusDataBlock(ModbusSparseDataBlock):
    """Custom Modbus data block that mirrors OpenPLC bool_output"""
    
    def __init__(self, runtime_args, buffer_index=0, num_coils=64):
        self.runtime_args = runtime_args
        self.buffer_index = buffer_index
        self.num_coils = num_coils
        
        # Initialize with zeros
        super().__init__([0] * num_coils)
    
    def getValues(self, address, count=1):
        """Get coil values from OpenPLC bool_output"""
        try:
            # Take mutex before reading
            if (hasattr(self.runtime_args, 'mutex_take') and 
                hasattr(self.runtime_args, 'buffer_mutex') and
                self.runtime_args.mutex_take and 
                self.runtime_args.buffer_mutex):
                self.runtime_args.mutex_take(self.runtime_args.buffer_mutex)
            
            values = []
            for i in range(count):
                coil_addr = address + i
                
                # Check if we have valid runtime args and buffer
                if (hasattr(self.runtime_args, 'bool_output') and 
                    self.runtime_args.bool_output and
                    coil_addr < self.num_coils and 
                    self.buffer_index < getattr(self.runtime_args, 'buffer_size', 1)):
                    
                    try:
                        # Extract bit from bool_output[buffer_index][byte_index]
                        byte_index = coil_addr // 8
                        bit_index = coil_addr % 8
                        
                        if byte_index < 8:  # 8 bytes per buffer
                            bool_array = self.runtime_args.bool_output[self.buffer_index]
                            # Get the boolean value directly
                            if bool_array and len(bool_array) > byte_index:
                                bit_value = bool(bool_array[byte_index].value if hasattr(bool_array[byte_index], 'value') else bool_array[byte_index])
                                values.append(1 if bit_value else 0)
                            else:
                                values.append(0)
                        else:
                            values.append(0)
                    except (IndexError, AttributeError, OSError):
                        values.append(0)
                else:
                    values.append(0)
            
            return values
            
        except Exception as e:
            # In case of any error, return zeros
            return [0] * count
        finally:
            # Release mutex
            if (hasattr(self.runtime_args, 'mutex_give') and 
                hasattr(self.runtime_args, 'buffer_mutex') and
                self.runtime_args.mutex_give and 
                self.runtime_args.buffer_mutex):
                try:
                    self.runtime_args.mutex_give(self.runtime_args.buffer_mutex)
                except:
                    pass
    
    def setValues(self, address, values):
        """Set coil values to OpenPLC bool_output"""
        try:
            # Take mutex before writing
            if (hasattr(self.runtime_args, 'mutex_take') and 
                hasattr(self.runtime_args, 'buffer_mutex') and
                self.runtime_args.mutex_take and 
                self.runtime_args.buffer_mutex):
                self.runtime_args.mutex_take(self.runtime_args.buffer_mutex)
            
            for i, value in enumerate(values):
                coil_addr = address + i
                
                # Check if we have valid runtime args and buffer
                if (hasattr(self.runtime_args, 'bool_output') and 
                    self.runtime_args.bool_output and
                    coil_addr < self.num_coils and 
                    self.buffer_index < getattr(self.runtime_args, 'buffer_size', 1)):
                    
                    try:
                        # Set bit in bool_output[buffer_index][byte_index]
                        byte_index = coil_addr // 8
                        
                        if byte_index < 8:  # 8 bytes per buffer
                            bool_array = self.runtime_args.bool_output[self.buffer_index]
                            if bool_array and len(bool_array) > byte_index:
                                # Set the boolean value directly
                                if hasattr(bool_array[byte_index], 'value'):
                                    bool_array[byte_index].value = bool(value)
                                else:
                                    bool_array[byte_index] = bool(value)
                    except (IndexError, AttributeError, OSError):
                        pass  # Ignore errors in setting values
            
        except Exception:
            pass  # Ignore any errors
        finally:
            # Release mutex
            if (hasattr(self.runtime_args, 'mutex_give') and 
                hasattr(self.runtime_args, 'buffer_mutex') and
                self.runtime_args.mutex_give and 
                self.runtime_args.buffer_mutex):
                try:
                    self.runtime_args.mutex_give(self.runtime_args.buffer_mutex)
                except:
                    pass

# Global variables for plugin lifecycle
server_task = None
server_context = None
runtime_args = None
update_thread = None
running = False

def init(args, host="127.0.0.1", port=5020):
    """Initialize the Modbus plugin"""
    global runtime_args, server_context
    
    runtime_args = args
    
    # Create OpenPLC-connected coils data block
    coils_block = OpenPLCModbusDataBlock(runtime_args, buffer_index=0, num_coils=64)
    
    # Standard data blocks for other Modbus types
    di = ModbusSparseDataBlock([0] * 64)   # Discrete Inputs
    ir = ModbusSparseDataBlock([0] * 32)   # Input Registers (16-bit)
    hr = ModbusSparseDataBlock([0] * 32)   # Holding Registers (16-bit)

    # Create device context with OpenPLC-connected coils
    device = ModbusDeviceContext(di=di, co=coils_block, ir=ir, hr=hr)
    server_context = ModbusServerContext(devices={1: device}, single=False)
    
    print(f"[MODBUS] Plugin initialized - Host: {host}, Port: {port}")
    return True

def start_loop():
    """Start the Modbus server"""
    global server_task, running, update_thread
    
    if server_context is None:
        print("[MODBUS] Error: Plugin not initialized")
        return False
    
    running = True
    
    # Start server in separate thread
    def run_server():
        asyncio.run(StartAsyncTcpServer(
            context=server_context,
            address=("172.29.65.104", 5020)
        ))
    
    server_task = threading.Thread(target=run_server, daemon=True)
    server_task.start()
    
    print("[MODBUS] Server started on 172.29.65.104:5020")
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
    # Mock runtime args for testing
    class MockArgs:
        def __init__(self):
            self.buffer_size = 1
            self.bits_per_buffer = 64
            # Create simple boolean list for testing
            self.bool_data = [[False] * 8]  # 1 buffer, 8 booleans
            self.bool_output = self.bool_data  # Simple reference
            self.mutex_take = None
            self.mutex_give = None
            self.buffer_mutex = None
    
    mock_args = MockArgs()
    
    # Initialize and start
    if init(mock_args):
        if start():
            print("Modbus server running on 172.29.65.104:5020")
            print("Press Ctrl+C to stop...")
            
            try:
                # Keep server running
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                print("\nStopping server...")
                stop()
                cleanup()
        else:
            print("Failed to start server")
    else:
        print("Failed to initialize plugin")

if __name__ == "__main__":
    asyncio.run(main())