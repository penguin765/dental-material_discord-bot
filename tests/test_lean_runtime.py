import asyncio
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from src import bot as app
from src import notification_outbox as outbox
from src.runtime_metrics import memory_snapshot
from test_bot import Doc, Sheet


class LeanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app.API_HEALTH.reset()
        app.GS_LOCK = app.CATALOG_LOCK = app.OUTBOX_LOCK = None
        app.CATALOG_LOADED_AT = time.monotonic()
        app.DATA_READY = True
        app.IS_ORDER_OPEN = True
        app.doc = Doc()
        app.members_sheet = Sheet([["Discord_User_ID", "姓名", "組別", "職位"], ["123", "Test", "1", "一般"]])
        app.products_sheet = Sheet([["Item_ID", "品項名稱", "單價", "最低購買量"], ["A", "Test", 10, 1]])
        app.orders_sheet = Sheet([app.ORDER_HEADERS])
        app.sys_sheet = Sheet([["設定項目", "設定值"], ["IS_ORDER_OPEN", "True"]])
        app.CACHE = {"members": app.members_sheet.get_all_records(),
                     "products": app.products_sheet.get_all_records(), "sys_config": {"IS_ORDER_OPEN": "True"}}

    async def test_catalog_reload_never_reads_or_retains_orders(self):
        with patch.object(app.orders_sheet, "get_all_records", side_effect=AssertionError("orders must not be cached")):
            self.assertTrue(await app.reload_cache())
        self.assertNotIn("orders", app.CACHE)

    async def test_catalog_ttl_avoids_network_on_repeated_commands(self):
        with patch.object(app, "reload_cache", AsyncMock()) as reload:
            self.assertTrue(await app.ensure_catalog())
            self.assertTrue(await app.ensure_catalog())
        reload.assert_not_awaited()

    async def test_concurrent_expired_catalog_requests_share_one_refresh(self):
        app.CATALOG_LOADED_AT = None
        async def load():
            await asyncio.sleep(.01)
            app.CATALOG_LOADED_AT = time.monotonic()
            return True
        with patch.object(app, "reload_cache", AsyncMock(side_effect=load)) as reload:
            self.assertEqual(await asyncio.gather(app.ensure_catalog(), app.ensure_catalog()), [True, True])
        reload.assert_awaited_once()

    async def test_initial_ack_precedes_catalog_network_io(self):
        order = []
        async def ack(_): order.append("ack"); return True
        async def catalog(): order.append("sheets"); return True
        with patch.object(app, "api_safe_defer", ack), patch.object(app, "ensure_catalog", catalog):
            self.assertTrue(await app.safe_defer(SimpleNamespace()))
        self.assertEqual(order, ["ack", "sheets"])

    async def test_failed_catalog_stops_operation_after_ack(self):
        with patch.object(app, "api_safe_defer", AsyncMock(return_value=True)), \
             patch.object(app, "ensure_catalog", AsyncMock(return_value=False)), \
             patch.object(app, "safe_followup", AsyncMock()) as reply:
            self.assertFalse(await app.safe_defer(SimpleNamespace()))
        reply.assert_awaited_once()

    async def test_my_orders_reads_fresh_sheet_after_external_change(self):
        event = SimpleNamespace(user=SimpleNamespace(id=123))
        app.orders_sheet.rows.append(["ORD-1", "123", "Test", "1", "A", 2, 20])
        with patch.object(app, "safe_defer", AsyncMock(return_value=True)), \
             patch.object(app, "send_view", AsyncMock()) as reply:
            await app.my_orders.callback(event)
            self.assertIn("20", reply.call_args.kwargs["embed"].description)
            app.orders_sheet.rows[1][5:] = [7, 70]
            await app.my_orders.callback(event)
            self.assertIn("70", reply.call_args.kwargs["embed"].description)
        self.assertNotIn("orders", app.CACHE)

    async def test_same_submission_is_not_appended_twice(self):
        event = SimpleNamespace(id=456, user=SimpleNamespace(id=123))
        form = SimpleNamespace(inputs=[(app.CACHE["products"][0], SimpleNamespace(value="2"))])
        with patch.object(app, "safe_defer", AsyncMock(return_value=True)), \
             patch.object(app, "safe_followup", AsyncMock()):
            await app.MultiOrderModal.on_submit(form, event)
            await app.MultiOrderModal.on_submit(form, event)
        self.assertEqual(len(app.orders_sheet.rows), 2)
        self.assertEqual(app.orders_sheet.rows[1][0], "ORD-456-0")
        self.assertNotIn("orders", app.CACHE)

    async def test_message_cache_is_disabled(self):
        client = app.DentalERPBot()
        try:
            self.assertIsNone(client._connection.max_messages)
        finally:
            await client.close()

    async def test_setup_does_not_register_idle_polling(self):
        client = app.DentalERPBot()
        scheduler = Mock(running=False)
        try:
            with patch.object(app, "scheduler", scheduler):
                await client.setup_hook()
            scheduler.add_job.assert_not_called()
        finally:
            await client.close()

    async def test_empty_outbox_does_not_schedule_another_scan(self):
        async def sheets(func): return func()
        with patch.object(app, "bot", SimpleNamespace(is_ready=lambda: True)), \
             patch.object(app, "run_gsheet", sheets), patch.object(app, "request_outbox_drain") as schedule:
            await app.drain_outbox()
        schedule.assert_not_called()

    async def test_queued_batch_schedules_only_when_more_work_might_exist(self):
        outbox.enqueue(app.doc, "period", [(str(i), discord.Embed(title="bill")) for i in range(6)])
        async def sheets(func): return func()
        with patch.object(app, "bot", SimpleNamespace(is_ready=lambda: True)), \
             patch.object(app, "run_gsheet", sheets), \
             patch.object(app, "send_dm_task", AsyncMock(return_value=SimpleNamespace(state="success"))), \
             patch.object(app, "request_outbox_drain") as schedule:
            await app.drain_outbox()
            schedule.assert_called_once_with(30)
            schedule.reset_mock()
            await app.drain_outbox()
            schedule.assert_not_called()
        self.assertEqual(outbox.pending(app.doc), [])

    async def test_history_payloads_are_not_loaded_for_pending_scan_or_status_update(self):
        outbox.enqueue(app.doc, "old", [("0", discord.Embed(title="old"))])
        outbox.mark(app.doc, "old:0", "sent")
        outbox.enqueue(app.doc, "new", [("1", discord.Embed(title="new"))])
        sheet = app.doc.sheets[outbox.TITLE]
        history = copy.deepcopy(sheet.rows[1])
        with patch.object(sheet, "get_all_values", side_effect=AssertionError("no whole-history read")), \
             patch.object(sheet, "get", wraps=sheet.get) as reads, \
             patch.object(sheet, "update", wraps=sheet.update) as writes:
            record = outbox.pending(app.doc, limit=5)[0]
            outbox.mark(app.doc, record["delivery_id"], "sent", row_number=record["_row"])
        self.assertEqual(sheet.rows[1], history)
        self.assertEqual(writes.call_args.kwargs["range_name"], "D3:E3")
        self.assertNotIn("A2:E2", [call.args[0] for call in reads.call_args_list])

    async def test_moved_invoice_row_is_not_sent_or_overwritten(self):
        outbox.enqueue(app.doc, "period", [("0", discord.Embed(title="bill"))])
        sheet = app.doc.sheets[outbox.TITLE]
        sheet.rows.insert(1, ["different", "1", "{}", "pending", ""])
        before = copy.deepcopy(sheet.rows)
        with self.assertRaises(RuntimeError):
            outbox.mark(app.doc, "period:0", "sending", row_number=2)
        self.assertEqual(sheet.rows, before)

    async def test_recovery_retry_is_bounded(self):
        with patch.object(app, "bot", SimpleNamespace(is_ready=lambda: True)), \
             patch.object(app, "run_gsheet", AsyncMock(side_effect=ConnectionError)), \
             patch.object(app, "request_outbox_drain") as schedule:
            await app.drain_outbox(attempt=4)
        schedule.assert_not_called()

    async def test_runtime_metrics_tolerates_non_linux_hosts(self):
        result = memory_snapshot()
        self.assertEqual(set(result), {"rss_mb", "peak_rss_mb", "container_mb", "limit_mb"})
        self.assertTrue(all(value is None or value >= 0 for value in result.values()))


if __name__ == "__main__":
    unittest.main()
