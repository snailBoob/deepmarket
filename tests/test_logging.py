"""Diagnostic transitions and real browser WebSocket lifecycle."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from deep_market import __version__
from deep_market.feed import FeedBase
from deep_market.server import Engine, ws_handler


class FeedLoggingTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('deep_market.feed.time.monotonic', return_value=100)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.feed = FeedBase(None, 'TEST', '0.01')

    def test_stale_once_and_recovery_only_after_applied_book(self):
        f = self.feed
        f.synced = True
        with self.assertLogs('deep_market.feed', level='INFO') as logs:
            f.after_event(1000)
            self.now.return_value = 129
            f.check_book_freshness()
            self.now.return_value = 130
            f.check_book_freshness()
            self.now.return_value = 160
            f.on_trade(2000, '1', 2, 1)
            f.check_book_freshness()
            f.after_event(3000)
            f.after_event(4000)
        self.assertEqual(sum('book_stale' in m for m in logs.output), 1)
        self.assertEqual(sum('book_live' in m for m in logs.output), 2)
        self.assertIn('previous=stale', logs.output[-1])

    def test_waiting_for_first_sync_is_reported(self):
        self.now.return_value = 129
        with self.assertNoLogs('deep_market.feed'):
            self.feed.check_book_freshness()
        self.now.return_value = 130
        with self.assertLogs('deep_market.feed', level='WARNING') as logs:
            self.feed.check_book_freshness()
        self.assertIn('synced=False', logs.output[0])


class AsyncLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_reconnect_and_book_recovery(self):
        feed = FeedBase(None, 'TEST', '0.01')
        feed.synced = True
        session = AsyncMock(side_effect=ConnectionError('test outage'))
        with patch('deep_market.feed.asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertLogs('deep_market.feed', level='INFO') as logs:
                with self.assertRaises(asyncio.CancelledError):
                    await feed._reconnecting('depth', session)
                self.assertFalse(feed.synced)
                feed.connection_opened('depth')
                feed.synced = True
                feed.after_event(1000)
        self.assertIn('retry_s=1', logs.output[0])
        self.assertIn('reconnects=1', logs.output[1])
        self.assertIn('previous=disconnected', logs.output[2])

    async def test_browser_websocket_version_lifecycle_and_upgrade_failure(self):
        app = web.Application()
        app['engine'] = Engine(None, 14_400_000)
        app['sockets'] = set()
        app.router.add_get('/ws', ws_handler)
        async with TestClient(TestServer(app)) as client:
            with self.assertLogs('deep_market.server', level='INFO') as logs:
                ws = await client.ws_connect('/ws')
                meta = await ws.receive_json()
                self.assertEqual(meta['version'], __version__)
                await ws.close()
                response = await client.get('/ws')
                self.assertEqual(response.status, 400)
        messages = '\n'.join(logs.output)
        for event in ['browser_ws_connected', 'browser_ws_disconnected', 'browser_ws_upgrade_failed']:
            self.assertIn(event, messages)
        self.assertFalse(app['sockets'])
        self.assertFalse(app['engine'].listeners)


if __name__ == '__main__':
    unittest.main()
