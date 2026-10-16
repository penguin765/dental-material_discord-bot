# 管理員與部署參考

[返回班級使用指南](../README.md) · [Discord API 限流診斷](DISCORD_API_DIAGNOSTICS.md)

以下維運說明供接手部署、試算表設定及故障恢復時使用。所有命令皆從專案根目錄執行。

> 從舊版升級：Render 的 Root Directory 保持專案根目錄，Build Command 沿用 `pip install -r requirements.txt`，Start Command 改為 `python -u -m src.bot`。舊的 `python -u bot.py` 已不適用；請在部署新版前更新服務設定。本機 `creds.json` 仍放專案根目錄，或沿用 `GOOGLE_CREDS_JSON` 環境變數。

使用 Discord 斜線指令及 Google Sheets 管理團購。請只執行一個 bot 程序；目前以單一程序的鎖保護訂單與結算，不支援多個 Render instance 共用同一份試算表。

## 2026-10 修正與原因

| 問題 | 修正 |
| --- | --- |
| Render 顯示 live，但不知道 Discord 登入卡在哪 | 啟用 discord.py INFO logging、每 60 秒記錄狀態、提供 `/readyz` |
| 初始化失敗後不再重試 | 成功載入並恢復狀態後才標記完成，失敗每 30 秒重試 |
| 換 bot 或同步暫時失敗後缺少指令 | 同步標記依 application ID 分開保存，失敗重試 |
| 網路回覆遺失時重複下單 | 不盲目重播寫入；同一 interaction 使用穩定 Order_ID；寫入前核對 ID |
| 同步執行緒逾時後仍在寫入 | HTTP 層設定 10 秒連線／30 秒讀取逾時；取消協程時等待 worker 結束後才放鎖 |
| 結算重試刪除歷史報表 | 唯一報表名稱、覆寫同一期固定範圍並讀回驗證，不刪除歷史工作表 |
| 清空訂單後結算中斷無法恢復 | 保存 PENDING_SETTLEMENT；已驗證階段從歷史明細恢復；保留 Orders_Temp 表頭 |
| 開團與關團狀態互相矛盾 | 下單只用統一檢查；未完成結算禁止開新團；開團與結算使用同一把鎖 |
| 舊選單失效無提示 | 5 分鐘逾時後停用選單並提示重新執行指令 |
| 可冒領管理職位 | 牙材長／小組長必須由管理員預先填入 Discord_User_ID |

## Render 設定

1. 合併修正 PR 後，確認服務部署的是該 commit。
2. Build Command：`pip install -r requirements.txt`
3. Start Command：`python -u -m src.bot`。不要用 `gunicorn src.bot:app`，那只會載入 Flask，不會執行 Discord 啟動點。
4. Python 固定為 `.python-version` 的 `3.14.3`，與先前提供的部署環境一致；若 Render 已設定 `PYTHON_VERSION`，請確認一致。
5. 設定下列環境變數：
   - `DISCORD_BOT_TOKEN`：Discord Developer Portal 的 Bot token。
   - `GOOGLE_CREDS_JSON`：完整 service account JSON，不要額外包外層引號。
   - `SPREADSHEET_ID`：建議填入 Google Sheet 網址 `/d/` 與 `/edit` 之間的 ID。
   - `SPREADSHEET_NAME`：未提供 ID 時使用，預設 `牙材_discord_bot`。
6. 將試算表分享給 service account 的 `client_email`，權限為編輯者，並啟用 Google Sheets API；以名稱查找時也需要 Drive API。
7. 保持單一 instance，勿同時在本機和 Render 啟動同一套訂單程式。Render 部署切換時先避免下單／結算。

`/` 是程序存活頁面，永遠可正常回應不代表 bot 已可用。`/readyz` 只有 Discord、資料及指令都就緒才回傳 200，否則 503。可用它檢查業務就緒；不要把外部 API 短暫失效直接變成不斷重部署的觸發器。

免費 Render Web Service 可能休眠或重啟；自我 ping 不能保證持續在線。正式收單應使用能持續運行的服務。程式保留 Flask 以符合 Web Service 的 PORT 要求。

## Google Sheets 表格

必須有 `Members`、`Products`、`Orders_Temp` 三個工作表。`System_Config` 可自動建立。

- Members：`Discord_User_ID`、`姓名`、`組別`、`職位`。綁定程式目前要求 ID 在 A 欄、姓名在 B 欄。
- Products：`Item_ID`、`品項名稱`、`單價`、`最低購買量`。單價請使用非負整數，不要填貨幣符號或千分位文字。
- Orders_Temp：`Order_ID`、`Discord_User_ID`、`姓名`、`組別`、`Item_ID`、`購買數量`、`單項總價`，依此順序放在第一列。

Discord_User_ID 欄設定為「純文字」後再貼入 ID，以免長整數精度遺失。已經被試算表四捨五入的 ID 必須重新填入；程式無法還原。

牙材長／小組長由管理員在 Members 預先填好 ID。Discord 開啟開發者模式後，可複製使用者 ID。一般學生仍可依姓名自行綁定，因此姓名必須唯一；這是信任班級成員的設計，不適合開放陌生人加入的公開伺服器。

## Discord 設定與驗收

1. 以 `bot` 和 `applications.commands` scopes 邀請至伺服器。
2. 授予公告頻道的 View Channel、Send Messages、Embed Links；不要為排錯盲目開啟所有管理員權限。
3. 目前使用預設 intents；斜線指令不要求 Message Content intent。
4. 部署 Logs 應依序出現：
   - `Starting Discord client`
   - `Discord REST login succeeded`
   - `Discord READY`
   - `DATA READY`
   - `COMMANDS READY`
5. 先使用測試試算表及測試頻道：刷新名冊 → 開團 → 訂購 1 項 → 我的訂單 → 取消 → 再下單 → 強制關團 → 核對歷史與結算表 → 回報匯款 → 對帳。
6. 關團後 Orders_Temp 應保留第一列表頭；歷史明細與結算報表應存在。測試同一天第二次開團，確認第一期報表仍保留。

## 排錯與恢復

- 只看到 Flask 200：查看 Discord logging；HTTP 網站正常不足以確認 Gateway 連線。
- Token rejected：檢查 DISCORD_BOT_TOKEN，使用 Bot token，不是 client secret。
- 429：依實際 HTTP 狀態與 Retry-After 等待，不因猜測共用 IP 而頻繁重啟。
- Google Sheets cache load failed：查看 traceback 的權限、JSON 或表頭錯誤；程式每 30 秒重試初始化。
- 訂單結果尚未確認：不要立刻重複送單。牙材長先刷新快取，再核對 Orders_Temp 的 Order_ID。不同次送出是不同 interaction，不能自動判定是否為重複購買。
- 結算失敗：保留 PENDING_SETTLEMENT 和所有歷史表，修復連線／資料問題後執行 `/強制關團` 接續。不要手動清空暫存或刪除備份。
- 若驗證失敗且曾手動編輯半成品報表，先備份、核對原始訂單與報表。程式會停止清理，避免資料遺失。
- 私訊可能因使用者隱私設定失敗；以結算工作表為帳務依據。

尚存限制：試算表不是交易資料庫；跨程序並行不受本機鎖保護；報表與通知不是原子交易；互動訊息在服務重啟後需重新執行指令。商品選單最多顯示 125 項、個人取消選單最多 25 筆；更多資料需後續加入分頁。一般姓名綁定仍需班級管理員監督。

## 測試

`python -m unittest discover -s tests -v`

測試以記憶體中的 Sheets 替身模擬網路失敗，不使用真實憑證、不發送 Discord 訊息。GitHub Actions 在 Python 3.12 與 3.14 執行相同測試。部署後仍需做上述真實環境驗收。

參考：[discord.py logging](https://discordpy.readthedocs.io/en/latest/logging.html)、[gspread timeout](https://docs.gspread.org/en/latest/api/http_client.html#gspread.HTTPClient.set_timeout)、[Render free limitations](https://render.com/docs/free)。
