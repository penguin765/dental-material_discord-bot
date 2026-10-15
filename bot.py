import os
import json
import datetime
import uuid
import asyncio
import traceback
import urllib.request
from threading import Thread
from flask import Flask
import discord
from discord import app_commands
from discord.ui import View, Select, Modal, TextInput
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= 0. 台灣時區與全域設定 =================
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))

def get_now_tw():
    """取得目前台灣時間（無時區標記，確保在 Render UTC 主機上時間精準）"""
    return datetime.datetime.now(TW_TZ).replace(tzinfo=None)

# ================= 1. Flask 防休眠網頁設定 =================
app = Flask('')

@app.route('/')
def home():
    bot_status = "🟢 已連線並就緒" if (bot and bot.is_ready()) else "🟡 啟動中或重新連線中"
    now_str = get_now_tw().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"🤖逼哩逼哩🤖 牙材訂購機器人一生懸命中！<br>"
        f"📡 Discord 狀態：{bot_status}<br>"
        f"🕒 台灣時間：{now_str}<br>"
        f"📦 快取狀態：名冊 {len(CACHE['members'])} 人 | 商品 {len(CACHE['products'])} 項 | 暫存訂單 {len(CACHE['orders'])} 筆"
    )

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

def keep_alive():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ================= 2. Google Sheets 安全連線與執行緒鎖 =================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
SPREADSHEET_NAME = "牙材_discord_bot"  # ⚠️ 修改為你的 Google Sheet 名稱

gc = None
doc = None
members_sheet = None
products_sheet = None
orders_sheet = None
sys_sheet = None

# 關鍵防護：gspread 底層的 requests.Session 並非執行緒安全 (Not Thread-Safe)
# 使用非同步鎖確保背景執行緒不會同時讀寫同一個連線導致卡死
GS_LOCK = asyncio.Lock()

def _init_gsheets_sync():
    """同步建立或重連 Google Sheets"""
    global gc, doc, members_sheet, products_sheet, orders_sheet, sys_sheet
    google_creds_env = os.getenv("GOOGLE_CREDS_JSON")
    if google_creds_env:
        cleaned_json_str = google_creds_env.strip().strip('"').strip("'")
        creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(cleaned_json_str), scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)

    gc = gspread.authorize(creds)
    doc = gc.open(SPREADSHEET_NAME)
    members_sheet = doc.worksheet("Members")
    products_sheet = doc.worksheet("Products")
    orders_sheet = doc.worksheet("Orders_Temp")
    try:
        sys_sheet = doc.worksheet("System_Config")
    except gspread.WorksheetNotFound:
        sys_sheet = doc.add_worksheet(title="System_Config", rows="50", cols="2")
        sys_sheet.append_row(["設定項目", "設定值"])

async def run_gsheet(func, *args, **kwargs):
    """
    安全執行 Google Sheets 操作的包裝器：
    1. 使用 GS_LOCK 避免多執行緒搶占同一個 Session 導致假死
    2. 加入 25 秒超時保護，避免網路卡死
    3. 若 Token 過期或連線中斷，自動重連並重試一次
    """
    async with GS_LOCK:
        for attempt in range(2):
            try:
                if doc is None:
                    await asyncio.wait_for(asyncio.to_thread(_init_gsheets_sync), timeout=20.0)
                return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=25.0)
            except Exception as e:
                print(f"⚠️ Google Sheets 操作異常 (嘗試 {attempt + 1}/2): {e}")
                if attempt == 0:
                    try:
                        await asyncio.wait_for(asyncio.to_thread(_init_gsheets_sync), timeout=20.0)
                    except Exception as re_err:
                        print(f"❌ 重新連線 Google Sheets 失敗: {re_err}")
                else:
                    raise e

# ─── 全域記憶體快取 (包含 orders 寫入穿透快取，徹底解決讀取卡頓與 API 超額) ───
CACHE = {
    "members": [],
    "products": [],
    "orders": [],
    "sys_config": {}
}

IS_ORDER_OPEN = False
ANNOUNCEMENT_CHANNEL_ID = None
scheduler = AsyncIOScheduler(timezone="Asia/Taipei")

# ================= 3. 輔助函式 (全面快取與非同步版) =================

async def reload_cache():
    """在背景執行緒中重新載入所有資料至記憶體，並過濾空白異常列"""
    def _fetch():
        m = members_sheet.get_all_records()
        raw_p = products_sheet.get_all_records()
        o = orders_sheet.get_all_records()
        c = sys_sheet.get_all_values()

        # 過濾掉空白列或重複 Item_ID，避免 Discord SelectOption 因空字串直接報錯 400
        valid_products = []
        seen_ids = set()
        for row in raw_p:
            item_id = str(row.get('Item_ID', '')).strip()
            name = str(row.get('品項名稱', '')).strip()
            if item_id and name and item_id not in seen_ids:
                seen_ids.add(item_id)
                valid_products.append(row)

        config_dict = {}
        for row in c:
            if len(row) >= 2 and str(row[0]).strip():
                config_dict[str(row[0]).strip()] = str(row[1]).strip().lstrip("'")

        return m, valid_products, o, config_dict

    try:
        m, p, o, c = await run_gsheet(_fetch)
        CACHE["members"] = m
        CACHE["products"] = p
        CACHE["orders"] = o
        CACHE["sys_config"] = c
        print(f"✅ 快取更新成功：名冊 {len(m)} 筆、商品 {len(p)} 筆、訂單 {len(o)} 筆。")
        return True
    except Exception as e:
        print(f"❌ 快取載入失敗: {e}")
        return False

def get_member_info(user_id):
    """直接從記憶體快取中尋找成員 (0 延遲)"""
    search_str = str(user_id).strip()
    for row in CACHE["members"]:
        sheet_uid = str(row.get('Discord_User_ID', '')).strip().lstrip("'")
        if '.' in sheet_uid:
            sheet_uid = sheet_uid.split('.')[0]
        if sheet_uid == search_str:
            return {
                "姓名": str(row.get("姓名", "")).strip(),
                "組別": str(row.get("組別", "")).strip(),
                "職位": str(row.get("職位", "")).strip()
            }
    return None

def get_sys_config(key):
    """直接從記憶體快取讀取設定"""
    return CACHE["sys_config"].get(key, None)

async def update_sys_config(key, value):
    """將設定寫入表單，並同步更新記憶體快取"""
    CACHE["sys_config"][key] = str(value).strip().lstrip("'")

    def _update():
        all_values = sys_sheet.get_all_values()
        for i, row in enumerate(all_values):
            if len(row) > 0 and str(row[0]).strip() == key:
                sys_sheet.update_cell(i + 1, 2, f"'{value}")
                return
        sys_sheet.append_row([key, f"'{value}"])

    await run_gsheet(_update)

def compute_live_product_summary(all_orders=None):
    """純記憶體計算湊單數量 (0 API 消耗)"""
    if all_orders is None:
        all_orders = CACHE["orders"]
    summary = {}
    for o in all_orders:
        item_id = str(o.get('Item_ID', '')).strip()
        if not item_id:
            continue
        try:
            qty = int(o.get('購買數量', 0) or 0)
        except ValueError:
            qty = 0
        summary[item_id] = summary.get(item_id, 0) + qty
    return summary

def self_ping_render():
    """定時自我喚醒，防止 Render 免費方案因 15 分鐘無 HTTP 流量而進入休眠"""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception:
            pass

# ================= 4. 自動收單、歷史歸檔、重置工作區 =================

async def auto_reminder():
    if not ANNOUNCEMENT_CHANNEL_ID:
        return
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not channel:
        return

    products = CACHE["products"]
    summary = compute_live_product_summary(CACHE["orders"])

    warning_text = ""
    for p in products:
        try:
            moq = int(p.get('最低購買量', 1) or 1)
        except Exception:
            moq = 1
        if moq > 1:
            current_total = summary.get(str(p['Item_ID']).strip(), 0)
            if current_total < moq:
                warning_text += f"⚠️ **[{p.get('品項名稱', '未知')}]** 目前全班僅湊 **{current_total}** / {moq} 支 (還差 {moq - current_total} 支才出貨！)\n"

    if warning_text:
        target_role_id = get_sys_config("TARGET_ROLE_ID")
        ping_text = f"<@&{target_role_id}>" if target_role_id and target_role_id.strip() else "【同學們】"
        embed = discord.Embed(title="🚨 牙材訂購截止倒數：湊單未達標品項公告！", description=warning_text[:4000], color=0xe67e22)
        await channel.send(content=f"{ping_text} 湊單品項如果截止時未達標，該品項將整單取消喔！請大家幫忙補刀！", embed=embed)

async def send_dm_task(user_id, embed):
    """發送私訊的工作單元"""
    try:
        user = await bot.fetch_user(int(user_id))
        await user.send(embed=embed)
    except Exception:
        pass

async def auto_close_order():
    global IS_ORDER_OPEN
    IS_ORDER_OPEN = False
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID) if ANNOUNCEMENT_CHANNEL_ID else None

    try:
        # 截單時向 Google Sheet 拉取最準確的訂單紀錄
        all_orders = await run_gsheet(orders_sheet.get_all_records)
        CACHE["orders"] = all_orders
        products = CACHE["products"]
        prod_map = {str(p['Item_ID']).strip(): p for p in products}
        summary = compute_live_product_summary(all_orders)
        date_str = get_now_tw().strftime("%m%d")

        if not all_orders:
            if channel:
                await channel.send("🔒 本期訂購已截止，因無任何同學下單，系統不生成報表。")
            await update_sys_config("IS_ORDER_OPEN", "False")
            await update_sys_config("CLOSE_TIME", "")
            return

        if channel:
            await channel.send("⏳ 正在產生歷史流水帳與結算報表，因資料量大可能需時數秒，請稍候...")

        raw_sheet_name = f"歷史_{date_str}原始明細"
        settle_sheet_name = f"{date_str}牙材團購結算"

        # ─── 記憶體內組裝結算資料 ───
        settle_data = []

        # 區塊 A
        settle_data.append(["【區塊 A：牙材長向廠商叫貨總表】"])
        settle_data.append(["品項 ID", "品項名稱", "全班叫貨總量", "單價", "總金額", "出貨狀態"])
        valid_items = set()
        for p in products:
            item_id = str(p['Item_ID']).strip()
            total_qty = summary.get(item_id, 0)
            try:
                moq = int(p.get('最低購買量', 1) or 1)
            except Exception:
                moq = 1
            try:
                price = int(p.get('單價', 0) or 0)
            except Exception:
                price = 0

            if total_qty == 0:
                continue
            if total_qty >= moq:
                status = "✅ 達標成團"
                valid_items.add(item_id)
            else:
                status = f"❌ 淘汰 (未滿最低購買量 {moq})"
            settle_data.append([item_id, p.get('品項名稱', '未知品項'), total_qty, price, total_qty * price, status])

        # 區塊 B
        settle_data.append([])
        settle_data.append(["【區塊 B：各小組分流對帳表】"])
        settle_data.append(["組別", "同學姓名", "訂購明細 (成功成團品項)", "應匯款總額", "回報末五碼", "對帳狀態"])

        group_billing = {}
        for order in all_orders:
            item_id = str(order.get('Item_ID', '')).strip()
            if item_id not in valid_items:
                continue
            uid = str(order.get('Discord_User_ID', '')).strip()
            if uid not in group_billing:
                group_billing[uid] = {"姓名": order.get('姓名', ''), "組別": order.get('組別', ''), "明細": [], "總價": 0}

            p_info = prod_map.get(item_id, {"品項名稱": "未知品項"})
            group_billing[uid]["明細"].append(f"{p_info.get('品項名稱', '未知')}x{order.get('購買數量', 0)}")
            try:
                subtotal = int(order.get('單項總價', 0) or 0)
            except Exception:
                subtotal = 0
            group_billing[uid]["總價"] += subtotal

        sorted_members = sorted(group_billing.values(), key=lambda x: str(x.get('組別', '')))
        for m in sorted_members:
            settle_data.append([f"第 {m['組別']} 組", m['姓名'], ", ".join(m['明細']), m['總價'], "", "未匯款"])

        # 區塊 C
        settle_data.append([])
        settle_data.append(["【區塊 C：各組上繳總表 (小組長向牙材長回報)】"])
        settle_data.append(["組別", "應上繳總額", "上繳末五碼", "牙材長確認狀態"])

        group_totals = {}
        for uid, data in group_billing.items():
            g_name = f"第 {data['組別']} 組"
            group_totals[g_name] = group_totals.get(g_name, 0) + data['總價']

        def extract_num(s):
            nums = [int(w) for w in s.split() if w.isdigit()]
            return nums[0] if nums else 0

        sorted_group_names = sorted(group_totals.keys(), key=extract_num)
        for g_name in sorted_group_names:
            settle_data.append([g_name, group_totals[g_name], "", "未匯款"])

        # 一次性在背景完成歷史備份、結算表建立與清空暫存
        def _write_all_sheets():
            # 1. 歷史明細
            try:
                doc.del_worksheet(doc.worksheet(raw_sheet_name))
            except Exception:
                pass
            raw_ws = doc.add_worksheet(title=raw_sheet_name, rows=str(max(100, len(all_orders) + 20)), cols="10")
            raw_data = [["Order_ID", "Discord_User_ID", "姓名", "組別", "Item_ID", "購買數量", "單項總價"]]
            for o in all_orders:
                raw_data.append([
                    o.get('Order_ID', ''), str(o.get('Discord_User_ID', '')),
                    o.get('姓名', ''), o.get('組別', ''),
                    str(o.get('Item_ID', '')), o.get('購買數量', 0), o.get('單項總價', 0)
                ])
            raw_ws.append_rows(raw_data)

            # 2. 結算表
            try:
                doc.del_worksheet(doc.worksheet(settle_sheet_name))
            except Exception:
                pass
            settle_ws = doc.add_worksheet(title=settle_sheet_name, rows=str(max(100, len(settle_data) + 20)), cols="10")
            settle_ws.append_rows(settle_data)

            # 3. 清空暫存訂單
            orders_sheet.clear()
            orders_sheet.append_row(["Order_ID", "Discord_User_ID", "姓名", "組別", "Item_ID", "購買數量", "單項總價"])

        await run_gsheet(_write_all_sheets)
        CACHE["orders"] = []

        await update_sys_config("LATEST_SETTLEMENT_SHEET", settle_sheet_name)
        await update_sys_config("IS_ORDER_OPEN", "False")
        await update_sys_config("CLOSE_TIME", "")

        if channel:
            await channel.send(
                f"🔒 **本期牙材訂購已順利截止！**\n"
                f"系統已成功產生歷史備份 `[{raw_sheet_name}]` 與結算報表 `[{settle_sheet_name}]`！\n"
                f"**當期暫存工作區已全數清空重置**，下單通道關閉。"
            )

        # ─── 併發發送私訊 ───
        dm_tasks = []
        for user_id, data in group_billing.items():
            embed = discord.Embed(title="🦷 您的當期牙材訂購個人帳單", color=0x3498db)
            detail_str = "\n".join(data["明細"])
            embed.add_field(name="訂購明細", value=detail_str[:1020] if detail_str else "無", inline=False)
            embed.add_field(name="💰 應匯總金額", value=f"NT$ {data['總價']:,}", inline=False)
            embed.set_footer(text="請匯款給您所屬的小組長後，使用 /回報匯款 登記末五碼。")
            dm_tasks.append(send_dm_task(user_id, embed))

        if dm_tasks:
            await asyncio.gather(*dm_tasks)

    except Exception as e:
        print(f"自動結算時發生崩潰:\n{traceback.format_exc()}")
        if channel:
            await channel.send(f"❌ **自動截單時發生錯誤，報表產生失敗！**\n錯誤代碼：`{e}`\n👉 請牙材長手動前往備份 `Orders_Temp` 後聯繫管理員。")


# ================= 5. 下單與對帳 UI 元件 (加上字數與數量嚴格防呆) =================

class MultiOrderModal(Modal):
    def __init__(self, selected_products):
        super().__init__(title="填寫購買數量 (輸入正整數)")
        self.selected_products = selected_products
        self.inputs = []
        for p in selected_products[:5]:
            try:
                price = int(p.get('單價', 0) or 0)
            except Exception:
                price = 0
            # 防呆：Discord TextInput 的 label 嚴格限制最多 45 字元，超過會直接報錯未回應！
            raw_name = str(p.get('品項名稱', '未知')).strip()
            price_suffix = f" (${price})"
            max_name_len = 45 - len(price_suffix)
            safe_name = (raw_name[:max_name_len - 1] + "…") if len(raw_name) > max_name_len else raw_name
            label_str = f"{safe_name}{price_suffix}"

            inp = TextInput(label=label_str, placeholder="請輸入正整數數量 (例如: 1)", required=True, max_length=4)
            self.add_item(inp)
            self.inputs.append((p, inp))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not IS_ORDER_OPEN:
            await interaction.followup.send("❌ 目前非訂購期間！", ephemeral=True)
            return
        mem = get_member_info(interaction.user.id)
        if not mem:
            await interaction.followup.send("❌ 找不到您的名冊紀錄！請先使用 `/綁定名冊`！", ephemeral=True)
            return

        rows_to_add = []
        cache_to_add = []
        reply_msg = "✅ **成功加入暫存訂單！**\n"
        total_cost = 0

        for p, inp in self.inputs:
            try:
                qty = int(inp.value.strip())
                if qty <= 0:
                    raise ValueError
            except ValueError:
                await interaction.followup.send(f"❌ 數量輸入錯誤：**{p.get('品項名稱')}** 必須為大於 0 的正整數！", ephemeral=True)
                return
            try:
                price = int(p.get('單價', 0) or 0)
            except Exception:
                price = 0
            subtotal = qty * price
            total_cost += subtotal
            order_id = f"ORD-{uuid.uuid4().hex[:6].upper()}"
            item_id_str = str(p['Item_ID']).strip()

            rows_to_add.append([order_id, str(interaction.user.id), mem['姓名'], mem['組別'], item_id_str, qty, subtotal])
            cache_to_add.append({
                "Order_ID": order_id,
                "Discord_User_ID": str(interaction.user.id),
                "姓名": mem['姓名'],
                "組別": mem['組別'],
                "Item_ID": item_id_str,
                "購買數量": qty,
                "單項總價": subtotal
            })
            reply_msg += f"• {p.get('品項名稱')} x {qty} (小計: ${subtotal})\n"

        try:
            await run_gsheet(orders_sheet.append_rows, rows_to_add)
            CACHE["orders"].extend(cache_to_add)
            reply_msg += f"\n**本次新增總金額：** NT$ {total_cost:,}\n*(可使用 `/我的訂單` 檢視或修改)*"
            await interaction.followup.send(reply_msg, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 寫入訂單時發生網路異常，請稍後再試：{e}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ 表單送出失敗：{error}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ 表單送出失敗：{error}", ephemeral=True)


class ProductSelect(Select):
    def __init__(self, products_chunk, summary, menu_index=0, total_menus=1):
        options = []
        for p in products_chunk[:25]:
            item_id = str(p.get('Item_ID', '')).strip()
            name = str(p.get('品項名稱', '未知品項')).strip()[:100]
            if not item_id or not name:
                continue
            current_total = summary.get(item_id, 0)
            try:
                moq = int(p.get('最低購買量', 1) or 1)
            except Exception:
                moq = 1
            try:
                price = int(p.get('單價', 0) or 0)
            except Exception:
                price = 0

            if moq <= 1:
                desc = f"單價: ${price} | 全班已訂: {current_total} 個"
            else:
                desc = f"單價: ${price} | 湊單進度: {current_total}/{moq} (差 {max(0, moq - current_total)} 支)"
            options.append(discord.SelectOption(label=name, description=desc[:100], value=item_id))

        max_selectable = min(5, len(options))
        prefix = f"【第 {menu_index + 1} 區】" if total_menus > 1 else ""
        super().__init__(
            placeholder=f"{prefix}請勾選欲訂購品項 (單次最多勾選 {max_selectable} 項)",
            min_values=1,
            max_values=max_selectable,
            options=options
        )
        self.products_chunk = products_chunk

    async def callback(self, interaction: discord.Interaction):
        selected_products = [p for p in self.products_chunk if str(p.get('Item_ID', '')).strip() in self.values]
        await interaction.response.send_modal(MultiOrderModal(selected_products))


class CancelOrderSelect(Select):
    def __init__(self, user_orders):
        options = []
        seen_oids = set()
        for o in user_orders[:25]:
            oid = str(o.get('Order_ID', '')).strip()
            if not oid or oid in seen_oids:
                continue
            seen_oids.add(oid)
            label = f"{o.get('品項名稱', '未知')} x {o.get('購買數量', 0)}"[:100]
            desc = f"總價: ${o.get('單項總價', 0)} (單號:{oid[-6:]})"[:100]
            options.append(discord.SelectOption(label=label, description=desc, value=oid))
        super().__init__(placeholder="❌ 若需修改，請選擇要「取消」的品項...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        # 第一時間 defer，確保絕不超時
        await interaction.response.defer(ephemeral=True)
        if not IS_ORDER_OPEN:
            await interaction.followup.send("❌ 目前非訂購期間，無法修改訂單！", ephemeral=True)
            return
        order_id_to_cancel = self.values[0]

        def _delete():
            cell = orders_sheet.find(order_id_to_cancel, in_column=1)
            if cell:
                orders_sheet.delete_rows(cell.row)
                return True
            return False

        try:
            deleted = await run_gsheet(_delete)
            CACHE["orders"] = [o for o in CACHE["orders"] if str(o.get("Order_ID", "")).strip() != order_id_to_cancel]
            if deleted:
                await interaction.followup.send("✅ 已成功取消該筆訂單！如需變更數量請重新使用 `/訂購牙材` 下單。", ephemeral=True)
            else:
                await interaction.followup.send("❌ 找不到該筆訂單，可能已經被取消了。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 取消時發生錯誤：{e}", ephemeral=True)


class CancelOrderView(View):
    def __init__(self, user_orders):
        super().__init__(timeout=300)
        self.add_item(CancelOrderSelect(user_orders))


# ─── 小組長專用一鍵確認選單 ───
class GroupConfirmSelect(Select):
    def __init__(self, pending_members, sheet_name, target_group):
        options = []
        seen = set()
        for m in pending_members[:25]:
            name = str(m['姓名']).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            options.append(discord.SelectOption(
                label=name[:100],
                description=f"回報末五碼: {m['code']} | 應繳: ${m['amount']}"[:100],
                value=name[:100]
            ))
        super().__init__(placeholder="✅ 在此勾選已確認入帳的同學", min_values=1, max_values=len(options), options=options)
        self.sheet_name = sheet_name
        self.target_group = target_group

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        def _batch_update():
            target_settle_sheet = doc.worksheet(self.sheet_name)
            records = target_settle_sheet.get_all_values()

            cells_to_update = []
            updated_names = []
            in_zone_b = False

            for idx, row in enumerate(records, start=1):
                title = str(row[0]).strip() if len(row) > 0 else ""
                if "區塊 B" in title:
                    in_zone_b = True
                    continue
                if "區塊 C" in title:
                    break

                if in_zone_b and len(row) >= 6:
                    if str(row[0]).strip() == self.target_group and str(row[1]).strip() in self.values:
                        cells_to_update.append(gspread.Cell(row=idx, col=6, value="✅ 已收妥完款"))
                        updated_names.append(str(row[1]).strip())

            if cells_to_update:
                target_settle_sheet.update_cells(cells_to_update)
            return updated_names

        try:
            updated_names = await run_gsheet(_batch_update)
            if updated_names:
                await interaction.followup.send(f"👍 已確認以下同學款項入帳，狀態更新完畢：\n**{', '.join(updated_names)}**", ephemeral=True)
            else:
                await interaction.followup.send("⚠️ 未找到符合的待審核紀錄。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 更新失敗：{e}", ephemeral=True)


class GroupConfirmView(View):
    def __init__(self, pending_members, sheet_name, target_group):
        super().__init__(timeout=300)
        self.add_item(GroupConfirmSelect(pending_members, sheet_name, target_group))


# ─── 牙材長專用一鍵確認選單 ───
class HeadConfirmSelect(Select):
    def __init__(self, pending_groups, sheet_name):
        options = []
        seen = set()
        for g in pending_groups[:25]:
            g_name = str(g['組別']).strip()
            if not g_name or g_name in seen:
                continue
            seen.add(g_name)
            options.append(discord.SelectOption(
                label=g_name[:100],
                description=f"上繳末五碼: {g['code']} | 應繳總額: ${g['amount']}"[:100],
                value=g_name[:100]
            ))
        super().__init__(placeholder="✅ 在此勾選已確認入帳的小組", min_values=1, max_values=len(options), options=options)
        self.sheet_name = sheet_name

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        def _batch_update():
            target_settle_sheet = doc.worksheet(self.sheet_name)
            records = target_settle_sheet.get_all_values()

            cells_to_update = []
            updated_groups = []
            in_zone_c = False

            for idx, row in enumerate(records, start=1):
                title = str(row[0]).strip() if len(row) > 0 else ""
                if "區塊 C" in title:
                    in_zone_c = True
                    continue

                if in_zone_c and len(row) >= 4:
                    if str(row[0]).strip() in self.values:
                        cells_to_update.append(gspread.Cell(row=idx, col=4, value="✅ 已收妥完款"))
                        updated_groups.append(str(row[0]).strip())

            if cells_to_update:
                target_settle_sheet.update_cells(cells_to_update)
            return updated_groups

        try:
            updated_groups = await run_gsheet(_batch_update)
            if updated_groups:
                await interaction.followup.send(f"👑 已確認收到以下小組的帳款：\n**{', '.join(updated_groups)}**", ephemeral=True)
            else:
                await interaction.followup.send("⚠️ 未找到符合的小組紀錄。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 更新失敗：{e}", ephemeral=True)


class HeadConfirmView(View):
    def __init__(self, pending_groups, sheet_name):
        super().__init__(timeout=300)
        self.add_item(HeadConfirmSelect(pending_groups, sheet_name))


# ================= 6. 機器人核心與非阻塞啟動流程 =================
class DentalERPBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self._initialized = False

    async def setup_hook(self):
        # 關鍵優化：不在 setup_hook 中阻塞等待 Google Sheets 與 sync，讓機器人秒速連上 Discord Gateway！
        if not scheduler.running:
            scheduler.start()
            scheduler.add_job(self_ping_render, 'interval', minutes=10, id='render_keep_alive', replace_existing=True)

    async def on_ready(self):
        print(f"🤖 機器人已成功登入 Discord：{self.user} (ID: {self.user.id})")
        if not self._initialized:
            self._initialized = True
            asyncio.create_task(self._background_startup())

    async def _background_startup(self):
        """背景完成快取載入、排程恢復與指令樹同步，絕不卡住主線程"""
        print("⏳ 正在背景載入 Google Sheets 快取與恢復排程...")
        await reload_cache()
        try:
            global IS_ORDER_OPEN, ANNOUNCEMENT_CHANNEL_ID
            is_open = get_sys_config("IS_ORDER_OPEN")
            if is_open == "True":
                IS_ORDER_OPEN = True
                close_time_str = get_sys_config("CLOSE_TIME")
                channel_id_str = get_sys_config("ANNOUNCEMENT_CHANNEL_ID")
                if channel_id_str and channel_id_str.isdigit():
                    ANNOUNCEMENT_CHANNEL_ID = int(channel_id_str)
                if close_time_str:
                    dt = datetime.datetime.strptime(close_time_str, "%Y-%m-%d %H:%M")
                    now_tw = get_now_tw()
                    if dt > now_tw:
                        scheduler.add_job(auto_close_order, 'date', run_date=dt, id='close_order_job', replace_existing=True)
                        reminder_time = dt - datetime.timedelta(days=3)
                        if reminder_time > now_tw:
                            scheduler.add_job(auto_reminder, 'date', run_date=reminder_time, id='reminder_job', replace_existing=True)
                        print(f"🔄 成功從系統恢復排程！將於台灣時間 {close_time_str} 截單。")
                    else:
                        print("⚠️ 偵測到過期的開團時間，立即執行自動收單...")
                        asyncio.create_task(auto_close_order())
        except Exception as e:
            print(f"⚠️ 恢復開團狀態失敗：{e}")

        try:
            await self.tree.sync()
            print("✅ 斜線指令樹同步完成。")
        except Exception as e:
            print(f"⚠️ 指令樹同步略過或受限：{e}")

bot = DentalERPBot()

# 全域斜線指令錯誤攔截器：確保任何未知錯誤都會回傳訊息，而不是顯示「應用程式未回應」
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"❌ 指令執行錯誤 [{interaction.command}]:\n{traceback.format_exc()}")
    err_msg = f"❌ 執行指令時發生異常：`{error}`\n請稍後再試，或請牙材長執行 `/刷新名冊與商品庫`。"
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(err_msg, ephemeral=True)
        else:
            await interaction.followup.send(err_msg, ephemeral=True)
    except Exception:
        pass


@bot.tree.command(name="刷新名冊與商品庫", description="【牙材長專用】手動重新從 Google Sheet 載入名冊、商品與訂單快取")
async def force_reload_cache(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not CACHE["members"]:
        await reload_cache()
    mem = get_member_info(interaction.user.id)
    if not mem or "牙材長" not in str(mem.get('職位', '')):
        await interaction.followup.send("❌ 您非牙材長，權限不足！", ephemeral=True)
        return
    ok = await reload_cache()
    if ok:
        await interaction.followup.send(
            f"✅ 已成功從 Google Sheet 重新載入最新快取！\n"
            f"• 名冊人數：`{len(CACHE['members'])}` 人\n"
            f"• 有效商品：`{len(CACHE['products'])}` 項\n"
            f"• 暫存訂單：`{len(CACHE['orders'])}` 筆",
            ephemeral=True
        )
    else:
        await interaction.followup.send("❌ 載入快取失敗，請檢查 Google Sheet 連線狀態。", ephemeral=True)


@bot.tree.command(name="綁定名冊", description="【全班同學必用】首次使用時，綁定您的 Discord 帳號")
async def bind_name(interaction: discord.Interaction, 真實姓名: str):
    await interaction.response.defer(ephemeral=True)
    if not CACHE["members"]:
        await reload_cache()

    all_members = CACHE["members"]
    user_id_str = str(interaction.user.id).strip()
    for row in all_members:
        sheet_uid = str(row.get('Discord_User_ID', '')).strip().lstrip("'").split('.')[0]
        if sheet_uid == user_id_str:
            await interaction.followup.send(f"❌ 您已綁定過姓名「{row.get('姓名')}」囉！若需更換請聯繫牙材長。", ephemeral=True)
            return

    target_name = 真實姓名.strip()
    found_row = None
    for row in all_members:
        if str(row.get('姓名', '')).strip() == target_name:
            current_bound_id = str(row.get('Discord_User_ID', '')).strip().lstrip("'")
            if current_bound_id and current_bound_id != "0":
                await interaction.followup.send(f"❌ 「{target_name}」已經被其他帳號綁定了！", ephemeral=True)
                return
            found_row = row
            break

    if not found_row:
        await interaction.followup.send(f"❌ 找不到名為「{target_name}」的同學，請確認是否有打錯字！", ephemeral=True)
        return

    def _update_bind():
        records = members_sheet.get_all_values()
        for idx, r in enumerate(records, start=1):
            if len(r) >= 2 and str(r[1]).strip() == target_name:
                members_sheet.update_cell(idx, 1, f"'{user_id_str}")
                return True
        return False

    ok = await run_gsheet(_update_bind)
    if ok:
        found_row['Discord_User_ID'] = user_id_str
        role = found_row.get('職位', '一般')
        await interaction.followup.send(f"🎉 綁定成功！**【{target_name}】** 歡迎！您的職位為 **[{role}]**。", ephemeral=True)
    else:
        await interaction.followup.send("❌ 寫入名冊失敗，請稍後再試。", ephemeral=True)


@bot.tree.command(name="開團訂購牙材", description="【牙材長專用】設定截止時間並開啟下單通道")
async def start_group_buy(interaction: discord.Interaction, 截止時間: str):
    global IS_ORDER_OPEN, ANNOUNCEMENT_CHANNEL_ID
    await interaction.response.defer(ephemeral=True)
    mem = get_member_info(interaction.user.id)
    if not mem or "牙材長" not in str(mem.get('職位', '')):
        await interaction.followup.send("❌ 您非牙材長，權限不足！", ephemeral=True)
        return
    try:
        dt = datetime.datetime.strptime(截止時間.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        await interaction.followup.send(f"❌ 時間格式錯誤！請依照格式輸入：`2026-10-30 23:59`\n（您剛才輸入的是：`{截止時間}`）", ephemeral=True)
        return

    now_tw = get_now_tw()
    if dt <= now_tw:
        await interaction.followup.send(f"❌ 截止時間必須是未來的時間！（目前台灣時間：`{now_tw.strftime('%Y-%m-%d %H:%M')}`）", ephemeral=True)
        return

    await reload_cache()
    IS_ORDER_OPEN = True
    ANNOUNCEMENT_CHANNEL_ID = interaction.channel_id

    for job_id in ['close_order_job', 'reminder_job']:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    scheduler.add_job(auto_close_order, 'date', run_date=dt, id='close_order_job', replace_existing=True)
    reminder_time = dt - datetime.timedelta(days=3)
    if reminder_time > now_tw:
        scheduler.add_job(auto_reminder, 'date', run_date=reminder_time, id='reminder_job', replace_existing=True)

    await update_sys_config("IS_ORDER_OPEN", "True")
    await update_sys_config("CLOSE_TIME", 截止時間.strip())
    await update_sys_config("ANNOUNCEMENT_CHANNEL_ID", str(interaction.channel_id))

    target_role_id = get_sys_config("TARGET_ROLE_ID")
    ping_text = f"<@&{target_role_id}>\n" if target_role_id and target_role_id.strip() else ""
    await interaction.followup.send("✅ 開團設定完成！正在發佈頻道公告...", ephemeral=True)
    if interaction.channel:
        await interaction.channel.send(f"{ping_text}📢 **當期牙材訂購正式開跑！**\n系統將在 `{截止時間.strip()}` 自動截單並清空重置。")


@bot.tree.command(name="強制關團", description="【牙材長專用】立即關閉下單通道並產生結算報表")
async def force_close_order(interaction: discord.Interaction):
    global IS_ORDER_OPEN, ANNOUNCEMENT_CHANNEL_ID
    await interaction.response.defer(ephemeral=True)
    mem = get_member_info(interaction.user.id)
    if not mem or "牙材長" not in str(mem.get('職位', '')):
        await interaction.followup.send("❌ 您非牙材長，權限不足！", ephemeral=True)
        return
    if not IS_ORDER_OPEN:
        await interaction.followup.send("❌ 目前沒有正在進行中的團購，不需要關團！", ephemeral=True)
        return

    if not ANNOUNCEMENT_CHANNEL_ID:
        ANNOUNCEMENT_CHANNEL_ID = interaction.channel_id

    for job_id in ['close_order_job', 'reminder_job']:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    await interaction.followup.send("✅ 收到強制關團指令！正在背景處理結算報表，請稍候並留意頻道公告...", ephemeral=True)
    asyncio.create_task(auto_close_order())


@bot.tree.command(name="修改截止時間", description="【牙材長專用】修改當前團購的截止時間")
async def modify_deadline(interaction: discord.Interaction, 新截止時間: str):
    global IS_ORDER_OPEN, ANNOUNCEMENT_CHANNEL_ID
    await interaction.response.defer(ephemeral=True)
    mem = get_member_info(interaction.user.id)
    if not mem or "牙材長" not in str(mem.get('職位', '')):
        await interaction.followup.send("❌ 您非牙材長，權限不足！", ephemeral=True)
        return
    if not IS_ORDER_OPEN:
        await interaction.followup.send("❌ 目前沒有正在進行中的團購！", ephemeral=True)
        return

    try:
        dt = datetime.datetime.strptime(新截止時間.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        await interaction.followup.send("❌ 時間格式錯誤！請依照格式輸入：`2026-10-30 23:59`", ephemeral=True)
        return

    now_tw = get_now_tw()
    if dt <= now_tw:
        await interaction.followup.send(f"❌ 截止時間必須是未來的時間！（目前台灣時間：`{now_tw.strftime('%Y-%m-%d %H:%M')}`）", ephemeral=True)
        return

    for job_id in ['close_order_job', 'reminder_job']:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    scheduler.add_job(auto_close_order, 'date', run_date=dt, id='close_order_job', replace_existing=True)
    reminder_time = dt - datetime.timedelta(days=3)
    if reminder_time > now_tw:
        scheduler.add_job(auto_reminder, 'date', run_date=reminder_time, id='reminder_job', replace_existing=True)

    await update_sys_config("CLOSE_TIME", 新截止時間.strip())
    await interaction.followup.send(f"✅ 成功修改！新的截止時間為 `{新截止時間.strip()}`。", ephemeral=True)

    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID) if ANNOUNCEMENT_CHANNEL_ID else interaction.channel
    if channel:
        target_role_id = get_sys_config("TARGET_ROLE_ID")
        ping_text = f"<@&{target_role_id}>\n" if target_role_id and target_role_id.strip() else ""
        await channel.send(f"{ping_text}📢 **【牙材長公告】截單時間已變更！**\n新的截單時間變更至 `{新截止時間.strip()}`，請大家留意！")


@bot.tree.command(name="團購進度總覽", description="【全班通用】即時查看當期牙材的湊單進度與數量")
async def progress_overview(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not IS_ORDER_OPEN and get_sys_config("IS_ORDER_OPEN") != "True":
        await interaction.followup.send("❌ 目前沒有正在進行的團購！", ephemeral=True)
        return

    products = CACHE["products"]
    summary = compute_live_product_summary(CACHE["orders"])

    embed = discord.Embed(title="📊 當期牙材湊單進度總覽", color=0x2ecc71)
    text_content = ""
    for p in products:
        item_id = str(p.get('Item_ID', '')).strip()
        name = p.get('品項名稱', '未知商品')
        try:
            moq = int(p.get('最低購買量', 1) or 1)
        except Exception:
            moq = 1
        current_total = summary.get(item_id, 0)
        if moq <= 1:
            text_content += f"🔹 **{name}**\n　 └ 已訂購: `{current_total}` 個\n"
        else:
            if current_total >= moq:
                text_content += f"✅ **{name}**\n　 └ 已達標: `{current_total} / {moq}` 個\n"
            else:
                text_content += f"⚠️ **{name}**\n　 └ 湊單中: `{current_total} / {moq}` 個 (還差 {moq - current_total})\n"

    embed.description = text_content[:4000] if text_content else "目前尚無商品資料。"
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="訂購牙材", description="挑選當期牙材並進行訂購（可一次勾選多項）")
async def order_material(interaction: discord.Interaction):
    # 第一時間 defer，完全使用記憶體快取回應，耗時 < 0.01 秒！
    await interaction.response.defer(ephemeral=True)
    if not IS_ORDER_OPEN and get_sys_config("IS_ORDER_OPEN") != "True":
        await interaction.followup.send("❌ 目前非訂購期間，無法進行訂購！", ephemeral=True)
        return

    products = CACHE["products"]
    if not products:
        await interaction.followup.send("❌ 目前商品清單為空，請牙材長確認 Products 表單並執行 `/刷新名冊與商品庫`！", ephemeral=True)
        return

    summary = compute_live_product_summary(CACHE["orders"])

    # 自動將超過 25 項的商品分區放入多個下拉選單 (最多支援 125 項商品不崩潰)
    chunks = [products[i:i + 25] for i in range(0, min(len(products), 125), 25)]
    view = View(timeout=300)
    for idx, chunk in enumerate(chunks):
        view.add_item(ProductSelect(chunk, summary, idx, len(chunks)))

    await interaction.followup.send(
        "🦷 **請勾選欲訂購的品項：**\n*(註：單次最多可同時勾選 5 項結帳，若超過請分次下單！)*",
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="我的訂單", description="【個人專用】檢視自己目前的暫存訂單，可修改刪除")
async def my_orders(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not IS_ORDER_OPEN and get_sys_config("IS_ORDER_OPEN") != "True":
        await interaction.followup.send("🔒 目前非訂購期間，無法查看或修改暫存區！", ephemeral=True)
        return

    all_orders = CACHE["orders"]
    products = CACHE["products"]
    prod_map = {str(p['Item_ID']).strip(): p.get('品項名稱', '未知品項') for p in products}

    user_orders = []
    total_cost = 0
    uid_str = str(interaction.user.id).strip()
    for o in all_orders:
        if str(o.get('Discord_User_ID', '')).strip().lstrip("'") == uid_str:
            o_copy = dict(o)
            o_copy['品項名稱'] = prod_map.get(str(o.get('Item_ID', '')).strip(), "未知品項")
            user_orders.append(o_copy)
            try:
                cost = int(o.get('單項總價', 0) or 0)
            except Exception:
                cost = 0
            total_cost += cost

    if not user_orders:
        await interaction.followup.send("🛒 您目前沒有任何訂購明細喔！", ephemeral=True)
        return

    embed = discord.Embed(title="🛒 您的當期購物車明細", color=0x2ecc71)
    lines = ["以下是您目前預訂的品項（尚未截單）：\n"]
    for o in user_orders:
        lines.append(f"• **{o['品項名稱']}** — 數量: `{o.get('購買數量', 0)}` | 小計: `${o.get('單項總價', 0)}`")
    lines.append(f"\n💰 **目前累積總額：NT$ {total_cost:,}**")
    embed.description = "\n".join(lines)[:4000]

    view = CancelOrderView(user_orders)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="回報匯款", description="【全班同學】匯款後回報您的帳戶末五碼")
async def report_payment(interaction: discord.Interaction, 末五碼: str):
    await interaction.response.defer(ephemeral=True)
    latest_sheet_name = get_sys_config("LATEST_SETTLEMENT_SHEET")
    if not latest_sheet_name:
        await interaction.followup.send("❌ 系統設定中找不到最新的結算表，請確認牙材長是否已經產生報表。", ephemeral=True)
        return

    user_info = get_member_info(interaction.user.id)
    if not user_info:
        await interaction.followup.send("❌ 找不到您的名冊，請先使用 `/綁定名冊`。", ephemeral=True)
        return

    def _report_sync():
        target_settle_sheet = doc.worksheet(latest_sheet_name)
        all_values = target_settle_sheet.get_all_values()
        in_zone_b = False
        for i, row in enumerate(all_values):
            title = str(row[0]).strip() if len(row) > 0 else ""
            if "區塊 B" in title:
                in_zone_b = True
                continue
            if "區塊 C" in title:
                break

            if in_zone_b and len(row) >= 6 and str(row[1]).strip() == user_info['姓名']:
                target_idx = i + 1
                cells = [
                    gspread.Cell(row=target_idx, col=5, value=f"'{末五碼.strip()}"),
                    gspread.Cell(row=target_idx, col=6, value="已匯款待審核")
                ]
                target_settle_sheet.update_cells(cells)
                return True
        return False

    try:
        updated = await run_gsheet(_report_sync)
        if updated:
            await interaction.followup.send(f"✅ 匯款回報成功！已在結算單中登記末五碼 `[{末五碼.strip()}]`，請等待小組長審核。", ephemeral=True)
        else:
            await interaction.followup.send("❌ 找不到您的應繳費紀錄（可能您本期無下單或品項遭淘汰）。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 讀取或寫入結算報表 `[{latest_sheet_name}]` 失敗：{e}", ephemeral=True)


@bot.tree.command(name="組內對帳", description="【小組長專用】查看並一鍵確認組內同學繳費（可跨組）")
@app_commands.describe(指定組別="若代班請填組別數字（可不填，預設為自己組別）")
async def group_check(interaction: discord.Interaction, 指定組別: int = None):
    await interaction.response.defer(ephemeral=True)
    leader = get_member_info(interaction.user.id)
    if not leader or "小組長" not in str(leader.get('職位', '')):
        await interaction.followup.send("❌ 您非登記之小組長，權限不足！", ephemeral=True)
        return

    target_group = f"第 {指定組別} 組" if 指定組別 is not None else f"第 {leader.get('組別', '')} 組"
    sheet_name = get_sys_config("LATEST_SETTLEMENT_SHEET")
    if not sheet_name:
        await interaction.followup.send("❌ 找不到當期結算報表設定。", ephemeral=True)
        return

    def _fetch_group_records():
        target_settle_sheet = doc.worksheet(sheet_name)
        return target_settle_sheet.get_all_values()

    try:
        records = await run_gsheet(_fetch_group_records)
    except Exception:
        await interaction.followup.send(f"❌ 找不到當期結算報表 `[{sheet_name}]`。", ephemeral=True)
        return

    embed = discord.Embed(title=f"📋 {target_group} 繳費對帳進度", color=0x9b59b6)
    found = False
    in_zone_b = False
    pending_members = []
    lines = []

    for row in records:
        title = str(row[0]).strip() if len(row) > 0 else ""
        if "區塊 B" in title:
            in_zone_b = True
            continue
        if "區塊 C" in title:
            break

        if in_zone_b and len(row) >= 6 and str(row[0]).strip() == target_group:
            found = True
            name = str(row[1]).strip()
            total_price = str(row[3]).strip()
            report_code = str(row[4]).strip().lstrip("'")
            status = str(row[5]).strip()

            status_text = f"👤 **{name}** — 💰 應繳: `${total_price}` | 狀態: **{status}**"
            if report_code:
                status_text += f" (末五碼: `{report_code}`)"
            lines.append(status_text)

            if "待審核" in status:
                pending_members.append({"姓名": name, "code": report_code, "amount": total_price})

    if not found:
        embed.description = f"本期 {target_group} 無人需繳費，或查無此組資料。"
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        embed.description = "\n".join(lines)[:4000]
        if pending_members:
            view = GroupConfirmView(pending_members, sheet_name, target_group)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="小組上繳匯款", description="【小組長專用】收齊款項後，向牙材長回報整組上繳之末五碼")
@app_commands.describe(末五碼="轉帳給牙材長帳戶的末五碼", 指定組別="若代班請填組別數字（預設為自己組別）")
async def report_group_payment(interaction: discord.Interaction, 末五碼: str, 指定組別: int = None):
    await interaction.response.defer(ephemeral=True)
    leader = get_member_info(interaction.user.id)
    if not leader or "小組長" not in str(leader.get('職位', '')):
        await interaction.followup.send("❌ 權限不足！", ephemeral=True)
        return

    target_group = f"第 {指定組別} 組" if 指定組別 is not None else f"第 {leader.get('組別', '')} 組"
    latest_sheet_name = get_sys_config("LATEST_SETTLEMENT_SHEET")
    if not latest_sheet_name:
        await interaction.followup.send("❌ 找不到最新的結算表。", ephemeral=True)
        return

    def _report_group_sync():
        target_settle_sheet = doc.worksheet(latest_sheet_name)
        records = target_settle_sheet.get_all_values()
        in_zone_c = False
        for i, row in enumerate(records):
            title = str(row[0]).strip() if len(row) > 0 else ""
            if "區塊 C" in title:
                in_zone_c = True
                continue

            if in_zone_c and len(row) >= 4 and str(row[0]).strip() == target_group:
                target_idx = i + 1
                cells = [
                    gspread.Cell(row=target_idx, col=3, value=f"'{末五碼.strip()}"),
                    gspread.Cell(row=target_idx, col=4, value="已匯款待審核")
                ]
                target_settle_sheet.update_cells(cells)
                return True
        return False

    try:
        updated = await run_gsheet(_report_group_sync)
        if updated:
            await interaction.followup.send(f"✅ 成功向牙材長回報！已登記 **{target_group}** 上繳末五碼 `[{末五碼.strip()}]`。", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ 找不到 {target_group} 的應上繳紀錄（可能該組本期無人成團）。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 讀取報表失敗：{e}", ephemeral=True)


@bot.tree.command(name="全班帳務總覽", description="【牙材長專用】查看並一鍵確認各小組長上繳狀態")
async def overall_check(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    mem = get_member_info(interaction.user.id)
    if not mem or "牙材長" not in str(mem.get('職位', '')):
        await interaction.followup.send("❌ 您非牙材長，權限不足！", ephemeral=True)
        return

    sheet_name = get_sys_config("LATEST_SETTLEMENT_SHEET")
    if not sheet_name:
        await interaction.followup.send("❌ 找不到當期結算報表設定。", ephemeral=True)
        return

    def _fetch_overall():
        target_settle_sheet = doc.worksheet(sheet_name)
        return target_settle_sheet.get_all_values()

    try:
        records = await run_gsheet(_fetch_overall)
    except Exception:
        await interaction.followup.send(f"❌ 找不到當期結算報表 `[{sheet_name}]`。", ephemeral=True)
        return

    embed = discord.Embed(title="👑 全班各組上繳總覽", color=0xf1c40f)
    found = False
    in_zone_c = False
    pending_groups = []

    for row in records:
        title = str(row[0]).strip() if len(row) > 0 else ""
        if "區塊 C" in title:
            in_zone_c = True
            continue

        if in_zone_c and len(row) >= 4 and "組" in str(row[0]):
            found = True
            group_name = str(row[0]).strip()
            total_price = str(row[1]).strip()
            report_code = str(row[2]).strip().lstrip("'")
            status = str(row[3]).strip()

            status_text = f"💰 應上繳: ${total_price} | 狀態: **{status}**"
            if report_code:
                status_text += f" (末五碼: {report_code})"
            embed.add_field(name=f"📦 {group_name}", value=status_text, inline=False)

            if "待審核" in status:
                pending_groups.append({"組別": group_name, "code": report_code, "amount": total_price})

    if not found:
        embed.description = "本期無任何小組需要上繳帳款。"
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        if pending_groups:
            view = HeadConfirmView(pending_groups, sheet_name)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)


# ================= 最底部啟動點 =================
if __name__ == "__main__":
    DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    print("🌐 正在啟動 Flask 背景網頁服務（Render 專用防休眠）...")
    keep_alive()
    print("🤖 正在連線至 Discord 核心伺服器...")
    bot.run(DISCORD_TOKEN)