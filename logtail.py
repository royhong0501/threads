#!/usr/bin/env python3
"""logtail — 以 log 輸出樣式在 terminal 瀏覽 Threads。

用法：
    ./logtail login      # 第一次使用：開啟視窗登入（只需一次）
    ./logtail            # 開始瀏覽「為你推薦」
    ./logtail -f         # 從「追蹤中」開始
    ./logtail @username  # 看某人的貼文
    ./logtail -s 關鍵字  # 搜尋
    ./logtail logout     # 登出（刪除本機 session）
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from playwright.async_api import async_playwright

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import select
    import termios
    import tty

# 提示訊息中顯示的啟動指令（容器內由 LOGTAIL_CMD 指定）
CMD = os.environ.get("LOGTAIL_CMD") or (".\\logtail.cmd" if WINDOWS else "./logtail")
# 額外的 Chrome 啟動參數（容器內需要 --no-sandbox 等）
EXTRA_CHROME_ARGS = os.environ.get("LOGTAIL_CHROME_ARGS", "").split()
# 登入視窗沒有關閉按鈕的環境（容器內的虛擬螢幕）：偵測到登入後自動關閉
LOGIN_AUTOCLOSE = os.environ.get("LOGTAIL_LOGIN_AUTOCLOSE") == "1"

BASE = "https://www.threads.com"
HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / ".profile"
FEEDS = {"foryou": f"{BASE}/", "following": f"{BASE}/following"}

# ───────────────────────── ANSI ─────────────────────────
RST = "\x1b[0m"
DIM = "\x1b[2m"
C_TS = "\x1b[38;5;242m"
C_INFO = "\x1b[38;5;71m"
C_WARN = "\x1b[38;5;178m"
C_DBG = "\x1b[38;5;67m"
C_ERR = "\x1b[38;5;167m"
C_KEY = "\x1b[38;5;245m"
C_TXT = "\x1b[38;5;250m"
C_LINK = "\x1b[38;5;67m"


def setup_console() -> None:
    """Windows：輸出改用 UTF-8，並開啟主控台的 ANSI（VT）跳脫序列處理。"""
    if not WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def link(url: str, label: str) -> str:
    """OSC 8 超連結：顯示短標籤，Ctrl+點擊開啟。"""
    return f"\x1b]8;;{url}\x1b\\{C_LINK}{label}{RST}\x1b]8;;\x1b\\"


def cwidth(ch: str) -> int:
    if unicodedata.combining(ch) or ch in "‍️︎":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def wrap(text: str, width: int) -> list[str]:
    """依顯示寬度折行：中日韓字元可任意斷行，英文單字盡量不拆開。"""
    out: list[str] = []
    for para in text.split("\n"):
        line, w = "", 0
        # 切成 token：連續的非空白 ASCII 視為一個單字，其餘逐字元
        for tok in re.findall(r"[!-~]+|.", para):
            tw = sum(cwidth(c) for c in tok)
            if w + tw > width and line:
                out.append(line.rstrip())
                line, w = "", 0
                if tok == " ":
                    continue
            while tw > width:  # 超長 token（例如網址）硬切
                out.append(tok[:width])
                tok, tw = tok[width:], tw - width
            line += tok
            w += tw
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


def now_ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def emit(s: str = "") -> None:
    # raw 模式下 \n 不會自動回到行首，所以明確輸出 \r\n
    sys.stdout.write(s.replace("\n", "\r\n") + "\r\n")
    sys.stdout.flush()


def log(level: str, comp: str, msg: str) -> None:
    color = {"INFO": C_INFO, "WARN": C_WARN, "DEBUG": C_DBG, "ERROR": C_ERR}[level]
    emit(f"{C_TS}{now_ts()}{RST} {color}{level:<5}{RST} {C_KEY}[{comp}]{RST} {C_TXT}{msg}{RST}")


# ───────────────────────── 資料模型 ─────────────────────────
@dataclass
class Post:
    pk: str
    code: str
    user: str
    text: str
    taken_at: int
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    quotes: int = 0
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    thumbs: list[str] = field(default_factory=list)  # 影片封面
    ext_link: str | None = None
    quoted: "Post | None" = None
    reposted_by: str | None = None

    @property
    def url(self) -> str:
        return f"{BASE}/@{self.user}/post/{self.code}"


def _best_image(media: dict) -> str | None:
    cands = (media.get("image_versions2") or {}).get("candidates") or []
    return cands[0].get("url") if cands else None


def unwrap_link(url: str) -> str:
    """l.threads.com 轉址連結還原成原始網址。"""
    u = urlparse(url)
    if u.netloc.startswith("l.threads."):
        target = parse_qs(u.query).get("u")
        if target:
            return target[0]
    return url


def parse_post(p: dict) -> Post | None:
    try:
        user = (p.get("user") or {}).get("username")
        pk = str(p.get("pk") or p.get("id") or "")
        if not user or not pk:
            return None
        tpai = p.get("text_post_app_info") or {}
        post = Post(
            pk=pk,
            code=p.get("code") or "",
            user=user,
            text=((p.get("caption") or {}).get("text") or "").strip(),
            taken_at=int(p.get("taken_at") or 0),
            likes=int(p.get("like_count") or 0),
            replies=int(tpai.get("direct_reply_count") or 0),
            reposts=int(tpai.get("repost_count") or 0),
            quotes=int(tpai.get("quote_count") or 0),
        )
        carousel = p.get("carousel_media") or []
        for m in carousel or [p]:
            vids = m.get("video_versions") or []
            if vids:
                post.videos.append(vids[0].get("url"))
                thumb = _best_image(m)
                if thumb:
                    post.thumbs.append(thumb)
            else:
                img = _best_image(m)
                if img:
                    post.images.append(img)
        lp = tpai.get("link_preview_attachment") or {}
        if lp.get("url"):
            post.ext_link = unwrap_link(lp["url"])
        share = tpai.get("share_info") or {}
        if share.get("quoted_post"):
            post.quoted = parse_post(share["quoted_post"])
        if share.get("reposted_post"):
            inner = parse_post(share["reposted_post"])
            if inner:
                inner.reposted_by = user
                return inner
        return post
    except Exception:
        return None


def find_threads(obj, out: list[list[Post]]) -> None:
    """遞迴找出所有貼文串，不依賴外層包裝結構。

    Threads 有兩種格式：
      - 動態牆／搜尋／個人頁：  {"thread_items": [{"post": {...}}, ...]}
      - 登入後貼文頁的回覆：    {"posts": {"edges": [{"node": {...}}, ...]}}
    """
    if isinstance(obj, dict):
        raw: list = []
        items = obj.get("thread_items")
        if isinstance(items, list):
            raw = [it.get("post") for it in items if isinstance(it, dict)]
        else:
            edges = obj.get("posts")
            edges = edges.get("edges") if isinstance(edges, dict) else None
            if isinstance(edges, list):
                raw = [e.get("node") for e in edges if isinstance(e, dict)]
        posts = [pp for r in raw if isinstance(r, dict) and (pp := parse_post(r))]
        if posts:
            out.append(posts)
        for v in obj.values():
            find_threads(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_threads(v, out)


def threads_from_text(text: str) -> list[list[Post]]:
    out: list[list[Post]] = []
    try:
        find_threads(json.loads(text), out)
        return out
    except Exception:
        pass
    for line in text.splitlines():  # 串流式回應：每行一個 JSON
        try:
            find_threads(json.loads(line), out)
        except Exception:
            continue
    return out


# ───────────────────────── 瀏覽器 ─────────────────────────
def chrome_version() -> str:
    """已安裝的 Chrome 版本字串；找不到時回傳空字串。"""
    if WINDOWS:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r"Software\Google\Chrome\BLBeacon") as key:
                    return str(winreg.QueryValueEx(key, "version")[0])
            except OSError:
                continue
        return ""
    return subprocess.run(["google-chrome", "--version"], capture_output=True, text=True).stdout


def chrome_ua() -> str:
    try:
        major = re.search(r"(\d+)\.", chrome_version()).group(1)
    except Exception:
        major = "140"
    platform = "Windows NT 10.0; Win64; x64" if WINDOWS else "X11; Linux x86_64"
    return f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


async def open_context(pw, headless: bool):
    PROFILE_DIR.mkdir(exist_ok=True)
    if not WINDOWS:
        os.chmod(PROFILE_DIR, 0o700)
    return await pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        channel="chrome",
        headless=headless,
        user_agent=chrome_ua(),
        locale="zh-TW",
        viewport={"width": 1280, "height": 900},
        # 登入(有視窗)與瀏覽(無頭)用同一種 cookie 加密方式，session 才能共用
        args=["--password-store=basic", "--disable-blink-features=AutomationControlled", *EXTRA_CHROME_ARGS],
        ignore_default_args=["--enable-automation"],
    )


async def is_logged_in(ctx) -> bool:
    return any(c["name"] == "sessionid" and c["value"] for c in await ctx.cookies(BASE))


class Source:
    """一個 Threads 頁面：收集初始 HTML 與後續 GraphQL 回應中的貼文。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.page = None
        self.queue: list[list[Post]] = []
        self.seen: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    def _add(self, threads: list[list[Post]]) -> int:
        n = 0
        for t in threads:
            if t[0].pk in self.seen:
                continue
            self.seen.update(p.pk for p in t)
            self.queue.append(t)
            n += 1
        return n

    def _on_response(self, resp) -> None:
        if "graphql" not in resp.url:
            return

        async def handle():
            try:
                self._add(threads_from_text(await resp.text()))
            except Exception:
                pass

        task = asyncio.ensure_future(handle())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def open(self, url: str) -> None:
        if self.page:
            await self.page.close()
        self.queue.clear()
        self.seen.clear()
        self.page = await self.ctx.new_page()
        self.page.on("response", self._on_response)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await self.page.wait_for_timeout(2500)
        scripts = await self.page.evaluate(
            """() => [...document.querySelectorAll('script[type="application/json"]')].map(s => s.textContent)"""
        )
        html_threads: list[list[Post]] = []
        for s in scripts:
            html_threads += threads_from_text(s)
        # 初始 HTML 的貼文排在最前面
        pending, self.queue = self.queue, []
        self.seen.clear()
        self._add(html_threads)
        self._add(pending)

    async def more(self) -> bool:
        """往下捲動觸發載入更多。"""
        before = len(self.seen)
        for _ in range(4):
            await self.page.mouse.wheel(0, random.randint(5000, 9000))
            await self.page.wait_for_timeout(random.randint(1200, 2200))
            if len(self.seen) > before:
                return True
        return False

    async def next(self) -> list[Post] | None:
        if not self.queue:
            await self.more()
        return self.queue.pop(0) if self.queue else None

    async def close(self) -> None:
        if self.page:
            await self.page.close()
            self.page = None


# ───────────────────────── 輸出樣式 ─────────────────────────
def age(ts: int) -> str:
    d = max(0, int(time.time()) - ts)
    for unit, sec in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= sec:
            return f"{d // sec}{unit}"
    return f"{d}s"


def short(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def fake_name(url: str, ext: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:10] + ext


def render_post(p: Post, comp: str, level: str | None = None, indent: int = 0, raw_urls: bool = False) -> None:
    cols = shutil.get_terminal_size((100, 30)).columns
    pad = " " * indent
    if level is None:
        level = "WARN" if p.likes >= 1000 else "INFO"
    color = {"INFO": C_INFO, "WARN": C_WARN, "DEBUG": C_DBG}[level]
    kv = f"usr={p.user} age={age(p.taken_at)} ack={short(p.likes)} resp={short(p.replies)} fwd={short(p.reposts + p.quotes)}"
    if p.reposted_by:
        kv += f" via={p.reposted_by}"
    head = f"{pad}{C_TS}{now_ts()}{RST} {color}{level:<5}{RST} {C_KEY}[{comp}]{RST} "
    emit(head + link(p.url, f"req={p.code[:8]}") + f" {C_KEY}{kv}{RST}")
    bar = f"{pad}{DIM}  │{RST} "
    width = max(20, cols - indent - 5)
    for ln in wrap(p.text, width) if p.text else []:
        emit(f"{bar}{C_TXT}{ln}{RST}")

    def attach(kind: str, url: str, ext: str) -> None:
        if raw_urls:
            emit(f"{bar}{C_KEY}{kind}{RST} {C_LINK}{url}{RST}")
        else:
            emit(f"{bar}{C_KEY}{kind}{RST} " + link(url, f"s3://cache/{fake_name(url, ext)}"))

    for i, u in enumerate(p.images):
        attach(f"blob[{i}]", u, ".jpg")
    for i, u in enumerate(p.videos):
        attach(f"stream[{i}]", u, ".mp4")
    if p.ext_link:
        emit(f"{bar}{C_KEY}upstream{RST} " + link(p.ext_link, p.ext_link[: max(20, width - 10)]))
    if p.quoted:
        q = p.quoted
        emit(f"{bar}{C_KEY}caused by:{RST} " + link(q.url, f"req={q.code[:8]}") + f" {C_KEY}usr={q.user}{RST}")
        for ln in wrap(q.text, width - 4)[:6]:
            emit(f"{bar}{DIM}  > {ln}{RST}")
        for i, u in enumerate(q.images):
            emit(f"{bar}{DIM}  > {RST}{C_KEY}blob[{i}]{RST} " + (u if raw_urls else link(u, f"s3://cache/{fake_name(u, '.jpg')}")))


# ───────────────────────── 圖片顯示 ─────────────────────────
def halfblock(data: bytes, width: int, max_rows: int) -> list[str]:
    """把圖片轉成半形方塊字元（▀）：每個字元格上下各一個像素，用 24-bit 前景/背景色上色。"""
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    rows = max(1, round(h * width / w / 2))
    if rows > max_rows:  # 太高的圖：縮窄以免超過一個畫面
        rows = max_rows
        width = max(8, round(w * rows * 2 / h))
    img = img.resize((width, rows * 2), Image.LANCZOS)
    px = img.load()
    lines = []
    for y in range(0, rows * 2, 2):
        parts = []
        for x in range(width):
            r1, g1, b1 = px[x, y]
            r2, g2, b2 = px[x, y + 1]
            parts.append(f"\x1b[38;2;{r1};{g1};{b1};48;2;{r2};{g2};{b2}m▀")
        lines.append("".join(parts) + RST)
    return lines


async def show_images(ctx, posts: list[Post], img_width: int, indent: int = 0) -> int:
    """下載並在 terminal 內畫出貼文的圖片（影片則畫封面）。回傳畫出的張數。"""
    size = shutil.get_terminal_size((100, 30))
    width = max(8, min(img_width, size.columns - indent - 6))
    bar = " " * indent + f"{DIM}  │{RST} "
    todo: list[tuple[str, str]] = []
    for p in posts:
        todo += [(f"blob[{i}]", u) for i, u in enumerate(p.images)]
        todo += [(f"stream[{i}] (cover)", u) for i, u in enumerate(p.thumbs)]
        if p.quoted:
            todo += [(f"> blob[{i}]", u) for i, u in enumerate(p.quoted.images)]
            todo += [(f"> stream[{i}] (cover)", u) for i, u in enumerate(p.quoted.thumbs)]
    n = 0
    for label, url in todo:
        try:
            resp = await ctx.request.get(url, timeout=20000)
            lines = halfblock(await resp.body(), width, max(4, size.lines - 3))
        except Exception as e:
            emit(f"{bar}{C_KEY}{label}{RST} {C_ERR}decode failed: {type(e).__name__}{RST}")
            continue
        emit(f"{bar}{C_KEY}{label}{RST}")
        for ln in lines:
            emit(bar + ln)
        n += 1
    return n


# ───────────────────────── 老闆鍵 ─────────────────────────
FAKE_MODULES = ["core/scheduler", "api/gateway", "db/migrations", "auth/session", "cache/lru", "queue/worker",
                "metrics/exporter", "storage/s3", "rpc/client", "config/loader", "utils/retry", "net/pool"]


def fake_line(i: int) -> str:
    r = random.random()
    mod = random.choice(FAKE_MODULES)
    if r < 0.55:
        return f"{C_TS}[{i:>4}/1284]{RST} {C_TXT}Compiling {mod}.o{RST} {DIM}({random.randint(40, 900)} ms){RST}"
    if r < 0.8:
        return f"{C_TS}[{i:>4}/1284]{RST} {C_INFO}PASS{RST}  {C_TXT}tests/{mod}_test{RST} {DIM}{random.randint(3, 60)} passed in {random.random() * 3:.2f}s{RST}"
    if r < 0.93:
        return f"{C_TS}[{i:>4}/1284]{RST} {C_TXT}Linking lib{mod.split('/')[1]}.so{RST}"
    return f"{C_TS}[{i:>4}/1284]{RST} {C_WARN}warning:{RST} {C_TXT}{mod}.c:{random.randint(10, 900)}: unused variable 'tmp'{RST}"


# ───────────────────────── 鍵盤 ─────────────────────────
class Keys:
    """單鍵輸入。POSIX 用 termios 的 cbreak 模式；Windows 用 msvcrt 直接讀主控台按鍵。"""

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = None

    def __enter__(self):
        if not WINDOWS:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if not WINDOWS:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def prompt(self, label: str) -> str:
        """暫時回到一般輸入模式讀一整行（中文輸入法可正常使用）。"""
        if WINDOWS:
            while msvcrt.kbhit():  # 丟掉先前殘留的按鍵
                msvcrt.getwch()
        else:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        try:
            sys.stdout.write(f"{C_KEY}{label}{RST}")
            sys.stdout.flush()
            return sys.stdin.readline().strip()
        except KeyboardInterrupt:
            return ""
        finally:
            if not WINDOWS:
                tty.setcbreak(self.fd)

    def poll(self) -> str | None:
        if WINDOWS:
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # 方向鍵、功能鍵：第二碼一併讀掉並忽略
                msvcrt.getwch()
                return None
            return ch
        if select.select([self.fd], [], [], 0)[0]:
            data = os.read(self.fd, 32).decode(errors="ignore")
            return data[:1] if data else None
        return None

    async def get(self, timeout: float | None = None) -> str | None:
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            k = self.poll()
            if k:
                return k
            if end is not None and time.monotonic() >= end:
                return None
            await asyncio.sleep(0.03)


async def boss_screen(keys: Keys) -> None:
    """切到替代畫面顯示假的編譯輸出；按任意鍵回來，原畫面完整還原。"""
    sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[H")
    emit(f"{C_TXT}$ make -j16 all test{RST}")
    i = random.randint(180, 420)
    while True:
        emit(fake_line(i))
        i = i + 1 if i < 1284 else 1
        if await keys.get(random.uniform(0.05, 0.6)):
            break
    sys.stdout.write("\x1b[?1049l")
    sys.stdout.flush()


# ───────────────────────── 訊息 ─────────────────────────
# 訊息內容走二進位的 websocket，不像貼文有 JSON 可攔，所以直接從頁面 DOM 讀取。
INBOX_JS = """() => [...document.querySelectorAll('a[href^="/messages/t/"]')].map(a => {
  const spans = [...a.querySelectorAll('span[dir="auto"] > span')]
    .map(s => s.innerText.trim()).filter(t => t && t !== '·');
  const ab = a.querySelector('abbr');
  return {href: a.getAttribute('href'), name: spans[0] || '', preview: spans[1] || '',
          age: ab ? ab.innerText.trim() : ''};
})"""

THREAD_JS = """() => [...document.querySelectorAll('div[role="grid"] div[data-virtualized]')].map(row => {
  const side = row.querySelector('div[style*="--x-justifyContent"]');
  const style = side ? side.getAttribute('style') : '';
  const mine = style.includes('end');
  if (!side || !(mine || style.includes('start'))) return null;
  let ts = '';
  for (const sp of row.querySelectorAll('span[dir="auto"]')) {
    // 時間戳在訊息之前；訊息之後的是「已看過」之類的狀態
    if (side.compareDocumentPosition(sp) & Node.DOCUMENT_POSITION_PRECEDING) { ts = sp.innerText.trim(); break; }
  }
  const post = side.querySelector('a[href*="/post/"]');
  const avatar = side.querySelector('a[href^="/@"]:not([href*="/post/"])');
  const skip = el => el.closest('[role="gridcell"]') || (post && post.contains(el)) || (avatar && avatar.contains(el));
  let text = '';
  for (const sp of side.querySelectorAll('span[dir="auto"]')) {
    if (skip(sp) || sp.querySelector('span[dir="auto"]')) continue;
    const t = sp.innerText.trim();
    if (t) { text = t; break; }   // 第一段是訊息本文，之後的是「已看過」之類的狀態
  }
  let shared = null;
  if (post) {
    const parts = [...post.querySelectorAll('span[dir="auto"]')]
      .filter(sp => !sp.querySelector('span[dir="auto"]')).map(sp => sp.innerText.trim()).filter(Boolean);
    shared = {href: post.getAttribute('href'), text: parts.sort((a, b) => b.length - a.length)[0] || ''};
  }
  const imgs = [...side.querySelectorAll('img')]
    .filter(im => !skip(im) && im.naturalWidth >= 100).map(im => im.src);
  if (!text && !shared && !imgs.length) return null;
  return {ts, mine, user: avatar ? avatar.getAttribute('href').slice(2) : '', text, shared, imgs};
}).filter(Boolean)"""

COMPOSER = 'div[role="textbox"][contenteditable="true"]'


class Mailbox:
    """未讀狀態。每個頁面載入時都會發出訊息徽章查詢，回應裡有各對話的 is_unread。"""

    def __init__(self, ctx):
        self.unread: set[str] = set()   # 未讀對話的 thread id
        self.notified: set[str] = set()  # 已經在動態牆上提醒過的
        self._tasks: set[asyncio.Task] = set()
        ctx.on("response", self._on_response)

    def _on_response(self, resp) -> None:
        if "graphql" not in resp.url or "GetBadgeCount" not in (resp.request.post_data or ""):
            return

        async def handle():
            try:
                data = json.loads(await resp.text())
                edges = data["data"]["get_slide_mailbox_for_iris_subscription"]["threads_by_folder"]["edges"]
                self.unread = {
                    str(e["node"]["id"]) for e in edges if (e["node"].get("as_micg_interface") or {}).get("is_unread")
                }
            except Exception:
                pass

        task = asyncio.ensure_future(handle())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def announce(self) -> None:
        """有新的未讀對話時，在動態牆上提醒一次。"""
        if self.unread - self.notified:
            log("WARN", "mq", f"pending={len(self.unread)} unread channel(s) — press m")
        self.notified = set(self.unread)


def thread_id(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1]


async def dismiss_dialog(page, wait_ms: int = 0) -> None:
    """關掉訊息頁的功能介紹視窗（只有一個按鈕的對話框）；它會擋住後面所有的點擊。"""
    try:
        if wait_ms:
            await page.wait_for_selector('div[role="dialog"]', timeout=wait_ms)
        buttons = page.locator('div[role="dialog"] div[role="button"]')
        if await buttons.count() == 1:
            await buttons.first.click(timeout=3000)
            await page.wait_for_timeout(600)
    except Exception:
        pass
CHAT_HINT = "t=傳送訊息, b=老闆鍵, q=離開對話"


def msg_sig(m: dict) -> tuple:
    return (m["mine"], m["text"], (m["shared"] or {}).get("href"), len(m["imgs"]))


def new_messages(old: list[tuple], cur: list[dict]) -> list[dict]:
    """比對上次看到的訊息簽章，回傳這次新出現在尾端的訊息。"""
    sigs = [msg_sig(m) for m in cur]
    if not old:
        return cur
    if len(sigs) >= len(old) and sigs[: len(old)] == old:  # 單純在尾端新增
        return cur[len(old):]
    k = min(6, len(old))  # 前面的舊訊息被捲掉或補載入：用最後幾則對齊
    for i in range(len(sigs) - k, -1, -1):
        if sigs[i : i + k] == old[-k:]:
            return cur[i + k :]
    return []


def render_msg(m: dict, peer: str, raw_urls: bool = False) -> None:
    cols = shutil.get_terminal_size((100, 30)).columns
    bar = f"{DIM}  │{RST} "
    if m["ts"]:
        emit(f"{bar}{DIM}-- {m['ts']} --{RST}")
    level, color = ("DEBUG", C_DBG) if m["mine"] else ("INFO", C_INFO)
    kv = "tx usr=me" if m["mine"] else f"rx usr={m['user'] or peer}"
    emit(f"{C_TS}{now_ts()}{RST} {color}{level:<5}{RST} {C_KEY}[mq-{peer}]{RST} {C_KEY}{kv}{RST}")
    width = max(20, cols - 5)
    for ln in wrap(m["text"], width) if m["text"] else []:
        emit(f"{bar}{C_TXT}{ln}{RST}")
    if m["shared"]:
        url = BASE + m["shared"]["href"]
        code = m["shared"]["href"].rstrip("/").rsplit("/", 1)[-1]
        emit(f"{bar}{C_KEY}fwd{RST} " + (f"{C_LINK}{url}{RST}" if raw_urls else link(url, f"req={code[:8]}")))
        for ln in wrap(m["shared"]["text"], width - 4)[:4]:
            emit(f"{bar}{DIM}  > {ln}{RST}")
    for i, u in enumerate(m["imgs"]):
        emit(f"{bar}{C_KEY}blob[{i}]{RST} " + (f"{C_LINK}{u}{RST}" if raw_urls else link(u, f"s3://cache/{fake_name(u, '.jpg')}")))


async def read_thread(page, peer: str) -> list[dict]:
    msgs = await page.evaluate(THREAD_JS)
    # 連續訊息只有最後一則帶頭像：由後往前補上發話者
    who = ""
    for m in reversed(msgs):
        if m["mine"]:
            who = ""
        elif m["user"]:
            who = m["user"]
        else:
            m["user"] = who or peer
    return msgs


async def chat(page, conv: dict, keys: Keys, args, mailbox: Mailbox) -> None:
    """單一對話：顯示最近的訊息，之後持續輪詢新訊息；按 t 輸入並送出。"""
    peer = conv["name"] or "unknown"
    log("INFO", "mq", f"subscribing channel={peer} ...")
    await dismiss_dialog(page)
    try:
        await page.locator(f'a[href="{conv["href"]}"]').first.click(timeout=6000)
    except Exception:  # 點不到就直接開對話的網址
        await page.goto(BASE + conv["href"], wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_selector('div[role="grid"]', timeout=15000)
    except Exception:
        log("ERROR", "mq", "subscribe failed: channel not ready")
        return
    await page.wait_for_timeout(2000)
    await dismiss_dialog(page)
    mailbox.unread.discard(thread_id(conv["href"]))
    msgs = await read_thread(page, peer)
    for m in msgs[-15:]:
        render_msg(m, peer, args.raw_urls)
    seen = [msg_sig(m) for m in msgs]
    log("INFO", "mq", f"subscribed channel={peer} backlog={len(msgs)} ({CHAT_HINT})")
    while True:
        k = await keys.get(random.uniform(1.5, 2.5))
        if k is None:
            msgs = await read_thread(page, peer)
            for m in new_messages(seen, msgs):
                render_msg(m, peer, args.raw_urls)
            if msgs:
                seen = [msg_sig(m) for m in msgs]
        elif k == "b":
            await boss_screen(keys)
        elif k in ("t", "\n", "\r"):
            text = keys.prompt("publish> ")
            if not text:
                log("INFO", "mq", "publish cancelled")
                continue
            try:
                await dismiss_dialog(page)
                box = page.locator(COMPOSER).last
                await box.click(timeout=6000)
                await page.keyboard.insert_text(text)
                await page.wait_for_timeout(random.randint(300, 700))
                await page.keyboard.press("Enter")
                log("DEBUG", "mq", f"published channel={peer} bytes={len(text.encode())}")
            except Exception as e:
                log("ERROR", "mq", f"publish failed, message NOT sent: {type(e).__name__}")
        elif k in ("h", "?"):
            log("INFO", "mq", CHAT_HINT)
        elif k in ("q", "m", "\x1b", "\x03", "\x04"):
            log("INFO", "mq", f"unsubscribed channel={peer}")
            return


async def messages(ctx, keys: Keys, args, mailbox: Mailbox) -> None:
    """訊息：列出對話，輸入編號進入聊天。"""
    page = await ctx.new_page()
    try:
        log("INFO", "mq", "listing channels ...")
        await page.goto(f"{BASE}/messages/", wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_selector('a[href^="/messages/t/"]', timeout=15000)
        except Exception:
            pass
        # 功能介紹視窗會比對話列表晚一點才跳出來，等它一下再關
        await dismiss_dialog(page, wait_ms=3000)
        convs = (await page.evaluate(INBOX_JS))[:20]
        if not convs:
            log("WARN", "mq", "no channels")
            return
        for i, c in enumerate(convs, 1):
            unread = thread_id(c["href"]) in mailbox.unread
            state = f"{C_WARN}pending{RST}" if unread else f"{DIM}acked  {RST}"
            emit(f"{DIM}  │{RST} {C_WARN}{i:>2}{RST} {state} {C_KEY}usr={c['name']} age={c['age']}{RST} {C_TXT}{c['preview'][:40]}{RST}")
        mailbox.notified = set(mailbox.unread)
        sel = keys.prompt("channel #> ")
        if not sel.isdigit() or not 1 <= int(sel) <= len(convs):
            log("INFO", "mq", "cancelled")
            return
        await chat(page, convs[int(sel) - 1], keys, args, mailbox)
    except Exception as e:
        log("ERROR", "mq", f"channel error: {type(e).__name__}")
    finally:
        await page.close()


# ───────────────────────── 主流程 ─────────────────────────
COMMANDS = [
    ("space / j", "下一則貼文（Enter 也可以）"),
    ("a", "自動捲動 開／關"),
    ("r", "展開剛剛那則貼文的回覆（每 8 則暫停，最多 40 則）"),
    ("i", "在終端機內顯示剛剛那則貼文的圖片"),
    ("/", "搜尋關鍵字（輸入後按 Enter，空白則取消）"),
    ("m", "訊息：列出對話，輸入編號進入聊天（對話中 t 傳送、q 離開）"),
    ("f", "切換 為你推薦 ↔ 追蹤中；在搜尋結果或個人頁時則回到動態牆"),
    ("R", "重新載入目前頁面"),
    ("b", "老闆鍵：切到假的編譯畫面，按任意鍵回來"),
    ("h / ?", "顯示這份指令表"),
    ("L", "登出帳號並離開（大寫 L，會先要求按 y 確認）"),
    ("q", "離開"),
]
HINT = "按 h 顯示指令表，space 看下一則，q 離開"


def show_help() -> None:
    log("INFO", "main", "available commands:")
    for key, desc in COMMANDS:
        emit(f"{DIM}  │{RST} {C_WARN}{key:<10}{RST}{C_TXT}{desc}{RST}")
    emit(f"{DIM}  │{RST} {C_KEY}連結（req=… 與 s3://…）可用 Ctrl+點擊開啟{RST}")


async def show_replies(ctx, post: Post, keys: Keys, args) -> None:
    src = Source(ctx)
    log("DEBUG", "trace", f"expanding span req={post.code[:8]} ...")
    try:
        await src.open(post.url)
        shown, batch = 0, []
        while shown < 40:
            t = await src.next()
            if not t:
                break
            for p in t:
                if p.pk == post.pk:
                    continue
                render_post(p, "trace", level="DEBUG", indent=4, raw_urls=args.raw_urls)
                batch.append(p)
                shown += 1
            if args.images:
                await show_images(ctx, t, args.img_width, indent=4)
            if shown and shown % 8 == 0:
                log("DEBUG", "trace", "-- more: space=continue, i=images, other=stop --")
                k = await keys.get()
                if k == "i":
                    await show_images(ctx, batch, args.img_width, indent=4)
                    k = await keys.get()
                batch = []
                if k not in (" ", "j", "\n", "\r"):
                    break
        log("DEBUG", "trace", f"span closed req={post.code[:8]} children={shown}")
    except Exception as e:
        log("ERROR", "trace", f"span failed: {type(e).__name__}")
    finally:
        await src.close()


async def browse(args) -> int:
    async with async_playwright() as pw:
        ctx = await open_context(pw, headless=True)
        try:
            if not await is_logged_in(ctx):
                print(f"尚未登入。請先執行： {CMD} login")
                return 1
            feed = "following" if args.following else "foryou"
            feed_label = {"foryou": "feed-fy", "following": "feed-fw"}

            def search_target(q: str) -> tuple[str, str]:
                kind = "&filter=recent" if args.recent else ""
                return f"{BASE}/search?q={quote(q)}&serp_type=default{kind}", "grep"

            if args.search:
                url, label = search_target(args.search)
            elif args.target:
                url, label = f"{BASE}/@{args.target.lstrip('@')}", f"usr-{args.target.lstrip('@')}"
            else:
                url, label = FEEDS[feed], feed_label[feed]
            on_feed = not (args.search or args.target)
            src = Source(ctx)
            mailbox = Mailbox(ctx)
            with Keys() as keys:
                log("INFO", "main", "connecting upstream ...")
                await src.open(url)
                if "/login" in src.page.url:
                    log("ERROR", "main", f"session expired — run: {CMD} login")
                    return 1
                log("INFO", "main", f"stream ready, buffered={len(src.queue)}")
                log("INFO", "main", HINT)
                auto, last, last_thread, n = args.auto, None, [], 0
                while True:
                    thread = await src.next()
                    if not thread:
                        log("WARN", label, "upstream idle, no more records (R=reload, q=quit)")
                    else:
                        for i, p in enumerate(thread):
                            render_post(p, label, indent=0 if i == 0 else 2, raw_urls=args.raw_urls)
                        last, last_thread, n = thread[0], thread, n + 1
                        if args.images:
                            await show_images(ctx, thread, args.img_width)
                    mailbox.announce()
                    # 等待按鍵（auto 模式下依字數決定停留時間）
                    while True:
                        wait = None
                        if auto and thread:
                            chars = sum(len(p.text) for p in thread)
                            wait = min(20.0, max(2.5, chars / args.cps)) + random.uniform(0, 1)
                        k = await keys.get(wait)
                        if k is None or k in (" ", "j", "\n", "\r"):
                            break
                        if k in ("q", "\x03", "\x04"):
                            log("INFO", "main", f"shutdown, records={n}")
                            return 0
                        if k == "b":
                            await boss_screen(keys)
                        elif k == "a":
                            auto = not auto
                            log("INFO", "main", f"follow mode {'on' if auto else 'off'}")
                            if auto:
                                break
                        elif k == "r" and last:
                            await show_replies(ctx, last, keys, args)
                        elif k == "i" and last_thread:
                            if not await show_images(ctx, last_thread, args.img_width):
                                log("DEBUG", "main", "no blobs attached")
                        elif k in ("f", "R"):
                            if k == "f":
                                # 在動態牆上：切換 為你推薦/追蹤中；在搜尋或個人頁：回到動態牆
                                if on_feed:
                                    feed = "following" if feed == "foryou" else "foryou"
                                url, label, on_feed = FEEDS[feed], feed_label[feed], True
                            log("INFO", "main", f"reconnecting -> {label}")
                            await src.open(url)
                            break
                        elif k == "m":
                            await messages(ctx, keys, args, mailbox)
                            log("INFO", "main", HINT)
                        elif k == "/":
                            q = keys.prompt("grep pattern> ")
                            if q:
                                (url, label), on_feed = search_target(q), False
                                log("INFO", "main", f"scanning pattern={q!r}")
                                await src.open(url)
                                break
                        elif k == "L":
                            log("WARN", "main", "登出並刪除本機登入資料？按 y 確認，其他鍵取消")
                            if await keys.get() in ("y", "Y"):
                                args.do_logout = True  # 等瀏覽器關閉後才刪除（見 main）
                                log("INFO", "main", f"shutdown, records={n}")
                                return 0
                            log("INFO", "main", "已取消")
                        elif k in ("h", "?"):
                            show_help()
        finally:
            try:
                await ctx.close()
            except Exception:  # Ctrl+C 會連瀏覽器一起中斷，此時已經關不了
                pass


async def login() -> int:
    print("即將開啟 Chrome 視窗，請在視窗中登入 Threads。")
    if LOGIN_AUTOCLOSE:
        print("登入完成後視窗會自動關閉（session 會存在 .profile/）。")
    else:
        print("登入完成並看到首頁動態後，直接關閉視窗即可（session 會存在 .profile/）。")
    async with async_playwright() as pw:
        ctx = await open_context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        closed = asyncio.Event()
        ctx.on("close", lambda *_: closed.set())
        ok = False
        while not closed.is_set():
            try:
                ok = await is_logged_in(ctx)
            except Exception:
                break
            if ok and LOGIN_AUTOCLOSE:
                print("✓ 已偵測到登入成功，稍候自動關閉視窗 ...")
                await asyncio.sleep(8)  # 等首頁載入完、cookie 寫入磁碟
                break
            if ok and not getattr(login, "_told", False):
                print("✓ 已偵測到登入成功，可以關閉視窗了。")
                login._told = True
            try:
                await asyncio.wait_for(closed.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
        try:
            await ctx.close()
        except Exception:
            pass
    print(f"登入完成，執行 {CMD} 開始瀏覽（進入後按 h 可看指令表）。" if ok else "未偵測到登入，請重試。")
    return 0 if ok else 1


def logout() -> int:
    """刪除本機保存的登入 session（.profile/ 資料夾的全部內容）。"""
    if not PROFILE_DIR.exists() or not any(PROFILE_DIR.iterdir()):
        print("目前沒有登入資料。")
        return 0
    # Windows 上 Chrome 剛關閉時檔案可能還被鎖住，稍等再試
    for attempt in range(6):
        try:
            # 逐項刪除內容：.profile/ 本身可能是掛載點（Docker volume），無法移除
            for child in PROFILE_DIR.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            break
        except OSError as e:
            if attempt == 5:
                print(f"刪除失敗（logtail 是否還在執行？）：{e}")
                return 1
            time.sleep(0.5)
    try:
        PROFILE_DIR.rmdir()
    except OSError:
        pass
    print(f"已登出：本機的登入資料已刪除。要再使用請重新執行 {CMD} login")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="logtail", description="tail -f for a certain upstream")
    ap.add_argument("target", nargs="?", help="login | logout | @username（省略則看動態牆）")
    ap.add_argument("-s", "--search", metavar="關鍵字", help="搜尋關鍵字")
    ap.add_argument("--recent", action="store_true", help="搜尋結果改為「最新」排序（預設為熱門）")
    ap.add_argument("-f", "--following", action="store_true", help="從「追蹤中」開始")
    ap.add_argument("-a", "--auto", action="store_true", help="自動捲動模式")
    ap.add_argument("--cps", type=float, default=12, help="auto 模式閱讀速度（字/秒，預設 12）")
    ap.add_argument("-i", "--images", action="store_true", help="每則貼文自動顯示圖片（預設按 i 才顯示）")
    ap.add_argument("--img-width", type=int, default=56, help="圖片寬度（字元數，預設 56）")
    ap.add_argument("--raw-urls", action="store_true", help="終端機不支援超連結時，直接印出完整網址")
    args = ap.parse_args()
    setup_console()
    if args.target == "logout":
        return logout()
    if args.target == "login":
        return asyncio.run(login())
    if not sys.stdin.isatty():
        print("需要在互動式 terminal 中執行")
        return 1
    try:
        rc = asyncio.run(browse(args)) or 0
        if getattr(args, "do_logout", False):
            return logout()
        return rc
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
