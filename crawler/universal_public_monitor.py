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

from playwright.async_api import async_playwright, Response

DEFAULT_TARGETS = [
    "https://www.kampojanechen.org/",
    "https://www.kampojanechen.org/v2/Official/NewestSalePage",
]

ID_KEYS = ["id","Id","ID","product_id","ProductId","productId","item_id","ItemId","sku","SKU","salePageId","SalePageId","goodsId","GoodsId"]
NAME_KEYS = ["title","Title","name","Name","productName","ProductName","itemName","ItemName","goodsName","GoodsName","displayName","DisplayName"]
PRICE_KEYS = ["price","Price","salePrice","SalePrice","sellingPrice","SellingPrice","displayPrice","DisplayPrice","amount","Amount","finalPrice","FinalPrice"]
QTY_KEYS = ["stock","Stock","inventory","Inventory","quantity","Quantity","qty","Qty","availableQuantity","AvailableQuantity","sellingQty","SellingQty","reqQty","ReqQty"]
STATUS_KEYS = ["isSoldOut","IsSoldOut","soldOut","SoldOut","status","Status","availability","Availability"]
IMAGE_KEYS = ["image","Image","imageUrl","ImageUrl","picUrl","PicUrl","thumbnail","Thumbnail","dynamicPicUrl","DynamicPicUrl"]
URL_KEYS = ["url","Url","href","Href","productUrl","ProductUrl","canonicalUrl","CanonicalUrl"]
SALES_WORDS = ["已售完","已售","銷量","成交","售出","sold","sales","orders","purchased"]
ID_PAT = re.compile(r"/(?:product|products|item|goods)/([^/?#]+)|/SalePage/Index/([^/?#]+)", re.I)


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

SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, source_type TEXT, source_url TEXT, entity_type TEXT, entity_id TEXT, entity_name TEXT, price REAL, currency TEXT, public_quantity REAL, quantity_field_name TEXT, status_text TEXT, public_sales_text TEXT, public_sales_count REAL, entity_url TEXT, image_url TEXT, raw_hash TEXT, raw_item TEXT);
CREATE TABLE IF NOT EXISTS entity_latest (entity_key TEXT PRIMARY KEY, updated_at TEXT, run_id TEXT, entity_id TEXT, entity_name TEXT, price REAL, public_quantity REAL, quantity_field_name TEXT, source_url TEXT);
CREATE TABLE IF NOT EXISTS dom_entity_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, source_page_url TEXT, dom_selector TEXT, entity_name TEXT, price REAL, public_quantity REAL, public_sales_text TEXT, entity_url TEXT, image_url TEXT, raw_text TEXT, raw_hash TEXT);
CREATE TABLE IF NOT EXISTS public_quantity_events (id INTEGER PRIMARY KEY AUTOINCREMENT, detected_at TEXT, run_id TEXT, event_type TEXT, entity_id TEXT, entity_name TEXT, price REAL, previous_quantity REAL, current_quantity REAL, delta REAL, quantity_field_name TEXT, estimated_public_amount_change REAL, source_url TEXT);
CREATE TABLE IF NOT EXISTS response_audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, request_url TEXT, response_url TEXT, status INTEGER, resource_type TEXT, content_type TEXT, extracted_count INTEGER, json_shape TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS extract_rejections (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, source_type TEXT, source_url TEXT, reason TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS run_summary (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, scanned_url_count INTEGER, intercepted_json_count INTEGER, saved_json_api_count INTEGER, entities_extracted_json INTEGER, entities_extracted_dom INTEGER, entities_extracted_hydration INTEGER, public_quantity_event_count INTEGER, public_sales_text_count INTEGER, inserted_snapshot_count INTEGER, rejection_summary TEXT);
CREATE TABLE IF NOT EXISTS defense_audit_findings (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, source_url TEXT, finding_type TEXT, severity TEXT, field_name TEXT, description TEXT, recommendation TEXT, evidence TEXT);
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


def get_first(d: dict[str, Any], keys: list[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k], k
    return None, None


def walk_entities(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk_entities(v)
    elif isinstance(node, list):
        for x in node:
            yield from walk_entities(x)


def to_num(x: Any):
    if x is None:
        return None
    if isinstance(x, (int,float)):
        return float(x)
    s = str(x)
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None

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
        self.conn.execute("INSERT INTO extract_rejections(captured_at,run_id,source_type,source_url,reason,detail) VALUES (?,?,?,?,?,?)", (now_iso(), self.run_id, source_type, source_url, reason, detail[:2000]))

    def save_entity(self, source_type: str, source_url: str, item: dict[str, Any]):
        entity_id, _ = get_first(item, ID_KEYS)
        name, _ = get_first(item, NAME_KEYS)
        price_raw, _ = get_first(item, PRICE_KEYS)
        qty_raw, qty_key = get_first(item, QTY_KEYS)
        status, _ = get_first(item, STATUS_KEYS)
        image, _ = get_first(item, IMAGE_KEYS)
        eurl, _ = get_first(item, URL_KEYS)
        sales_text = next((str(v) for v in item.values() if isinstance(v, str) and any(w.lower() in v.lower() for w in SALES_WORDS)), None)
        sales_count = to_num(sales_text)
        price = to_num(price_raw)
        qty = to_num(qty_raw)
        if not entity_id and eurl:
            m = ID_PAT.search(str(eurl)); entity_id = next((g for g in m.groups() if g), None) if m else None
        if not entity_id and source_url:
            m = ID_PAT.search(source_url); entity_id = next((g for g in m.groups() if g), None) if m else None
        if not name and not entity_id:
            return
        raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
        raw_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.conn.execute("INSERT INTO entity_snapshots(captured_at,run_id,source_type,source_url,entity_type,entity_id,entity_name,price,currency,public_quantity,quantity_field_name,status_text,public_sales_text,public_sales_count,entity_url,image_url,raw_hash,raw_item) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (now_iso(), self.run_id, source_type, source_url, "generic", str(entity_id) if entity_id else None, str(name) if name else None, price, "TWD", qty, qty_key, str(status) if status else None, sales_text, sales_count, str(eurl) if eurl else None, str(image) if image else None, raw_hash, raw))
        self.stats.inserted_snapshot_count += 1
        key = f"{entity_id or name}|{source_url}"
        prev = self.conn.execute("SELECT public_quantity, price, entity_name FROM entity_latest WHERE entity_key=?", (key,)).fetchone()
        self.conn.execute("INSERT INTO entity_latest(entity_key,updated_at,run_id,entity_id,entity_name,price,public_quantity,quantity_field_name,source_url) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(entity_key) DO UPDATE SET updated_at=excluded.updated_at,run_id=excluded.run_id,entity_id=excluded.entity_id,entity_name=excluded.entity_name,price=excluded.price,public_quantity=excluded.public_quantity,quantity_field_name=excluded.quantity_field_name,source_url=excluded.source_url", (key, now_iso(), self.run_id, str(entity_id) if entity_id else None, str(name) if name else None, price, qty, qty_key, source_url))
        if prev and prev[0] is not None and qty is not None and qty != prev[0]:
            event_type = "public_quantity_decrease" if qty < prev[0] else "public_quantity_increase"
            delta = abs(prev[0] - qty)
            est = (price or prev[1] or 0) * delta
            self.conn.execute("INSERT INTO public_quantity_events(detected_at,run_id,event_type,entity_id,entity_name,price,previous_quantity,current_quantity,delta,quantity_field_name,estimated_public_amount_change,source_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (now_iso(), self.run_id, event_type, str(entity_id) if entity_id else None, str(name) if name else None, price, prev[0], qty, delta, qty_key, est, source_url))
            self.stats.public_quantity_event_count += 1
            if event_type == "public_quantity_decrease":
                print(f"\033[91m[💰 實時訂單/數量下降] {name} | 單價: {price} | 消耗數量: {delta} | 預估流水 (Estimated Revenue): {est}\033[0m")
        if sales_text:
            self.stats.public_sales_text_count += 1

    async def worker(self):
        while True:
            source_type, source_url, payload = await self.queue.get()
            try:
                count = 0
                for obj in walk_entities(payload):
                    self.save_entity(source_type, source_url, obj)
                    count += 1
                if source_type == "json_api":
                    self.stats.entities_extracted_json += count
                elif source_type == "hydration":
                    self.stats.entities_extracted_hydration += count
            except Exception as e:
                self.reject(source_type, source_url, "worker_parse_error", str(e))
            finally:
                self.queue.task_done()

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

    async def scan_url(self, context, url: str):
        for i in range(self.max_retries):
            page = await context.new_page()
            page.set_default_timeout(45000)
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
                    self.stats.saved_json_api_count += 1
                    shape = type(body).__name__
                    self.conn.execute("INSERT INTO response_audit_log(captured_at,run_id,request_url,response_url,status,resource_type,content_type,extracted_count,json_shape,error) VALUES (?,?,?,?,?,?,?,?,?,?)", (now_iso(), self.run_id, resp.request.url, resp.url, resp.status, rt, ct, 1, shape, None))
                except Exception as e:
                    self.conn.execute("INSERT INTO response_audit_log(captured_at,run_id,request_url,response_url,status,resource_type,content_type,extracted_count,json_shape,error) VALUES (?,?,?,?,?,?,?,?,?,?)", (now_iso(), self.run_id, resp.request.url, resp.url, resp.status, resp.request.resource_type, resp.headers.get("content-type",""), 0, None, str(e)))
            page.on("response", on_response)
            try:
                r = await page.goto(url, wait_until="domcontentloaded")
                if r and r.status in {403,429}:
                    await context.clear_cookies()
                    await page.close()
                    await asyncio.sleep(2 ** (i + 1))
                    continue
                await page.wait_for_timeout(1200)
                hydration = await page.evaluate("""() => ({initial: window.__INITIAL_STATE__ || null, nuxt: window.__NUXT__ || null, apollo: window.__APOLLO_STATE__ || null, redux: window.__REDUX_STATE__ || null, preloaded: window.__PRELOADED_STATE__ || null, nextData: window.__NEXT_DATA__ || null, ldjson: Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map(s=>s.textContent)})""")
                await self.queue.put(("hydration", url, hydration))
                cards = await page.evaluate("""() => Array.from(document.querySelectorAll('article,.product,.product-card,.item,.card')).slice(0,100).map(el=>({name: el.querySelector('h1,h2,h3,.title,.name')?.textContent?.trim()||'', price: el.textContent?.match(/[\$NT\s]?\d+[\d,]*/)?.[0]||null, url: el.querySelector('a')?.href||null, imageUrl: el.querySelector('img')?.src||null, raw: el.textContent?.trim()?.slice(0,500)}))""")
                for c in cards:
                    self.save_entity("dom", url, c)
                    self.stats.entities_extracted_dom += 1
                await page.close()
                await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))
                return
            except Exception as e:
                self.reject("page", url, "scan_error", str(e))
                await page.close()
                await asyncio.sleep(2 ** (i + 1))
        self.reject("page", url, "max_retries_exceeded", f"Failed after {self.max_retries} retries")

    def write_summary(self):
        rej = self.conn.execute("SELECT reason, COUNT(*) FROM extract_rejections WHERE run_id=? GROUP BY reason", (self.run_id,)).fetchall()
        rej_map = {k: v for k,v in rej}
        self.conn.execute("INSERT INTO run_summary(captured_at,run_id,scanned_url_count,intercepted_json_count,saved_json_api_count,entities_extracted_json,entities_extracted_dom,entities_extracted_hydration,public_quantity_event_count,public_sales_text_count,inserted_snapshot_count,rejection_summary) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (now_iso(), self.run_id, len(self.targets), self.stats.intercepted_json_count, self.stats.saved_json_api_count, self.stats.entities_extracted_json, self.stats.entities_extracted_dom, self.stats.entities_extracted_hydration, self.stats.public_quantity_event_count, self.stats.public_sales_text_count, self.stats.inserted_snapshot_count, json.dumps(rej_map, ensure_ascii=False)))
        events = self.conn.execute("SELECT entity_name, SUM(estimated_public_amount_change) FROM public_quantity_events WHERE run_id=? AND event_type='public_quantity_decrease' GROUP BY entity_name ORDER BY SUM(estimated_public_amount_change) DESC LIMIT 20", (self.run_id,)).fetchall()
        hot = [{"entity_name":n,"estimated_revenue":v} for n,v in events]
        evt = self.conn.execute("SELECT detected_at,event_type,entity_name,price,delta,estimated_public_amount_change,source_url FROM public_quantity_events WHERE run_id=? ORDER BY id DESC LIMIT 100", (self.run_id,)).fetchall()
        summary = {"run_id": self.run_id, "captured_at": now_iso(), "targets": self.targets, "stats": self.stats.__dict__, "hot_entities": hot, "events": [{"detected_at":r[0],"event_type":r[1],"entity_name":r[2],"price":r[3],"delta":r[4],"estimated_public_amount_change":r[5],"source_url":r[6]} for r in evt], "rejections": rej_map}
        os.makedirs("data", exist_ok=True)
        with open("data/latest_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        self.conn.commit()

if __name__ == "__main__":
    asyncio.run(Monitor().run())
