"""Offline robots and slow dashboard storage must not block the gateway."""
import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

from test_turn_spine import _SpineAdapter, NativeJiboAdapter

module = __import__(NativeJiboAdapter.__module__, fromlist=['device_status'])


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_device_keeps_its_adapter_registered_for_reconnection(self):
        adapter = _SpineAdapter()
        adapter.client.connected = False
        adapter.client.start = AsyncMock()
        async def offline(**kwargs):
            await asyncio.sleep(10)
            return False
        adapter.client.wait_until_connected = AsyncMock(side_effect=offline)
        adapter._start_turn_ingest = AsyncMock()
        adapter._mark_disconnected = lambda: None
        adapter._mark_connected = lambda: None
        self.assertTrue(await asyncio.wait_for(adapter.connect(), timeout=0.2))
        self.assertFalse(adapter.connected)
        adapter.client.start.assert_awaited_once()
        adapter.client.wait_until_connected.assert_not_called()
        if getattr(adapter, '_ingest_start_task', None):
            await adapter._ingest_start_task

    async def test_status_writes_run_off_loop_and_coalesce_in_order(self):
        started, finish = threading.Event(), threading.Event()
        writes = []

        def publish(snapshot):
            writes.append(snapshot['robot']['connected'])
            started.set()
            finish.wait(2)

        publisher = module.device_status.StatusPublisher()
        with patch.object(module.device_status, 'publish', publish):
            try:
                publisher.update({'robot': {'connected': True}})
                await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
                publisher.update({'robot': {'connected': True}})
                publisher.update({'robot': {'connected': False}})
                # If publication occupied the event loop these callbacks could
                # not run before the deliberately blocked writer is released.
                await asyncio.sleep(0)
                self.assertEqual(writes, [True])
                finish.set()
                await publisher.flush()
                self.assertEqual(writes, [True, False])
            finally:
                finish.set()
                await publisher.flush()


if __name__ == '__main__':
    unittest.main()
