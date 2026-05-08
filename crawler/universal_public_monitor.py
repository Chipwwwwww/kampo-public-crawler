import asyncio
import hashlib
import json
import os
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Response, async_playwright

DEFAULT_TARGETS = [
    "https://www.kampojanechen.org/",
    "https://www.kampojanechen.org/v2/Official/NewestSalePage",
]

ID_KEYS = ["Id", "id"]
NAME_KEYS = ["Title", "title"]
PRICE_KEYS = ["Price", "price"]
QTY_KEYS = ["SellingQty", "sellingQty", "Stock", "stock", "Inventory", "inventory", "ReqQty", "reqQty"]
STATUS_KEYS = ["IsSoldOut", "isSoldOut"]
IMAGE_KEYS = ["PicUrl", "picUrl", "DynamicPicUrl", "dynamicPicUrl"]
URL_KEYS = ["url", "Url", "href", "Href", "productUrl", "ProductUrl", "canonicalUrl", "CanonicalUrl"]

CATEGORY_KEYS = {"categoryList", "ChildList", "OrderByDef", "ParentId", "ParentName", "Sort"}
SALEPAGE_ID_PAT = re.compile(r"/SalePage/Index/(\d+)", re.I)
SALEPAGE_API = "https://fts-api.91app.com/salepage-listing/api/mweb/salepage-list/41047"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "y"}


def parse_targets() -> list[str]:
    raw = os.getenv("TARGET_URLS", "")
    if not raw.strip():
        return DEFAULT_TARGETS
    return [x.strip() for x in raw.split(",") if x.strip()]


def connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def get_first(d: dict[str, Any], keys: list[str]) -> tuple[Any, str | None]:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k], k
    return None, None


def to_num(x: Any):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x)
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


def walk_entities(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk_entities(v)
    elif isinstance(node, list):
        for x in node:
            yield from walk_entities(x)


def is_category_like_entity(item: dict[str, Any]) -> bool:
    return any(k in item for k in CATEGORY_KEYS)


def is_salepage_listing_url(url: str) -> bool:
    return "salepage-listing/api/mweb/salepage-list" in (url or "")


def looks_like_product_entity(item: dict[str, Any], source_url: str) -> bool:
    if is_category_like_entity(item):
        return False

    entity_id, _ = get_first(item, ID_KEYS)
    title, _ = get_first(item, NAME_KEYS)
    price_raw, _ = get_first(item, PRICE_KEYS)
    qty_raw, _ = get_first(item, QTY_KEYS)
    pic, _ = get_first(item, IMAGE_KEYS)

    has_id = entity_id is not None
    has_title = title is not None
    has_price = to_num(price_raw) is not None
    has_qty = to_num(qty_raw) is not None

    sale_page_id = item.get("SalePageId") or item.get("salePageId") or entity_id
    has_sale_page_id = sale_page_id is not None
    has_image_or_price = pic is not None or has_price

    return (
        (has_id and has_price)
        or (has_id and has_qty)
        or (has_title and has_price)
        or (has_sale_page_id and has_image_or_price)
        or (is_salepage_listing_url(source_url) and any(item.get(k) is not None for k in ["Id", "Title", "Price", "SellingQty"]))
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, source_type TEXT, source_url TEXT, entity_type TEXT, entity_id TEXT, entity_name TEXT, price REAL, currency TEXT, public_quantity REAL, quantity_field_name TEXT, status_text TEXT, public_sales_text TEXT, public_sales_count REAL, entity_url TEXT, image_url TEXT, raw_hash TEXT, raw_item TEXT);
CREATE TABLE IF NOT EXISTS entity_latest (entity_key TEXT PRIMARY KEY, updated_at TEXT, run_id TEXT, entity_id TEXT, entity_name TEXT, price REAL, public_quantity REAL, quantity_field_name TEXT, source_url TEXT);
CREATE TABLE IF NOT EXISTS public_quantity_events (id INTEGER PRIMARY KEY AUTOINCREMENT, detected_at TEXT, run_id TEXT, event_type TEXT, entity_id TEXT, entity_name TEXT, price REAL, previous_quantity REAL, current_quantity REAL, delta REAL, quantity_field_name TEXT, estimated_public_amount_change REAL, source_url TEXT);
CREATE TABLE IF NOT EXISTS response_audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, request_url TEXT, response_url TEXT, status INTEGER, resource_type TEXT, content_type TEXT, extracted_count INTEGER, json_shape TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS extract_rejections (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, source_type TEXT, source_url TEXT, reason TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS run_summary (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, scanned_url_count INTEGER, intercepted_json_count INTEGER, saved_json_api_count INTEGER, entities_extracted_json INTEGER, entities_extracted_dom INTEGER, entities_extracted_hydration INTEGER, public_quantity_event_count INTEGER, public_sales_text_count INTEGER, inserted_snapshot_count INTEGER, rejection_summary TEXT);
"""


@dataclass
class Stats:
    intercepted_json_count: int = 0
    saved_json_api_count: int = 0
    entities_extracted_json: int = 0
    entities_extracted_dom: int = 0
    entities_extracted_hydration: int = 0
    public_quantity_event_count: int = 0
    public_sales_text_count: int = 0
    inserted_snapshot_count: int = 0
    priced_entity_count: int = 0
    quantity_entity_count: int = 0
    batch_api_product_count: int = 0
    category_like_rejected_count: int = 0


class Monitor:
    def __init__(self):
        self.targets = parse_targets()
        self.headless = env_bool("HEADLESS", True)
        self.db_name = os.getenv("DB_NAME", "gampo_public_monitor.db")
        self.max_retries = int(os.getenv("MAX_RETRIES", "3"))
        self.delay_min = float(os.getenv("REQUEST_DELAY_MIN_SECONDS", "2"))
        self.delay_max = float(os.getenv("REQUEST_DELAY_MAX_SECONDS", "4"))
        self.run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.queue: asyncio.Queue[tuple[str, str, Any]] = asyncio.Queue()
        self.conn = connect_db(self.db_name)
        self.conn.executescript(SCHEMA)
        self.stats = Stats()

    def reject(self, source_type: str, source_url: str, reason: str, detail: str):
        self.conn.execute(
            "INSERT INTO extract_rejections(captured_at,run_id,source_type,source_url,reason,detail) VALUES (?,?,?,?,?,?)",
            (now_iso(), self.run_id, source_type, source_url, reason, detail[:2000]),
        )

    def save_entity(self, source_type: str, source_url: str, item: dict[str, Any]) -> bool:
        if is_category_like_entity(item):
            self.reject(source_type, source_url, "category_like_entity", json.dumps(item, ensure_ascii=False)[:2000])
            self.stats.category_like_rejected_count += 1
            return False
        if not looks_like_product_entity(item, source_url):
            return False

        entity_id, _ = get_first(item, ID_KEYS)
        name, _ = get_first(item, NAME_KEYS)
        price_raw, _ = get_first(item, PRICE_KEYS)
        qty_raw, qty_key = get_first(item, QTY_KEYS)
        status, _ = get_first(item, STATUS_KEYS)
        image, _ = get_first(item, IMAGE_KEYS)
        eurl, _ = get_first(item, URL_KEYS)

        price = to_num(price_raw)
        qty = to_num(qty_raw)
        if not entity_id and eurl:
            m = SALEPAGE_ID_PAT.search(str(eurl))
            entity_id = m.group(1) if m else None
        if not entity_id and source_url:
            m = SALEPAGE_ID_PAT.search(source_url)
            entity_id = m.group(1) if m else None
        if not name and not entity_id:
            return False

        raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
        raw_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.conn.execute(
            "INSERT INTO entity_snapshots(captured_at,run_id,source_type,source_url,entity_type,entity_id,entity_name,price,currency,public_quantity,quantity_field_name,status_text,public_sales_text,public_sales_count,entity_url,image_url,raw_hash,raw_item) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), self.run_id, source_type, source_url, "generic", str(entity_id) if entity_id else None, str(name) if name else None, price, "TWD", qty, qty_key, str(status) if status else None, None, None, str(eurl) if eurl else None, str(image) if image else None, raw_hash, raw),
        )
        self.stats.inserted_snapshot_count += 1
        self.stats.entities_extracted_json += 1 if source_type == "json_api" else 0
        self.stats.entities_extracted_hydration += 1 if source_type == "hydration" else 0
        self.stats.entities_extracted_dom += 1 if source_type == "dom" else 0
        if price is not None:
            self.stats.priced_entity_count += 1
        if qty is not None:
            self.stats.quantity_entity_count += 1

        key = f"{entity_id or name}|{source_url}"
        prev = self.conn.execute("SELECT public_quantity, price, entity_name FROM entity_latest WHERE entity_key=?", (key,)).fetchone()
        self.conn.execute(
            "INSERT INTO entity_latest(entity_key,updated_at,run_id,entity_id,entity_name,price,public_quantity,quantity_field_name,source_url) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_key) DO UPDATE SET updated_at=excluded.updated_at,run_id=excluded.run_id,entity_id=excluded.entity_id,entity_name=excluded.entity_name,price=excluded.price,public_quantity=excluded.public_quantity,quantity_field_name=excluded.quantity_field_name,source_url=excluded.source_url",
            (key, now_iso(), self.run_id, str(entity_id) if entity_id else None, str(name) if name else None, price, qty, qty_key, source_url),
        )
        if prev and prev[0] is not None and qty is not None and isinstance(prev[0], (int, float)):
            prev_qty = float(prev[0])
            if qty != prev_qty:
                event_type = "public_quantity_decrease" if qty < prev_qty else "public_quantity_increase"
                delta = abs(prev_qty - qty)
                est = (price or prev[1] or 0) * delta
                self.conn.execute(
                    "INSERT INTO public_quantity_events(detected_at,run_id,event_type,entity_id,entity_name,price,previous_quantity,current_quantity,delta,quantity_field_name,estimated_public_amount_change,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now_iso(), self.run_id, event_type, str(entity_id) if entity_id else None, str(name) if name else None, price, prev_qty, qty, delta, qty_key, est, source_url),
                )
                self.stats.public_quantity_event_count += 1
                if event_type == "public_quantity_decrease":
                    print(f"\033[93;41m[💰 實時訂單/進帳偵測] {name} | 單價: {price} | 消耗數量: {delta} | 預估流水 (Estimated Revenue): {est}\033[0m")
        return True

    async def worker(self):
        while True:
            source_type, source_url, payload = await self.queue.get()
            try:
                for obj in walk_entities(payload):
                    self.save_entity(source_type, source_url, obj)
            except Exception as e:
                self.reject(source_type, source_url, "worker_parse_error", str(e))
            finally:
                self.queue.task_done()

    async def fetch_batch_salepage_details(self, context, ids: list[str]):
        uniq = sorted(set(ids))
        if not uniq:
            return
        page = await context.new_page()
        for i in range(0, len(uniq), 20):
            batch = uniq[i : i + 20]
            url = f"{SALEPAGE_API}?Ids={','.join(batch)}&lang=zh-TW&includeSalePageGroup=false&includeInvisibleSalepage=true"
            for backoff in [2, 4, 8]:
                try:
                    resp = await page.request.get(url, headers=self.stealth_headers())
                    if resp.status == 403:
                        await context.clear_cookies()
                        await asyncio.sleep(backoff)
                        continue
                    data = await resp.json()
                    product_nodes = self.extract_priority_products(data)
                    success_products = 0
                    for p in product_nodes:
                        if self.save_entity("json_api", url, p):
                            success_products += 1
                    if success_products > 0:
                        self.stats.saved_json_api_count += 1
                        self.stats.batch_api_product_count += success_products
                    break
                except Exception as e:
                    self.reject("json_api", url, "batch_fetch_error", str(e))
                    await context.clear_cookies()
                    await asyncio.sleep(backoff)
            await asyncio.sleep(random.uniform(2, 4))
        await page.close()

    def extract_priority_products(self, payload: Any) -> list[dict[str, Any]]:
        nodes = []
        if isinstance(payload, dict):
            for key_path in [("Data", "SalePageList"), ("SalePageList",), ("List",), ("Data", "List")]:
                cur = payload
                ok = True
                for k in key_path:
                    if isinstance(cur, dict) and k in cur:
                        cur = cur[k]
                    else:
                        ok = False
                        break
                if ok and isinstance(cur, list):
                    nodes.extend([x for x in cur if isinstance(x, dict)])
            if nodes:
                return nodes
        for obj in walk_entities(payload):
            if looks_like_product_entity(obj, ""):
                nodes.append(obj)
        return nodes

    def stealth_headers(self) -> dict[str, str]:
        uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        ]
        return {
            "User-Agent": random.choice(uas),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.kampojanechen.org/",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    async def scan_url(self, context, url: str):
        for i in range(self.max_retries):
            page = await context.new_page()
            page.set_default_timeout(45000)
            await page.set_extra_http_headers(self.stealth_headers())
            await page.add_init_script("""() => {
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(p){
                    if (p === 37445) return 'Intel Inc.';
                    if (p === 37446) return 'Intel Iris OpenGL Engine';
                    return getParameter.call(this,p);
                };
                const toDataURL = HTMLCanvasElement.prototype.toDataURL;
                HTMLCanvasElement.prototype.toDataURL = function(...args){
                    const ctx = this.getContext('2d');
                    if (ctx) {
                        ctx.globalAlpha = 0.01;
                        ctx.fillRect(0,0,1,1);
                    }
                    return toDataURL.apply(this,args);
                };
            }""")
            sale_page_ids: list[str] = []

            async def on_response(resp: Response):
                try:
                    ct = resp.headers.get("content-type", "")
                    rt = resp.request.resource_type
                    is_json = "json" in ct.lower() or rt in {"xhr", "fetch"}
                    if not is_json:
                        return
                    self.stats.intercepted_json_count += 1
                    body = await resp.json()
                    await self.queue.put(("json_api", resp.url, body))
                except Exception:
                    pass

            page.on("response", on_response)
            try:
                r = await page.goto(url, wait_until="domcontentloaded")
                if r and r.status in {403, 429}:
                    await context.clear_cookies()
                    await page.close()
                    await asyncio.sleep(2 ** (i + 1))
                    continue
                await page.wait_for_timeout(1500)
                hrefs = await page.evaluate("""() => Array.from(document.querySelectorAll('a[href*="/SalePage/Index/"]')).map(a => a.href)""")
                for h in hrefs:
                    m = SALEPAGE_ID_PAT.search(h or "")
                    if m:
                        sale_page_ids.append(m.group(1))
                hydration = await page.evaluate("""() => ({initial: window.__INITIAL_STATE__ || null, nuxt: window.__NUXT__ || null, nextData: window.__NEXT_DATA__ || null})""")
                await self.queue.put(("hydration", url, hydration))
                await page.close()
                await self.fetch_batch_salepage_details(context, sale_page_ids)
                await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))
                return
            except Exception as e:
                self.reject("page", url, "scan_error", str(e))
                await context.clear_cookies()
                await page.close()
                await asyncio.sleep(2 ** (i + 1))

    def write_summary(self):
        rej = self.conn.execute("SELECT reason, COUNT(*) FROM extract_rejections WHERE run_id=? GROUP BY reason", (self.run_id,)).fetchall()
        rej_map = {k: v for k, v in rej}
        self.conn.execute(
            "INSERT INTO run_summary(captured_at,run_id,scanned_url_count,intercepted_json_count,saved_json_api_count,entities_extracted_json,entities_extracted_dom,entities_extracted_hydration,public_quantity_event_count,public_sales_text_count,inserted_snapshot_count,rejection_summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), self.run_id, len(self.targets), self.stats.intercepted_json_count, self.stats.saved_json_api_count, self.stats.entities_extracted_json, self.stats.entities_extracted_dom, self.stats.entities_extracted_hydration, self.stats.public_quantity_event_count, self.stats.public_sales_text_count, self.stats.inserted_snapshot_count, json.dumps(rej_map, ensure_ascii=False)),
        )
        summary = {"run_id": self.run_id, "captured_at": now_iso(), "targets": self.targets, "stats": self.stats.__dict__, "rejections": rej_map}
        os.makedirs("data", exist_ok=True)
        with open("data/latest_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        self.conn.commit()

    async def run(self):
        worker_task = asyncio.create_task(self.worker())
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(locale="zh-TW")
            for url in self.targets:
                await self.scan_url(context, url)
            await self.queue.join()
            worker_task.cancel()
            await browser.close()
        self.write_summary()


if __name__ == "__main__":
    asyncio.run(Monitor().run())
