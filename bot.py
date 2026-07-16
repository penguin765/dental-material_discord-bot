import os
import json
import datetime
from threading import Thread
from flask import Flask
import discord
from discord import app_commands
from discord.ui import View, Select, Modal, TextInput, Button
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= 1. Flask 防休眠網頁設定 =================
app = Flask('')
@app.route('/')
def home():
    return "🤖 牙材團購 ERP 機器人在線運作中！"

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
SPREADSHEET_NAME = "牙材_discord_bot" # ⚠️ 請記得修改為你的 Google Sheet 名稱
doc = gc.open(SPREADSHEET_NAME)

members_sheet = doc.worksheet("Members")
products_sheet = doc.worksheet("Products")
orders_sheet = doc.worksheet("Orders_Temp")

IS_ORDER_OPEN = True
ANNOUNCEMENT_CHANNEL_ID = None
scheduler = AsyncIOScheduler(timezone="Asia/Taipei")

# ================= 3. 輔助函式 (權限與即時計算) =================
def get_member_info(user_id):
    """從 Members 分頁取得使用者資料"""
    try:
        cell = members_sheet.find(str(user_id))
        row = members_sheet.row_values(cell.row)
        return {"姓名": row[1], "組別": row[2], "職位": row[3]}
    except:
        return None

def get_live_product_summary():
    """動態計算全班當前每項牙材累計訂購總量"""
    all_orders = orders_sheet.get_all_records()
    summary = {}
    for o in all_orders:
        item_id = str(o['Item_ID'])
        qty = int(o['購買數量'])
        summary[item_id] = summary.get(item_id, 0) + qty
    return summary

# ================= 4. 自動催單與最終結算自動分流 =================
async def auto_reminder():
    """截止前 3 天自動觸發的催單廣播"""
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
        embed = discord.Embed(title="🚨 牙材團購截止倒數 3 天：湊單危急品項公告！", description=warning_text, color=0xe67e22)
        await channel.send(content="@everyone 倒數三天！未達出貨門檻之牙材在截止時將「無法下單」，請大家幫忙補刀湊單！", embed=embed)

async def auto_close_order():
    """自動截單並生成「牙材長叫貨表」與「小組長對帳表」"""
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

        # 1. 建立當期全新 Excel 分頁
        date_str = datetime.datetime.now().strftime("%m%d")
        new_sheet_name = f"{date_str}牙材團購結算"
        
        try:
            new_ws = doc.worksheet(new_sheet_name)
            doc.del_worksheet(new_ws)
        except: pass
        new_ws = doc.add_worksheet(title=new_sheet_name, rows="100", cols="20")

        # 2. 寫入 A 區：給牙材長的叫貨總表
        new_ws.append_row(["【區塊 A：牙材長向廠商叫貨總表】"])
        new_ws.append_row(["品項 ID", "品項名稱", "全班叫貨總量", "單價", "總金額", "出貨狀態"])
        
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
                status = f"❌ ❌ 慘遭淘汰 (未滿最低購買量 {moq})"
            
            new_ws.append_row([item_id, p['品項名稱'], total_qty, p['單價'], total_qty * int(p['單價']), status])

        # 3. 寫入 B 區：給小組長的組內對帳明細
        new_ws.append_row([])
        new_ws.append_row(["【區塊 B：一至四組小組長分流對帳表】"])
        new_ws.append_row(["組別", "同學姓名", "訂購明細 (成功成團品項)", "應匯款總額", "回報末五碼", "對帳狀態"])

        # 以組別 (1~4) 進行分流彙整
        group_billing = {}
        for order in all_orders:
            item_id = str(order['Item_ID'])
            if item_id not in valid_items: continue # 淘汰的就不計費
            
            uid = str(order['Discord_User_ID'])
            if uid not in group_billing:
                group_billing[uid] = {
                    "姓名": order['姓名'], "組別": order['組別'],
                    "明細": [], "總價": 0, "末五碼": order.get('匯款末五碼', ''), "狀態": order.get('對帳狀態', '未匯款')
                }
            p_info = prod_map[item_id]
            group_billing[uid]["明細"].append(f"{p_info['品項名稱']}x{order['購買數量']}")
            group_billing[uid]["總價"] += int(order['單項總價'])

        # 排序並寫入 Excel
        sorted_members = sorted(group_billing.values(), key=lambda x: str(x['組別']))
        for m in sorted_members:
            new_ws.append_row([f"第 {m['組別']} 組", m['姓名'], ", ".join(m['明細']), m['總價'], m['末五碼'], m['狀態']])

        await channel.send(f"🔒 **本期牙材團購已截止！**\n系統已自動在 Google Sheets 產生分頁 `[{new_sheet_name}]`！\nA區與B區報表皆已自動分流完畢，下單通道全面鎖定。")
        
        # 4. 私訊全班個人帳單
        for user_id, data in group_billing.items():
            try:
                user = await bot.fetch_user(int(user_id))
                embed = discord.Embed(title="🦷 您的當期牙材訂購個人帳單", color=0x3498db)
                embed.add_field(name="訂購明細", value="\n".join(data["明細"]), inline=False)
                embed.add_field(name="💰 應匯總金額", value=f"NT$ {data['總價']:,}", inline=False)
                embed.set_footer(text="請匯款給您所屬的小組長後，回 Discord 頻道輸入 /回報匯款 登記對帳。")
                await user.send(embed=embed)
            except: pass
    except Exception as e:
        print(f"結算崩潰: {e}")

# ================= 5. 下單與對帳 UI 元件 =================
class OrderModal(Modal):
    def __init__(self, product):
        super().__init__(title=f"訂購：{product['品項名稱']}")
        self.product = product
        self.qty_input = TextInput(label="請輸入欲購買數量", placeholder="請輸入正整數", required=True)
        self.add_item(self.qty_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not IS_ORDER_OPEN:
            await interaction.response.send_message("❌ 本期訂購已截止！", ephemeral=True)
            return
        mem = get_member_info(interaction.user.id)
        if not mem:
            await interaction.response.send_message("❌ 找不到您的班級名冊紀錄，請先聯繫牙材長在 Members 表格中登記您的 Discord ID！", ephemeral=True)
            return

        try:
            qty = int(self.qty_input.value)
            if qty <= 0: raise ValueError
        except:
            await interaction.response.send_message("❌ 數量輸入錯誤，請輸入正整數！", ephemeral=True)
            return

        total_price = qty * int(self.product['單價'])
        order_id = f"ORD-{int(datetime.datetime.now().timestamp())}"
        
        # 直接追加到暫存區
        orders_sheet.append_row([
            order_id, str(interaction.user.id), mem['姓名'], mem['組別'], 
            str(self.product['Item_ID']), qty, total_price, "", "未匯款"
        ])
        await interaction.response.send_message(f"✅ 成功加入暫存訂單！\n**品項：** {self.product['品項名稱']} x {qty}\n**當前小計：** NT$ {total_price:,}", ephemeral=True)

class ProductSelect(Select):
    def __init__(self, products):
        summary = get_live_product_summary()
        options = []
        for p in products:
            item_id = str(p['Item_ID'])
            current_total = summary.get(item_id, 0)
            moq = int(p['最低購買量'])
            
            # 動態組裝即時預訂量說明
            if moq <= 1:
                desc = f"單價: ${p['單價']} | 全班已訂: {current_total} 個"
            else:
                desc = f"湊單制 | 進度: {current_total}/{moq} (還差 {max(0, moq-current_total)} 支)"
                
            options.append(discord.SelectOption(label=p['品項名稱'], description=desc, value=item_id))
        super().__init__(placeholder="請選擇你要訂購的牙材品項...", options=options)
        self.products = products

    async def callback(self, interaction: discord.Interaction):
        selected = next(p for p in self.products if str(p['Item_ID']) == self.values[0])
        await interaction.response.send_modal(OrderModal(selected))

# ================= 6. 機器人主體指令群 =================
class DentalERPBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
    async def setup_hook(self):
        scheduler.start()
        await self.tree.sync()

bot = DentalERPBot()

@bot.tree.command(name="開團訂購牙材", description="【牙材長專用】設定開團與截止時間並啟動監控")
@app_commands.describe(time_str="格式: YYYY-MM-DD HH:MM")
async def start_group_buy(interaction: discord.Interaction, time_str: str):
    global IS_ORDER_OPEN, ANNOUNCEMENT_CHANNEL_ID
    mem = get_member_info(interaction.user.id)
    if not mem or mem['職位'] != "牙材長":
        await interaction.response.send_message("❌ 您非牙材長，權限不足！", ephemeral=True)
        return

    try:
        dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        if dt <= datetime.datetime.now():
            await interaction.response.send_message("❌ 截止時間必須是未來的時間！", ephemeral=True)
            return

        IS_ORDER_OPEN = True
        ANNOUNCEMENT_CHANNEL_ID = interaction.channel_id
        scheduler.remove_all_jobs()
        
        # 排程：截止當下自動收單
        scheduler.add_job(auto_close_order, 'date', run_date=dt)
        # 排程：截止前 3 天發布自動催單 (若開團時間短於三天，則會自動忽略或可手動觸發)
        reminder_time = dt - datetime.timedelta(days=3)
        if reminder_time > datetime.datetime.now():
            scheduler.add_job(auto_reminder, 'date', run_date=reminder_time)

        await interaction.response.send_message(f"📢 **牙材團購正式開跑！**\n系統將在 `{time_str}` 自動截止、鎖定並結算。前 3 天會自動發布 MOQ 湊單催促公告。")
    except Exception as e:
        await interaction.response.send_message(f"❌ 時間格式錯誤！請依照範例輸入：`2026-07-30 23:59`", ephemeral=True)

@bot.tree.command(name="訂購牙材", description="挑選當期牙材並進行訂購（選單即時顯示全班當前湊單總量）")
async def order_material(interaction: discord.Interaction):
    if not IS_ORDER_OPEN:
        await interaction.response.send_message("❌ 目前非開團期間，無法進行訂購！", ephemeral=True)
        return
    products = products_sheet.get_all_records()
    view = View()
    view.add_item(ProductSelect(products))
    await interaction.response.send_message("🦷 **請由下方選單挑選品項，後方皆會同步顯示班級當前即時累計湊單進度：**", view=view, ephemeral=True)

@bot.tree.command(name="回報匯款", description="【全班同學】匯款給小組長後，回報您的帳戶末五碼登記對帳")
async def report_payment(interaction: discord.Interaction,末五碼: str):
    await interaction.response.defer(ephemeral=True)
    all_orders = orders_sheet.get_all_records()
    
    updated = False
    for idx, order in enumerate(all_orders, start=2):
        if str(order['Discord_User_ID']) == str(interaction.user.id):
            orders_sheet.update_cell(idx, 8, str(末五碼)) # 第8欄是末五碼
            orders_sheet.update_cell(idx, 9, "已匯款待審核") # 第9欄是狀態
            updated = True
            
    if updated:
        await interaction.followup.send(f"✅ 匯款回報成功！已登記末五碼 `[{末五碼}]`，已通知您所屬的小組長進行審核對帳。", ephemeral=True)
    else:
        await interaction.followup.send("❌ 找不到您在本期的訂單明細，請確認您是否有下單成功。", ephemeral=True)

@bot.tree.command(name="組內對帳", description="【小組長專用】查看自己組內 9 位同學的末五碼回報與繳費進度")
async def group_check(interaction: discord.Interaction):
    leader = get_member_info(interaction.user.id)
    if not leader or leader['職位'] != "小組長":
        await interaction.response.send_message("❌ 您非登記之小組長，無法使用此指令！", ephemeral=True)
        return

    all_orders = orders_sheet.get_all_records()
    embed = discord.Embed(title=f"📋 第 {leader['組別']} 組組內繳費對帳進度報告", color=0x9b59b6)
    
    found = False
    for order in all_orders:
        if str(order['組別']) == str(leader['組別']):
            found = True
            status_text = f"💰 金額: ${order['單項總價']} | 狀態: **{order['對帳狀態']}**"
            if order['匯款末五碼']:
                status_text += f" (末五碼: {order['匯款末五碼']})"
            embed.add_field(name=f"👤 {order['姓名']}", value=status_text, inline=False)
            
    if not found:
        embed.description = "目前本組內尚無任何人下單。"
        
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="確認收妥", description="【小組長專用】確認網銀入帳後，將該同學狀態改為已確認完款")
async def confirm_payment(interaction: discord.Interaction, 同學姓名: str):
    leader = get_member_info(interaction.user.id)
    if not leader or leader['職位'] != "小組長":
        await interaction.response.send_message("❌ 權限不足！", ephemeral=True)
        return

    all_orders = orders_sheet.get_all_records()
    updated = False
    for idx, order in enumerate(all_orders, start=2):
        if str(order['組別']) == str(leader['組別']) and str(order['姓名']) == str(同學姓名):
            orders_sheet.update_cell(idx, 9, "✅ 已收妥完款")
            updated = True
            
    if updated:
        await interaction.response.send_message(f"👍 已成功將同學 **{同學姓名}** 的對帳狀態變更為「✅ 已收妥完款」！", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ 在您的組內找不到名為 **{同學姓名}** 的當期訂單，請檢查字是否有打錯。", ephemeral=True)

# ================= 最底部啟動點 =================
if __name__ == "__main__":
    DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    print("🌐 正在啟動 Flask 背景網頁服務（Render 專用防休眠）...")
    keep_alive()
    print("🤖 正在連線至 Discord 核心伺服器...")
    bot.run(DISCORD_TOKEN)
