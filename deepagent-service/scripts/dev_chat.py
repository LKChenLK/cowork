"""直打 deepagent `/chat` 的開發用 chat client——不需要起 Java/前端。

模擬 backend 的跨輪簿記:自動維護 sessionId/history/previousDashboardHtml,把 CSV 排進
`uploads/` 佈局(resolve_source_path 的路徑形狀要求),SSE 事件即時印出,DASHBOARD_HTML
落地成檔案。狀態存在 `.dev-session/`(gitignored),`--new` 開新對話。

用法:
    uv run scripts/dev_chat.py --csv ~/data.csv "哪個系統最需要改善?"   # 首輪
    uv run scripts/dev_chat.py "改成圓餅圖"                             # 後續輪自動帶狀態
    uv run scripts/dev_chat.py --new --csv ~/other.csv "換一份資料"     # 重開 session

`/chat` 掛 bearer 驗證,token 預設與服務端同源(env `AGENT_API_BEARER_TOKEN` >
`one-local.properties`,都沒有則退回 compose 預設 `dev-agent-token`),`--token` 可覆寫。
ERROR 事件或連不上服務時 exit code 非 0。
"""

import argparse
import json
import os
import re
import shutil
import sys
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import httpx

SERVICE_ROOT = Path(__file__).resolve().parent.parent
# bearer token 解析與服務端同源(env > one-local.properties > 欄位預設)——對不上就是 401,
# 這裡刻意重用 app.config 而非另抄一套讀法。properties 路徑預設相對於 CWD,腳本可能從別的
# 目錄執行,所以先補成絕對路徑(已設 ONE_PROPERTIES_PATH 時尊重使用者設定)。
os.environ.setdefault("ONE_PROPERTIES_PATH", str(SERVICE_ROOT / "one-local.properties"))
sys.path.insert(0, str(SERVICE_ROOT))

# MUST 在上面的 sys.path/env 準備之後才 import。
from app.config import get_settings

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_STATE_DIR = SERVICE_ROOT / ".dev-session"
DEV_USER_ID = "dev-user"
# docker-compose.app.yml 的 AGENT_API_BEARER_TOKEN 預設值;env 與 properties 都沒設時的退路。
FALLBACK_BEARER_TOKEN = "dev-agent-token"

# 副檔名 → wire 上的 fileType。刻意不收 .xls:source_cache 只認 .csv/.parquet/.xlsx 三種
# resolved path,.xls 會走純複製路徑再被 duckdb 當 csv 讀而炸掉。
FILE_TYPE_BY_SUFFIX = {".csv": "csv", ".xlsx": "xlsx", ".parquet": "parquet"}

STEP_STATUS_MARKS = {"RUNNING": "⏳", "SUCCESS": "✅", "ERROR": "❌"}


def _alias_for(source_file: Path, occupied_aliases: set[str]) -> str:
    """檔名 stem 轉成可當 SQL 資料表名的 alias:小寫、非英數轉底線、開頭補字母。
    撞到已占用的 alias 時加 `_N` 後綴——同名衝突會讓 duckdb 的 CREATE TABLE 直接失敗。"""
    alias = re.sub(r"[^a-z0-9]+", "_", source_file.stem.lower()).strip("_")
    if not alias or not alias[0].isalpha():
        alias = f"t_{alias}" if alias else "data"
    if alias not in occupied_aliases:
        return alias
    suffix_number = 2
    while f"{alias}_{suffix_number}" in occupied_aliases:
        suffix_number += 1
    return f"{alias}_{suffix_number}"


def _load_state(state_dir: Path) -> dict[str, Any] | None:
    state_path = state_dir / "state.json"
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def _save_state(state_dir: Path, state: dict[str, Any]) -> None:
    (state_dir / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _stage_sources(
    state_dir: Path, session_id: str, csv_paths: list[str], occupied_aliases: set[str]
) -> list[dict[str, str]]:
    """把資料檔複製進含 `uploads` 段的佈局(鏡射 backend 給 deepagent 的路徑形狀)。
    `occupied_aliases` 是本 session 已用掉的 alias,逐檔就地更新以避開同批內的碰撞。"""
    sources: list[dict[str, str]] = []
    for raw_path in csv_paths:
        source_file = Path(raw_path).expanduser().resolve()
        if not source_file.exists():
            sys.exit(f"找不到資料檔: {source_file}")
        file_type = FILE_TYPE_BY_SUFFIX.get(source_file.suffix.lower())
        if file_type is None:
            sys.exit(f"不支援的副檔名: {source_file.suffix}(支援 csv/xlsx/parquet)")
        # uuid 前綴鏡射 backend 的 `uploads/{sessionId}/{uuid}_{name}`——同一 session 內重傳
        # 同名但內容已改的檔案時,source_cache 視上傳檔 immutable 會直接命中舊快取。
        staged_path = state_dir / "uploads" / session_id / f"{uuid.uuid4()}_{source_file.name}"
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, staged_path)
        alias = _alias_for(source_file, occupied_aliases)
        occupied_aliases.add(alias)
        sources.append({"alias": alias, "path": str(staged_path), "fileType": file_type})
    return sources


def _print_event(event: dict[str, Any], state_dir: Path) -> tuple[str | None, str | None]:
    """印出單一 wire 事件;回傳 (answer_text, dashboard_html) 中本事件產出的部分。"""
    event_type = event.get("type")
    if event_type == "STEP":
        mark = STEP_STATUS_MARKS.get(event.get("status", ""), "·")
        print(f"{mark} {event.get('title')}", flush=True)
    elif event_type == "TOKEN":
        print(event.get("delta", ""), end="", flush=True)
    elif event_type == "TABLE":
        rows = event.get("rows", [])
        print(f"▦ {event.get('tableId')} — {event.get('intent')}({len(rows)} rows)", flush=True)
    elif event_type == "QUESTION":
        print("\n❓ 模型反問:")
        for question in event.get("questions", []):
            options = "/".join(question.get("options", []))
            print(f"   - {question.get('text')}({options})")
    elif event_type == "ANSWER":
        answer_text = event.get("text", "")
        print(f"\n──── ANSWER ────\n{answer_text}")
        return answer_text, None
    elif event_type == "DASHBOARD_HTML":
        dashboard_html = event.get("html", "")
        dashboard_path = state_dir / "dashboard.html"
        dashboard_path.write_text(dashboard_html, encoding="utf-8")
        print(f"📊 dashboard 已更新 → {dashboard_path}", flush=True)
        return None, dashboard_html
    elif event_type == "ERROR":
        print(f"\n💥 ERROR [{event.get('code')}] {event.get('message')}")
    return None, None


def _stream_chat(
    base_url: str, payload: dict[str, Any], state_dir: Path, token: str
) -> tuple[str | None, str | None, bool]:
    """POST /chat 並即時消化 SSE;回傳 (最終 answer, 最終 dashboard html, 是否收到 ERROR)。"""
    answer_text: str | None = None
    dashboard_html: str | None = None
    saw_error = False
    timeout = httpx.Timeout(600.0, connect=10.0)
    headers = {"Authorization": f"Bearer {token}"}
    with (
        httpx.Client(timeout=timeout, trust_env=False) as client,
        client.stream("POST", f"{base_url}/chat", json=payload, headers=headers) as response,
    ):
        # 不用 raise_for_status:401 的 traceback 看不出是 token 不對,直接把 body 印出來。
        if response.status_code != 200:
            response.read()
            sys.exit(f"HTTP {response.status_code}: {response.text}")
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue  # comment ping/空行
            event = json.loads(line[len("data:") :].strip())
            event_answer, event_dashboard = _print_event(event, state_dir)
            answer_text = event_answer or answer_text
            dashboard_html = event_dashboard or dashboard_html
            saw_error = saw_error or event.get("type") == "ERROR"
    return answer_text, dashboard_html, saw_error


def main() -> None:
    parser = argparse.ArgumentParser(description="直打 deepagent /chat 的開發用 client")
    parser.add_argument("message", help="這一輪要對 agent 說的話")
    parser.add_argument(
        "--csv", action="append", default=[], help="資料檔路徑(csv/xlsx,可重複;首輪必填)"
    )
    parser.add_argument("--new", action="store_true", help="放棄現有 session 重新開始")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="deepagent 服務位址")
    parser.add_argument(
        "--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="session 狀態資料夾"
    )
    parser.add_argument("--open", action="store_true", help="本輪結束後用瀏覽器開 dashboard")
    parser.add_argument(
        "--token", help="bearer token(預設取 AGENT_API_BEARER_TOKEN:env > one-local.properties)"
    )
    args = parser.parse_args()

    token = args.token or get_settings().AGENT_API_BEARER_TOKEN or FALLBACK_BEARER_TOKEN

    state_dir: Path = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    state = None if args.new else _load_state(state_dir)
    if state is None:
        if not args.csv:
            sys.exit("首輪(或 --new)必須用 --csv 指定至少一個資料檔")
        session_id = f"dev-{uuid.uuid4().hex[:8]}"
        state = {
            "sessionId": session_id,
            "sources": _stage_sources(state_dir, session_id, args.csv, set()),
            "history": [],
        }
        dashboard_file = state_dir / "dashboard.html"
        if dashboard_file.exists():
            dashboard_file.unlink()
        print(
            f"🆕 新 session: {session_id}(sources: "
            f"{', '.join(source['alias'] for source in state['sources'])})"
        )
    elif args.csv:
        state["sources"].extend(
            _stage_sources(
                state_dir,
                state["sessionId"],
                args.csv,
                {source["alias"] for source in state["sources"]},
            )
        )
        print(
            f"➕ 加入資料檔,sources 現有: "
            f"{', '.join(source['alias'] for source in state['sources'])}"
        )

    payload: dict[str, Any] = {
        "sessionId": state["sessionId"],
        "userId": DEV_USER_ID,
        "message": args.message,
        "history": state["history"],
        "sources": state["sources"],
    }
    dashboard_file = state_dir / "dashboard.html"
    if dashboard_file.exists():
        payload["previousDashboardHtml"] = dashboard_file.read_text(encoding="utf-8")

    try:
        answer_text, dashboard_html, saw_error = _stream_chat(
            args.base_url, payload, state_dir, token
        )
    except httpx.ConnectError:
        sys.exit(f"連不上 {args.base_url}——deepagent 起了嗎?(uv run uvicorn app.main:app)")

    if answer_text is None:
        print("\n(本輪沒有 ANSWER——多半以 ERROR 收場,history 不推進)")
        sys.exit(1)

    state["history"].append({"role": "user", "text": args.message})
    state["history"].append({"role": "assistant", "text": answer_text})
    _save_state(state_dir, state)

    if args.open and dashboard_html is not None:
        webbrowser.open(dashboard_file.resolve().as_uri())
    # ERROR 事件即使後面補了 ANSWER 也算這輪失敗——非 0 才串得進其他腳本。
    if saw_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
