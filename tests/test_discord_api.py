import asyncio
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from src import discord_api as api
from src import bot


def rate_error(headers=None):
    response = SimpleNamespace(status=429, reason='Too Many Requests', headers=headers or {})
    return discord.HTTPException(response, {'message': 'temporary limit', 'code': 0})


def interaction():
    return SimpleNamespace(id=123, command=SimpleNamespace(name='test'),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: False,
                                 send_message=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()))


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        api.API_HEALTH.reset()

    async def test_429_stops_business_operation_before_opening_period(self):
        event = interaction()
        event.response.defer.side_effect = rate_error({'Retry-After': '60'})
        with patch.object(bot, 'run_gsheet', AsyncMock()) as sheets:
            await bot.start_group_buy.callback(event, '2099-01-01 23:59')
        sheets.assert_not_awaited()
        event.followup.send.assert_not_awaited()
        self.assertEqual(api.API_HEALTH.snapshot()['state'], 'cooldown')

    async def test_second_interaction_does_not_request_during_cooldown(self):
        first, second = interaction(), interaction()
        first.response.defer.side_effect = rate_error({'Retry-After': '60'})
        self.assertFalse(await api.safe_defer(first))
        self.assertFalse(await api.safe_defer(second))
        second.response.defer.assert_not_awaited()

    async def test_error_handler_does_not_reply_to_429(self):
        event = interaction()
        await api.handle_interaction_error(event, rate_error({'Retry-After': '60'}))
        event.response.send_message.assert_not_awaited()
        event.followup.send.assert_not_awaited()

    async def test_failed_followup_is_not_automatically_replayed(self):
        event = interaction()
        event.followup.send.side_effect = rate_error({'Retry-After': '10'})
        self.assertIsNone(await api.safe_followup(event, 'test'))
        self.assertIsNone(await api.safe_followup(event, 'error fallback'))
        self.assertEqual(event.followup.send.await_count, 1)

    async def test_cooldown_expiry_requires_fresh_success_to_verify(self):
        now = [100.0]
        health = api.APIHealth(clock=lambda: now[0])
        with patch.object(api, 'API_HEALTH', health):
            health.fail(rate_error({'retry-after': '5.5'}), 'defer')
            self.assertEqual(health.snapshot()['retry_after_seconds'], 7)
            now[0] = 107
            self.assertEqual(health.snapshot()['state'], 'awaiting_verification')
            self.assertTrue(await api.safe_defer(interaction()))
            self.assertEqual(health.snapshot()['state'], 'verified')

    async def test_older_success_cannot_hide_newer_429(self):
        generation = api.API_HEALTH.begin()
        api.API_HEALTH.fail(rate_error({'Retry-After': '60'}), 'defer')
        api.API_HEALTH.success(generation)
        self.assertEqual(api.API_HEALTH.snapshot()['state'], 'cooldown')

    async def test_expired_interaction_is_not_sent_or_executed(self):
        event = interaction()
        event.created_at -= datetime.timedelta(seconds=4)
        self.assertFalse(await api.safe_defer(event))
        event.response.defer.assert_not_awaited()

    async def test_slow_initial_ack_times_out_without_running_open_period(self):
        event = interaction()
        async def slow_ack(**kwargs):
            await asyncio.sleep(10)
        event.response.defer.side_effect = slow_ack
        with patch.object(api, 'initial_budget', return_value=.01), \
             patch.object(bot, 'run_gsheet', AsyncMock()) as sheets:
            await bot.start_group_buy.callback(event, '2099-01-01 23:59')
        sheets.assert_not_awaited()
        event.followup.send.assert_not_awaited()
        self.assertEqual(api.API_HEALTH.snapshot()['state'], 'awaiting_verification')

    async def test_no_token_or_request_url_in_diagnostics(self):
        error = rate_error({'Retry-After': '60', 'Authorization': 'SECRET',
                            'X-RateLimit-Scope': 'shared', 'CF-Ray': 'ray-id'})
        with self.assertLogs('dental_bot.api', level='WARNING') as logs:
            api.API_HEALTH.fail(error, 'defer')
        self.assertNotIn('SECRET', str(logs.output))
        self.assertEqual(api.API_HEALTH.snapshot()['rate_limit_headers']['X-RateLimit-Scope'], 'shared')

    async def test_gateway_connected_is_not_ready_when_http_is_limited(self):
        api.API_HEALTH.fail(rate_error({'Retry-After': '10'}), 'defer')
        with patch.object(bot.bot, 'is_ready', return_value=True), \
             patch.object(bot, 'DATA_READY', True), \
             patch.object(bot.bot, 'commands_ready', True):
            response = bot.app.test_client().get('/readyz')
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json['discord'])
        self.assertEqual(response.json['interaction_api']['state'], 'cooldown')

    def test_missing_or_invalid_retry_after_uses_conservative_fallback(self):
        for value in (None, 'bad', 'nan', '-1', 'inf'):
            headers = {} if value is None else {'Retry-After': value}
            self.assertEqual(api.retry_seconds(rate_error(headers)), 900)

    def test_classify_429_sources(self):
        self.assertEqual(api.classify_429({'Retry-After': '60'}), 'cloudflare_ip_ban')
        self.assertEqual(api.classify_429({'Via': '1.1 google', 'X-RateLimit-Global': 'true'}), 'global')
        self.assertEqual(api.classify_429({'Via': '1.1 google', 'X-RateLimit-Scope': 'user'}), 'user')
        self.assertEqual(api.classify_429({'Via': '1.1 google'}), 'route')

    def test_cooldown_is_transient_for_discord_connect_loop(self):
        import aiohttp
        self.assertTrue(issubclass(api.CooldownActive, aiohttp.ClientError))

    async def test_route_429_in_trace_does_not_open_global_circuit(self):
        trace = api.http_trace()
        end = trace.on_request_end[0]
        response = SimpleNamespace(status=429, reason='Too Many Requests',
                                   headers={'Via': '1.1 google', 'X-RateLimit-Scope': 'user',
                                            'Retry-After': '2'})
        await end(None, SimpleNamespace(operation='followup'), SimpleNamespace(response=response))
        self.assertFalse(api.API_HEALTH.blocked())
        response.headers = {'Retry-After': '60'}  # Cloudflare edge ban: no Via
        await end(None, SimpleNamespace(operation='login'), SimpleNamespace(response=response))
        self.assertTrue(api.API_HEALTH.blocked())
        self.assertEqual(api.API_HEALTH.snapshot()['rate_limit_source'], 'cloudflare_ip_ban')


if __name__ == '__main__':
    unittest.main()
