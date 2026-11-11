# logtail

**上班摸魚滑 Threads 用的終端機工具。**

把 Threads 的貼文偽裝成 `tail -f` 的 log 輸出，旁人瞄到你的螢幕，只會覺得你在盯 log。

```
15:55:01.594 WARN  [feed-fy] req=DfaNgswk usr=someone age=3d ack=2.3k resp=514 fwd=132
  │ 貼文內容 ...
  │ blob[0] s3://cache/aabb6eafd6.jpg
  │ stream[0] s3://cache/53e3940c69.mp4
```

## 🚨 老闆鍵：按 `b`

有人走過來的時候，按 **`b`**，畫面立刻整個換成假的編譯輸出：

```
$ make -j16 all test
[ 231/1284] Compiling core/scheduler.o (412 ms)
[ 232/1284] PASS  tests/db/migrations_test 17 passed in 1.42s
[ 233/1284] Linking libgateway.so
[ 234/1284] warning: cache/lru.c:88: unused variable 'tmp'
```

- **一鍵切換**：Threads 的內容瞬間從畫面上消失。
- **會自己動**：假的 build 輸出以不規則的速度持續滾動，看起來就像真的在編譯、跑測試。
- **按任意鍵回來**：回到原本的畫面，剛剛滑到哪裡都原封不動。

## ⚠️ 使用風險（請先讀完）

**這個工具違反 Threads 的使用條款，用了帳號就有被限制或停用的風險，請自行評估、自行負責。**

Threads 由 Instagram 提供，所以《Threads 使用條款》和《Instagram 使用條款》同時適用。相關條文如下：

| 條款 | 原文 | 白話 |
|---|---|---|
| [Threads 使用條款](https://help.instagram.com/769983657850450)<br>3.1 (iv) | "use any robot, spider, crawlers, scraper or other automatic device, process, software or queries that intercepts, 'mines', scrapes, extracts or otherwise accesses the Threads Service to monitor, extract, copy or collect information or data from the Threads Service, or engage in any manual process to do the same" | 禁止用任何自動化程式存取 Threads 來擷取、複製或蒐集資料。本工具正是這麼做的。 |
| Threads 使用條款<br>3.1 (iii) | "circumvent, remove, alter, deactivate, degrade or thwart any technological measure or content protections of the Threads Service" | 禁止規避技術保護措施。本工具會隱藏瀏覽器的自動化特徵，可能被認定屬於這一項。 |
| [Instagram 使用條款](https://help.instagram.com/581066165581870)<br>「你的承諾」 | "You can't attempt to create accounts or access or collect information in unauthorized ways. This includes creating accounts or accessing or collecting information in an automated way without our express permission, regardless of whether such automated access or collection is undertaken while logged-in to an Instagram account." | 未經明確許可，不得以自動化方式存取或蒐集資訊，**登入自己的帳號也一樣**。 |
| Threads 使用條款<br>第 5 節 | "If you violate these Terms or our policies ... we can refuse to provide or stop providing all or part of the Threads Service to you (including terminating or disabling your access to the Threads Service) without notice" | 違反條款時，Meta 可以**不經通知**終止或停用你的 Threads 存取權。 |

實際上要注意的事：

- **帳號可能被限制或停用。** Threads 帳號與 Instagram 帳號綁在一起，Instagram 條款的處置範圍包含其他 Meta 產品，所以受影響的可能不只 Threads。
- **「只讀不寫」和「隨機間隔」不構成豁免。** 條款禁止的是自動化存取本身，不論讀寫、頻率。這些設計只是降低被偵測的機率，不保證安全。
- **訊息功能的風險比瀏覽更高。** 瀏覽貼文只讀取資料，但訊息功能（`m`）會用自動化程式以你的名義送出訊息、送出已讀狀態，屬於寫入行為，更容易被判定為自動化操作。不想承擔這個風險就不要按 `m`。
- **Meta 設有專門偵測並封鎖自動化存取的機制**，也曾對大規模擷取資料的業者提起訴訟。個人瀏覽自己的動態牆規模差很多，但條款並沒有區分。
- **建議不要用你的主要帳號。**
- **上班摸魚本身的風險**（公司的電腦與網路使用規範、被主管發現）不在本文件的保障範圍內，老闆鍵也不保證有效。

以上條文擷取自 [Open Terms Archive](https://github.com/OpenTermsArchive/pga-versions) 於 2026 年 6 月（Threads）與 8 月（Instagram）存檔的歐洲地區版本；各地區的版本用字可能略有不同，條款也會更新，請以官方頁面的現行內容為準。本說明不是法律意見。

## 偽裝對照表

畫面上看到的每個欄位其實都是 Threads 的東西：

| 畫面上顯示 | 其實是 |
|---|---|
| 一筆 log 記錄 | 一則貼文 |
| `[feed-fy]` | 「為你推薦」動態牆 |
| `[feed-fw]` | 「追蹤中」動態牆 |
| `[grep]` | 搜尋結果 |
| `[usr-name]` | 某個人的個人頁 |
| `[trace]` | 貼文底下的回覆 |
| `[mq]`、`[mq-name]` | 訊息（私訊）：`rx` 是對方傳來的，`tx` 是你傳出去的 |
| `req=` | 貼文代碼（Ctrl+點擊開啟原貼文） |
| `usr=` | 發文者 |
| `ack=` | 讚數（破千的貼文會標成 `WARN`） |
| `resp=` | 回覆數 |
| `fwd=` | 轉發數＋引用數 |
| `via=` | 誰轉發的 |
| `blob[n] s3://cache/….jpg` | 圖片 |
| `stream[n] s3://cache/….mp4` | 影片 |
| `caused by:` | 被引用的貼文 |
| `pending` / `acked` | 未讀／已讀的對話 |
| `upstream idle`、`session expired` 等訊息 | 沒有更多貼文、登入過期等狀態 |

以下的說明沿用偽裝後的用語：「上游」就是 Threads，「記錄」就是貼文，「驗證」就是登入。

## 安裝與啟動

```bash
git clone https://github.com/royhong0501/threads.git
cd threads
```

**以下所有指令都要在 `threads/` 資料夾裡執行。** 三種方式擇一：

**Linux**（需要 Google Chrome、Python 3.12+）

```bash
python3 -m venv .venv
.venv/bin/pip install playwright pillow
./logtail login     # 登入，只需一次
./logtail           # 開始滑
```

**Windows**（需要 Google Chrome、Python 3.12+，建議用 Windows Terminal）

```powershell
py -m venv .venv
.venv\Scripts\pip install playwright pillow
.\logtail.cmd login
.\logtail.cmd
```

**Docker**（不需要另外裝 Chrome 或 Python）

```bash
docker compose build
docker compose run --rm --service-ports logtail login
docker compose run --rm --service-ports logtail
```

- `login` 會開啟瀏覽器視窗，登入並看到首頁後把視窗關掉即可；之後都在背景執行。
- Docker 的 `login` 會印出 `http://localhost:6080/vnc.html?...`，用主機的瀏覽器打開它來操作登入視窗，登入成功後會自動關閉。這個畫面沒有密碼，只綁定本機、只在 `login` 期間存在。
- Docker 的登入資料存在名為 `profile` 的 volume 裡，不在專案資料夾。
- 進入後按 `h` 看指令表。終端機需支援 24-bit 色彩。

下文的範例都寫 `./logtail`；Windows 換成 `.\logtail.cmd`，Docker 換成 `docker compose run --rm --service-ports logtail`。

## 啟動方式

| 指令 | 說明 |
|---|---|
| `./logtail` | 預設串流（`feed-fy`） |
| `./logtail -f` | 訂閱串流（`feed-fw`），只含已訂閱的來源 |
| `./logtail @name` | 只看單一來源的記錄 |
| `./logtail -s pattern` | 依關鍵字過濾（有空白要加引號，例如 `-s "foo bar"`） |
| `./logtail login` | 驗證 |
| `./logtail logout` | 清除驗證資料 |

### 選項

| 選項 | 說明 |
|---|---|
| `-a`, `--auto` | follow 模式，自動往下輸出 |
| `--cps N` | follow 模式的輸出速度，字／秒（預設 12，越大越快） |
| `-i`, `--images` | 每筆記錄都自動展開附件預覽（預設按 `i` 才展開） |
| `--img-width N` | 附件預覽寬度，字元數（預設 56） |
| `--recent` | 過濾結果依時間排序（預設依熱度） |
| `--raw-urls` | 終端機不支援超連結時，直接印出完整網址 |

選項可以組合，例如 `./logtail -f -a --cps 20`。

## 按鍵

| 按鍵 | 功能 |
|---|---|
| `space` / `j` / `Enter` | 下一筆記錄 |
| `a` | follow 模式 開／關 |
| `r` | 展開上一筆記錄的子記錄（trace） |
| `i` | 展開上一筆記錄的附件預覽 |
| `/` | 輸入過濾關鍵字（Enter 送出；空白直接 Enter 則取消） |
| `m` | 訊息（私訊）：列出對話，輸入編號進入聊天 |
| `f` | 切換 `feed-fy` ↔ `feed-fw`；在過濾結果或單一來源檢視時則回到串流 |
| `R` | 重新連線 |
| **`b`** | **老闆鍵**：切換到假的 build 輸出畫面，按任意鍵返回 |
| `h` / `?` | 顯示指令表 |
| `L` | 清除驗證資料並離開（大寫 L，需按 `y` 確認） |
| `q` | 離開 |

`r` 和 `i` 都作用在**最後輸出的那一筆**記錄上，要在輸出下一筆之前按。

## 欄位說明

```
15:55:01.594 WARN  [feed-fy] req=DfaNgswk usr=someone age=3d ack=2.3k resp=514 fwd=132
```

| 欄位 | 意思 |
|---|---|
| `INFO` / `WARN` | 一般記錄為 `INFO`；`ack` 超過 1000 的為 `WARN` |
| `[...]` | 元件：`feed-fy` 預設串流、`feed-fw` 訂閱串流、`grep` 過濾結果、`usr-name` 單一來源、`trace` 子記錄 |
| `req=` | 記錄代碼，Ctrl+點擊可開啟原始記錄 |
| `usr=` | 記錄的來源 |
| `age=` | 記錄產生至今的時間（`s`、`m`、`h`、`d`） |
| `ack=` | 確認數（讚數） |
| `resp=` | 子記錄數（回覆數） |
| `fwd=` | 轉送數（轉發數＋引用數） |
| `via=` | 經由誰轉送（只有轉送的記錄才有） |

記錄內容下方可能出現：

| 行 | 意思 |
|---|---|
| `blob[n] s3://cache/….jpg` | 靜態附件，Ctrl+點擊開啟 |
| `stream[n] s3://cache/….mp4` | 串流附件，Ctrl+點擊開啟 |
| `upstream https://…` | 記錄附帶的外部連結 |
| `caused by: req=… usr=…` | 這筆記錄所參照的另一筆記錄，內容以 `>` 開頭列在下方 |

`s3://cache/…` 只是顯示用的短名稱，點擊後開啟的是實際的附件網址。

## 子記錄（trace）

按 `r` 展開，以縮排的 `DEBUG [trace]` 行顯示。

- 每 8 筆暫停一次：`space` 繼續、`i` 展開這一批的附件預覽、其他鍵結束。
- 一次最多 40 筆。
- 只展開第一層。

## 訊息（message queue）

按 `m` 列出最近的對話，輸入編號後按 Enter 進入聊天（直接按 Enter 取消）：

```
12:30:57.962 INFO  [mq] listing channels ...
  │  1 pending usr=friend_a age=5分鐘 最後一則訊息的預覽
  │  2 acked   usr=friend_b age=7週 ...
channel #> 1
12:31:02.197 INFO  [mq-friend_a] rx usr=friend_a
  │ 對方傳來的訊息
12:31:02.197 DEBUG [mq-friend_a] tx usr=me
  │ 你傳出去的訊息
```

- **未讀標示**：列表中 `pending` 是有未讀訊息的對話，`acked` 是已讀。
- **未讀提醒**：滑動態牆時如果出現新的未讀對話，會印出一行 `WARN [mq] pending=1 unread channel(s) — press m`。未讀狀態在每次載入頁面時更新（啟動、按 `R`、切換動態牆、搜尋、按 `m`），不是即時推播。

進入後會先顯示最近 15 則，之後有新訊息會自動印出來。對話中的按鍵：

| 按鍵 | 功能 |
|---|---|
| `t` / `Enter` | 輸入訊息，按 Enter 送出（空白直接 Enter 則取消） |
| `b` | 老闆鍵，聊天中一樣有效 |
| `←` / `q` | 離開對話，回到動態牆（`Esc` 也可以；其他方向鍵沒有作用） |

注意：

- **送出就收不回來**。在 `publish>` 提示後按下 Enter，訊息就會以你的名義傳給對方，本工具沒有收回功能。
- **進入對話會讓對方看到「已看過」**，和在網頁上打開對話一樣。
- 只支援單行文字訊息；圖片、分享的貼文可以看（以連結顯示），但不能傳送。
- 只列出收件匣的前 20 個對話，不含「陌生訊息」；一次只載入最近的訊息，不會往上捲更舊的。

## 附件預覽

按 `i` 會在終端機內以色塊字元畫出預覽（串流附件只畫封面）。解析度有限，要看原始檔請 Ctrl+點擊對應的 `s3://cache/…`。

## 清除驗證資料

三種方式效果相同：

- 執行中按 `L`，再按 `y` 確認
- 執行 `./logtail logout`（要先結束執行中的 logtail）
- 直接刪除 `.profile/` 資料夾（Docker 則是執行 `docker compose down -v` 刪除 volume）

這只會清除本機資料，上游那邊的登入階段仍然有效。要徹底作廢，請到上游服務的帳號安全設定中，把這台裝置的登入階段移除。

## 注意事項

- **服務條款**：自動化存取違反 Threads 與 Instagram 的使用條款，帳號可能被限制或停用，詳見上方「使用風險」。
- **憑證保管**：`.profile/` 等同登入狀態，不要分享、上傳或提交到版本控制。
- **單一執行個體**：同一時間只能執行一個 logtail。
- **超連結**：Ctrl+點擊會用一般瀏覽器分頁開啟。
- **捲動紀錄**：從 build 輸出檢視返回後，先前的輸出仍留在終端機的捲動紀錄中。

## 疑難排解

| 狀況 | 處理方式 |
|---|---|
| 找不到 `./logtail`、`.\logtail.cmd`，或 Docker 顯示找不到設定檔 | 不在專案資料夾裡，先 `cd threads` |
| 顯示「尚未登入」 | 執行 `./logtail login` |
| 顯示 `session expired` | 驗證過期，重新執行 `./logtail login` |
| 啟動時出現 `ProcessSingleton` 或 profile 已被使用 | 已有另一個 logtail 在執行，先結束它 |
| 連結不能點、出現奇怪字元 | 終端機不支援超連結，加上 `--raw-urls` |
| 附件預覽顏色錯誤或亂碼 | 終端機不支援 24-bit 色彩 |
| Windows 上連結不能點 | 舊版主控台不支援超連結，改用 Windows Terminal 或加上 `--raw-urls` |
| Docker 的 `login` 網址打不開 | 指令要帶 `--service-ports`；並確認本機的 6080 連接埠沒有被占用 |
| 持續顯示 `upstream idle` | 按 `R` 重新連線；仍無資料則可能是上游格式變動 |
| 抓不到記錄或子記錄 | 上游格式變動，需調整 `logtail.py` 的 `find_threads` 與 `parse_post` |

## 檔案

| 檔案 | 說明 |
|---|---|
| `logtail` | 啟動腳本（Linux） |
| `logtail.cmd` | 啟動腳本（Windows） |
| `logtail.py` | 主程式 |
| `Dockerfile`、`compose.yaml`、`docker-entrypoint.sh` | Docker 映像檔與執行設定 |
| `.venv/` | Python 虛擬環境（安裝後才會出現） |
| `.profile/` | 驗證資料（驗證後才會出現） |
