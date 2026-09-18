"""A value the plugin could not read must be reported Bad, never defaulted.

This is the behaviour the STRING bug hid behind. Every STRING read returned ''
with a Good status while the PLC held a value, so from the client's side a
broken server and an empty string were the same answer. The fix was not only to
read strings correctly — it was to stop a failed read from looking like a
successful one, because that is what made the failure invisible for so long.

Covered here:
- the read callback stamps BadNoDataAvailable when the leaf does not read
- it stamps Good when it does
- an ARRAY whose element fails is marked Bad too, rather than pushed as a
  default at Good (the read callback was fixed, the subscription push was not)

The wire codec has its own suite next door in test_string_types.py.
"""

import asyncio

import pytest
from asyncua import ua

from opcua_types import VariableNode
from synchronization import SynchronizationManager


class UnreadableArgs:
    """A runtime that answers every read with "nothing". `debug_read`
    returning 0 is how the plugin learns a leaf gave it no bytes."""

    def debug_read(self, arr, elem, dest):
        return 0


class ReadableArgs:
    """One DINT leaf that reads as 7, and any other leaf that does not read.

    `elem` 0 succeeds and the rest fail, which is what makes the partially
    readable ARRAY below possible."""

    def __init__(self, good_elems=(0,)):
        self.good_elems = good_elems

    def debug_read(self, arr, elem, dest):
        # `debug_read` is called with ctypes scalars, not ints — unwrap the way
        # the plugin's own callers do.
        if int(getattr(elem, "value", elem)) not in self.good_elems:
            return 0
        for i, b in enumerate((7).to_bytes(4, "little")):
            dest[i] = b
        return 4


def _node(datatype="DINT", array_length=0):
    return VariableNode(
        node=None,
        arr=0,
        elem=0,
        datatype=datatype,
        access_mode="r",
        array_length=array_length,
    )


def _callback(args, node, arr=0, elem=0):
    mgr = SynchronizationManager(args, {}, server=None)
    return mgr._make_read_callback(arr, elem, node)


class TestReadCallbackStatus:
    def test_a_leaf_that_does_not_read_is_bad(self):
        dv = _callback(UnreadableArgs(), _node())(None, None)
        assert dv.StatusCode_.value == ua.StatusCodes.BadNoDataAvailable

    def test_a_leaf_that_reads_is_good(self):
        dv = _callback(ReadableArgs(), _node())(None, None)
        assert dv.StatusCode_.value == ua.StatusCodes.Good
        assert dv.Value.Value == 7

    def test_a_string_that_does_not_read_is_bad_not_empty_and_good(self):
        # The exact shape of the original bug: '' stamped Good.
        dv = _callback(UnreadableArgs(), _node("STRING"))(None, None)
        assert dv.StatusCode_.value == ua.StatusCodes.BadNoDataAvailable

    def test_an_array_with_an_unreadable_element_is_bad(self):
        dv = _callback(ReadableArgs(good_elems=(0,)), _node(array_length=3))(None, None)
        assert dv.StatusCode_.value == ua.StatusCodes.BadNoDataAvailable


class TestSubscriptionPushStatus:
    """The push path kept its own copy of the mistake the callback lost."""

    def test_an_array_element_that_fails_is_not_pushed_as_good(self):
        pushed = {}

        mgr = SynchronizationManager(ReadableArgs(good_elems=(0,)), {}, server=None)

        async def fake_push(node, variant, status=ua.StatusCodes.Good):
            pushed["status"] = status
            pushed["values"] = variant.Value

        mgr._push_value = fake_push
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            mgr._push_array_node(_node(array_length=3), 0, 0)
        )

        assert pushed["status"] == ua.StatusCodes.BadNoDataAvailable
        # The substituted defaults still travel — a client that ignores the
        # status gets a well-formed array rather than a fault — but the status
        # is what says they are not real.
        assert len(pushed["values"]) == 3

    def test_a_fully_readable_array_is_good(self):
        pushed = {}
        mgr = SynchronizationManager(ReadableArgs(good_elems=(0, 1, 2)), {}, server=None)

        async def fake_push(node, variant, status=ua.StatusCodes.Good):
            pushed["status"] = status

        mgr._push_value = fake_push
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            mgr._push_array_node(_node(array_length=3), 0, 0)
        )
        assert pushed["status"] == ua.StatusCodes.Good
