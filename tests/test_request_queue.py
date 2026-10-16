import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
import discord
import discord_api as api
import bot as app
import notification_outbox as outbox
from test_bot import Doc


def limited():
    return discord.HTTPException(SimpleNamespace(status=429, reason='limited',
        headers={'Retry-After': '60'}), {'message': 'limited'})


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        api.API_HEALTH.reset()
        api.METRICS.clear()
        api.BACKGROUND_LOCK = None
        api.BACKGROUND_NEXT = 0
        app.OUTBOX_LOCK = None

    async def test_waiting_background_job_rechecks_after_another_429(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def first():
            started.set()
            await release.wait()
            raise limited()
        second = AsyncMock()
        one = asyncio.create_task(api.controlled_request('one', first, background=True))
        await started.wait()
        two = asyncio.create_task(api.controlled_request('two', second, background=True))
        release.set()
        self.assertEqual((await one).state, 'rate_limited')
        self.assertEqual((await two).state, 'cooldown')
        second.assert_not_awaited()

    async def test_slow_background_does_not_block_interaction(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow():
            started.set()
            await release.wait()
        task = asyncio.create_task(api.controlled_request('slow', slow, background=True))
        await started.wait()
        try:
            result = await api.controlled_request('defer', AsyncMock(), timeout=.1)
            self.assertEqual(result.state, 'success')
        finally:
            release.set()
            await task

    async def test_actual_http_trace_counts_and_stops_internal_retry(self):
        # Real aiohttp HTTP/trace lifecycle against localhost, no Discord traffic.
        hits = []
        async def reject(request):
            hits.append(True)
            return web.Response(status=429, headers={'Retry-After': '60'})
        server = web.Application()
        server.router.add_get('/', reject)
        runner = web.AppRunner(server)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession(trace_configs=[api.http_trace()]) as session:
                async with session.get(f'http://127.0.0.1:{port}/') as response:
                    self.assertEqual(response.status, 429)
                with self.assertRaises(api.CooldownActive):
                    await session.get(f'http://127.0.0.1:{port}/')
            self.assertEqual(len(hits), 1)
            self.assertEqual(api.METRICS['attempts'], 1)
            self.assertEqual(api.METRICS['status:429'], 1)
        finally:
            await runner.cleanup()

    async def test_dm_does_not_send_after_fetch_triggers_another_rate_limit(self):
        user = SimpleNamespace(dm_channel=SimpleNamespace(send=AsyncMock()))
        async def fetch(*args):
            api.API_HEALTH.fail(limited(), 'other_request')
            return user
        fake = SimpleNamespace(get_user=lambda uid: None, fetch_user=fetch)
        with patch.object(app, 'bot', fake):
            result = await app.send_dm_task('123', discord.Embed(title='invoice'))
        self.assertEqual(result.state, 'cooldown')
        user.dm_channel.send.assert_not_awaited()

    async def test_outbox_survives_reload_and_does_not_repeat_sent_or_uncertain(self):
        doc = Doc()
        messages = [('123', discord.Embed(title='invoice'))]
        outbox.enqueue(doc, 'period1', messages)
        outbox.enqueue(doc, 'period1', messages)
        self.assertEqual(len(outbox.pending(doc)), 1)
        outbox.mark(doc, 'period1:123', 'sending')
        self.assertEqual(outbox.pending(doc), [])
        self.assertEqual(doc.sheets[outbox.TITLE].rows[1][3], 'uncertain')
        outbox.enqueue(doc, 'period1', messages)
        self.assertEqual(outbox.pending(doc), [])
        outbox.mark(doc, 'period1:123', 'sent')
        outbox.enqueue(doc, 'period1', messages)
        self.assertEqual(outbox.pending(doc), [])

    async def test_drain_preserves_rejected_delivery_for_later(self):
        doc = Doc()
        outbox.enqueue(doc, 'period1', [('123', discord.Embed(title='invoice'))])
        async def sheets(func):
            return func()
        fake = SimpleNamespace(is_ready=lambda: True)
        with patch.object(app, 'doc', doc), patch.object(app, 'DATA_READY', True), \
             patch.object(app, 'bot', fake), patch.object(app, 'run_gsheet', sheets), \
             patch.object(app, 'send_dm_task', AsyncMock(return_value=api.RequestResult('rate_limited'))):
            await app.drain_outbox()
        self.assertEqual(len(outbox.pending(doc)), 1)

    async def test_background_success_does_not_verify_interaction_health(self):
        await api.controlled_request('bill_send', AsyncMock(), background=True)
        self.assertEqual(api.API_HEALTH.snapshot()['state'], 'not_tested')

    async def test_drain_limits_batch_and_does_not_repeat_delivered_invoices(self):
        doc = Doc()
        outbox.enqueue(doc, 'period1', [(str(i), discord.Embed(title='invoice')) for i in range(8)])
        async def sheets(func):
            return func()
        send = AsyncMock(return_value=api.RequestResult('success'))
        with patch.object(app, 'doc', doc), patch.object(app, 'DATA_READY', True), \
             patch.object(app, 'bot', SimpleNamespace(is_ready=lambda: True)), \
             patch.object(app, 'run_gsheet', sheets), patch.object(app, 'send_dm_task', send):
            await app.drain_outbox()
            self.assertEqual(send.await_count, 5)
            await app.drain_outbox()
            self.assertEqual(send.await_count, 8)
            await app.drain_outbox()
            self.assertEqual(send.await_count, 8)

    async def test_timeout_is_uncertain_and_never_automatically_replayed(self):
        doc = Doc()
        outbox.enqueue(doc, 'period1', [('123', discord.Embed(title='invoice'))])
        async def sheets(func):
            return func()
        send = AsyncMock(return_value=api.RequestResult('uncertain'))
        with patch.object(app, 'doc', doc), patch.object(app, 'DATA_READY', True), \
             patch.object(app, 'bot', SimpleNamespace(is_ready=lambda: True)), \
             patch.object(app, 'run_gsheet', sheets), patch.object(app, 'send_dm_task', send):
            await app.drain_outbox()
            await app.drain_outbox()
        send.assert_awaited_once()
        self.assertEqual(doc.sheets[outbox.TITLE].rows[1][3], 'uncertain')


if __name__ == '__main__':
    unittest.main()
