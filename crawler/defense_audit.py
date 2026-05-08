import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from playwright.async_api import async_playwright

TARGETS = [u.strip() for u in os.getenv("TARGET_URLS", "https://www.kampojanechen.org/,https://www.kampojanechen.org/v2/Official/NewestSalePage").split(",") if u.strip()]
DB_NAME = os.getenv("DB_NAME", "gampo_public_monitor.db")
RUN_ID = f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db():
    c = sqlite3.connect(DB_NAME)
    c.execute("PRAGMA journal_mode=WAL;")
    return c


async def main():
    findings = []
    apis = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        for url in TARGETS:
            page = await context.new_page()
            captured = []
            async def on_resp(resp):
                ct = resp.headers.get("content-type", "")
                if "json" in ct.lower() or resp.request.resource_type in {"xhr", "fetch"}:
                    try:
                        body = await resp.json()
                        keys = list(body.keys())[:30] if isinstance(body, dict) else []
                        captured.append({"url": resp.url, "status": resp.status, "keys": keys, "size_hint": len(json.dumps(body, ensure_ascii=False))})
                    except Exception:
                        captured.append({"url": resp.url, "status": resp.status, "keys": [], "size_hint": -1})
            page.on("response", on_resp)
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            headers = await page.evaluate("""() => ({csp: document.querySelector('meta[http-equiv="Content-Security-Policy"]')?.content || null})""")
            hydration = await page.evaluate("""() => ({initial: !!window.__INITIAL_STATE__, nuxt: !!window.__NUXT__, apollo: !!window.__APOLLO_STATE__, redux: !!window.__REDUX_STATE__, nextData: !!window.__NEXT_DATA__})""")
            if any(hydration.values()):
                findings.append({"source_url": url, "finding_type": "hydration_exposed", "severity": "medium", "field_name": "hydration", "description": "Hydration state objects are exposed to frontend runtime.", "recommendation": "Review exposed payload and remove sensitive fields.", "evidence": json.dumps(hydration)})
            if not headers.get("csp"):
                findings.append({"source_url": url, "finding_type": "missing_csp_meta", "severity": "low", "field_name": "CSP", "description": "No CSP meta tag detected.", "recommendation": "Set strict CSP in HTTP headers.", "evidence": "meta not found"})
            apis.extend(captured)
            for a in captured:
                if any(k.lower().endswith("id") for k in a["keys"]):
                    findings.append({"source_url": a["url"], "finding_type": "internal_id_exposure", "severity": "low", "field_name": "id", "description": "JSON appears to expose id-like fields.", "recommendation": "Minimize internal identifiers when unnecessary.", "evidence": ",".join(a["keys"][:10])})
                if any("debug" in k.lower() for k in a["keys"]):
                    findings.append({"source_url": a["url"], "finding_type": "debug_field_exposure", "severity": "medium", "field_name": "debug", "description": "Potential debug fields exposed.", "recommendation": "Strip debug fields from production responses.", "evidence": ",".join(a["keys"][:10])})
                if a["size_hint"] > 500_000:
                    findings.append({"source_url": a["url"], "finding_type": "large_unpaged_json", "severity": "medium", "field_name": "payload_size", "description": "Large JSON payload observed.", "recommendation": "Add pagination/field filtering.", "evidence": str(a["size_hint"])})
            await page.close()
        for path in ["/robots.txt", "/sitemap.xml"]:
            for base in TARGETS:
                b = base.rstrip("/")
                full = f"{b}{path}"
                p = await context.new_page()
                r = await p.goto(full)
                findings.append({"source_url": full, "finding_type": "crawler_policy", "severity": "info", "field_name": path, "description": f"{path} checked.", "recommendation": "Ensure policy reflects intended crawling visibility.", "evidence": f"status={r.status if r else 'N/A'}"})
                await p.close()
        await browser.close()

    conn = db()
    conn.execute("CREATE TABLE IF NOT EXISTS defense_audit_findings (id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, run_id TEXT, source_url TEXT, finding_type TEXT, severity TEXT, field_name TEXT, description TEXT, recommendation TEXT, evidence TEXT)")
    for f in findings:
        conn.execute("INSERT INTO defense_audit_findings(captured_at,run_id,source_url,finding_type,severity,field_name,description,recommendation,evidence) VALUES (?,?,?,?,?,?,?,?,?)", (now_iso(), RUN_ID, f["source_url"], f["finding_type"], f["severity"], f["field_name"], f["description"], f["recommendation"], f["evidence"]))
    conn.commit()
    os.makedirs("data", exist_ok=True)
    report = {"run_id": RUN_ID, "captured_at": now_iso(), "targets": TARGETS, "api_count": len(apis), "api_samples": apis[:80], "findings": findings}
    with open("data/defense_audit_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    md = [f"# Defense Audit Report\n\n- Run ID: `{RUN_ID}`\n- Captured At: `{now_iso()}`\n- API Count: **{len(apis)}**\n"]
    md.append("## Findings\n")
    for f in findings:
        md.append(f"- **[{f['severity']}] {f['finding_type']}** @ {f['source_url']}\n  - Field: `{f['field_name']}`\n  - Description: {f['description']}\n  - Recommendation: {f['recommendation']}\n  - Evidence: `{f['evidence']}`\n")
    with open("data/defense_audit_report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md))

if __name__ == "__main__":
    asyncio.run(main())
