#!/usr/bin/env python3
"""
Example demonstrating comprehensive buffer access with the enhanced SafeBufferAccess class
This example shows how to use all the new read functions and batch operations for optimized mutex usage.
"""

import sys
import os

# Add the shared directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

from python_plugin_types import (
    PluginRuntimeArgs, 
    SafeBufferAccess, 
    safe_extract_runtime_args_from_capsule,
    PluginStructureValidator
)

def demonstrate_individual_operations(buffer_access):
    """
    Demonstrate individual read/write operations with thread-safe parameter
    """
    print("\n=== Individual Operations Demo ===")
    
    # Boolean operations
    print("\n1. Boolean Operations:")
    success, msg = buffer_access.write_bool_output(0, 0, True, thread_safe=True)
    print(f"Write bool_output[0][0] = True: {success} - {msg}")
    
    value, msg = buffer_access.read_bool_output(0, 0, thread_safe=True)
    print(f"Read bool_output[0][0]: {value} - {msg}")
    
    value, msg = buffer_access.read_bool_input(0, 0, thread_safe=True)
    print(f"Read bool_input[0][0]: {value} - {msg}")
    
    # Byte operations
    print("\n2. Byte Operations:")
    success, msg = buffer_access.write_byte_output(0, 42, thread_safe=True)
    print(f"Write byte_output[0] = 42: {success} - {msg}")
    
    value, msg = buffer_access.read_byte_output(0, thread_safe=True)
    print(f"Read byte_output[0]: {value} - {msg}")
    
    value, msg = buffer_access.read_byte_input(0, thread_safe=True)
    print(f"Read byte_input[0]: {value} - {msg}")
    
    # Int operations (16-bit)
    print("\n3. Int Operations (16-bit):")
    success, msg = buffer_access.write_int_output(0, 1234, thread_safe=True)
    print(f"Write int_output[0] = 1234: {success} - {msg}")
    
    value, msg = buffer_access.read_int_output(0, thread_safe=True)
    print(f"Read int_output[0]: {value} - {msg}")
    
    value, msg = buffer_access.read_int_input(0, thread_safe=True)
    print(f"Read int_input[0]: {value} - {msg}")
    
    # Memory operations
    print("\n4. Memory Operations:")
    success, msg = buffer_access.write_int_memory(0, 5678, thread_safe=True)
    print(f"Write int_memory[0] = 5678: {success} - {msg}")
    
    value, msg = buffer_access.read_int_memory(0, thread_safe=True)
    print(f"Read int_memory[0]: {value} - {msg}")
    
    # Dint operations (32-bit)
    print("\n5. Dint Operations (32-bit):")
    success, msg = buffer_access.write_dint_output(0, 123456789, thread_safe=True)
    print(f"Write dint_output[0] = 123456789: {success} - {msg}")
    
    value, msg = buffer_access.read_dint_output(0, thread_safe=True)
    print(f"Read dint_output[0]: {value} - {msg}")
    
    value, msg = buffer_access.read_dint_input(0, thread_safe=True)
    print(f"Read dint_input[0]: {value} - {msg}")
    
    success, msg = buffer_access.write_dint_memory(0, 987654321, thread_safe=True)
    print(f"Write dint_memory[0] = 987654321: {success} - {msg}")
    
    value, msg = buffer_access.read_dint_memory(0, thread_safe=True)
    print(f"Read dint_memory[0]: {value} - {msg}")
    
    # Lint operations (64-bit)
    print("\n6. Lint Operations (64-bit):")
    success, msg = buffer_access.write_lint_output(0, 1234567890123456789, thread_safe=True)
    print(f"Write lint_output[0] = 1234567890123456789: {success} - {msg}")
    
    value, msg = buffer_access.read_lint_output(0, thread_safe=True)
    print(f"Read lint_output[0]: {value} - {msg}")
    
    value, msg = buffer_access.read_lint_input(0, thread_safe=True)
    print(f"Read lint_input[0]: {value} - {msg}")
    
    success, msg = buffer_access.write_lint_memory(0, 9876543210987654321, thread_safe=True)
    print(f"Write lint_memory[0] = 9876543210987654321: {success} - {msg}")
    
    value, msg = buffer_access.read_lint_memory(0, thread_safe=True)
    print(f"Read lint_memory[0]: {value} - {msg}")

def demonstrate_batch_operations(buffer_access):
    """
    Demonstrate batch operations for optimized mutex usage
    """
    print("\n=== Batch Operations Demo ===")
    
    # Batch read operations
    print("\n1. Batch Read Operations:")
    read_operations = [
        ('bool_output', 0, 0),      # Read bool_output[0][0]
        ('byte_output', 0),         # Read byte_output[0]
        ('int_output', 0),          # Read int_output[0]
        ('dint_output', 0),         # Read dint_output[0]
        ('lint_output', 0),         # Read lint_output[0]
        ('int_memory', 0),          # Read int_memory[0]
        ('dint_memory', 0),         # Read dint_memory[0]
        ('lint_memory', 0),         # Read lint_memory[0]
    ]
    
    results, msg = buffer_access.batch_read_values(read_operations)
    print(f"Batch read result: {msg}")
    for i, (success, value, error_msg) in enumerate(results):
        op = read_operations[i]
        print(f"  {op[0]}[{op[1]}{',' + str(op[2]) if len(op) > 2 else ''}]: {success} - Value: {value} - {error_msg}")
    
    # Batch write operations
    print("\n2. Batch Write Operations:")
    write_operations = [
        ('bool_output', 1, True, 0),    # Write bool_output[1][0] = True
        ('byte_output', 1, 100),        # Write byte_output[1] = 100
        ('int_output', 1, 2000),        # Write int_output[1] = 2000
        ('dint_output', 1, 300000000),  # Write dint_output[1] = 300000000
        ('lint_output', 1, 4000000000000000000),  # Write lint_output[1] = 4000000000000000000
        ('int_memory', 1, 1500),        # Write int_memory[1] = 1500
        ('dint_memory', 1, 250000000),  # Write dint_memory[1] = 250000000
        ('lint_memory', 1, 3000000000000000000),  # Write lint_memory[1] = 3000000000000000000
    ]
    
    results, msg = buffer_access.batch_write_values(write_operations)
    print(f"Batch write result: {msg}")
    for i, (success, error_msg) in enumerate(results):
        op = write_operations[i]
        print(f"  {op[0]}[{op[1]}{',' + str(op[3]) if len(op) > 3 else ''}] = {op[2]}: {success} - {error_msg}")
    
    # Mixed batch operations
    print("\n3. Mixed Batch Operations:")
    read_ops = [
        ('bool_output', 1, 0),      # Read the boolean we just wrote
        ('byte_output', 1),         # Read the byte we just wrote
        ('int_output', 1),          # Read the int we just wrote
    ]
    
    write_ops = [
        ('bool_output', 2, False, 0),   # Write bool_output[2][0] = False
        ('byte_output', 2, 200),        # Write byte_output[2] = 200
        ('int_output', 2, 3000),        # Write int_output[2] = 3000
    ]
    
    results, msg = buffer_access.batch_mixed_operations(read_ops, write_ops)
    print(f"Mixed batch result: {msg}")
    
    if 'reads' in results:
        print("  Read results:")
        for i, (success, value, error_msg) in enumerate(results['reads']):
            op = read_ops[i]
            print(f"    {op[0]}[{op[1]}{',' + str(op[2]) if len(op) > 2 else ''}]: {success} - Value: {value} - {error_msg}")
    
    if 'writes' in results:
        print("  Write results:")
        for i, (success, error_msg) in enumerate(results['writes']):
            op = write_ops[i]
            print(f"    {op[0]}[{op[1]}{',' + str(op[3]) if len(op) > 3 else ''}] = {op[2]}: {success} - {error_msg}")

def demonstrate_manual_mutex_control(buffer_access):
    """
    Demonstrate manual mutex control for custom operations
    """
    print("\n=== Manual Mutex Control Demo ===")
    
    # Acquire mutex manually
    success, msg = buffer_access.acquire_mutex()
    print(f"Manual mutex acquisition: {success} - {msg}")
    
    if success:
        try:
            # Perform multiple operations without individual mutex overhead
            print("Performing operations with manual mutex control:")
            
            # Read some values (thread_safe=False since we already have the mutex)
            value, msg = buffer_access.read_bool_output(0, 0, thread_safe=False)
            print(f"  Read bool_output[0][0]: {value} - {msg}")
            
            value, msg = buffer_access.read_byte_output(0, thread_safe=False)
            print(f"  Read byte_output[0]: {value} - {msg}")
            
            # Write some values (thread_safe=False since we already have the mutex)
            success, msg = buffer_access.write_bool_output(3, 0, True, thread_safe=False)
            print(f"  Write bool_output[3][0] = True: {success} - {msg}")
            
            success, msg = buffer_access.write_byte_output(3, 255, thread_safe=False)
            print(f"  Write byte_output[3] = 255: {success} - {msg}")
            
        finally:
            # Always release the mutex
            success, msg = buffer_access.release_mutex()
            print(f"Manual mutex release: {success} - {msg}")

def demonstrate_thread_safe_parameter(buffer_access):
    """
    Demonstrate the thread_safe parameter usage
    """
    print("\n=== Thread-Safe Parameter Demo ===")
    
    print("1. Operations with thread_safe=True (default):")
    value, msg = buffer_access.read_byte_output(0, thread_safe=True)
    print(f"  Read with mutex: {value} - {msg}")
    
    print("2. Operations with thread_safe=False (manual mutex control):")
    # This would normally be used when you've manually acquired the mutex
    # For demo purposes, we'll show it works but note it's not thread-safe
    value, msg = buffer_access.read_byte_output(0, thread_safe=False)
    print(f"  Read without mutex: {value} - {msg}")
    print("  Note: thread_safe=False should only be used with manual mutex control!")

def main():
    """
    Main demonstration function
    Note: This is a demonstration of the API. In a real plugin, you would receive
    the runtime_args from the OpenPLC runtime via the plugin interface.
    """
    print("OpenPLC Python Plugin Buffer Access Demonstration")
    print("=" * 60)
    
    # Print structure information
    print("\nStructure Validation:")
    PluginStructureValidator.print_structure_info()
    
    print("\n" + "=" * 60)
    print("IMPORTANT NOTE:")
    print("This is a demonstration of the SafeBufferAccess API.")
    print("In a real plugin, you would receive the runtime_args structure")
    print("from the OpenPLC runtime via the plugin interface.")
    print("The following demonstrations show the API usage patterns.")
    print("=" * 60)
    
    # In a real plugin, you would get runtime_args from the plugin interface
    # For demonstration purposes, we'll show the API patterns
    print("\nAPI Usage Patterns:")
    
    print("\n1. Extracting runtime args from capsule:")
    print("   runtime_args, error = safe_extract_runtime_args_from_capsule(capsule)")
    print("   if runtime_args is None:")
    print("       print(f'Failed to extract runtime args: {error}')")
    print("       return")
    
    print("\n2. Creating SafeBufferAccess instance:")
    print("   buffer_access = SafeBufferAccess(runtime_args)")
    print("   if not buffer_access.is_valid:")
    print("       print(f'Invalid buffer access: {buffer_access.error_msg}')")
    print("       return")
    
    print("\n3. Individual operations:")
    print("   # Read operations")
    print("   value, msg = buffer_access.read_bool_input(0, 0)")
    print("   value, msg = buffer_access.read_byte_input(0)")
    print("   value, msg = buffer_access.read_int_input(0)")
    print("   value, msg = buffer_access.read_dint_input(0)")
    print("   value, msg = buffer_access.read_lint_input(0)")
    print("   value, msg = buffer_access.read_int_memory(0)")
    print("   value, msg = buffer_access.read_dint_memory(0)")
    print("   value, msg = buffer_access.read_lint_memory(0)")
    
    print("\n   # Write operations")
    print("   success, msg = buffer_access.write_bool_output(0, 0, True)")
    print("   success, msg = buffer_access.write_byte_output(0, 255)")
    print("   success, msg = buffer_access.write_int_output(0, 65535)")
    print("   success, msg = buffer_access.write_dint_output(0, 4294967295)")
    print("   success, msg = buffer_access.write_lint_output(0, 18446744073709551615)")
    print("   success, msg = buffer_access.write_int_memory(0, 1000)")
    print("   success, msg = buffer_access.write_dint_memory(0, 2000000)")
    print("   success, msg = buffer_access.write_lint_memory(0, 3000000000)")
    
    print("\n4. Batch operations for optimized mutex usage:")
    print("   # Batch reads")
    print("   read_ops = [('bool_input', 0, 0), ('byte_input', 0), ('int_input', 0)]")
    print("   results, msg = buffer_access.batch_read_values(read_ops)")
    
    print("\n   # Batch writes")
    print("   write_ops = [('bool_output', 0, True, 0), ('byte_output', 0, 100)]")
    print("   results, msg = buffer_access.batch_write_values(write_ops)")
    
    print("\n   # Mixed batch operations")
    print("   results, msg = buffer_access.batch_mixed_operations(read_ops, write_ops)")
    
    print("\n5. Manual mutex control:")
    print("   success, msg = buffer_access.acquire_mutex()")
    print("   try:")
    print("       # Multiple operations with thread_safe=False")
    print("       value, msg = buffer_access.read_byte_input(0, thread_safe=False)")
    print("       success, msg = buffer_access.write_byte_output(0, 50, thread_safe=False)")
    print("   finally:")
    print("       success, msg = buffer_access.release_mutex()")
    
    print("\n6. Thread-safe parameter usage:")
    print("   # Default behavior (thread_safe=True)")
    print("   value, msg = buffer_access.read_byte_input(0)")
    print("   # Manual mutex control (thread_safe=False)")
    print("   value, msg = buffer_access.read_byte_input(0, thread_safe=False)")
    
    print("\n" + "=" * 60)
    print("Key Benefits:")
    print("- Complete read/write access to all IEC buffer types")
    print("- Optional thread-safe parameter for all operations")
    print("- Batch operations for optimized mutex usage")
    print("- Manual mutex control for custom operation sequences")
    print("- Comprehensive error handling and validation")
    print("- Maximum flexibility for plugin developers")
    print("=" * 60)

if __name__ == "__main__":
    main()
