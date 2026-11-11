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
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from playwright.async_api import async_playwright

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
def chrome_ua() -> str:
    try:
        ver = subprocess.run(["google-chrome", "--version"], capture_output=True, text=True).stdout
        major = re.search(r"(\d+)\.", ver).group(1)
    except Exception:
        major = "140"
    return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


async def open_context(pw, headless: bool):
    PROFILE_DIR.mkdir(exist_ok=True)
    os.chmod(PROFILE_DIR, 0o700)
    return await pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        channel="chrome",
        headless=headless,
        user_agent=chrome_ua(),
        locale="zh-TW",
        viewport={"width": 1280, "height": 900},
        # 登入(有視窗)與瀏覽(無頭)用同一種 cookie 加密方式，session 才能共用
        args=["--password-store=basic", "--disable-blink-features=AutomationControlled"],
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
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = None

    def __enter__(self):
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def prompt(self, label: str) -> str:
        """暫時回到一般輸入模式讀一整行（中文輸入法可正常使用）。"""
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        try:
            sys.stdout.write(f"{C_KEY}{label}{RST}")
            sys.stdout.flush()
            return sys.stdin.readline().strip()
        except KeyboardInterrupt:
            return ""
        finally:
            tty.setcbreak(self.fd)

    def poll(self) -> str | None:
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


# ───────────────────────── 主流程 ─────────────────────────
COMMANDS = [
    ("space / j", "下一則貼文（Enter 也可以）"),
    ("a", "自動捲動 開／關"),
    ("r", "展開剛剛那則貼文的回覆（每 8 則暫停，最多 40 則）"),
    ("i", "在終端機內顯示剛剛那則貼文的圖片"),
    ("/", "搜尋關鍵字（輸入後按 Enter，空白則取消）"),
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
                print("尚未登入。請先執行： ./logtail login")
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
            with Keys() as keys:
                log("INFO", "main", "connecting upstream ...")
                await src.open(url)
                if "/login" in src.page.url:
                    log("ERROR", "main", "session expired — run: ./logtail login")
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
            await ctx.close()


async def login() -> int:
    print("即將開啟 Chrome 視窗，請在視窗中登入 Threads。")
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
    print("登入完成，執行 ./logtail 開始瀏覽（進入後按 h 可看指令表）。" if ok else "未偵測到登入，請重試。")
    return 0 if ok else 1


def logout() -> int:
    """刪除本機保存的登入 session（整個 .profile/ 資料夾）。"""
    if not PROFILE_DIR.exists():
        print("目前沒有登入資料。")
        return 0
    try:
        shutil.rmtree(PROFILE_DIR)
    except OSError as e:
        print(f"刪除失敗（logtail 是否還在執行？）：{e}")
        return 1
    print("已登出：本機的登入資料已刪除。要再使用請重新執行 ./logtail login")
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
