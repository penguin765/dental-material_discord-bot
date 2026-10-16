import asyncio
import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import bot as app


class Sheet:
    def __init__(self, rows=None):
        self.rows = copy.deepcopy(rows or [])
        self.clear_count = 0

    def get_all_records(self, **kwargs):
        return [dict(zip(self.rows[0], row)) for row in self.rows[1:]] if self.rows else []

    def get_all_values(self):
        return [[str(value) for value in row] for row in self.rows]

    def update(self, *, values, **kwargs):
        self.rows = copy.deepcopy(values)

    def batch_clear(self, ranges):
        assert ranges == ['A2:G']
        self.rows = self.rows[:1]
        self.clear_count += 1


class Doc:
    def __init__(self):
        self.sheets = {}

    def worksheet(self, name):
        if name not in self.sheets:
            raise app.gspread.WorksheetNotFound(name)
        return self.sheets[name]

    def add_worksheet(self, title, **kwargs):
        self.sheets[title] = Sheet()
        return self.sheets[title]


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_client_recovers_http_after_login_rate_limit(self):
        # Real discord.py/aiohttp sessions and connectors; only HTTP I/O is mocked.
        old = app.DentalERPBot()
        for command in app.bot.tree.get_commands():
            old.tree.add_command(command)
        import aiohttp
        sessions = []
        real_session = aiohttp.ClientSession
        def make_session(**kwargs):
            session = real_session(**kwargs)
            sessions.append(session)
            return session
        response = SimpleNamespace(status=429, reason='Too Many Requests')
        async def rejected(*args, **kwargs):
            raise app.discord.HTTPException(response, 'temporary limit')
        with patch('discord.http.aiohttp.ClientSession', side_effect=make_session), \
             patch.object(old.http, 'request', rejected):
            with self.assertRaises(app.discord.HTTPException):
                await old.http.static_login('offline-test-token')
        await old.close()
        self.assertTrue(sessions[0].closed)
        fresh = app.replacement_client(old)
        try:
            with patch('discord.http.aiohttp.ClientSession', side_effect=make_session), \
                 patch.object(fresh.http, 'request', AsyncMock(return_value={'id': '123'})):
                result = await fresh.http.static_login('offline-test-token')
            self.assertFalse(sessions[-1].closed)
            self.assertEqual(result['id'], '123')
            self.assertEqual(len(fresh.tree.get_commands()), len(old.tree.get_commands()))
            self.assertFalse(fresh._initialized)
            self.assertFalse(fresh.commands_ready)
        finally:
            await fresh.close()

    async def asyncSetUp(self):
        app.API_HEALTH.reset()
        import discord_api
        discord_api.BACKGROUND_LOCK = None
        discord_api.BACKGROUND_NEXT = 0
        app.OUTBOX_LOCK = None
        app.GS_LOCK = None
        app.SETTLEMENT_LOCK = None
        app.DATA_READY = True
        app.IS_ORDER_OPEN = True
        app.ANNOUNCEMENT_CHANNEL_ID = None
        app.CACHE = {'members': [], 'products': [
            {'Item_ID': 'A', '品項名稱': 'Test', '單價': 10, '最低購買量': 1}],
            'orders': [], 'sys_config': {'IS_ORDER_OPEN': 'True'}}
        self.doc = Doc()
        self.orders = Sheet([app.ORDER_HEADERS, ['ORD-1', '123', 'Test', '1', 'A', 2, 20]])
        app.doc = self.doc
        app.orders_sheet = self.orders
        app.members_sheet = app.products_sheet = app.sys_sheet = Sheet()

    async def test_ambiguous_write_is_not_replayed(self):
        writes = []
        def write():
            writes.append('accepted')
            raise ConnectionError('response lost')
        with self.assertRaises(ConnectionError):
            await app.run_gsheet(write)
        self.assertEqual(writes, ['accepted'])

    async def test_cancellation_keeps_lock_until_worker_finishes(self):
        started, release = threading.Event(), threading.Event()
        def slow_write():
            started.set()
            release.wait(5)
        first = asyncio.create_task(app.run_gsheet(slow_write))
        try:
            while not started.is_set():
                await asyncio.sleep(.01)
            first.cancel()
            second = asyncio.create_task(app.run_gsheet(lambda: 'second'))
            await asyncio.sleep(.02)
            self.assertFalse(second.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(await second, 'second')

    async def test_cache_failure_retries_before_ready(self):
        client = app.DentalERPBot()
        reload = AsyncMock(side_effect=[False, True])
        async def no_delay(_):
            self.assertFalse(client._initialized)
        with patch.object(app, 'reload_cache', reload), \
             patch.object(app.asyncio, 'sleep', no_delay), \
             patch.object(app, 'get_sys_config', return_value=None), \
             patch.object(app, 'update_sys_config', AsyncMock()), \
             patch.object(client.tree, 'sync', AsyncMock()):
            await client._background_startup()
        self.assertEqual(reload.await_count, 2)
        self.assertTrue(client._initialized)
        self.assertTrue(client.commands_ready)

    async def test_failed_config_write_does_not_update_cache(self):
        with patch.object(app, 'run_gsheet', AsyncMock(side_effect=ConnectionError)):
            with self.assertRaises(ConnectionError):
                await app.update_sys_config('TEST', 'new')
        self.assertNotIn('TEST', app.CACHE['sys_config'])

    async def test_pending_settlement_blocks_orders(self):
        app.CACHE['sys_config']['PENDING_SETTLEMENT'] = '{}'
        self.assertFalse(app.orders_open())

    async def test_closed_state_cannot_be_overridden_by_stale_config(self):
        app.IS_ORDER_OPEN = False
        self.assertFalse(app.orders_open())

    async def test_settlement_recovers_after_orders_cleared(self):
        fail_once = True
        async def update(key, value):
            nonlocal fail_once
            if key == 'PENDING_SETTLEMENT' and value == '' and fail_once:
                fail_once = False
                raise ConnectionError('response lost after clear')
            app.CACHE['sys_config'][key] = str(value)
        with patch.object(app, 'update_sys_config', update), \
             patch.object(app, 'send_dm_task', AsyncMock()):
            await app.auto_close_order()
            self.assertEqual(len(self.orders.rows), 1)
            pending = json.loads(app.get_sys_config('PENDING_SETTLEMENT'))
            archive = copy.deepcopy(self.doc.sheets[pending['raw']].rows)
            await app.auto_close_order()
        self.assertEqual(self.doc.sheets[pending['raw']].rows, archive)
        self.assertEqual(len(archive), 2)
        self.assertEqual(app.get_sys_config('PENDING_SETTLEMENT'), '')
        self.assertFalse(app.IS_ORDER_OPEN)

    async def test_failed_archive_verification_keeps_orders(self):
        async def update(key, value):
            app.CACHE['sys_config'][key] = str(value)
        original = copy.deepcopy(self.orders.rows)
        with patch.object(app, 'update_sys_config', update), \
             patch.object(Sheet, 'get_all_values', return_value=[]):
            await app.auto_close_order()
        self.assertEqual(self.orders.rows, original)
        self.assertEqual(self.orders.clear_count, 0)
        self.assertTrue(app.get_sys_config('PENDING_SETTLEMENT'))

    async def test_outbox_failure_preserves_source_orders_and_recovery_checkpoint(self):
        async def update(key, value):
            app.CACHE['sys_config'][key] = str(value)
        original = copy.deepcopy(self.orders.rows)
        with patch.object(app, 'update_sys_config', update), \
             patch.object(app.outbox, 'enqueue', side_effect=ConnectionError('write uncertain')):
            await app.auto_close_order()
        self.assertEqual(self.orders.rows, original)
        self.assertEqual(self.orders.clear_count, 0)
        self.assertEqual(json.loads(app.get_sys_config('PENDING_SETTLEMENT'))['stage'], 'ready')

    async def test_repeated_close_does_not_destroy_archive(self):
        async def update(key, value):
            app.CACHE['sys_config'][key] = str(value)
        with patch.object(app, 'update_sys_config', update), \
             patch.object(app, 'send_dm_task', AsyncMock()):
            await app.auto_close_order()
            before = {k: copy.deepcopy(v.rows) for k, v in self.doc.sheets.items()}
            await app.auto_close_order()
        self.assertEqual(before, {k: v.rows for k, v in self.doc.sheets.items()})
        self.assertEqual(self.orders.clear_count, 1)

    async def test_readiness_is_not_http_liveness(self):
        with app.app.test_client() as client:
            self.assertEqual(client.get('/').status_code, 200)
            self.assertEqual(client.get('/readyz').status_code, 503)

    async def test_expired_view_disables_controls(self):
        view = app.ExpiringView(timeout=300)
        view.add_item(app.discord.ui.Button(label='Test'))
        view.message = AsyncMock()
        await view.on_timeout()
        self.assertTrue(view.children[0].disabled)
        view.message.edit.assert_awaited_once()

    async def test_privileged_role_cannot_be_claimed_by_name(self):
        app.CACHE['members'] = [{'姓名': 'Head', 'Discord_User_ID': '', '職位': '牙材長'}]
        interaction = AsyncMock()
        interaction.user.id = 123
        interaction.response.is_done = Mock(return_value=False)
        with patch.object(app, 'run_gsheet', AsyncMock()) as writes:
            await app.bind_name.callback(interaction, 'Head')
        writes.assert_not_awaited()
        self.assertIn('管理職位', interaction.followup.send.call_args.args[0])

    async def test_invalid_product_price_fails_cache_loading(self):
        app.products_sheet = Sheet([['Item_ID', '品項名稱', '單價'], ['A', 'Bad', '$100']])
        self.assertFalse(await app.reload_cache())
        self.assertFalse(app.DATA_READY)


if __name__ == '__main__':
    unittest.main()
