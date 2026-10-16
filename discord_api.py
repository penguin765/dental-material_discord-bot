"""Interaction HTTP safeguards. Never log tokens, request URLs or command values."""
import asyncio
import datetime
import logging
import math
import threading
import time
import traceback
from pathlib import Path

import discord

logger = logging.getLogger('dental_bot.api')
SAFE_HEADERS = ('Retry-After', 'X-RateLimit-Scope', 'X-RateLimit-Global',
                'X-RateLimit-Reset-After', 'CF-Ray', 'Server')


def is_rate_limit(error):
    return isinstance(error, discord.HTTPException) and error.status == 429


def retry_seconds(error):
    headers = getattr(getattr(error, 'response', None), 'headers', {}) or {}
    raw = next((v for k, v in headers.items() if k.lower() == 'retry-after'), None)
    try:
        seconds = float(raw)
        if math.isfinite(seconds) and seconds >= 0:
            return max(1, seconds + 1)  # avoid retrying before fractional limits expire
    except (TypeError, ValueError):
        pass
    # No reliable server deadline: pause conservatively, never repeatedly probe.
    return 900


class APIHealth:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.until = 0
            self.unresolved = False
            self.verified = False
            self.failure = None
            self.headers = {}
            self.generation = 0

    def snapshot(self):
        with self.lock:
            remaining = max(0, math.ceil(self.until - self.clock()))
            state = ('cooldown' if remaining else 'awaiting_verification' if self.unresolved
                     else 'verified' if self.verified else 'not_tested')
            return {'state': state, 'retry_after_seconds': remaining,
                    'last_failure': self.failure, 'rate_limit_headers': dict(self.headers)}

    def blocked(self):
        return self.snapshot()['retry_after_seconds'] > 0

    def begin(self):
        with self.lock:
            return self.generation

    def success(self, generation):
        with self.lock:
            # A request started before another request's 429 cannot clear its failure.
            if generation == self.generation:
                self.verified = True
                self.unresolved = False

    def fail(self, error, operation):
        headers = getattr(getattr(error, 'response', None), 'headers', {}) or {}
        safe = {key: str(value).replace('\n', '').replace('\r', '')[:100]
                for key, value in headers.items()
                if key.lower() in {name.lower() for name in SAFE_HEADERS}}
        limited = is_rate_limit(error)
        with self.lock:
            self.generation += 1
            self.unresolved = True
            self.failure = 'http_429' if limited else type(error).__name__
            if limited:
                self.until = max(self.until, self.clock() + retry_seconds(error))
                self.headers = safe
        logger.warning('Discord HTTP failure operation=%s status=%s code=%s cooldown=%ss headers=%s',
                       operation, getattr(error, 'status', None), getattr(error, 'code', None),
                       self.snapshot()['retry_after_seconds'], safe)


API_HEALTH = APIHealth()


async def _request(operation, factory, timeout):
    if API_HEALTH.blocked():
        logger.info('Discord HTTP suppressed operation=%s cooldown=%ss', operation,
                    API_HEALTH.snapshot()['retry_after_seconds'])
        return False, None
    generation = API_HEALTH.begin()
    try:
        result = await asyncio.wait_for(factory(), timeout=timeout)
    except discord.HTTPException as error:
        if is_rate_limit(error):
            API_HEALTH.fail(error, operation)
            return False, None
        # An expired interaction cannot be recovered by replying to it again.
        if getattr(error, 'code', None) in (10062, 40060):
            logger.warning('Interaction expired/already acknowledged operation=%s code=%s',
                           operation, error.code)
            return False, None
        API_HEALTH.fail(error, operation)
        return False, None
    except (TimeoutError, OSError) as error:
        API_HEALTH.fail(error, operation)
        return False, None
    API_HEALTH.success(generation)
    logger.info('Discord HTTP succeeded operation=%s', operation)
    return True, result


def initial_budget(interaction):
    created = getattr(interaction, 'created_at', None)
    if isinstance(created, datetime.datetime):
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds()
        return max(0, min(2.5, 2.8 - elapsed))
    return 2.5


async def initial_response(interaction, method, *args, **kwargs):
    command = getattr(getattr(interaction, 'command', None), 'name', None)
    logger.info('Interaction received id=%s command=%s response=%s',
                getattr(interaction, 'id', None), command if isinstance(command, str) else None, method)
    budget = initial_budget(interaction)
    if budget <= 0:
        logger.warning('Interaction arrived too late; no business operation executed')
        return False
    success, _ = await _request(method,
        lambda: getattr(interaction.response, method)(*args, **kwargs), budget)
    return success


async def safe_defer(interaction):
    if interaction.response.is_done() is True:
        return True
    return await initial_response(interaction, 'defer', ephemeral=True)


async def safe_initial_reply(interaction, *args, **kwargs):
    return await initial_response(interaction, 'send_message', *args, **kwargs)


async def safe_modal(interaction, modal):
    return await initial_response(interaction, 'send_modal', modal)


async def safe_followup(interaction, *args, **kwargs):
    success, result = await _request('followup',
        lambda: interaction.followup.send(*args, **kwargs), 20)
    return result if success else None


async def handle_interaction_error(interaction, error):
    original = getattr(error, 'original', error)
    if is_rate_limit(original):
        API_HEALTH.fail(original, 'error_handler')
        return  # replying to a 429 with another HTTP request only adds failures
    frames = [(Path(frame.filename).name, frame.lineno, frame.name)
              for frame in traceback.extract_tb(original.__traceback__)]
    logger.error('Interaction handler failed error_type=%s frames=%s', type(original).__name__, frames)
    if API_HEALTH.blocked():
        return
    message = '❌ 指令處理失敗，請將操作時間提供給牙材長；勿立即重複下單。'
    try:
        if interaction.response.is_done() is True:
            await safe_followup(interaction, message, ephemeral=True)
        else:
            await safe_initial_reply(interaction, message, ephemeral=True)
    except discord.HTTPException as reply_error:
        # Record failure type without leaking interaction-token URLs in exception text.
        logger.warning('Error reply failed status=%s code=%s', reply_error.status, reply_error.code)
