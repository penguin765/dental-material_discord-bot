import os
import json
import discord
from discord import app_commands
from discord.ui import View, Select, Modal, TextInput, Button
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= 0. 全域變數與資料庫連線 =================
# ================= 1. Google Sheets 連線設定（修改為雲端安全版） =================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

# 💡 雲端安全版：改由環境變數讀取 JSON 字串
google_creds_env = os.getenv("GOOGLE_CREDS_JSON")
if google_creds_env:
    creds_dict = json.loads(google_creds_env)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
else:
    # 如果本地測試找不到環境變數，就用原本的檔案
    creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)

gc = gspread.authorize(creds)
# ... 中間的程式碼完全不需要變動 ...

SPREADSHEET_NAME = "牙材_discord_bot" # ⚠️ 請修改為你實際的 Google Sheet 名稱
doc = gc.open(SPREADSHEET_NAME)
inventory_sheet = doc.worksheet("Inventory")
orders_sheet = doc.worksheet("Orders")

# 系統收單狀態控制
IS_ORDER_OPEN = True
ANNOUNCEMENT_CHANNEL_ID = None # 用來記錄發送結算報表的頻道 ID
scheduler = AsyncIOScheduler(timezone="Asia/Taipei")

# ================= 2. 自動收單結算邏輯 =================
async def auto_close_order():
    global IS_ORDER_OPEN
    IS_ORDER_OPEN = False
    print("⏰ 截止時間已到，系統已停止收單！")
    
    if not ANNOUNCEMENT_CHANNEL_ID:
        return
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not channel:
        return

    try:
        orders = orders_sheet.get_all_records()
        if not orders:
            await channel.send("🔔 **本期訂購已截止！** 本期無任何人下單。")
            return

        # 彙整統計資料
        item_summary = {}
        user_summary = {}
        
        for order in orders:
            item = str(order['Item_ID'])
            qty = int(order['購買數量'])
            price = int(order['單項總價'])
            user = str(order['使用者名稱'])
            
            # 品項總計
            item_summary[item] = item_summary.get(item, 0) + qty
            # 使用者金額總計
            user_summary[user] = user_summary.get(user, 0) + price

        embed = discord.Embed(title="🔒 本期牙材團購已自動截止！結算報表如下", color=0xe74c3c)
        
        # 品項統計表
        item_text = ""
        for item_id, total_qty in item_summary.items():
            item_text += f"`[{item_id}]` 總計訂購：**{total_qty}** 個\n"
        embed.add_field(name="📦 品項向廠商叫貨總量", value=item_text, inline=False)
        
        # 使用者繳費表
        user_text = ""
        for user_name, total_price in user_summary.items():
            user_text += f"👤 **{user_name}**：應繳 NT$ {total_price:,}\n"
        embed.add_field(name="💰 個人帳單總計", value=user_text, inline=False)
        embed.set_footer(text="系統已自動鎖定訂購，欲修改請聯繫管理員人工處理。")

        await channel.send(content="角色標記提醒 @everyone", embed=embed)
    except Exception as e:
        print(f"產生報表錯誤: {e}")

# ================= 3. 改單與下單 UI 元件 =================
class ModifyModal(Modal):
    def __init__(self, order_record, row_num):
        super().__init__(title="修改訂單數量")
        self.order = order_record
        self.row_num = row_num
        
        self.new_qty_input = TextInput(
            label=f"修改：{order_record['Item_ID']} 的購買數量",
            default=str(order_record['購買數量']),
            placeholder="請輸入修改後的正整數總量",
            required=True,
            max_length=4
        )
        self.add_item(self.new_qty_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not IS_ORDER_OPEN:
            await interaction.response.send_message("❌ 本期訂購已截止，無法再修改訂單！", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        try:
            new_qty = int(self.new_qty_input.value)
            if new_qty <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send("❌ 數量必須為正整數！", ephemeral=True)
            return

        old_qty = int(self.order['購買數量'])
        diff = new_qty - old_qty # 差異量 (正數代表多買，負數代表退回)
        
        # 讀取當前庫存
        inv_cell = inventory_sheet.find(str(self.order['Item_ID']))
        inv_row_data = inventory_sheet.row_values(inv_cell.row)
        current_stock = int(inv_row_data[3]) # 第4欄為當前庫存
        unit_price = int(inv_row_data[2])    # 第3欄為單價
        
        if diff > 0 and current_stock < diff:
            await interaction.followup.send(f"❌ 庫存不足以追加！目前庫存僅剩 **{current_stock}** 個。", ephemeral=True)
            return

        # 1. 更新庫存
        inventory_sheet.update_cell(inv_cell.row, 4, current_stock - diff)
        # 2. 更新訂單表 (第5欄數量, 第6欄總價)
        orders_sheet.update_cell(self.row_num, 5, new_qty)
        orders_sheet.update_cell(self.row_num, 6, new_qty * unit_price)

        await interaction.followup.send(f"✅ 修改成功！訂單數量已更新為 **{new_qty}** 個，新總價：NT$ {new_qty * unit_price:,}", ephemeral=True)

class ManageOrderView(View):
    def __init__(self, order_record, row_num):
        super().__init__(timeout=120)
        self.order = order_record
        self.row_num = row_num

    @discord.ui.button(label="✏️ 修改數量", style=discord.ButtonStyle.primary)
    async def modify_btn(self, interaction: discord.Interaction, button: Button):
        if not IS_ORDER_OPEN:
            await interaction.response.send_message("❌ 目前已結單，無法修改！", ephemeral=True)
            return
        await interaction.response.send_modal(ModifyModal(self.order, self.row_num))

    @discord.ui.button(label="🗑️ 取消此訂單", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: Button):
        if not IS_ORDER_OPEN:
            await interaction.response.send_message("❌ 目前已結單，無法取消！", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        
        # 退回庫存
        inv_cell = inventory_sheet.find(str(self.order['Item_ID']))
        current_stock = int(inventory_sheet.cell(inv_cell.row, 4).value)
        inventory_sheet.update_cell(inv_cell.row, 4, current_stock + int(self.order['購買數量']))
        
        # 刪除訂單
        orders_sheet.delete_rows(self.row_num)
        await interaction.followup.send(f"✅ 訂單 `{self.order['Order_ID']}` 已成功取消，數量已全額退回庫存！", ephemeral=True)

class MyOrdersSelect(Select):
    def __init__(self, user_orders):
        options = []
        for order in user_orders:
            options.append(discord.SelectOption(
                label=f"品項: {order['data']['Item_ID']} | 數量: {order['data']['購買數量']}",
                description=f"單號: {order['data']['Order_ID']} | 總價: ${order['data']['單項總價']}",
                value=str(order['row'])
            ))
        super().__init__(placeholder="請選擇一筆您要修改或取消的訂單...", options=options)
        self.user_orders = user_orders

    async def callback(self, interaction: discord.Interaction):
        selected_row = int(self.values[0])
        selected_order = next(x['data'] for x in self.user_orders if x['row'] == selected_row)
        
        embed = discord.Embed(title="⚙️ 訂單管理與變更", color=0xf39c12)
        embed.add_field(name="訂單編號", value=selected_order['Order_ID'])
        embed.add_field(name="品項 ID", value=selected_order['Item_ID'])
        embed.add_field(name="目前數量", value=selected_order['購買數量'])
        
        await interaction.response.send_message(
            embed=embed, 
            view=ManageOrderView(selected_order, selected_row), 
            ephemeral=True
        )

# ================= 4. 新增的下單 Modal 邏輯 (保留上一步驟功能) =================
class OrderModal(Modal):
    def __init__(self, item):
        super().__init__(title=f"訂購：{item['品項名稱']}")
        self.item = item
        self.quantity_input = TextInput(
            label="請輸入購買數量",
            placeholder=f"目前庫存: {item['當前庫存']} | 單價: NT$ {item['單價']}",
            required=True, max_length=4
        )
        self.add_item(self.quantity_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not IS_ORDER_OPEN:
            await interaction.response.send_message("❌ 本期訂購已自動截止！", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            qty = int(self.quantity_input.value)
            if qty <= 0: raise ValueError
        except ValueError:
            await interaction.followup.send("❌ 格式錯誤！請輸入正整數。", ephemeral=True)
            return

        all_items = inventory_sheet.get_all_records()
        current_item = next((x for x in all_items if str(x["Item_ID"]) == str(self.item["Item_ID"])), None)
        if not current_item or current_item["當前庫存"] < qty:
            await interaction.followup.send("❌ 庫存不足！", ephemeral=True)
            return

        total_price = qty * int(current_item["單價"])
        cell = inventory_sheet.find(str(self.item["Item_ID"]))
        inventory_sheet.update_cell(cell.row, 4, current_item["當前庫存"] - qty)
        
        order_id = f"ORD-{int(datetime.datetime.now().timestamp())}"
        orders_sheet.append_row([order_id, str(interaction.user.id), interaction.user.display_name, str(self.item["Item_ID"]), qty, total_price])

        embed = discord.Embed(title="✅ 訂購成功！", color=0x2ecc71)
        embed.add_field(name="單號", value=order_id); embed.add_field(name="數量", value=str(qty)); embed.add_field(name="總價", value=f"NT$ {total_price:,}")
        await interaction.followup.send(embed=embed, ephemeral=True)

class ItemSelect(Select):
    def __init__(self, items):
        options = [discord.SelectOption(label=f"{i['品項名稱']} (${i['單價']})", description=f"剩餘: {i['當前庫存']}", value=str(i["Item_ID"])) for i in items if int(i["當前庫存"]) > 0]
        if not options: options = [discord.SelectOption(label="已售完", value="none")]
        super().__init__(placeholder="請選擇品項...", options=options)
        self.items = items
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none": return
        selected = next((i for i in self.items if str(i["Item_ID"]) == self.values[0]), None)
        if selected: await interaction.response.send_modal(OrderModal(selected))

# ================= 5. 機器人主體與指令 =================
class DentalBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
    async def setup_hook(self):
        scheduler.start()
        await self.tree.sync()
        print("🤖 斜線指令與排程器已啟動！")

bot = DentalBot()

@bot.tree.command(name="訂購牙材", description="開啟牙材訂購介面")
async def order_command(interaction: discord.Interaction):
    if not IS_ORDER_OPEN:
        await interaction.response.send_message("🔒 本期訂購已截止，目前不開放下單！", ephemeral=True)
        return
    items = inventory_sheet.get_all_records()
    view = View(); view.add_item(ItemSelect(items))
    await interaction.response.send_message("🦷 **請從下方選單選擇欲訂購的牙材：**", view=view)

@bot.tree.command(name="我的訂單", description="查詢自己目前的訂單，可修改數量或取消")
async def my_orders_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    all_orders = orders_sheet.get_all_records()
    
    # 找出自己的訂單與其在表格中的行號 (Header 是第1行，資料從第2行開始)
    user_orders = []
    for idx, order in enumerate(all_orders, start=2):
        if str(order['Discord_User_ID']) == str(interaction.user.id):
            user_orders.append({'row': idx, 'data': order})
            
    if not user_orders:
        await interaction.followup.send("❌ 您目前沒有任何暫存訂單紀錄！", ephemeral=True)
        return

    view = View()
    view.add_item(MyOrdersSelect(user_orders))
    await interaction.followup.send("📋 **以下是您目前的訂購明細，請點選選單進行修改或取消：**", view=view, ephemeral=True)

@bot.tree.command(name="設定截止時間", description="【管理員專用】設定自動收單時間 (格式: YYYY-MM-DD HH:MM)")
async def set_deadline_command(interaction: discord.Interaction, time_str: str):
    global IS_ORDER_OPEN, ANNOUNCEMENT_CHANNEL_ID
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 只有管理員可以使用此指令！", ephemeral=True)
        return

    try:
        dt = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")
        now = datetime.datetime.now()
        if dt <= now:
            await interaction.response.send_message("❌ 截止時間必須設定在「未來的時間」！", ephemeral=True)
            return

        IS_ORDER_OPEN = True
        ANNOUNCEMENT_CHANNEL_ID = interaction.channel_id
        
        # 移除舊任務並新增自動結算任務
        scheduler.remove_all_jobs()
        scheduler.add_job(auto_close_order, 'date', run_date=dt)
        
        await interaction.response.send_message(f"⏰ **已成功設定收單倒數！**\n系統將於 `{time_str}` 自動關閉下單，並在此頻道發布結算報表。")
    except ValueError:
        await interaction.response.send_message("❌ 格式錯誤！請依照格式輸入，例如：`2026-07-10 18:00`", ephemeral=True)

@bot.tree.command(name="立即收單", description="【管理員專用】強制提早收單並印出報表")
async def force_close_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 權限不足！", ephemeral=True)
        return
    global ANNOUNCEMENT_CHANNEL_ID
    ANNOUNCEMENT_CHANNEL_ID = interaction.channel_id
    await interaction.response.send_message("🚨 **管理員已觸發強制收單！** 正在運算報表...")
    await auto_close_order()

# ================= 最底部啟動機器人（修改為雲端安全版） =================
# 💡 改由環境變數讀取 Token
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
bot.run(DISCORD_TOKEN)