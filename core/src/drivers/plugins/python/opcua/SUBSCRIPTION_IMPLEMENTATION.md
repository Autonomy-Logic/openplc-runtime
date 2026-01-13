# OPC-UA Subscription Implementation Plan

## Overview

This document outlines the implementation plan for adding OPC-UA subscription support to the OpenPLC OPC-UA plugin. Subscriptions enable push-based data updates, replacing inefficient polling with server-initiated notifications.

## Current State

The plugin currently uses a synchronization loop that:
1. Reads PLC memory every `cycle_time_ms` (default 100ms)
2. Updates OPC-UA node values
3. Clients must poll to get updated values

## Target State

With subscriptions:
1. Clients create subscriptions with desired parameters
2. Server monitors values and pushes changes automatically
3. Reduced network traffic and lower latency

## asyncua Subscription Support

The asyncua library provides built-in subscription support:

```python
# Client-side (for reference)
subscription = await client.create_subscription(period=100, handler=handler)
handle = await subscription.subscribe_data_change(node)

# Server-side (what we need to support)
# asyncua Server automatically handles subscriptions when clients request them
# We need to ensure our value updates trigger proper notifications
```

## Implementation Tasks

### Phase 1: Enable Native Subscription Support
- [ ] Verify asyncua server subscription handling works with current setup
- [ ] Ensure `set_value()` calls trigger data change notifications
- [ ] Test with UAExpert or similar client

### Phase 2: Optimize Value Updates
- [ ] Use `write_attribute()` with proper timestamps
- [ ] Implement source timestamps from PLC cycle
- [ ] Add server timestamps for audit trail

### Phase 3: Subscription Configuration
- [ ] Add subscription-related settings to config
- [ ] Configure default publishing intervals
- [ ] Set limits on max subscriptions/monitored items

### Phase 4: Advanced Features
- [ ] Deadband filtering for analog values
- [ ] Queue size configuration
- [ ] Sampling interval limits

## Key asyncua APIs

### Server Value Updates (triggers notifications)
```python
# Current approach - may not trigger notifications properly
await node.write_value(value)

# Recommended approach - explicit data value with timestamps
from asyncua import ua
dv = ua.DataValue(
    ua.Variant(value, variant_type),
    SourceTimestamp=source_time,
    ServerTimestamp=server_time
)
await node.write_attribute(ua.AttributeIds.Value, dv)
```

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
