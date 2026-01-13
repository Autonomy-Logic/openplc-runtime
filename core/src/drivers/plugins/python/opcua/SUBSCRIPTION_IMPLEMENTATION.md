# OPC-UA Subscription Implementation Plan

## Overview

This document outlines the implementation plan for adding OPC-UA subscription support to the OpenPLC OPC-UA plugin. Subscriptions enable push-based data updates, replacing inefficient polling with server-initiated notifications.

## Current State (UPDATED)

The plugin now supports subscriptions via asyncua's built-in subscription handling:
1. Reads PLC memory every `cycle_time_ms` (default 100ms)
2. Updates OPC-UA node values using `write_attribute_value()` with DataValue
3. Clients can create subscriptions and receive push notifications on value changes
4. Proper timestamps (SourceTimestamp, ServerTimestamp) are included

## Implementation Status

### Phase 1: Enable Native Subscription Support - COMPLETED
- [x] Verified asyncua server subscription handling works
- [x] Updated `_update_opcua_node()` to use `write_attribute_value()` with DataValue
- [x] Server reference passed to SynchronizationManager

### Phase 2: Optimize Value Updates - COMPLETED
- [x] Using `write_attribute_value()` with proper DataValue objects
- [x] SourceTimestamp set to PLC cycle time (when value was read)
- [x] ServerTimestamp set to processing time
- [x] StatusCode set to Good for valid values

### Phase 3: Subscription Configuration - PENDING
- [ ] Add subscription-related settings to config
- [ ] Configure default publishing intervals
- [ ] Set limits on max subscriptions/monitored items

### Phase 4: Advanced Features - PENDING
- [ ] Deadband filtering for analog values
- [ ] Queue size configuration
- [ ] Sampling interval limits

## Key asyncua APIs

### Server Value Updates (IMPLEMENTED)
```python
# Our implementation in synchronization.py:
from datetime import datetime, timezone
from asyncua import ua

# Create DataValue with timestamps
data_value = ua.DataValue(
    Value=ua.Variant(opcua_value, expected_type),
    StatusCode_=ua.StatusCode(ua.StatusCodes.Good),
    SourceTimestamp=self._cycle_timestamp,  # PLC cycle time
    ServerTimestamp=datetime.now(timezone.utc)
)

# Use write_attribute_value for optimal subscription triggering
await self.server.write_attribute_value(
    var_node.node.nodeid,
    data_value
)
```

This approach:
- Triggers data change notifications for subscribed clients
- Is faster than `write_value()` (fewer validation checks)
- Includes proper timestamps for audit trail
- Bypasses PreWrite callbacks (server-internal operation)

### Subscription Parameters
- **PublishingInterval**: How often server sends notifications (ms)
- **LifetimeCount**: Number of publishing intervals before subscription expires
- **MaxKeepAliveCount**: Max intervals without notification before keep-alive
- **MaxNotificationsPerPublish**: Limit notifications per publish response
- **Priority**: Relative priority among subscriptions

## Testing Strategy

1. **Unit Tests**: Mock asyncua server, verify notification triggers
2. **Integration Tests**: Real server with Python client
3. **Manual Testing**: UAExpert, Prosys OPC UA Browser
4. **Performance Tests**: Compare bandwidth with polling vs subscriptions

## References

- [asyncua Documentation](https://opcua-asyncio.readthedocs.io/)
- [OPC UA Part 4: Services - Subscription Services](https://reference.opcfoundation.org/Core/Part4/)
- [OPC UA Part 5: Information Model - Subscription](https://reference.opcfoundation.org/Core/Part5/)
