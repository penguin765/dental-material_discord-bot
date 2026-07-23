import os
import json
import datetime
import uuid
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

IS_ORDER_OPEN = False
ANNOUNCEMENT_CHANNEL_ID = None
scheduler = AsyncIOScheduler(timezone="Asia/Taipei")

# ================= 3. 輔助函式 (升級防呆版) =================

def get_member_info(user_id):
    """從 Members 分頁取得使用者資料"""
    try:
        search_str = str(user_id).strip()
        all_members = members_sheet.get_all_records()
        for row in all_members:
            sheet_uid = str(row.get('Discord_User_ID', '')).strip()
            if '.' in sheet_uid:
                sheet_uid = sheet_uid.split('.')[0]
            if sheet_uid == search_str:
                return {"姓名": row.get("姓名"), "組別": row.get("組別"), "職位": str(row.get("職位", ""))}
        return None
    except:
        return None

def get_live_product_summary():
    """動態計算當期廚房暫存區的累計量"""
    all_orders = orders_sheet.get_all_records()
    summary = {}
    for o in all_orders:
        item_id = str(o['Item_ID'])
        qty = int(o['購買數量'])
        summary[item_id] = summary.get(item_id, 0) + qty
    return summary

# ================= 4. 自動收單、歷史歸檔、重置工作區 =================

async def auto_reminder():
    if not ANNOUNCEMENT_CHANNEL_ID: return
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not channel: return

    products = products_sheet.get_all_records()
    summary = get_live_product_summary()
    warning_text = ""
    for p in products:
        moq = int(p['最低購買量'])
        if moq > 1:
            current_total = summary.get(str(p['Item_ID']), 0)
            if current_total < moq:
                warning_text += f"⚠️ **[{p['品項名稱']}]** 目前全班僅湊 **{current_total}** / {moq} 支 (還差 {moq - current_total} 支才出貨！)\n"
    if warning_text:
        embed = discord.Embed(title="🚨 牙材訂購截止倒數：湊單未達標品項公告！", description=warning_text, color=0xe67e22)
        await channel.send(content="@everyone 湊單品項如果截止時未達標，該品項將整單取消喔！請大家幫忙補刀！", embed=embed)

async def auto_close_order():
    """核心：自動截單、分流報表、歷史歸檔、清空重置廚房"""
    global IS_ORDER_OPEN
    IS_ORDER_OPEN = False
    if not ANNOUNCEMENT_CHANNEL_ID: return
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not channel: return

    try:
        all_orders = orders_sheet.get_all_records()
        products = products_sheet.get_all_records()
        prod_map = {str(p['Item_ID']): p for p in products}
        summary = get_live_product_summary()
        date_str = datetime.datetime.now().strftime("%m%d")

        if not all_orders:
            await channel.send("🔒 本期訂購已截止，因無任何同學下單，系統不生成報表。")
            return

        # ─── 步驟一：建立當期原始流水帳備份 (歷史保留) ───
        raw_sheet_name = f"歷史_{date_str}原始明細"
        try: doc.del_worksheet(doc.worksheet(raw_sheet_name))
        except: pass
        raw_ws = doc.add_worksheet(title=raw_sheet_name, rows="100", cols="10")
        raw_ws.append_row(["Order_ID", "Discord_User_ID", "姓名", "組別", "Item_ID", "購買數量", "單項總價", "匯款末五碼", "對帳狀態"])
        for o in all_orders:
            raw_ws.append_row([o['Order_ID'], str(o['Discord_User_ID']), o['姓名'], o['組別'], str(o['Item_ID']), o['購買數量'], o['單項總價'], o['匯款末五碼'], o['對帳狀態']])

        # ─── 步驟二：建立二合一結算報表 (牙材長+小組長) ───
        settle_sheet_name = f"{date_str}牙材團購結算"
        try: doc.del_worksheet(doc.worksheet(settle_sheet_name))
        except: pass
        settle_ws = doc.add_worksheet(title=settle_sheet_name, rows="100", cols="10")

        # A 區寫入
        settle_ws.append_row(["【區塊 A：牙材長向廠商叫貨總表】"])
        settle_ws.append_row(["品項 ID", "品項名稱", "全班叫貨總量", "單價", "總金額", "出貨狀態"])
        valid_items = set()
        for p in products:
            item_id = str(p['Item_ID'])
            total_qty = summary.get(item_id, 0)
            moq = int(p['最低購買量'])
            if total_qty == 0: continue
            if total_qty >= moq:
                status = "✅ 達標成團"
                valid_items.add(item_id)
            else:
                status = f"❌ 淘汰 (未滿最低購買量 {moq})"
            settle_ws.append_row([item_id, p['品項名稱'], total_qty, p['單價'], total_qty * int(p['單價']), status])

        # B 區寫入
        settle_ws.append_row([])
        settle_ws.append_row(["【區塊 B：一至四組小組長分流對帳表】"])
        settle_ws.append_row(["組別", "同學姓名", "訂購明細 (成功成團品項)", "應匯款總額", "回報末五碼", "對帳狀態"])

        group_billing = {}
        for order in all_orders:
            item_id = str(order['Item_ID'])
            if item_id not in valid_items: continue
            uid = str(order['Discord_User_ID'])
            if uid not in group_billing:
                group_billing[uid] = {"姓名": order['姓名'], "組別": order['組別'], "明細": [], "總價": 0}
            p_info = prod_map[item_id]
            group_billing[uid]["明細"].append(f"{p_info['品項名稱']}x{order['購買數量']}")
            group_billing[uid]["總價"] += int(order['單項總價'])

        sorted_members = sorted(group_billing.values(), key=lambda x: str(x['組別']))
        for m in sorted_members:
            settle_ws.append_row([f"第 {m['組別']} 組", m['姓名'], ", ".join(m['明細']), m['總價'], "", "未匯款"])

        # ─── 步驟三：洗碗清空廚房 (重置 Orders_Temp) ───
        orders_sheet.clear()
        orders_sheet.append_row(["Order_ID", "Discord_User_ID", "姓名", "組別", "Item_ID", "購買數量", "單項總價", "匯款末五碼", "對帳狀態"])

        await channel.send(f"🔒 **本期牙材訂購已截止！**\n系統已產生歷史備份 `[{raw_sheet_name}]` 與結算報表 `[{settle_sheet_name}]`！\n**當期暫存工作區已全數清空重置**，下單通道關閉。")
        
        # ─── 步驟四：發送個人帳單私訊 ───
        for user_id, data in group_billing.items():
            try:
                user = await bot.fetch_user(int(user_id))
                embed = discord.Embed(title="🦷 您的當期牙材訂購個人帳單", color=0x3498db)
                embed.add_field(name="訂購明細", value="\n".join(data["明細"]), inline=False)
                embed.add_field(name="💰 應匯總金額", value=f"NT$ {data['總價']:,}", inline=False)
                embed.set_footer(text="請匯款給您所屬的小組長後，使用 /回報匯款 登記末五碼。")
                await user.send(embed=embed)
            except: pass
    except Exception as e:
        print(f"自動結算時發生崩潰: {e}")

# ================= 5. 下單 UI 與 取消訂單 UI =================

class MultiOrderModal(Modal):
    """改良版：一次輸入多個品項的數量"""
    def __init__(self, selected_products):
        super().__init__(title="填寫購買數量 (輸入正整數)")
        self.selected_products = selected_products
        self.inputs = []
        
        for p in selected_products:
            # 建立每個品項的輸入框
            inp = TextInput(
                label=f"{p['品項名稱']} (單價:${p['單價']})", 
                placeholder="請輸入數量", 
                required=True,
                max_length=4
            )
            self.add_item(inp)
            self.inputs.append((p, inp))

    async def on_submit(self, interaction: discord.Interaction):
        # 💡 新增 defer()：因為寫入多筆資料到 Google 表單也可能超過 3 秒
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

        # 驗證所有輸入
        for p, inp in self.inputs:
            try:
                qty = int(inp.value)
                if qty <= 0: raise ValueError
            except ValueError:
                await interaction.followup.send(f"❌ 數量輸入錯誤：{p['品項名稱']} 必須為正整數！", ephemeral=True)
                return
            
            subtotal = qty * int(p['單價'])
            total_cost += subtotal
            # 產生唯一訂單編號
            order_id = f"ORD-{uuid.uuid4().hex[:6].upper()}"
            rows_to_add.append([order_id, str(interaction.user.id), mem['姓名'], mem['組別'], str(p['Item_ID']), qty, subtotal, "", "未匯款"])
            reply_msg += f"• {p['品項名稱']} x {qty} (小計: ${subtotal})\n"

        # 批次寫入 Google Sheets (效能更好)
        orders_sheet.append_rows(rows_to_add)
        reply_msg += f"\n**本次新增總金額：** NT$ {total_cost:,}\n*(可使用 `/我的訂單` 檢視或修改)*"
        
        # 💡 將 response.send_message 改為 followup.send
        await interaction.followup.send(reply_msg, ephemeral=True)

class ProductSelect(Select):
    def __init__(self, products):
        summary = get_live_product_summary()
        options = []
        for p in products:
            item_id = str(p['Item_ID'])
            current_total = summary.get(item_id, 0)
            moq = int(p['最低購買量'])
            if moq <= 1:
                desc = f"單價: ${p['單價']} | 全班已訂: {current_total} 個"
            else:
                desc = f"湊單制 | 進度: {current_total}/{moq} (還差 {max(0, moq-current_total)} 支)"
            options.append(discord.SelectOption(label=p['品項名稱'], description=desc, value=item_id))
        
        # 開放多選，但受限於 Discord Modal 限制，最多只能選 5 項
        max_selectable = min(5, len(options))
        super().__init__(
            placeholder=f"請勾選欲訂購品項 (單次最多勾選 {max_selectable} 項)", 
            min_values=1, 
            max_values=max_selectable,
            options=options
        )
        self.products = products

    async def callback(self, interaction: discord.Interaction):
        # 找出使用者勾選的所有品項
        selected_products = [p for p in self.products if str(p['Item_ID']) in self.values]
        # 彈出包含多個數量輸入框的視窗
        await interaction.response.send_modal(MultiOrderModal(selected_products))


class CancelOrderSelect(Select):
    """讓同學可以自行刪除下錯的訂單"""
    def __init__(self, user_orders):
        options = []
        for o in user_orders:
            # user_orders 傳進來的有加註 '品項名稱'
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
        try:
            # 找尋該筆訂單在 Google Sheet 的哪一列，並將其刪除
            cell = orders_sheet.find(order_id_to_cancel, in_column=1)
            orders_sheet.delete_rows(cell.row)
            await interaction.followup.send("✅ 已成功取消該筆訂單！如需變更數量請重新使用 `/訂購牙材` 下單。", ephemeral=True)
        except gspread.CellNotFound:
            await interaction.followup.send("❌ 找不到該筆訂單，可能已經被取消了。", ephemeral=True)

class CancelOrderView(View):
    def __init__(self, user_orders):
        super().__init__()
        self.add_item(CancelOrderSelect(user_orders))

# ================= 6. 機器人核心指令群 =================
class DentalERPBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
    async def setup_hook(self):
        scheduler.start()
        await self.tree.sync()

bot = DentalERPBot()

@bot.tree.command(name="綁定名冊", description="【全班同學必用】首次使用時，綁定您的 Discord 帳號")
async def bind_name(interaction: discord.Interaction, 真實姓名: str):
    await interaction.response.defer(ephemeral=True)
    try:
        all_members = members_sheet.get_all_records()
        user_id_str = str(interaction.user.id).strip()
        
        for row in all_members:
            sheet_uid = str(row.get('Discord_User_ID', '')).strip().split('.')[0]
            if sheet_uid == user_id_str:
                await interaction.followup.send(f"❌ 您已綁定過姓名「{row['姓名']}」囉！若需更換請聯繫牙材長。", ephemeral=True)
                return

        updated = False
        for idx, row in enumerate(all_members, start=2):
            if str(row.get('姓名', '')).strip() == 真實姓名.strip():
                current_bound_id = str(row.get('Discord_User_ID', '')).strip()
                if current_bound_id and current_bound_id != "0" and current_bound_id != "":
                    await interaction.followup.send(f"❌ 「{真實姓名}」已經被其他帳號綁定了！", ephemeral=True)
                    return
                
                members_sheet.update_cell(idx, 1, f"'{user_id_str}")
                updated = True
                await interaction.followup.send(f"🎉 綁定成功！**【{真實姓名}】** 歡迎！您的職位為 **[{row['職位']}]**。", ephemeral=True)
                break
        
        if not updated:
            await interaction.followup.send(f"❌ 找不到名為「{真實姓名}」的同學，請確認是否有打錯字！", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 綁定失敗，系統發生錯誤: {e}", ephemeral=True)

@bot.tree.command(name="開團訂購牙材", description="【牙材長專用】設定截止時間並開啟下單通道")
async def start_group_buy(interaction: discord.Interaction, 截止時間: str):
    global IS_ORDER_OPEN, ANNOUNCEMENT_CHANNEL_ID
    mem = get_member_info(interaction.user.id)
    # 修正 1：利用 in 判斷，允許「牙材長,小組長」的雙重身分
    if not mem or "牙材長" not in mem['職位']:
        await interaction.response.send_message("❌ 您非牙材長，權限不足！", ephemeral=True)
        return
    try:
        dt = datetime.datetime.strptime(截止時間, "%Y-%m-%d %H:%M")
        if dt <= datetime.datetime.now():
            await interaction.response.send_message("❌ 截止時間必須是未來的時間！", ephemeral=True)
            return

        IS_ORDER_OPEN = True
        ANNOUNCEMENT_CHANNEL_ID = interaction.channel_id
        scheduler.remove_all_jobs()
        
        scheduler.add_job(auto_close_order, 'date', run_date=dt)
        reminder_time = dt - datetime.timedelta(days=3)
        if reminder_time > datetime.datetime.now():
            scheduler.add_job(auto_reminder, 'date', run_date=reminder_time)

        await interaction.response.send_message(f"📢 **當期牙材訂購正式開跑！**\n系統將在 `{截止時間}` 自動截單並清空重置。")
    except:
        await interaction.response.send_message(f"❌ 時間格式錯誤！請依照格式輸入：`2026-07-30 23:59`", ephemeral=True)


@bot.tree.command(name="訂購牙材", description="挑選當期牙材並進行訂購（可一次勾選多項）")
async def order_material(interaction: discord.Interaction):
    if not IS_ORDER_OPEN:
        await interaction.response.send_message("❌ 目前非訂購期間，無法進行訂購！", ephemeral=True)
        return
        
    # 💡 核心解法：先 defer，讓機器人顯示「正在思考中...」，爭取讀取 Excel 的時間
    await interaction.response.defer(ephemeral=True)
    
    try:
        products = products_sheet.get_all_records()
        view = View()
        view.add_item(ProductSelect(products))
        
        # 💡 已經 defer 過了，這裡要改成使用 followup.send 來發送真正的訊息
        await interaction.followup.send("🦷 **請勾選欲訂購的品項：**\n*(註：受限於系統，單次最多只能同時結帳 5 項。若超過 5 項請分多次下單！)*", view=view, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 讀取資料失敗，可能網路延遲過大，請稍後再試。錯誤: {e}", ephemeral=True)


@bot.tree.command(name="我的訂單", description="【個人專用】檢視自己目前的暫存訂單，可修改刪除")
async def my_orders(interaction: discord.Interaction):
    if not IS_ORDER_OPEN:
        await interaction.response.send_message("🔒 目前非訂購期間，無法查看或修改暫存區！", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    all_orders = orders_sheet.get_all_records()
    products = products_sheet.get_all_records()
    prod_map = {str(p['Item_ID']): p['品項名稱'] for p in products}

    # 過濾出自己的訂單
    user_orders = []
    total_cost = 0
    for o in all_orders:
        if str(o.get('Discord_User_ID', '')) == str(interaction.user.id):
            o['品項名稱'] = prod_map.get(str(o['Item_ID']), "未知品項")
            user_orders.append(o)
            total_cost += int(o['單項總價'])

    if not user_orders:
        await interaction.followup.send("🛒 您目前沒有任何訂購明細喔！", ephemeral=True)
        return

    embed = discord.Embed(title="🛒 您的當期購物車明細", description="以下是您目前預訂的品項（尚未截單）：", color=0x2ecc71)
    for o in user_orders:
        embed.add_field(name=o['品項名稱'], value=f"數量: {o['購買數量']} | 小計: ${o['單項總價']}", inline=False)
    
    embed.add_field(name="💰 目前累積總額", value=f"**NT$ {total_cost:,}**", inline=False)
    
    # 產生 View 以提供取消訂單功能
    view = CancelOrderView(user_orders)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="回報匯款", description="【全班同學】匯款後回報您的帳戶末五碼")
async def report_payment(interaction: discord.Interaction, 末五碼: str):
    await interaction.response.defer(ephemeral=True)
    date_str = datetime.datetime.now().strftime("%m%d")
    try:
        target_settle_sheet = doc.worksheet(f"{date_str}牙材團購結算")
    except:
        await interaction.followup.send("❌ 找不到當期的結算報表，請確認牙材長是否已經截止收單。", ephemeral=True)
        return

    records = target_settle_sheet.get_all_records()
    user_info = get_member_info(interaction.user.id)
    if not user_info:
        await interaction.followup.send("❌ 系統認不出您的 Discord 帳號，請先使用 `/綁定名冊`。", ephemeral=True)
        return

    updated = False
    for idx, row in enumerate(records, start=5):
        if row.get('同學姓名') == user_info['姓名']:
            target_settle_sheet.update_cell(idx, 5, f"'{末五碼}") 
            target_settle_sheet.update_cell(idx, 6, "已匯款待審核") 
            updated = True
            break
            
    if updated:
        await interaction.followup.send(f"✅ 匯款回報成功！已在登記末五碼 `[{末五碼}]`，請等待小組長審核。", ephemeral=True)
    else:
        await interaction.followup.send("❌ 在本期結算名單中找不到您的應繳費紀錄。", ephemeral=True)


@bot.tree.command(name="組內對帳", description="【小組長專用】查看自己組內同學的繳費進度")
async def group_check(interaction: discord.Interaction):
    leader = get_member_info(interaction.user.id)
    # 修正 1：利用 in 判斷，允許雙重身分
    if not leader or "小組長" not in leader['職位']:
        await interaction.response.send_message("❌ 您非登記之小組長，權限不足！", ephemeral=True)
        return

    date_str = datetime.datetime.now().strftime("%m%d")
    try: target_settle_sheet = doc.worksheet(f"{date_str}牙材團購結算")
    except:
        await interaction.response.send_message("❌ 找不到當期結算報表。", ephemeral=True)
        return

    records = target_settle_sheet.get_all_records()
    embed = discord.Embed(title=f"📋 第 {leader['組別']} 組繳費對帳進度報告", color=0x9b59b6)
    
    found = False
    for row in records:
        if str(row.get('組別')) == f"第 {leader['組別']} 組":
            found = True
            status_text = f"💰 應繳: ${row['應匯款總額']} | 狀態: **{row['對帳狀態']}**"
            if row.get('回報末五碼'):
                status_text += f" (末五碼: {row['回報末五碼']})"
            embed.add_field(name=f"👤 {row['同學姓名']}", value=status_text, inline=False)
            
    if not found: embed.description = "本期本組無人需繳費。"
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="確認收妥", description="【小組長專用】核對網銀入帳後，變更同學狀態為已完款")
async def confirm_payment(interaction: discord.Interaction, 同學姓名: str):
    leader = get_member_info(interaction.user.id)
    # 修正 1：利用 in 判斷，允許雙重身分
    if not leader or "小組長" not in leader['職位']:
        await interaction.response.send_message("❌ 權限不足！", ephemeral=True)
        return

    date_str = datetime.datetime.now().strftime("%m%d")
    try: target_settle_sheet = doc.worksheet(f"{date_str}牙材團購結算")
    except:
        await interaction.response.send_message("❌ 找不到當期結算報表。", ephemeral=True)
        return

    records = target_settle_sheet.get_all_records()
    updated = False
    for idx, row in enumerate(records, start=5):
        if str(row.get('組別')) == f"第 {leader['組別']} 組" and row.get('同學姓名') == 同學姓名:
            target_settle_sheet.update_cell(idx, 6, "✅ 已收妥完款")
            updated = True
            break
            
    if updated:
        await interaction.response.send_message(f"👍 已確認 **{同學姓名}** 款項入帳，狀態已更新！", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ 在您的組內找不到名為 **{同學姓名}** 的繳費紀錄。", ephemeral=True)

# ================= 最底部啟動點 =================
if __name__ == "__main__":
    DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    print("🌐 正在啟動 Flask 背景網頁服務（Render 專用防休眠）...")
    keep_alive()
    print("🤖 正在連線至 Discord 核心伺服器...")
    bot.run(DISCORD_TOKEN)