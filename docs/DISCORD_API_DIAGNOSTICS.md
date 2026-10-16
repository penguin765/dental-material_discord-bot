# Discord 互動 API 限流診斷

[返回班級使用指南](../README.md) · [管理員與部署指南](ADMIN_GUIDE.md)

Gateway 連線、資料載入、指令註冊與「能回覆互動」是不同的狀態。網站現在會分別顯示，不再把 Gateway 在線寫成所有功能就緒。

## 此次修正

- slash、選單、表單的初始回覆都會檢查剩餘的 3 秒期限。未成功確認收到互動，就停止該次業務處理，避免默默開團或新增訂單。
- 收到 429 時依 Retry-After 冷卻，沒有可靠數值時保守等待 900 秒。冷卻期間不重送失敗指令、不追加錯誤訊息、不用其他帳號或代理繞過限制。
- 後續回覆若失敗，不重跑已完成的試算表寫入。使用者仍應先核對訂單，不能把畫面失敗當成未寫入。
- 冷卻期間不發公告、私訊或過期選單更新。公告不自動補發；帳單先寫入 Notifications_Outbox，確認保存後才清空暫存訂單。pending 帳單冷卻後補送，請以結算表為準。
- Logs 記錄指令名稱、interaction ID、回覆階段與允許清單內的標頭；不記錄 token、互動 URL 或輸入欄位值。
- 冷卻結束只代表允許下一個新互動驗證，並不代表 Discord 已解除限制。初始回覆過期的舊指令不會恢復執行。

## 如何判讀

`Interaction received` 表示收到操作；`Discord HTTP succeeded operation=defer` 表示 Discord 接受初始確認；`operation=followup` 表示後續回覆成功。

`Discord HTTP failure` 會附上 status、code、cooldown，以及 Retry-After、X-RateLimit-Scope、X-RateLimit-Global、X-RateLimit-Reset-After、CF-Ray、Server 中實際存在的欄位。缺少標頭時不能斷言是 token 全域限流或 Render 共用 IP 問題。

## 健康檢查

- `/`：程序存活、供 UptimeRobot 喚醒。200 不保證 Discord 可用。
- `/readyz`：在已知互動 HTTP 限流或尚未驗證恢復時回傳 503。
- interaction_api.state：not_tested（尚未做互動驗證）、verified（最近一次回覆成功）、cooldown（冷卻）、awaiting_verification（失敗後等待新互動驗證）。

not_tested 不代表 API 已成功測試。verified 也不能保證下一個請求成功。不要把 `/readyz` 設為反覆自動重啟的條件；外部限流無法靠重啟修復。保持 UptimeRobot 的根網址喚醒監控，並分別判讀功能狀態。

## 驗收與限制

冷卻結束後先用 `/我的訂單` 測試一次。確認 defer、followup 都成功，再使用開團指令。如果仍收到 429，停止連續嘗試，依新 Retry-After 等待，並用經過遮蔽的診斷資訊向 Discord／Render 支援確認限制範圍。程式不能代替 Discord 解除伺服器端限制，也不能保證免費服務永不中斷。

若網站還是正常、但 Discord 顯示 did not respond，先看當次 interaction ID 的 defer 是否成功。若 defer 成功而寫入後 followup 失敗，先核對試算表，再決定是否重新操作。

參考：[Discord rate limits](https://docs.discord.com/developers/topics/rate-limits)、[Interaction response deadline](https://docs.discord.com/developers/interactions/receiving-and-responding)。


## 背景流量控制與帳單補送（2026-10-16）

背景 HTTP 操作共用單一發送槽，每次完成後至少相隔 0.5 秒；帳單每 30 秒最多處理 5 人，避免 Discord 與 Sheets 同時爆量。slash 初始回覆不等待這個背景槽，仍受互動期限限制。每個 fetch user、create DM、send message 步驟前都重新檢查冷卻，並先使用 Discord 使用者快取。底層 aiohttp trace 也會攔住冷卻期間的內部重試。

Logs 每 60 秒輸出 `Discord HTTP metrics`：`attempts` 是 aiohttp 實際開始的 HTTP 嘗試（可能尚未送到伺服器）；`status:429` 等為實際收到的回應；`logical:*` 是程式呼叫次數；`suppressed` 是冷卻攔截。這些數字只涵蓋此程序，不涵蓋其他主機或同 IP 的其他服務。操作名稱為固定標籤，不含 token、完整 URL、姓名或帳單內容。

Google Sheets 會自動建立 `Notifications_Outbox`，存放帳單及以下狀態；沿用原試算表的存取權，不需增加 Discord intents：

- `pending`：待送／被限流拒絕，冷卻後由背景排程繼續。
- `sent`：Discord 已接受訊息；同一期同一人不再次入列。
- `failed`：明確 4xx 拒絕，例如關閉私訊；不自動重試。
- `sending`：已開始本次處理。重啟或處理中斷後會轉成 `uncertain`。
- `uncertain`：逾時、斷線或中斷，無法確認 Discord 是否已收到；不自動重送。管理員先核對對方是否收到帳單，再決定是否把該列 status 改為 pending。不可整張清空或把全部列重設 pending，否則可能重複寄送。

此清單含個人帳單，僅供既有試算表管理者查看；不要公開分享。只允許一個 bot 程序操作清單，此設計不提供多主機分散式鎖。大量歷史列可在備份後由管理員整理；若 Sheets 寫入失敗，結算保留恢復資訊，不假裝通知已送出。

429 的冷卻狀態目前保存在程序記憶體，重啟不等於解除 Discord 的限制。不要為了測試反覆部署／重啟。若非常少量的實際請求仍被拒絕，保留時間、狀態與 CF-Ray 供 Discord/Render 支援診斷；不能只憑 Cloudflare 標頭判定是共享 IP。
