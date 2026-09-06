# deepagent-service

**analysis 線的分析服務**（`ERD_AGENT_PROVIDER=langgraph-analysis` 時由 Java 端呼叫）。基於
[deepagents](https://github.com/langchain-ai/deepagents) harness 的 `create_deep_agent`——單一
agent + skills 漸進揭露 + 內建 planning/檔案工具——模型用 DuckDB 工具自行查資料，再直寫
self-contained HTML dashboard。目標模型 **qwen3.6-35B**。

與 llm api 線最大的差別在**資料怎麼進 HTML**：模型用 SQL 把統計算好，只把**被引用到的查詢結果**
注入 `window.__ERD_RESULTS__`，瀏覽器只做笨渲染（不像 llm api 線注入全量原始資料、由瀏覽器 JS 現算）。
文字結論與圖表數字因此同源，結構性消除抄錯。

圖表知識放在 **skills**（`skills/dashboard/`，漸進揭露），不是硬塞進 system prompt。

完整設計脈絡見 spec：
[`docs/superpowers/specs/2026-07-29-deepagent-dashboard-design.md`](../docs/superpowers/specs/2026-07-29-deepagent-dashboard-design.md)。

Java 端 `LangGraphAnalysisProvider` 只認 `/chat`（SSE）與 `/health` 兩個端點的 wire 契約，
`ERD_AGENT_ANALYSIS_BASE_URL` 指向哪個 host 就連哪個。`engine/` 層（`duck.py`、`theme.py` 等）
刻意是**複本而非共用套件**，與 backend 對應檔案同步，見各檔頭的 `MUST-sync` 註解。

## 前置需求

| 要裝 | 版本 | 說明 |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 最新 | 唯一需要裝的 Python 工具 |
| ~~Python / pip~~ | **不用另外裝** | uv 自行管理 Python（`pyproject.toml` 的 `requires-python = ">=3.11"`；容器用 `python:3.11-slim`） |

⚠️ **所有 Python 指令一律走 `uv run`，NEVER 用 `pip install`**——相依由 `pyproject.toml` + `uv.lock`
鎖定（皆已進版控），只有 `uv sync` 會裝到正確版本。驗證：`uv --version`。

## 啟動 ①：本機直跑（localhost，建議）

首次安裝：

```bash
cd deepagent-service && uv sync
```

**建議用 `--env-file` 直接吃 `.env.local`**（repo 根目錄那份，本機開發專用）：

```bash
cd deepagent-service
uv run --env-file ../.env.local fastapi dev --port 8000 --reload-dir app
```

> `--reload-dir app` 把 auto-reload 的監看範圍限縮在 `app/` 原始碼——沒有它，agent 往
> workspace（如 `.local-workspace/`）寫出任何 `.py` 檔都會觸發 reload，殺掉進行中的 run。

也可以逐項指定（會覆蓋 `--env-file` 的值，適合臨時換模型試）：

```bash
cd deepagent-service
OPENAI_BASE_URL=https://openrouter.ai/api/v1 \
OPENAI_API_KEY=<your-openrouter-key> \
AGENT_MODEL=<OpenRouter 上的 qwen3.6-35b model id> \
AGENT_WORKSPACE_ROOT=/tmp/deepagent-workspace \
uv run fastapi dev --port 8000 --reload-dir app
```

> `OPENAI_BASE_URL` **必須含 `/v1` 後綴**（Python openai SDK 慣例）——與 Java 端的
> `ERD_AGENT_OPENAI_COMPATIBLE_BASE_URL`（**不含** `/v1`）格式不可互換。

服務會在 `http://localhost:8000` 起來，`/health` 應回 `{"status": "ok"}`。

再讓**本機直跑的 backend** 連上（`local` profile 會載入 `.env.local`）：

```bash
cd backend
ERD_AGENT_PROVIDER=langgraph-analysis \
ERD_AGENT_ANALYSIS_BASE_URL=http://localhost:8000 \
./mvnw spring-boot:run -Dspring-boot.run.profiles=local
```

> **疑難排解**：啟動前先 `lsof -ti:8000` 確認埠淨空——若埠被舊實例佔住，`fastapi dev` 只會在
> log 裡留下 `Address already in use`，而 `/health` 仍會由**舊代碼**回 200，測起來像新改動沒生效。

## 啟動 ②：docker（app stack 的 `deepagent` profile）

compose 會自動載入 repo 根目錄的 `.env`（docker 專用那份），不需要 `--env-file`：

```bash
docker compose -f docker-compose.app.yml \
  --profile deepagent up -d --build deepagent-service
```

再讓 backend 連上（改 `.env`）——注意 host 是**服務名**不是 localhost：

```bash
ERD_AGENT_PROVIDER=langgraph-analysis
ERD_AGENT_ANALYSIS_BASE_URL=http://deepagent-service:8000
```

其餘環境變數（`AGENT_MODEL`、`LANGFUSE_*` 等）見 `docker-compose.app.yml` 的
`deepagent-service` service 定義；`DEEPAGENT_MODEL` 可覆寫預設模型（`qwen3.6-35b`）。

## Workspace 佈局

`$AGENT_WORKSPACE_ROOT`（docker 內固定 `/data/workspace`，掛 named volume
`deepagent-workspace`；本機直跑用任意路徑）下，每個 user 一個目錄：

```
{userId}/
  skills/                       # 該 user 的個人 skill（M3 蒸餾產物落點；load 邏輯第一天就支援，
                                 # 蒸餾動作本身留 M3）
  sessions/
    {sessionId}/
      queries/{query_id}.sql    # 每次 run_sql 的 SQL 原文
      results/{query_id}.json   # 對應查詢結果（columns、以欄名為 key 的物件列 rows、truncated）
      dashboard.html            # 模型直寫的 self-contained dashboard（迭代一律整份 write_file 重寫）
      sources.md                # 本 turn 可用的資料來源（alias + fileType，供模型讀）
      notes.md                  # 模型自行維護的分析筆記（deepagents 內建檔案工具）
      todo.md                   # deepagents 內建 planning（write_todos）
      .skills/                  # 每 turn 重新 stage：builtin skills/ + {userId}/skills/（後者覆寫前者）
```

實際落地路徑另包一層 write-once generation 快照（`gen-{epochMillis}-{hex}/`）——
`local`／`s3`（`STORAGE_BACKEND`）兩種後端現在共用同一套機制，只差底層物件 client；完整位址
與 turn 生命週期見 `docs/architecture.md`「deepagent-service Workspace：檔案地圖與 Turn 生命週期」節。

`dashboard.html` 是否要發 `DASHBOARD_HTML` 事件，由 turn 結束時檔案 mtime 是否變動決定——
**不靠模型自己宣告**（見 `app/agent/chat_turn.py` 的 `ChatTurn` 內 mtime 快照比對）。

送出前沒有驗證關卡——只做主題改寫（`apply_erd_theme`，`app/engine/theme_rewrite.py`）與結果注入
（`inject_results`，物件列外包一層 Proxy，未知欄名/index 存取直接 throw）。真正的品質防線是
使用者觸發的瀏覽器修復（`POST /repair`），詳見 `docs/architecture.md`「deepagent-service
品質防線（注入契約 + 瀏覽器修復）」節。

## 手動測 `/chat`（不用 backend／frontend）

`scripts/dev_chat.py` 直接對本服務的 `/chat`（SSE）打一輪對話，事件即時印出、`DASHBOARD_HTML`
落地成檔案。服務照上面任一種方式起好即可，不需要 backend、前端或 Mongo。

```bash
cd deepagent-service
uv run scripts/dev_chat.py --csv ~/data/sales.csv "哪個月最需要改善?"   # 首輪（--csv 可重複）
uv run scripts/dev_chat.py "改成長條圖" --open                          # 續問，畫完用瀏覽器開
uv run scripts/dev_chat.py --new --csv ~/other.csv "換一份資料"         # 重開 session
```

必填的只有 message。sessionId／history／sources／`previousDashboardHtml` 由腳本自動跨輪維護
（狀態存 `.dev-session/`，gitignored），模擬 backend 的簿記；bearer token 與服務端同源解析
（env `AGENT_API_BEARER_TOKEN` > `one-local.properties`，都沒設則退回 compose 預設
`dev-agent-token`），`--token` 可覆寫。`--csv` 的複製目的地帶 `uploads/` 路徑段是硬性要求——
local 模式下 `app/engine/source_cache.py` 的快取 key 就是從該段起算。

只涵蓋 `/chat`；`/repair` 不在此腳本範圍。

## 測試

```bash
cd deepagent-service
uv run pytest -q       # 單元 + 事件橋接 + FastAPI 契約測試（fake chat model，不打真 LLM）
uv run ruff check .    # 含 engine/ 層 banned-api 檢查（禁止 import langchain*/langgraph/deepagents）
```
