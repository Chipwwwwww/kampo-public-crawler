# Universal Intelligence Striker (Public Data Edition)

這是一套高階商業情報強襲系統（公開資料版），透過前端動態渲染觀測與公開 API 攔截，將網站公開庫存消耗轉化為「預估真實營收 (Estimated Revenue)」與實時訂單流水。

> 僅針對目標網站**主動提供給前端**的公開資料做分析，不包含 SQL injection、密碼破解、權限繞過。

## 功能
- Playwright asyncio 監控公開頁面與 XHR/Fetch JSON。
- Hydration state 抽取：`__INITIAL_STATE__`, `__NUXT__`, `__APOLLO_STATE__`, `__REDUX_STATE__`, `__PRELOADED_STATE__`, `__NEXT_DATA__`, `ld+json`。
- DOM fallback 抽取商品/內容卡。
- SQLite (WAL mode) 儲存快照、事件、稽核日誌。
- 當公開數量下降時，輸出即時訂單提示與 Estimated Revenue。
- Defense Audit：公開 API 面、key shape、可能敏感欄位暴露、robots/sitemap、基本 header 訊號。

## PowerShell 執行
```powershell
./run_monitor.ps1
./run_defense_audit.ps1
```
可覆蓋目標：
```powershell
$env:TARGET_URLS = "https://site1.com/,https://site2.com/products"
```

## GitHub Actions
`.github/workflows/crawler.yml`
- `workflow_dispatch` 手動觸發。
- 每 6 小時跑 monitor。
- 每天跑 defense audit。
- 自動 commit `data/*.json`, `data/*.md`, `gampo_public_monitor.db`。

## Vercel Dashboard
`index.html` 為純靜態頁，可直接部署；會讀取：
- `data/latest_summary.json`
- `data/defense_audit_report.json`

## SQLite 資料表
- `entity_snapshots`
- `entity_latest`
- `dom_entity_snapshots`
- `public_quantity_events`
- `response_audit_log`
- `extract_rejections`
- `run_summary`
- `defense_audit_findings`

## 環境變數
- `TARGET_URLS`
- `HEADLESS`
- `DB_NAME`
- `MAX_BATCH_SIZE`
- `REQUEST_DELAY_MIN_SECONDS`
- `REQUEST_DELAY_MAX_SECONDS`
- `MAX_RETRIES`
