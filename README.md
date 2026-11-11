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
| `req=` | 貼文代碼（Ctrl+點擊開啟原貼文） |
| `usr=` | 發文者 |
| `ack=` | 讚數（破千的貼文會標成 `WARN`） |
| `resp=` | 回覆數 |
| `fwd=` | 轉發數＋引用數 |
| `via=` | 誰轉發的 |
| `blob[n] s3://cache/….jpg` | 圖片 |
| `stream[n] s3://cache/….mp4` | 影片 |
| `caused by:` | 被引用的貼文 |
| `upstream idle`、`session expired` 等訊息 | 沒有更多貼文、登入過期等狀態 |

以下的說明沿用偽裝後的用語：「上游」就是 Threads，「記錄」就是貼文，「驗證」就是登入。

## 需求

三種執行方式擇一：

| 方式 | 需求 |
|---|---|
| Linux | Google Chrome、Python 3.12 以上 |
| Windows | Windows 10 以上、Google Chrome、Python 3.12 以上，建議使用 Windows Terminal |
| Docker | Docker（含 Compose），不需要另外安裝 Chrome 或 Python |

終端機都需要支援 24-bit 色彩。

## 安裝

### Linux

```bash
python3 -m venv .venv
.venv/bin/pip install playwright pillow
```

### Windows

在 PowerShell 或命令提示字元中：

```powershell
py -m venv .venv
.venv\Scripts\pip install playwright pillow
```

不需要執行 `playwright install`，程式直接使用系統的 Chrome。

### Docker

```bash
docker compose build
```

## 快速開始

Linux：

```bash
./logtail login     # 第一次使用：向上游驗證（只需一次）
./logtail           # 開始檢視
```

Windows：

```powershell
.\logtail.cmd login
.\logtail.cmd
```

`login` 會開啟一個瀏覽器視窗，完成驗證並看到首頁後把視窗關掉即可。之後都在背景執行，不會有視窗。

進入後按 `h` 可以看指令表。

本文件其餘的範例都以 `./logtail` 表示；Windows 請換成 `.\logtail.cmd`，Docker 請換成 `docker compose run --rm --service-ports logtail`。

### 使用 Docker

```bash
docker compose run --rm --service-ports logtail login   # 第一次使用
docker compose run --rm --service-ports logtail         # 開始檢視
```

容器裡沒有螢幕，所以 `login` 會把瀏覽器視窗開在虛擬螢幕上，並印出一個網址：

```
http://localhost:6080/vnc.html?autoconnect=1&resize=scale
```

用主機的瀏覽器開啟這個網址，就能操作容器裡的登入視窗。偵測到驗證成功後視窗會自動關閉，指令也隨之結束。

- 這個遠端畫面沒有密碼，連接埠只綁定在本機（`127.0.0.1:6080`），而且只在 `login` 期間存在。
- 驗證資料存放在名為 `profile` 的 Docker volume 中，不在專案資料夾裡。
- 選項與參數照常接在後面，例如 `docker compose run --rm --service-ports logtail -f -a`。

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

## 附件預覽

按 `i` 會在終端機內以色塊字元畫出預覽（串流附件只畫封面）。解析度有限，要看原始檔請 Ctrl+點擊對應的 `s3://cache/…`。

## 清除驗證資料

三種方式效果相同：

- 執行中按 `L`，再按 `y` 確認
- 執行 `./logtail logout`（要先結束執行中的 logtail）
- 直接刪除 `.profile/` 資料夾（Docker 則是執行 `docker compose down -v` 刪除 volume）

這只會清除本機資料，上游那邊的登入階段仍然有效。要徹底作廢，請到上游服務的帳號安全設定中，把這台裝置的登入階段移除。

## 注意事項

- **服務條款**：自動化存取不符合上游的使用條款。本工具只讀取、不寫入，請求也有隨機間隔，但不保證帳號不受限制。
- **憑證保管**：`.profile/` 等同登入狀態，不要分享、上傳或提交到版本控制。
- **單一執行個體**：同一時間只能執行一個 logtail。
- **超連結**：Ctrl+點擊會用一般瀏覽器分頁開啟。
- **捲動紀錄**：從 build 輸出檢視返回後，先前的輸出仍留在終端機的捲動紀錄中。

## 疑難排解

| 狀況 | 處理方式 |
|---|---|
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
