import os
import json
import datetime
import uuid
import asyncio
from threading import Thread
from flask import Flask
import discord
from discord import app_commands
from discord.ui import View, Select, Modal, TextInput
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= 1. Flask 防休眠網頁設定 =================
app = Flask('')
@app.route('/')
def home():
    return " 🤖逼哩逼哩🤖 \n  牙材訂購機器人一生懸命中！"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

def keep_alive():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()

# ================= 2. Google Sheets 安全連線 =================
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
google_creds_env = os.getenv("GOOGLE_CREDS_JSON")

if google_creds_env:
    cleaned_json_str = google_creds_env.strip().strip('"').strip("'")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(cleaned_json_str), scope)
else:
    creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)

gc = gspread.authorize(creds)
SPREADSHEET_NAME = "牙材_discord_bot" # ⚠️ 修改為你的 Google Sheet 名稱
doc = gc.open(SPREADSHEET_NAME)

members_sheet = doc.worksheet("Members")
products_sheet = doc.worksheet("Products")
orders_sheet = doc.worksheet("Orders_Temp")

try:
    sys_sheet = doc.worksheet("System_Config")
except gspread.WorksheetNotFound:
    sys_sheet = doc.add_worksheet(title="System_Config", rows="50", cols="2")
    sys_sheet.append_row(["設定項目", "設定值"])

# ─── 新增：全域快取 (Cache) 變數 ───
CACHE = {
    "members": [],
    "products": [],
    "sys_config": {}
}

IS_ORDER_OPEN = False
ANNOUNCEMENT_CHANNEL_ID = None
scheduler = AsyncIOScheduler(timezone="Asia/Taipei")

# ================= 3. 輔助函式 (升級防呆與非同步版) =================

async def reload_cache():
    """在背景執行緒中重新載入靜態資料至記憶體"""
    def _fetch():
        m = members_sheet.get_all_records()
        p = products_sheet.get_all_records()
        c = sys_sheet.get_all_values()
        
        config_dict = {}
        for row in c:
            if len(row) >= 2:
                config_dict[str(row[0]).strip()] = str(row[1]).strip()
        
        return m, p, config_dict

    try:
        m, p, c = await asyncio.to_thread(_fetch)
        CACHE["members"] = m
        CACHE["products"] = p
        CACHE["sys_config"] = c
        print("✅ 快取資料已成功更新。")
    except Exception as e:
        print(f"❌ 快取載入失敗: {e}")

def get_member_info(user_id):
    """直接從記憶體快取中尋找成員"""
    search_str = str(user_id).strip()
    for row in CACHE["members"]:
        sheet_uid = str(row.get('Discord_User_ID', '')).strip()
        if '.' in sheet_uid:
            sheet_uid = sheet_uid.split('.')[0]
        if sheet_uid == search_str:
            return {"姓名": row.get("姓名"), "組別": row.get("組別"), "職位": str(row.get("職位", ""))}
    return None

def get_sys_config(key):
    """直接從記憶體快取讀取設定"""
    return CACHE["sys_config"].get(key, None)

async def update_sys_config(key, value):
    """將設定寫入表單，並同步更新記憶體快取 (放入背景執行)"""
    def _update():
        all_values = sys_sheet.get_all_values()
        for i, row in enumerate(all_values):
            if len(row) > 0 and str(row[0]).strip() == key:
                sys_sheet.update_cell(i + 1, 2, f"'{value}")
                return
        sys_sheet.append_row([key, f"'{value}"])
        
    await asyncio.to_thread(_update)
    CACHE["sys_config"][key] = str(value) # 同步更新快取

async def async_get_all_records(sheet):
    """非同步讀取整個分頁記錄"""
    return await asyncio.to_thread(sheet.get_all_records)

async def async_get_all_values(sheet):
     """非同步讀取整個分頁值"""
     return await asyncio.to_thread(sheet.get_all_values)

def compute_live_product_summary(all_orders):
    """純計算，不再呼叫 API"""
    summary = {}
    for o in all_orders:
        item_id = str(o['Item_ID'])
        try:
            qty = int(o.get('購買數量', 0) or 0)
        except ValueError:
            qty = 0
        summary[item_id] = summary.get(item_id, 0) + qty
    return summary


# ================= 4. 自動收單、歷史歸檔、重置工作區 =================

async def auto_reminder():
    if not ANNOUNCEMENT_CHANNEL_ID: return
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not channel: return

    products = CACHE["products"]
    # 這裡必須即時讀取暫存區
    all_orders = await async_get_all_records(orders_sheet)
    summary = compute_live_product_summary(all_orders)
    
    warning_text = ""
    for p in products:
        try: moq = int(p.get('最低購買量', 1) or 1)
        except: moq = 1
        if moq > 1:
            current_total = summary.get(str(p['Item_ID']), 0)
            if current_total < moq:
                warning_text += f"⚠️ **[{p.get('品項名稱', '未知')}]** 目前全班僅湊 **{current_total}** / {moq} 支 (還差 {moq - current_total} 支才出貨！)\n"
    
    if warning_text:
        target_role_id = get_sys_config("TARGET_ROLE_ID")
        ping_text = f"<@&{target_role_id}>" if target_role_id and target_role_id.strip() else "【同學們】"
        embed = discord.Embed(title="🚨 牙材訂購截止倒數：湊單未達標品項公告！", description=warning_text, color=0xe67e22)
        await channel.send(content=f"{ping_text} 湊單品項如果截止時未達標，該品項將整單取消喔！請大家幫忙補刀！", embed=embed)

async def send_dm_task(user_id, embed):
    """發送私訊的工作單元"""
    try:
        user = await bot.fetch_user(int(user_id))
        await user.send(embed=embed)
    except:
        pass

async def auto_close_order():
    global IS_ORDER_OPEN
    IS_ORDER_OPEN = False
    if not ANNOUNCEMENT_CHANNEL_ID: return
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not channel: return

    try:
        # 非同步讀取
        all_orders = await async_get_all_records(orders_sheet)
        products = CACHE["products"]
        prod_map = {str(p['Item_ID']): p for p in products}
        summary = compute_live_product_summary(all_orders)
        date_str = datetime.datetime.now().strftime("%m%d")

        if not all_orders:
            await channel.send("🔒 本期訂購已截止，因無任何同學下單，系統不生成報表。")
            await update_sys_config("IS_ORDER_OPEN", "False")
            return

        await channel.send("⏳ 正在產生歷史流水帳與結算報表，因資料量大可能需時數秒，請稍候...")

        # ─── 歷史備份 ───
        raw_sheet_name = f"歷史_{date_str}原始明細"
        def _build_raw():
            try: doc.del_worksheet(doc.worksheet(raw_sheet_name))
            except: pass
            raw_ws = doc.add_worksheet(title=raw_sheet_name, rows="100", cols="10")
            raw_data = [["Order_ID", "Discord_User_ID", "姓名", "組別", "Item_ID", "購買數量", "單項總價"]]
            for o in all_orders:
                raw_data.append([o['Order_ID'], str(o['Discord_User_ID']), o['姓名'], o['組別'], str(o['Item_ID']), o['購買數量'], o['單項總價']])
            raw_ws.append_rows(raw_data)
        await asyncio.to_thread(_build_raw)

        # ─── 結算報表 ───
        settle_sheet_name = f"{date_str}牙材團購結算"
        
        # 記憶體內組裝結算資料
        settle_data = [] 
        
        # 區塊 A
        settle_data.append(["【區塊 A：牙材長向廠商叫貨總表】"])
        settle_data.append(["品項 ID", "品項名稱", "全班叫貨總量", "單價", "總金額", "出貨狀態"])
        valid_items = set()
        for p in products:
            item_id = str(p['Item_ID'])
            total_qty = summary.get(item_id, 0)
            try: moq = int(p.get('最低購買量', 1) or 1)
            except: moq = 1
            try: price = int(p.get('單價', 0) or 0)
            except: price = 0

            if total_qty == 0: continue
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
            item_id = str(order['Item_ID'])
            if item_id not in valid_items: continue
            uid = str(order['Discord_User_ID'])
            if uid not in group_billing:
                group_billing[uid] = {"姓名": order['姓名'], "組別": order['組別'], "明細": [], "總價": 0}
            
            p_info = prod_map.get(item_id, {"品項名稱": "未知品項"})
            group_billing[uid]["明細"].append(f"{p_info.get('品項名稱', '未知')}x{order['購買數量']}")
            try: subtotal = int(order.get('單項總價', 0) or 0)
            except: subtotal = 0
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
            nums = [int(s) for s in s.split() if s.isdigit()]
            return nums[0] if nums else 0
            
        sorted_group_names = sorted(group_totals.keys(), key=extract_num)
        for g_name in sorted_group_names:
            settle_data.append([g_name, group_totals[g_name], "", "未匯款"])

        # 背景寫入結算報表與清空暫存
        def _write_settle_and_clear():
            try: doc.del_worksheet(doc.worksheet(settle_sheet_name))
            except: pass
            settle_ws = doc.add_worksheet(title=settle_sheet_name, rows="100", cols="10")
            settle_ws.append_rows(settle_data)
            orders_sheet.clear()
            orders_sheet.append_row(["Order_ID", "Discord_User_ID", "姓名", "組別", "Item_ID", "購買數量", "單項總價"])
        await asyncio.to_thread(_write_settle_and_clear)

        await update_sys_config("LATEST_SETTLEMENT_SHEET", settle_sheet_name)
        await update_sys_config("IS_ORDER_OPEN", "False")
        await update_sys_config("CLOSE_TIME", "")

        await channel.send(f"🔒 **本期牙材訂購已順利截止！**\n系統已成功產生歷史備份 `[{raw_sheet_name}]` 與結算報表 `[{settle_sheet_name}]`！\n**當期暫存工作區已全數清空重置**，下單通道關閉。")
        
        # ─── 高效併發發送私訊 ───
        dm_tasks = []
        for user_id, data in group_billing.items():
            embed = discord.Embed(title="🦷 您的當期牙材訂購個人帳單", color=0x3498db)
            embed.add_field(name="訂購明細", value="\n".join(data["明細"]), inline=False)
            embed.add_field(name="💰 應匯總金額", value=f"NT$ {data['總價']:,}", inline=False)
            embed.set_footer(text="請匯款給您所屬的小組長後，使用 /回報匯款 登記末五碼。")
            dm_tasks.append(send_dm_task(user_id, embed))
            
        if dm_tasks:
             await asyncio.gather(*dm_tasks) # 同時發送

    except Exception as e:
        import traceback
        print(f"自動結算時發生崩潰:\n{traceback.format_exc()}")
        await channel.send(f"❌ **自動截單時發生錯誤，報表產生失敗！**\n錯誤代碼：`{e}`\n👉 請牙材長手動前往備份 `Orders_Temp` 後聯繫管理員。")


# ================= 5. 下單與對帳 UI 元件 =================

class MultiOrderModal(Modal):
    def __init__(self, selected_products):
        super().__init__(title="填寫購買數量 (輸入正整數)")
        self.selected_products = selected_products
        self.inputs = []
        for p in selected_products:
            try: price = int(p.get('單價', 0) or 0)
            except: price = 0
            inp = TextInput(label=f"{p['品項名稱']} (單價:${price})", placeholder="請輸入數量", required=True, max_length=4)
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
        reply_msg = "✅ **成功加入暫存訂單！**\n"
        total_cost = 0

        for p, inp in self.inputs:
            try:
                qty = int(inp.value)
                if qty <= 0: raise ValueError
            except ValueError:
                await interaction.followup.send(f"❌ 數量輸入錯誤：{p['品項名稱']} 必須為正整數！", ephemeral=True)
                return
            try: price = int(p.get('單價', 0) or 0)
            except: price = 0
            subtotal = qty * price
            total_cost += subtotal
            order_id = f"ORD-{uuid.uuid4().hex[:6].upper()}"
            rows_to_add.append([order_id, str(interaction.user.id), mem['姓名'], mem['組別'], str(p['Item_ID']), qty, subtotal])
            reply_msg += f"• {p['品項名稱']} x {qty} (小計: ${subtotal})\n"

        # 背景寫入訂單
        await asyncio.to_thread(orders_sheet.append_rows, rows_to_add)
        
        reply_msg += f"\n**本次新增總金額：** NT$ {total_cost:,}\n*(可使用 `/我的訂單` 檢視或修改)*"
        await interaction.followup.send(reply_msg, ephemeral=True)

class ProductSelect(Select):
    # 修改：不再在 __init__ 中抓資料，改由外部傳入 summary
    def __init__(self, products, summary):
        options = []
        for p in products:
            item_id = str(p['Item_ID'])
            current_total = summary.get(item_id, 0)
            try: moq = int(p.get('最低購買量', 1) or 1)
            except: moq = 1
            try: price = int(p.get('單價', 0) or 0)
            except: price = 0

            if moq <= 1:
                desc = f"單價: ${price} | 全班已訂: {current_total} 個"
            else:
                desc = f"湊單制 | 進度: {current_total}/{moq} (還差 {max(0, moq-current_total)} 支)"
            options.append(discord.SelectOption(label=p['品項名稱'], description=desc, value=item_id))
        
        max_selectable = min(5, len(options))
        super().__init__(placeholder=f"請勾選欲訂購品項 (單次最多勾選 {max_selectable} 項)", min_values=1, max_values=max_selectable, options=options)
        self.products = products

    async def callback(self, interaction: discord.Interaction):
        selected_products = [p for p in self.products if str(p['Item_ID']) in self.values]
        await interaction.response.send_modal(MultiOrderModal(selected_products))

class CancelOrderSelect(Select):
    def __init__(self, user_orders):
        options = []
        for o in user_orders:
            label = f"{o['品項名稱']} x {o['購買數量']}"
            desc = f"總價: ${o['單項總價']} (單號:{o['Order_ID'][-6:]})"
            options.append(discord.SelectOption(label=label, description=desc, value=o['Order_ID']))
        super().__init__(placeholder="❌ 若需修改，請選擇要「取消」的品項...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not IS_ORDER_OPEN:
            await interaction.response.send_message("❌ 目前非訂購期間，無法修改訂單！", ephemeral=True)
            return
        order_id_to_cancel = self.values[0]
        await interaction.response.defer(ephemeral=True)
        
        def _delete():
             cell = orders_sheet.find(order_id_to_cancel, in_column=1)
             orders_sheet.delete_rows(cell.row)
             
        try:
            # 背景執行刪除
            await asyncio.to_thread(_delete)
            await interaction.followup.send("✅ 已成功取消該筆訂單！如需變更數量請重新使用 `/訂購牙材` 下單。", ephemeral=True)
        except gspread.CellNotFound:
            await interaction.followup.send("❌ 找不到該筆訂單，可能已經被取消了。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 取消時發生錯誤：{e}", ephemeral=True)

class CancelOrderView(View):
    def __init__(self, user_orders):
        super().__init__(timeout=None)
        self.add_item(CancelOrderSelect(user_orders))

# ─── 小組長專用一鍵確認選單 ───
class GroupConfirmSelect(Select):
    def __init__(self, pending_members, sheet_name, target_group):
        options = []
        for m in pending_members[:25]:
            options.append(discord.SelectOption(label=m['姓名'], description=f"回報末五碼: {m['code']} | 應繳: ${m['amount']}", value=m['姓名']))
        super().__init__(placeholder="✅ 在此勾選已確認入帳的同學", min_values=1, max_values=len(options), options=options)
        self.sheet_name = sheet_name
        self.target_group = target_group

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # 修改：批次更新寫入邏輯
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
             updated_names = await asyncio.to_thread(_batch_update)
             await interaction.followup.send(f"👍 已確認以下同學款項入帳，狀態更新完畢：\n**{', '.join(updated_names)}**", ephemeral=True)
        except Exception as e:
             await interaction.followup.send(f"❌ 更新失敗：{e}", ephemeral=True)

class GroupConfirmView(View):
    def __init__(self, pending_members, sheet_name, target_group):
        super().__init__(timeout=None)
        self.add_item(GroupConfirmSelect(pending_members, sheet_name, target_group))

# ─── 牙材長專用一鍵確認選單 ───
class HeadConfirmSelect(Select):
    def __init__(self, pending_groups, sheet_name):
        options = []
        for g in pending_groups[:25]: 
            options.append(discord.SelectOption(label=g['組別'], description=f"上繳末五碼: {g['code']} | 應繳總額: ${g['amount']}", value=g['組別']))
        super().__init__(placeholder="✅ 在此勾選已確認入帳的小組", min_values=1, max_values=len(options), options=options)
        self.sheet_name = sheet_name

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # 修改：批次更新寫入邏輯
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
            updated_groups = await asyncio.to_thread(_batch_update)
            await interaction.followup.send(f"👑 已確認收到以下小組的帳款：\n**{', '.join(updated_groups)}**", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ 更新失敗：{e}", ephemeral=True)

class HeadConfirmView(View):
    def __init__(self, pending_groups, sheet_name):
        super().__init__(timeout=None)
        self.add_item(HeadConfirmSelect(pending_groups, sheet_name))


# ================= 6. 機器人核心指令群 =================
class DentalERPBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        
    async def setup_hook(self):
        # 啟動時預先載入快取
        await reload_cache()
        scheduler.start()
        await self.tree.sync()
        print("✅ 指令樹同步完成。")
        try:
            global IS_ORDER_OPEN, ANNOUNCEMENT_CHANNEL_ID
            is_open = get_sys_config("IS_ORDER_OPEN")
            if is_open == "True":
                IS_ORDER_OPEN = True
                close_time_str = get_sys_config("CLOSE_TIME")
                channel_id_str = get_sys_config("ANNOUNCEMENT_CHANNEL_ID")
                if channel_id_str: ANNOUNCEMENT_CHANNEL_ID = int(channel_id_str)
                if close_time_str:
                    dt = datetime.datetime.strptime(close_time_str, "%Y-%m-%d %H:%M")
                    if dt > datetime.datetime.now():
                        scheduler.add_job(auto_close_order, 'date', run_date=dt)
                        reminder_time = dt - datetime.timedelta(days=3)
                        if reminder_time > datetime.datetime.now():
                            scheduler.add_job(auto_reminder, 'date', run_date=reminder_time)
                        print(f"🔄 成功從系統恢復排程！將於 {close_time_str} 截單。")
        except Exception as e:
            print(f"⚠️ 恢復開團狀態失敗：{e}")

bot = DentalERPBot()


@bot.tree.command(name="刷新名冊與商品庫", description="【牙材長專用】手動重新從 Google Sheet 載入名冊與商品清單")
async def force_reload_cache(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    mem = get_member_info(interaction.user.id)
    if not mem or "牙材長" not in str(mem.get('職位', '')):
        await interaction.followup.send("❌ 您非牙材長，權限不足！", ephemeral=True)
        return
    await reload_cache()
    await interaction.followup.send("✅ 已成功從 Google Sheet 重新載入最新名冊與商品清單至記憶體！", ephemeral=True)


@bot.tree.command(name="綁定名冊", description="【全班同學必用】首次使用時，綁定您的 Discord 帳號")
async def bind_name(interaction: discord.Interaction, 真實姓名: str):
    await interaction.response.defer(ephemeral=True)
    try:
        # 直接拿快取資料判斷
        all_members = CACHE["members"]
        user_id_str = str(interaction.user.id).strip()
        for row in all_members:
            sheet_uid = str(row.get('Discord_User_ID', '')).strip().split('.')[0]
            if sheet_uid == user_id_str:
                await interaction.followup.send(f"❌ 您已綁定過姓名「{row.get('姓名')}」囉！若需更換請聯繫牙材長。", ephemeral=True)
                return
        
        updated = False
        target_idx = -1
        for idx, row in enumerate(all_members, start=2):
            if str(row.get('姓名', '')).strip() == 真實姓名.strip():
                current_bound_id = str(row.get('Discord_User_ID', '')).strip()
                if current_bound_id and current_bound_id != "0" and current_bound_id != "":
                    await interaction.followup.send(f"❌ 「{真實姓名}」已經被其他帳號綁定了！", ephemeral=True)
                    return
                target_idx = idx
                role = row.get('職位')
                updated = True
                break
                
        if updated:
            # 寫入背景化
            def _update_bind():
                members_sheet.update_cell(target_idx, 1, f"'{user_id_str}")
            await asyncio.to_thread(_update_bind)
            await reload_cache() # 綁定完刷新快取
            await interaction.followup.send(f"🎉 綁定成功！**【{真實姓名}】** 歡迎！您的職位為 **[{role}]**。", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ 找不到名為「{真實姓名}」的同學，請確認是否有打錯字！", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 綁定失敗，系統發生錯誤: {e}", ephemeral=True)

@bot.tree.command(name="開團訂購牙材", description="【牙材長專用】設定截止時間並開啟下單通道")
async def start_group_buy(interaction: discord.Interaction, 截止時間: str):
    global IS_ORDER_OPEN, ANNOUNCEMENT_CHANNEL_ID
    await interaction.response.defer(ephemeral=True)
    mem = get_member_info(interaction.user.id)
    if not mem or "牙材長" not in str(mem.get('職位', '')):
        await interaction.followup.send("❌ 您非牙材長，權限不足！", ephemeral=True)
        return
    try:
        dt = datetime.datetime.strptime(截止時間, "%Y-%m-%d %H:%M")
    except ValueError:
        await interaction.followup.send(f"❌ 時間格式錯誤！請依照格式輸入：`2026-07-30 23:59`\n（您剛才輸入的是：`{截止時間}`）", ephemeral=True)
        return
    if dt <= datetime.datetime.now():
        await interaction.followup.send("❌ 截止時間必須是未來的時間！", ephemeral=True)
        return
    try:
        IS_ORDER_OPEN = True
        ANNOUNCEMENT_CHANNEL_ID = interaction.channel_id
        scheduler.remove_all_jobs()
        scheduler.add_job(auto_close_order, 'date', run_date=dt)
        reminder_time = dt - datetime.timedelta(days=3)
        if reminder_time > datetime.datetime.now():
            scheduler.add_job(auto_reminder, 'date', run_date=reminder_time)

        await update_sys_config("IS_ORDER_OPEN", "True")
        await update_sys_config("CLOSE_TIME", 截止時間)
        await update_sys_config("ANNOUNCEMENT_CHANNEL_ID", str(interaction.channel_id))

        target_role_id = get_sys_config("TARGET_ROLE_ID")
        ping_text = f"<@&{target_role_id}>\n" if target_role_id and target_role_id.strip() else ""
        await interaction.followup.send(f"{ping_text}📢 **當期牙材訂購正式開跑！**\n系統將在 `{截止時間}` 自動截單並清空重置。")
    except Exception as e:
        await interaction.followup.send(f"❌ 開團失敗，系統發生錯誤：`{e}`", ephemeral=True)

@bot.tree.command(name="強制關團", description="【牙材長專用】立即關閉下單通道並產生結算報表")
async def force_close_order(interaction: discord.Interaction):
    global IS_ORDER_OPEN
    await interaction.response.defer(ephemeral=True)
    mem = get_member_info(interaction.user.id)
    if not mem or "牙材長" not in str(mem.get('職位', '')):
        await interaction.followup.send("❌ 您非牙材長，權限不足！", ephemeral=True)
        return
    if not IS_ORDER_OPEN:
        await interaction.followup.send("❌ 目前沒有正在進行中的團購，不需要關團！", ephemeral=True)
        return
    
    try:
        scheduler.remove_all_jobs()
        await interaction.followup.send("✅ 收到強制關團指令！正在處理結算報表，請稍候並留意頻道公告...", ephemeral=True)
        # 背景化處理
        asyncio.create_task(auto_close_order())
    except Exception as e:
        await interaction.followup.send(f"❌ 強制關團時發生錯誤：{e}", ephemeral=True)

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
        dt = datetime.datetime.strptime(新截止時間, "%Y-%m-%d %H:%M")
    except ValueError:
        await interaction.followup.send(f"❌ 時間格式錯誤！請依照格式輸入：`2026-07-30 23:59`", ephemeral=True)
        return
    if dt <= datetime.datetime.now():
        await interaction.followup.send("❌ 截止時間必須是未來的時間！", ephemeral=True)
        return
    try:
        scheduler.remove_all_jobs()
        scheduler.add_job(auto_close_order, 'date', run_date=dt)
        reminder_time = dt - datetime.timedelta(days=3)
        if reminder_time > datetime.datetime.now():
            scheduler.add_job(auto_reminder, 'date', run_date=reminder_time)
        await update_sys_config("CLOSE_TIME", 新截止時間)
        await interaction.followup.send(f"✅ 成功修改！新的截止時間為 `{新截止時間}`。", ephemeral=True)
        if ANNOUNCEMENT_CHANNEL_ID:
            channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
            if channel:
                target_role_id = get_sys_config("TARGET_ROLE_ID")
                ping_text = f"<@&{target_role_id}>\n" if target_role_id and target_role_id.strip() else ""
                await channel.send(f"{ping_text}📢 **【牙材長公告】截單時間已變更！**\n新的截單時間變更至 `{新截止時間}`，請大家留意！")
    except Exception as e:
        await interaction.followup.send(f"❌ 修改時間失敗：`{e}`", ephemeral=True)

@bot.tree.command(name="團購進度總覽", description="【全班通用】即時查看當期牙材的湊單進度與數量")
async def progress_overview(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    is_open_str = get_sys_config("IS_ORDER_OPEN")
    if is_open_str != "True":
        await interaction.followup.send("❌ 目前沒有正在進行的團購！", ephemeral=True)
        return
    try:
        # 改為非同步讀取
        products = CACHE["products"]
        all_orders = await async_get_all_records(orders_sheet)
        summary = compute_live_product_summary(all_orders)
        
        embed = discord.Embed(title="📊 當期牙材湊單進度總覽", color=0x2ecc71)
        text_content = ""
        for p in products:
            item_id = str(p.get('Item_ID', ''))
            name = p.get('品項名稱', '未知商品')
            try: moq = int(p.get('最低購買量', 1))
            except: moq = 1
            current_total = summary.get(item_id, 0)
            if moq <= 1:
                text_content += f"🔹 **{name}**\n　 └ 已訂購: `{current_total}` 個\n"
            else:
                if current_total >= moq:
                    text_content += f"✅ **{name}**\n　 └ 已達標: `{current_total} / {moq}` 個\n"
                else:
                    text_content += f"⚠️ **{name}**\n　 └ 湊單中: `{current_total} / {moq}` 個 (還差 {moq - current_total})\n"
        embed.description = text_content
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 查詢失敗: {e}", ephemeral=True)

@bot.tree.command(name="訂購牙材", description="挑選當期牙材並進行訂購（可一次勾選多項）")
async def order_material(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True) # 立刻 defer 避免超時
    if not IS_ORDER_OPEN:
        await interaction.followup.send("❌ 目前非訂購期間，無法進行訂購！", ephemeral=True)
        return
    
    try:
        # 背景化處理 API 請求
        products = CACHE["products"]
        all_orders = await async_get_all_records(orders_sheet)
        summary = compute_live_product_summary(all_orders)

        view = View()
        view.add_item(ProductSelect(products, summary))
        await interaction.followup.send("🦷 **請勾選欲訂購的品項：**\n*(註：單次最多只能同時結帳 5 項。若超過請分多次下單！)*", view=view, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 讀取資料失敗，可能網路延遲過大。錯誤: {e}", ephemeral=True)

@bot.tree.command(name="我的訂單", description="【個人專用】檢視自己目前的暫存訂單，可修改刪除")
async def my_orders(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not IS_ORDER_OPEN:
        await interaction.followup.send("🔒 目前非訂購期間，無法查看或修改暫存區！", ephemeral=True)
        return
    
    try:
        # 非同步請求
        all_orders = await async_get_all_records(orders_sheet)
        products = CACHE["products"]
        prod_map = {str(p['Item_ID']): p.get('品項名稱', '未知品項') for p in products}

        user_orders = []
        total_cost = 0
        for o in all_orders:
            if str(o.get('Discord_User_ID', '')) == str(interaction.user.id):
                o['品項名稱'] = prod_map.get(str(o['Item_ID']), "未知品項")
                user_orders.append(o)
                try: cost = int(o.get('單項總價', 0) or 0)
                except: cost = 0
                total_cost += cost

        if not user_orders:
            await interaction.followup.send("🛒 您目前沒有任何訂購明細喔！", ephemeral=True)
            return
        embed = discord.Embed(title="🛒 您的當期購物車明細", description="以下是您目前預訂的品項（尚未截單）：", color=0x2ecc71)
        for o in user_orders:
            embed.add_field(name=o['品項名稱'], value=f"數量: {o.get('購買數量', 0)} | 小計: ${o.get('單項總價', 0)}", inline=False)
        embed.add_field(name="💰 目前累積總額", value=f"**NT$ {total_cost:,}**", inline=False)
        
        view = CancelOrderView(user_orders)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 讀取訂單失敗：{e}", ephemeral=True)

@bot.tree.command(name="回報匯款", description="【全班同學】匯款後回報您的帳戶末五碼")
async def report_payment(interaction: discord.Interaction, 末五碼: str):
    await interaction.response.defer(ephemeral=True)
    latest_sheet_name = get_sys_config("LATEST_SETTLEMENT_SHEET")
    if not latest_sheet_name:
        await interaction.followup.send("❌ 系統設定中找不到最新的結算表，請確認牙材長是否已經產生報表。", ephemeral=True)
        return
    try:
        target_settle_sheet = doc.worksheet(latest_sheet_name)
        all_values = await async_get_all_values(target_settle_sheet)
    except:
        await interaction.followup.send(f"❌ 找不到結算報表 `[{latest_sheet_name}]`。", ephemeral=True)
        return

    user_info = get_member_info(interaction.user.id)
    if not user_info:
        await interaction.followup.send("❌ 找不到您的名冊，請先使用 `/綁定名冊`。", ephemeral=True)
        return

    updated = False
    in_zone_b = False 
    target_idx = -1
    for i, row in enumerate(all_values):
        title = str(row[0]).strip() if len(row) > 0 else ""
        if "區塊 B" in title:
            in_zone_b = True
            continue
        if "區塊 C" in title:
            break
            
        if in_zone_b and len(row) >= 6 and str(row[1]).strip() == user_info['姓名']:
            target_idx = i + 1
            updated = True
            break
            
    if updated:
        def _update():
            target_settle_sheet.update_cell(target_idx, 5, f"'{末五碼}") 
            target_settle_sheet.update_cell(target_idx, 6, "已匯款待審核") 
        await asyncio.to_thread(_update)
        await interaction.followup.send(f"✅ 匯款回報成功！已在結算單中登記末五碼 `[{末五碼}]`，請等待小組長審核。", ephemeral=True)
    else:
        await interaction.followup.send("❌ 找不到您的應繳費紀錄（可能您本期無下單或品項遭淘汰）。", ephemeral=True)

@bot.tree.command(name="組內對帳", description="【小組長專用】查看並一鍵確認組內同學繳費（可跨組）")
@app_commands.describe(指定組別="若代班請填組別數字（可不填，預設為自己組別）")
async def group_check(interaction: discord.Interaction, 指定組別: int = None):
    await interaction.response.defer(ephemeral=True)
    leader = get_member_info(interaction.user.id)
    if not leader or "小組長" not in str(leader.get('職位', '')):
        await interaction.followup.send("❌ 您非登記之小組長，權限不足！", ephemeral=True)
        return

    target_group = f"第 {指定組別} 組" if 指定組別 is not None else f"第 {leader.get('組別', '')} 組"

    try: 
        sheet_name = get_sys_config("LATEST_SETTLEMENT_SHEET")
        target_settle_sheet = doc.worksheet(sheet_name)
        records = await async_get_all_values(target_settle_sheet)
    except:
        await interaction.followup.send("❌ 找不到當期結算報表。", ephemeral=True)
        return

    embed = discord.Embed(title=f"📋 {target_group} 繳費對帳進度", color=0x9b59b6)
    found = False
    in_zone_b = False
    pending_members = []

    for row in records:
        title = str(row[0]).strip() if len(row) > 0 else ""
        if "區塊 B" in title:
            in_zone_b = True
            continue
        if "區塊 C" in title:
            break
            
        if in_zone_b and len(row) >= 6 and str(row[0]).strip() == target_group:
            found = True
            name = str(row[1])
            total_price = str(row[3])
            report_code = str(row[4]).strip("'")
            status = str(row[5])
            
            status_text = f"💰 應繳: ${total_price} | 狀態: **{status}**"
            if report_code: status_text += f" (末五碼: {report_code})"
            embed.add_field(name=f"👤 {name}", value=status_text, inline=False)
            
            if "待審核" in status:
                pending_members.append({"姓名": name, "code": report_code, "amount": total_price})
            
    if not found: 
        embed.description = f"本期 {target_group} 無人需繳費，或查無此組資料。"
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
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

    try:
        target_settle_sheet = doc.worksheet(latest_sheet_name)
        records = await async_get_all_values(target_settle_sheet)
    except:
        await interaction.followup.send(f"❌ 讀取報表失敗。", ephemeral=True)
        return

    updated = False
    in_zone_c = False 
    target_idx = -1
    for i, row in enumerate(records):
        title = str(row[0]).strip() if len(row) > 0 else ""
        if "區塊 C" in title:
            in_zone_c = True
            continue
            
        if in_zone_c and len(row) >= 4 and str(row[0]).strip() == target_group:
            target_idx = i + 1
            updated = True
            break
            
    if updated:
        def _update():
            target_settle_sheet.update_cell(target_idx, 3, f"'{末五碼}") 
            target_settle_sheet.update_cell(target_idx, 4, "已匯款待審核") 
        await asyncio.to_thread(_update)
        await interaction.followup.send(f"✅ 成功向牙材長回報！已登記 **{target_group}** 上繳末五碼 `[{末五碼}]`。", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ 找不到 {target_group} 的應上繳紀錄（可能該組本期無人成團）。", ephemeral=True)

@bot.tree.command(name="全班帳務總覽", description="【牙材長專用】查看並一鍵確認各小組長上繳狀態")
async def overall_check(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    mem = get_member_info(interaction.user.id)
    if not mem or "牙材長" not in str(mem.get('職位', '')):
        await interaction.followup.send("❌ 您非牙材長，權限不足！", ephemeral=True)
        return

    try: 
        sheet_name = get_sys_config("LATEST_SETTLEMENT_SHEET")
        target_settle_sheet = doc.worksheet(sheet_name)
        records = await async_get_all_values(target_settle_sheet)
    except:
        await interaction.followup.send("❌ 找不到當期結算報表。", ephemeral=True)
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
            group_name = str(row[0])
            total_price = str(row[1])
            report_code = str(row[2]).strip("'")
            status = str(row[3])
            
            status_text = f"💰 應上繳: ${total_price} | 狀態: **{status}**"
            if report_code: status_text += f" (末五碼: {report_code})"
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