# Discord 互動 API 限流診斷

Gateway 連線、資料載入、指令註冊與「能回覆互動」是不同的狀態。網站現在會分別顯示，不再把 Gateway 在線寫成所有功能就緒。

## 此次修正

- slash、選單、表單的初始回覆都會檢查剩餘的 3 秒期限。未成功確認收到互動，就停止該次業務處理，避免默默開團或新增訂單。
- 收到 429 時依 Retry-After 冷卻，沒有可靠數值時保守等待 900 秒。冷卻期間不重送失敗指令、不追加錯誤訊息、不用其他帳號或代理繞過限制。
- 後續回覆若失敗，不重跑已完成的試算表寫入。使用者仍應先核對訂單，不能把畫面失敗當成未寫入。
- 冷卻期間跳過公告及私訊；結算資料仍可保留。被跳過的通知不會自動補發，請以結算表為準。
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
