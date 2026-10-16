"""Interaction HTTP safeguards. Never log tokens, request URLs or command values."""
import asyncio
import datetime
import logging
import math
import threading
import time
import traceback
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import aiohttp
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


class CooldownActive(Exception):
    """Raised before HTTP I/O; never includes request URLs or credentials."""


OPERATION = ContextVar('discord_operation', default='client')
METRICS = Counter()
BACKGROUND_LOCK = None
BACKGROUND_NEXT = 0
INTERACTION_SLOTS = None


def log_http_metrics():
    # Event-loop only: no awaits while taking/resetting this interval's counters.
    counts = dict(METRICS)
    METRICS.clear()
    logger.info('Discord HTTP metrics interval=60s counts=%s', counts)


def http_trace():
    """Count actual aiohttp attempts, including discord.py internal retries.

    Only fixed operation names and HTTP statuses are retained, never URLs.
    The start hook also blocks internal retries during a known cooldown.
    """
    trace = aiohttp.TraceConfig()

    async def start(session, context, params):
        if API_HEALTH.blocked():
            METRICS['suppressed'] += 1
            raise CooldownActive()
        context.operation = OPERATION.get()
        METRICS['attempts'] += 1
        METRICS['operation:' + context.operation] += 1

    async def end(session, context, params):
        METRICS['status:' + str(params.response.status)] += 1
        if params.response.status == 429:
            # Headers arrive before discord.py's own retry loop. Open the
            # circuit now so another coroutine cannot issue the next request.
            error = discord.HTTPException(params.response, {'message': 'rate limited'})
            API_HEALTH.fail(error, context.operation)

    async def failed(session, context, params):
        if not isinstance(params.exception, CooldownActive):
            METRICS['transport_errors'] += 1

    trace.on_request_start.append(start)
    trace.on_request_end.append(end)
    trace.on_request_exception.append(failed)
    return trace


@dataclass
class RequestResult:
    state: str
    value: object = None
    status: int = 0


async def controlled_request(operation, factory, timeout=20, *, background=False):
    """One logical operation; never replay writes. Background work is serial
    and spaced by >=0.5s, leaving interaction acknowledgements independent.
    This is an application traffic policy, not a hardcoded Discord limit.
    """
    global BACKGROUND_LOCK, BACKGROUND_NEXT
    async def execute():
        if API_HEALTH.blocked():
            METRICS['suppressed'] += 1
            return RequestResult('cooldown')
        generation = API_HEALTH.begin()
        token = OPERATION.set(operation)
        METRICS['logical:' + operation] += 1
        try:
            value = await factory()
            # Background successes must not claim that interactions work.
            if not background:
                API_HEALTH.success(generation)
            logger.info('Discord HTTP succeeded operation=%s', operation)
            return RequestResult('success', value)
        except CooldownActive:
            return RequestResult('cooldown')
        except discord.HTTPException as error:
            API_HEALTH.fail(error, operation)
            return RequestResult('rate_limited' if is_rate_limit(error) else 'http_error',
                                 status=error.status)
        except (TimeoutError, OSError) as error:
            API_HEALTH.fail(error, operation)
            return RequestResult('uncertain')
        finally:
            OPERATION.reset(token)

    async def run():
        global BACKGROUND_NEXT
        if not background:
            global INTERACTION_SLOTS
            if INTERACTION_SLOTS is None:
                INTERACTION_SLOTS = asyncio.Semaphore(4)
            async with INTERACTION_SLOTS:
                return await execute()  # includes queue time in interaction budget
        global BACKGROUND_LOCK
        if BACKGROUND_LOCK is None:
            BACKGROUND_LOCK = asyncio.Lock()
        async with BACKGROUND_LOCK:
            delay = max(0, BACKGROUND_NEXT - time.monotonic())
            if delay:
                await asyncio.sleep(delay)
            try:
                return await execute()  # recheck after waiting for the slot
            finally:
                BACKGROUND_NEXT = time.monotonic() + 0.5

    try:
        return await asyncio.wait_for(run(), timeout)
    except TimeoutError as error:
        API_HEALTH.fail(error, operation)
        return RequestResult('uncertain')


async def _request(operation, factory, timeout):
    result = await controlled_request(operation, factory, timeout)
    return result.state == 'success', result.value


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
