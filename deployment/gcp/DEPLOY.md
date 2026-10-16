# Google Cloud 部署準備

目前階段：部署檔已準備；尚未建立 VM、尚未遷移憑證、尚未在 Google Cloud 啟動 Bot。
Google Cloud 帳戶需先完成兩步驟驗證，並確認計費帳戶抵免額與期限。

## 1. 建立主機前

- Render 保持 Suspend，只執行一份 Bot。
- 使用你選定的 Google Cloud 專案與計費帳戶；先檢視建立頁的估價，再決定是否建立。預算通知不是硬性消費上限。
- 建議先評估 e2-micro、Ubuntu 24.04 LTS、10 GB **standard persistent disk**、一般非 Spot VM。
- 若要使用 Compute Engine 免費使用額度，限符合條件的 us-west1、us-central1、us-east1；台灣區域不在這項免費額度內。外部 IPv4、超額流量及其他資源需另查費用，不能保證零帳單。
- 此階段不需要對公網開放 HTTP、HTTPS 或 10000 埠。Bot 主動連 Discord；狀態頁先只透過 SSH 在 VM 內檢查。
- 如採用既有服務帳戶 JSON，VM 本身不需被授予專案 Editor 權限。沿用原 Sheets service account 及原試算表編輯權，不需要更換 Discord Token。

## 2. 安裝程式

將本資料夾五個檔案放進 VM 的一個資料夾（例如透過 SSH 檔案上傳），在 Ubuntu 24.04 的 SSH 終端執行：

```bash
sudo bash install.sh
```

安裝器固定部署已核對的 commit `355a813943699e6838595c752c17a20eed0beb68`，執行相依檢查及回歸測試。使用 Ubuntu 24.04 的 Python 3.12（專案 CI 已涵蓋）；不使用 Render 專用的 Python 下載流程。安裝器不會啟動 Bot。

## 3. 私下設定憑證

不要把 Token 或 Google JSON 貼到聊天、公開 GitHub、啟動腳本或 shell 命令參數。

```bash
sudoedit /etc/dental-bot/bot.env
```

填入原本的 DISCORD_BOT_TOKEN 和 SPREADSHEET_ID。systemd 的 EnvironmentFile 不會自動讀取專案 .env；不需要寫 export。

由你本人將原本服務帳戶 JSON 安全上傳至 VM 暫存位置，再安裝為：

```bash
sudo install -o root -g dentalbot -m 0640 ~/creds.json /etc/dental-bot/creds.json
```

憑證有備份後，移除上傳時留下的暫存副本。不要將憑證放入 Git checkout 或改成公眾可讀。

## 4. 啟動一次並驗收

確認舊 Render 已暫停、沒有本機 Bot，且先前 Discord Retry-After 已結束後：

```bash
sudo systemctl start dental-bot
sudo systemctl status dental-bot --no-pager
sudo journalctl -u dental-bot -n 100 --no-pager
curl -s http://127.0.0.1:10000/
```

應看到 Discord REST login succeeded、Discord READY、DATA READY、COMMANDS READY。若收到 429，讓程式依 Retry-After 等待，不重啟、不再次送指令。新的網路環境不保證解除限制。

只執行一次 `/我的訂單`，同時查看：

```bash
sudo journalctl -u dental-bot -f
```

必須確認 defer 與 followup 成功，才算互動回覆驗收完成。網頁 200、systemd active 或 Gateway 在線都不足以證明指令正常。

驗收後才設定開機自動啟動：

```bash
sudo systemctl enable dental-bot
```

停止服務：`sudo systemctl stop dental-bot`。確認停止後才可恢復 Render，不能兩邊同時啟動。

## 維運與既有設定

- 啟動模組為 `python -u -m src.bot`；systemd 用 PYTHONPATH 指向固定版本，工作目錄設在受保護的 /etc/dental-bot，以讀取 creds.json。
- 不設定 RENDER_EXTERNAL_URL；VM 不需 Render keep-alive。舊 UptimeRobot 的 Render 監控應暫停，以免持續發出告警。
- 服務使用非 root 帳戶執行，憑證不公開，未配置公開網站。
- Notifications_Outbox、System_Config 與帳務資料仍使用原 Google Sheets。服務啟動時可能恢復既有排程，請在正式下單前核對原開團與截止時間。
- 私訊 uncertain 狀態仍需人工核對，不要全部改回 pending。
- 日後更新需要停止服務、準備另一個固定版本並跑測試，再切換 current；本檔不提供無審查自動拉取 main 的排程。

官方參考：

- [建立 Compute Engine VM](https://docs.cloud.google.com/compute/docs/instances/create-start-instance)
- [Google Cloud 免費額度](https://docs.cloud.google.com/free/docs/free-cloud-features#compute)
- [VPC 網路計費](https://cloud.google.com/vpc/network-pricing)
