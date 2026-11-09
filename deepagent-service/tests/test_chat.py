import json

import duckdb
import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage

from app import main as main_module
from app.agent import chat_turn
from app.agent.events import EventBridge
from app.agent.tools.recording import ToolResultRecorder
from app.api.events import ErrorEvent
from app.engine.workspace import WorkspacePersistError
from app.engine.workspace_store import build_workspace_store
from tests.conftest import TEST_BEARER_TOKEN
from tests.fake_model import FailingChatModel, ScriptedChatModel


def _sse_events(raw_body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in raw_body.splitlines()
        if line.startswith("data: ")
    ]


DASHBOARD_HTML_CONTENT = (
    '<html><head><script src="https://cdn.tailwindcss.com"></script></head>'
    '<body><div id="c"></div><script>'
    'const table = window.__ERD_RESULTS__["q1"];'
    "const chart = echarts.init(document.getElementById('c'), 'erd');"
    "chart.setOption({ tooltip: { trigger: 'axis' } });"
    "</script></body></html>"
)

BROKEN_DASHBOARD_HTML_CONTENT = (
    '<html><head><script src="https://cdn.tailwindcss.com"></script></head>'
    '<body><div id="c"></div><script>'
    'const table = window.__ERD_RESULTS__["q9"];'
    "echarts.init(document.getElementById('c'), 'erd');"
    "</script></body></html>"
)

# 模擬 Java 端 LangGraphAnalysisProvider 帶來的「選定歷史版本」rawHtml -- 已是本服務先前
# 注入過的 artifact(含帶 id 的 __ERD_RESULTS__/主題 script),外加一個可識別的標記字串
# version-marker-v2,用來驗證進場基底重建確實剝掉了注入區塊、又確實沿用了該版內容。
PREVIOUS_VERSION_DASHBOARD_HTML_CONTENT = (
    '<html><head><script src="https://cdn.tailwindcss.com"></script>'
    '<script id="erd-results-data">window.__ERD_RESULTS__ = {"q1": '
    '{"columns": ["system"], "rows": [["CRM"]], "truncated": false}};</script>'
    '<script id="erd-theme">(function(){registerErdTheme();})();</script>'
    "</head><body>"
    '<div id="version-marker-v2">v2 content</div>'
    '<div id="c"></div><script>'
    'const table = window.__ERD_RESULTS__["q1"];'
    "const chart = echarts.init(document.getElementById('c'), 'erd');"
    "chart.setOption({ tooltip: { trigger: 'axis' } });"
    "</script></body></html>"
)

# 模擬模型整份重寫 dashboard.html 這輪的產出——沿用進場基底重建剝掉注入區塊後的內容,只是
# 這次透過 write_file 整份送出,標記字串本身被更新過,
# 用來驗證基底沿用(而非從零重寫)這件事本身,與模型改用哪個工具無關。
PREVIOUS_VERSION_DASHBOARD_HTML_REWRITTEN_CONTENT = (
    '<html><head><script src="https://cdn.tailwindcss.com"></script>'
    "</head><body>"
    '<div id="version-marker-v2">v2 content updated</div>'
    '<div id="c"></div><script>'
    'const table = window.__ERD_RESULTS__["q1"];'
    "const chart = echarts.init(document.getElementById('c'), 'erd');"
    "chart.setOption({ tooltip: { trigger: 'axis' } });"
    "</script></body></html>"
)


def _skill_read_step() -> AIMessage:
    """DashboardSkillGateMiddleware 擋掉未讀過 skill 的 dashboard.html write_file/edit_file——
    必讀清單是動態掃描整個 `.skills/builtin/dashboard` 資料夾底下的 `.md`,每個腳本需要
    在寫檔前補這一步(讀 SKILL.md + references/ 底下全部 `.md`)才能通過 gate。回傳新實例
    (不共用同一個物件),避免多個腳本重用同一個 AIMessage.id。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_file",
                "id": "read-skill",
                "args": {"file_path": ".skills/builtin/dashboard/SKILL.md", "limit": 1000},
            },
            {
                "name": "read_file",
                "id": "read-examples",
                "args": {
                    "file_path": ".skills/builtin/dashboard/references/examples.md",
                    "limit": 1000,
                },
            },
            {
                "name": "read_file",
                "id": "read-html-contract",
                "args": {
                    "file_path": ".skills/builtin/dashboard/references/html-contract.md",
                    "limit": 1000,
                },
            },
            {
                "name": "read_file",
                "id": "read-chart-rules",
                "args": {
                    "file_path": ".skills/builtin/dashboard/references/chart-rules.md",
                    "limit": 1000,
                },
            },
        ],
    )


@pytest.fixture()
def scripted_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scripted = ScriptedChatModel(
        [
            _skill_read_step(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_sql",
                        "id": "call1",
                        "args": {
                            "sql": "SELECT system, COUNT(*) AS tickets FROM orders GROUP BY system",
                            "intent": "各系統工單數",
                        },
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "id": "call2",
                        "args": {"file_path": "dashboard.html", "content": DASHBOARD_HTML_CONTENT},
                    }
                ],
            ),
            AIMessage(content="CRM 系統工單最多,最需要改善。"),
        ]
    )
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)
    return scripted


@pytest.fixture()
def scripted_flow_dashboard_updated_empty_answer(tmp_path, monkeypatch):
    """儀表板成功寫入,但模型最終一輪沒有文字說明(content="")——此時 ANSWER 應為
    DASHBOARD_UPDATED_FALLBACK_MESSAGE,不是誤導性的 EMPTY_ANSWER_FALLBACK_MESSAGE
    (工作明明成功了)。"""
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scripted = ScriptedChatModel(
        [
            _skill_read_step(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_sql",
                        "id": "call1",
                        "args": {
                            "sql": "SELECT system, COUNT(*) AS tickets FROM orders GROUP BY system",
                            "intent": "各系統工單數",
                        },
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "id": "call2",
                        "args": {"file_path": "dashboard.html", "content": DASHBOARD_HTML_CONTENT},
                    }
                ],
            ),
            AIMessage(content=""),
        ]
    )
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)
    return scripted


@pytest.fixture()
def scripted_flow_dashboard_references_missing_query_id(tmp_path, monkeypatch):
    """dashboard.html 引用不存在的 query id(q9,run_sql 只產生了 q1)——舊版 guard 會擋下這份
    輸出並觸發修復迴圈;guard 移除後直接出貨,被引用但不存在的結果單純不會出現在注入內容裡。"""
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scripted = ScriptedChatModel(
        [
            _skill_read_step(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_sql",
                        "id": "call1",
                        "args": {
                            "sql": "SELECT system, COUNT(*) AS tickets FROM orders GROUP BY system",
                            "intent": "各系統工單數",
                        },
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "id": "call2",
                        "args": {
                            "file_path": "dashboard.html",
                            "content": BROKEN_DASHBOARD_HTML_CONTENT,
                        },
                    }
                ],
            ),
            AIMessage(content="CRM 系統工單最多,最需要改善。"),
        ]
    )
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)
    return scripted


@pytest.fixture()
def scripted_flow_previous_version(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scripted = ScriptedChatModel(
        [
            _skill_read_step(),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_sql",
                        "id": "call1",
                        "args": {
                            "sql": "SELECT system, COUNT(*) AS tickets FROM orders GROUP BY system",
                            "intent": "各系統工單數",
                        },
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "id": "call2",
                        "args": {
                            "file_path": "dashboard.html",
                            "content": PREVIOUS_VERSION_DASHBOARD_HTML_REWRITTEN_CONTENT,
                        },
                    }
                ],
            ),
            AIMessage(content="已依歷史版本基底更新完成。"),
        ]
    )
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)
    return scripted


async def _post_chat(
    tmp_path,
    previous_dashboard_html: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> list[dict]:
    # local 模式 resolve_source_path 現在要求路徑含 "uploads" 段(鏡射 backend 實際給的路徑
    # 形狀)——放在 uploads/ 子目錄下,而非直接丟在 tmp_path 根目錄。
    csv_path = tmp_path / "uploads" / "sess-1" / "orders.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("system\nCRM\nCRM\nERP\n", encoding="utf-8")
    payload = {
        "sessionId": "sess-1",
        "userId": "user-1",
        "message": "哪個系統最需要改善?",
        "history": [],
        "sources": [{"alias": "orders", "path": str(csv_path), "fileType": "csv"}],
    }
    if previous_dashboard_html is not None:
        payload["previousDashboardHtml"] = previous_dashboard_html
    headers = {"Authorization": f"Bearer {TEST_BEARER_TOKEN}"}
    if extra_headers is not None:
        headers.update(extra_headers)
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=headers,
    ) as client:
        response = await client.post("/chat", json=payload)
    return _sse_events(response.text)


async def test_chat_full_flow_emits_contracted_events(tmp_path, scripted_flow) -> None:
    events = await _post_chat(tmp_path)
    types = [event["type"] for event in events]

    assert "STEP" in types and "TABLE" in types
    dashboard_events = [event for event in events if event["type"] == "DASHBOARD_HTML"]
    assert len(dashboard_events) == 1
    assert "window.__ERD_RESULTS__" in dashboard_events[0]["html"]  # 結果已注入
    # 主題注入現由 Java 後端負責,deepagent 輸出不含 registerTheme。
    assert "registerTheme('erd'" not in dashboard_events[0]["html"]
    assert events[-1] == {"type": "ANSWER", "text": "CRM 系統工單最多,最需要改善。"}


async def test_chat_forwards_sso_headers_into_request_context(
    tmp_path, scripted_flow, monkeypatch
) -> None:
    """main.py 的 /chat handler 依 `Settings.SSO_TOKEN_HEADER`/`SSO_URL_HEADER`(預設
    X-SSO-Token/X-SSO-Url)從 `Request.headers` 讀值,以 keyword-only 參數轉呼叫
    `ChatTurn` —— 驗證這兩個 header 值確實流進 `set_request_identity`,而非被忽略或改走
    ChatRequest body(schemas.py 已不含 ssoToken 欄位)。"""
    captured: dict[str, str | None] = {}
    original_set_request_identity = chat_turn.set_request_identity

    def spy_set_request_identity(user_id, session_id, sso_token=None, sso_url=None):
        captured["sso_token"] = sso_token
        captured["sso_url"] = sso_url
        return original_set_request_identity(user_id, session_id, sso_token, sso_url)

    monkeypatch.setattr(chat_turn, "set_request_identity", spy_set_request_identity)

    await _post_chat(
        tmp_path,
        extra_headers={
            "X-SSO-Token": "header-token",
            "X-SSO-Url": "https://sso.example/auth",
        },
    )

    assert captured == {"sso_token": "header-token", "sso_url": "https://sso.example/auth"}


async def test_chat_without_sso_headers_passes_none_through(
    tmp_path, scripted_flow, monkeypatch
) -> None:
    captured: dict[str, str | None] = {}
    original_set_request_identity = chat_turn.set_request_identity

    def spy_set_request_identity(user_id, session_id, sso_token=None, sso_url=None):
        captured["sso_token"] = sso_token
        captured["sso_url"] = sso_url
        return original_set_request_identity(user_id, session_id, sso_token, sso_url)

    monkeypatch.setattr(chat_turn, "set_request_identity", spy_set_request_identity)

    await _post_chat(tmp_path)

    assert captured == {"sso_token": None, "sso_url": None}


async def test_chat_event_payloads_pin_exact_wire_contract_keys(
    tmp_path, scripted_flow, monkeypatch
) -> None:
    """Java's LangGraphAnalysisProvider deserializes these dicts with Jackson
    @JsonSubTypes -- an added or renamed key breaks that, and a subset check would miss
    an added key entirely. Asserts an exact key set (and value types) per event type,
    for every event type this scripted flow can actually produce, plus ERROR driven
    separately via FailingChatModel. TOKEN is not covered: no fixture drives text
    streaming before the first tool call, so EventBridge never emits one (see
    app/agent/events.py's `_handle_chat_model_stream` -- forwarding stops once
    `tool_started` flips true, and every scripted AIMessage here starts with a tool
    call and empty content).
    """
    events = await _post_chat(tmp_path)

    step_events = [event for event in events if event["type"] == "STEP"]
    assert step_events
    for event in step_events:
        assert set(event.keys()) == {"type", "stepKey", "title", "status"}
        assert isinstance(event["stepKey"], str)
        assert isinstance(event["title"], str)
        assert event["status"] in ("RUNNING", "SUCCESS", "ERROR")

    table_events = [event for event in events if event["type"] == "TABLE"]
    assert table_events
    for event in table_events:
        assert set(event.keys()) == {
            "type",
            "tableId",
            "intent",
            "columns",
            "rows",
            "truncated",
        }
        assert isinstance(event["tableId"], str)
        assert isinstance(event["intent"], str)
        assert isinstance(event["columns"], list)
        assert isinstance(event["rows"], list)
        assert isinstance(event["truncated"], bool)

    dashboard_events = [event for event in events if event["type"] == "DASHBOARD_HTML"]
    assert dashboard_events
    for event in dashboard_events:
        assert set(event.keys()) == {"type", "html"}
        assert isinstance(event["html"], str)

    answer_events = [event for event in events if event["type"] == "ANSWER"]
    assert answer_events
    for event in answer_events:
        assert set(event.keys()) == {"type", "text"}
        assert isinstance(event["text"], str)

    # Same session, same workspace -- switch the model to drive the ERROR path too, so
    # this one test pins every event type the wire contract defines except TOKEN.
    monkeypatch.setattr(chat_turn, "build_model", lambda: FailingChatModel())
    error_flow_events = await _post_chat(tmp_path)
    error_events = [event for event in error_flow_events if event["type"] == "ERROR"]
    assert error_events
    for event in error_events:
        assert set(event.keys()) == {"type", "code", "message"}
        assert isinstance(event["code"], str)
        assert isinstance(event["message"], str)


async def test_chat_dashboard_file_persisted_in_workspace(tmp_path, scripted_flow) -> None:
    await _post_chat(tmp_path)
    # local 模式現在走 WorkspaceStore 的 generation 快照佈局(非直寫 session 目錄)——
    # 用 build_workspace_store().prepare() 拉回最新完整 generation 來驗證確實有持久化,
    # 這正是下一輪 /chat 或 /repair 實際會拿到的內容,比直接戳 generation 目錄結構更貼近意圖。
    workspace = build_workspace_store().prepare("user-1", "sess-1")
    assert workspace.dashboard_path.is_file()
    assert (workspace.queries_dir / "q1.sql").is_file()
    assert (workspace.results_dir / "q1.json").is_file()


async def test_chat_dashboard_referencing_missing_query_id_still_ships(
    tmp_path, scripted_flow_dashboard_references_missing_query_id
) -> None:
    """沒有 guard 層可退貨——DASHBOARD_HTML 照樣發出,主題改寫與結果注入都跑過,只是被引用
    但不存在的 q9 單純不出現在注入的 __ERD_RESULTS__ 內容裡。"""
    events = await _post_chat(tmp_path)

    assert not [
        event
        for event in events
        if event["type"] == "STEP" and event.get("stepKey") == "dashboard_guard"
    ]
    dashboard_events = [event for event in events if event["type"] == "DASHBOARD_HTML"]
    assert len(dashboard_events) == 1
    assert '"q9":' not in dashboard_events[0]["html"]
    assert 'window.__ERD_RESULTS__["q9"]' in dashboard_events[0]["html"]

    answer_events = [event for event in events if event["type"] == "ANSWER"]
    assert answer_events[0] == {"type": "ANSWER", "text": "CRM 系統工單最多,最需要改善。"}


async def test_chat_previous_dashboard_html_becomes_editing_base(
    tmp_path, scripted_flow_previous_version, monkeypatch
) -> None:
    # 用 spy 包住 app/main.py 進場基底重建呼叫的 strip_injected_blocks,直接耦合到那段程式碼
    # 本身。這輪的模型腳本改用 write_file 整份重寫 dashboard.html 之後,workspace 檔案最終
    # 內容已經不能反推「entry-rebuild 真的執行過」——就算整段 entry-rebuild 邏輯被刪掉,
    # 模型腳本裡 hardcode 的 write_file content 一樣能讓「檔案內容含標記、缺注入區塊」這種
    # 事後斷言通過。spy 直接斷言 entry-rebuild 呼叫過 strip_injected_blocks、輸入是這輪的
    # previousDashboardHtml,這段邏輯被刪掉時 spy 從未被呼叫,測試就會失敗。
    entry_rebuild_calls: list[tuple[str, str]] = []
    original_strip_injected_blocks = chat_turn.strip_injected_blocks

    def spy_strip_injected_blocks(html: str) -> str:
        stripped = original_strip_injected_blocks(html)
        entry_rebuild_calls.append((html, stripped))
        return stripped

    monkeypatch.setattr(chat_turn, "strip_injected_blocks", spy_strip_injected_blocks)

    events = await _post_chat(
        tmp_path, previous_dashboard_html=PREVIOUS_VERSION_DASHBOARD_HTML_CONTENT
    )

    # (a) 進場基底重建 MUST 真的呼叫 strip_injected_blocks(previousDashboardHtml),輸出剝掉
    # 帶 id 的 results 注入區塊、留著標記字串——這就是寫進 workspace 當這輪編輯基底的內容。
    # 主題注入/剝除現由 Java 後端負責,deepagent 端的 strip 不再處理 erd-theme,故該區塊
    # 原樣保留。
    assert len(entry_rebuild_calls) == 1
    entry_rebuild_input, entry_rebuild_output = entry_rebuild_calls[0]
    assert entry_rebuild_input == PREVIOUS_VERSION_DASHBOARD_HTML_CONTENT
    assert 'id="version-marker-v2"' in entry_rebuild_output
    assert 'id="erd-results-data"' not in entry_rebuild_output
    assert 'id="erd-theme"' in entry_rebuild_output

    workspace = build_workspace_store().prepare("user-1", "sess-1")
    workspace_dashboard_html = workspace.dashboard_path.read_text(encoding="utf-8")
    assert 'id="version-marker-v2"' in workspace_dashboard_html
    assert 'id="erd-results-data"' not in workspace_dashboard_html
    assert 'id="erd-theme"' not in workspace_dashboard_html

    # (b) 最終送出的 DASHBOARD_HTML 事件仍帶著同一個標記字串 -- 模型整份重寫 dashboard.html,
    # 但保留了基底標記,證明基底確實被沿用,不是憑空生出全新內容。
    dashboard_events = [event for event in events if event["type"] == "DASHBOARD_HTML"]
    assert len(dashboard_events) == 1
    assert 'id="version-marker-v2"' in dashboard_events[0]["html"]


async def test_chat_without_previous_dashboard_html_unaffected(tmp_path, scripted_flow) -> None:
    # (c) 不帶 previousDashboardHtml 的既有流程不受影響 -- 沿用既有 fixture 重跑一次全流程斷言。
    events = await _post_chat(tmp_path)
    dashboard_events = [event for event in events if event["type"] == "DASHBOARD_HTML"]
    assert len(dashboard_events) == 1
    assert events[-1] == {"type": "ANSWER", "text": "CRM 系統工單最多,最需要改善。"}


async def test_chat_dashboard_updated_with_empty_final_text_uses_dashboard_fallback(
    tmp_path, scripted_flow_dashboard_updated_empty_answer
) -> None:
    events = await _post_chat(tmp_path)

    dashboard_events = [event for event in events if event["type"] == "DASHBOARD_HTML"]
    assert len(dashboard_events) == 1
    assert events[-1] == {
        "type": "ANSWER",
        "text": chat_turn.DASHBOARD_UPDATED_FALLBACK_MESSAGE,
    }


# -- STREAM_RETRY_MAX_RUNS / _is_transient_stream_error(Task「串流斷線 turn 級自動重試」)------


def _history_seed_request(role: str) -> main_module.ChatRequest:
    return main_module.ChatRequest(
        sessionId="sess-history",
        userId="user-1",
        message="second question",
        history=[main_module.HistoryItem(role=role, text="previous turn text")],
    )


def test_seed_messages_role_assistant_produces_ai_message() -> None:
    """Java 端 LangGraphAnalysisProvider 一律送 `\"assistant\"`(OpenAI 角色詞彙),從未送
    `\"AI\"`——這是實際 wire 上會發生的情況,MUST 重建成 AIMessage,否則每次 checkpoint 缺失
    時整段 AI 歷史都被誤植成 HumanMessage。"""
    messages = chat_turn._seed_messages(_history_seed_request("assistant"))
    assert isinstance(messages[0], AIMessage)
    assert messages[0].content == "previous turn text"


def test_seed_messages_role_user_produces_human_message() -> None:
    messages = chat_turn._seed_messages(_history_seed_request("user"))
    assert isinstance(messages[0], HumanMessage)


def test_seed_messages_sources_changed_note_appended_to_current_turn_message() -> None:
    request = _history_seed_request("user")
    messages = chat_turn._seed_messages(request, "\n\n(System note: sources changed.)")
    assert messages[-1].content == "second question\n\n(System note: sources changed.)"


def test_seed_messages_without_sources_changed_note_unaffected() -> None:
    request = _history_seed_request("user")
    messages = chat_turn._seed_messages(request, None)
    assert messages[-1].content == "second question"


# -- mid-session 上傳/刪除/換版本來源檔的 sources-changed system note --------------------
#
# checkpoint 已存在時(第二輪起)模型記憶還卡著舊的 get_schema 結果,不會自動感知來源已變
# ——ChatTurn.__aenter__ 在連線鎖門後建本輪 manifest(含 DESCRIBE 出的 schema)、跟上一輪存的
# manifest(`.sources-manifest.json`,隨 generation 快照跨輪持久化)做 diff,有差異就經
# _seed_messages 把提示附加進本輪的 HumanMessage。這裡 spy `chat_turn._seed_messages`,直接
# 檢查它實際組出的訊息內容,不必讓模型做任何有意義的事——最貼近「模型真的會看到什麼」這件事
# 本身。


async def test_chat_second_turn_gained_alias_includes_sources_changed_note(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scripted = ScriptedChatModel(
        [AIMessage(content="第一輪回答。"), AIMessage(content="第二輪回答。")]
    )
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)

    captured_message_batches: list[list] = []
    original_seed_messages = chat_turn._seed_messages

    def spy_seed_messages(request, sources_changed_note=None):
        messages = original_seed_messages(request, sources_changed_note)
        captured_message_batches.append(messages)
        return messages

    monkeypatch.setattr(chat_turn, "_seed_messages", spy_seed_messages)

    orders_csv_path = tmp_path / "uploads" / "sess-1" / "orders.csv"
    orders_csv_path.parent.mkdir(parents=True, exist_ok=True)
    orders_csv_path.write_text("system\nCRM\n", encoding="utf-8")
    usage_log_csv_path = tmp_path / "uploads" / "sess-1" / "usage_log.csv"
    usage_log_csv_path.write_text("system\nCRM\n", encoding="utf-8")

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_BEARER_TOKEN}"},
    ) as client:
        first_turn_response = await client.post(
            "/chat",
            json={
                "sessionId": "sess-1",
                "userId": "user-1",
                "message": "第一個問題",
                "history": [],
                "sources": [{"alias": "orders", "path": str(orders_csv_path), "fileType": "csv"}],
            },
        )
        assert first_turn_response.status_code == 200
        second_turn_response = await client.post(
            "/chat",
            json={
                "sessionId": "sess-1",
                "userId": "user-1",
                "message": "第二個問題",
                "history": [],
                "sources": [
                    {"alias": "orders", "path": str(orders_csv_path), "fileType": "csv"},
                    {"alias": "usage_log", "path": str(usage_log_csv_path), "fileType": "csv"},
                ],
            },
        )
        assert second_turn_response.status_code == 200

    assert len(captured_message_batches) == 2
    # 首輪:沒有前一輪 manifest 可比,不該有提示。
    first_turn_message = captured_message_batches[0][-1]
    assert "System note" not in first_turn_message.content

    # 第二輪:sources 集合多了 usage_log -- HumanMessage MUST 含變更提示,且提到新增的 alias、
    # 要求重新呼叫 get_schema。
    second_turn_message = captured_message_batches[1][-1]
    assert "the data source list has changed" in second_turn_message.content
    assert "usage_log" in second_turn_message.content
    assert "Call get_schema" in second_turn_message.content


async def test_chat_second_turn_reuploaded_alias_with_schema_change_includes_note(
    tmp_path, monkeypatch
) -> None:
    """同 alias 第二輪換了一個不同的原始路徑(模擬同名重上傳出一個新 uuid)且欄位也變了
    (多一欄 region)——manifest diff MUST 把它歸進 schema_changed(換版本又剛好 schema 也變,
    schema 訊息已經涵蓋換檔案這件事,不重複講 re-uploaded),note 裡要看得到新增的欄位名。
    也順帶驗證 `.sources-manifest.json` 確實跨輪持久化(第二輪能讀到第一輪存的基準,不然
    不會有任何提示)。"""
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scripted = ScriptedChatModel(
        [AIMessage(content="第一輪回答。"), AIMessage(content="第二輪回答。")]
    )
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)

    captured_message_batches: list[list] = []
    original_seed_messages = chat_turn._seed_messages

    def spy_seed_messages(request, sources_changed_note=None):
        messages = original_seed_messages(request, sources_changed_note)
        captured_message_batches.append(messages)
        return messages

    monkeypatch.setattr(chat_turn, "_seed_messages", spy_seed_messages)

    orders_v1_csv_path = tmp_path / "uploads" / "sess-1" / "uuid1_orders.csv"
    orders_v1_csv_path.parent.mkdir(parents=True, exist_ok=True)
    orders_v1_csv_path.write_text("system,tickets\nCRM,42\n", encoding="utf-8")
    orders_v2_csv_path = tmp_path / "uploads" / "sess-1" / "uuid2_orders.csv"
    orders_v2_csv_path.write_text("system,tickets,region\nCRM,42,APAC\n", encoding="utf-8")

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_BEARER_TOKEN}"},
    ) as client:
        first_turn_response = await client.post(
            "/chat",
            json={
                "sessionId": "sess-1",
                "userId": "user-1",
                "message": "第一個問題",
                "history": [],
                "sources": [
                    {"alias": "orders", "path": str(orders_v1_csv_path), "fileType": "csv"}
                ],
            },
        )
        assert first_turn_response.status_code == 200
        second_turn_response = await client.post(
            "/chat",
            json={
                "sessionId": "sess-1",
                "userId": "user-1",
                "message": "第二個問題",
                "history": [],
                "sources": [
                    {"alias": "orders", "path": str(orders_v2_csv_path), "fileType": "csv"}
                ],
            },
        )
        assert second_turn_response.status_code == 200

    assert len(captured_message_batches) == 2
    first_turn_message = captured_message_batches[0][-1]
    assert "System note" not in first_turn_message.content

    second_turn_message = captured_message_batches[1][-1]
    assert "the data source list has changed" in second_turn_message.content
    assert "Schema changed for `orders`" in second_turn_message.content
    assert "region" in second_turn_message.content
    assert "Re-uploaded" not in second_turn_message.content
    assert "Call get_schema" in second_turn_message.content


async def test_chat_second_turn_identical_sources_no_change_note(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scripted = ScriptedChatModel(
        [AIMessage(content="第一輪回答。"), AIMessage(content="第二輪回答。")]
    )
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)

    captured_message_batches: list[list] = []
    original_seed_messages = chat_turn._seed_messages

    def spy_seed_messages(request, sources_changed_note=None):
        messages = original_seed_messages(request, sources_changed_note)
        captured_message_batches.append(messages)
        return messages

    monkeypatch.setattr(chat_turn, "_seed_messages", spy_seed_messages)

    orders_csv_path = tmp_path / "uploads" / "sess-1" / "orders.csv"
    orders_csv_path.parent.mkdir(parents=True, exist_ok=True)
    orders_csv_path.write_text("system\nCRM\n", encoding="utf-8")
    same_sources = [{"alias": "orders", "path": str(orders_csv_path), "fileType": "csv"}]

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_BEARER_TOKEN}"},
    ) as client:
        await client.post(
            "/chat",
            json={
                "sessionId": "sess-1",
                "userId": "user-1",
                "message": "第一個問題",
                "history": [],
                "sources": same_sources,
            },
        )
        await client.post(
            "/chat",
            json={
                "sessionId": "sess-1",
                "userId": "user-1",
                "message": "第二個問題(來源不變)",
                "history": [],
                "sources": same_sources,
            },
        )

    assert len(captured_message_batches) == 2
    second_turn_message = captured_message_batches[1][-1]
    assert "System note" not in second_turn_message.content


def test_is_transient_stream_error_matches_connection_keywords() -> None:
    assert chat_turn._is_transient_stream_error(ConnectionError("Network connection lost."))
    assert chat_turn._is_transient_stream_error(Exception("Read timed out"))
    assert chat_turn._is_transient_stream_error(httpx.ConnectError("connect failed"))
    # str(exc) 為空但類名本身透露連線性質的情況也算。
    assert chat_turn._is_transient_stream_error(ConnectionResetError())


def test_is_transient_stream_error_rejects_unrelated_errors() -> None:
    assert not chat_turn._is_transient_stream_error(ValueError("bad input"))
    assert not chat_turn._is_transient_stream_error(RuntimeError("something else broke"))


class _FakeAgent:
    """假 agent——`astream_events` 依呼叫次數回放不同的結果序列(事件 list 或待拋出的例外),
    模擬第一次呼叫連線中斷、重試後第二次正常的情境。"""

    def __init__(self, outcomes: list[list[dict] | Exception]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    async def astream_events(self, run_input, config, version):
        outcome = self._outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        for agent_event in outcome:
            yield agent_event


class _StreamHarness:
    """duck-typed `ChatTurn` 替身——只帶 `ChatTurn.stream()` 實際讀取的欄位(`_agent`/
    `_run_input`/`_run_config`/`_recorder`),略過 `__aenter__` 的 workspace/duckdb 建構,
    直接把方法以 `ChatTurn.stream(harness)` 呼叫。"""

    def __init__(self, agent) -> None:
        self._agent = agent
        self._run_input: dict = {}
        self._run_config: dict = {}
        self._recorder = ToolResultRecorder()
        self.bridge: EventBridge | None = None


async def test_chat_turn_stream_retries_once_on_transient_connection_error(monkeypatch) -> None:
    created_bridges: list[EventBridge] = []
    original_event_bridge = chat_turn.EventBridge

    def tracking_event_bridge(recorder):
        bridge = original_event_bridge(recorder)
        created_bridges.append(bridge)
        return bridge

    monkeypatch.setattr(chat_turn, "EventBridge", tracking_event_bridge)

    fake_agent = _FakeAgent(
        [
            ConnectionError("Network connection lost."),
            [
                {
                    "event": "on_chat_model_end",
                    "data": {"output": AIMessage(content="重試後正常回答")},
                }
            ],
        ]
    )
    harness = _StreamHarness(fake_agent)

    events = [event async for event in chat_turn.ChatTurn.stream(harness)]

    assert not [event for event in events if isinstance(event, ErrorEvent)]
    assert harness.bridge.final_answer() == "重試後正常回答"
    assert fake_agent.calls == 2
    assert len(created_bridges) == 1, "重試應沿用同一顆 bridge，不應重建"


async def test_chat_turn_stream_does_not_retry_non_transient_error(monkeypatch) -> None:
    fake_agent = _FakeAgent([ValueError("bad input")])
    harness = _StreamHarness(fake_agent)

    events = [event async for event in chat_turn.ChatTurn.stream(harness)]

    assert events == [ErrorEvent(code="AGENT_FAILURE", message="bad input")]
    assert fake_agent.calls == 1


# -- 併發 edit_file lost-update 回歸 -------------------------------------------------------
#
# 這條守的是 `SerializedToolCallsMiddleware` 下併發 edit_file 的 lost-update 行為,與
# dashboard.html 無涉——目標檔改用 notes.md,不受 dashboard.html 專屬的 skill gate 影響,
# 腳本因此可以省去 skill 讀取與 run_sql 這兩步。

_CONCURRENT_EDIT_BASE_NOTES_CONTENT = "# Notes\n<!-- SLOT_A -->\n<!-- SLOT_B -->\n"


@pytest.fixture()
def scripted_flow_concurrent_edit(tmp_path, monkeypatch):
    """同一則 AI message 併發兩個 edit_file 改同一檔案的不相交區段——`perform_string_replacement`
    monkeypatch 把 deepagents `FilesystemBackend.edit` 的讀改寫窗口撐開,沒有序列化中介層時
    後寫者一定覆蓋前寫者(本測試因此在缺陷還在時必紅)。"""
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))

    import time

    from deepagents.backends import filesystem as filesystem_backend

    original_replacement = filesystem_backend.perform_string_replacement

    def slow_replacement(*arguments, **keyword_arguments):
        time.sleep(0.05)
        return original_replacement(*arguments, **keyword_arguments)

    monkeypatch.setattr(filesystem_backend, "perform_string_replacement", slow_replacement)

    scripted = ScriptedChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "id": "call1",
                        "args": {
                            "file_path": "notes.md",
                            "content": _CONCURRENT_EDIT_BASE_NOTES_CONTENT,
                        },
                    }
                ],
            ),
            # 一則 AI message 兩個 edit_file -> ToolNode 併發送出。
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "id": "call-edit-a",
                        "args": {
                            "file_path": "notes.md",
                            "old_string": "<!-- SLOT_A -->",
                            "new_string": "<div id='panel-a'>A</div>",
                        },
                    },
                    {
                        "name": "edit_file",
                        "id": "call-edit-b",
                        "args": {
                            "file_path": "notes.md",
                            "old_string": "<!-- SLOT_B -->",
                            "new_string": "<div id='panel-b'>B</div>",
                        },
                    },
                ],
            ),
            # deepagents 0.7 起同一則 message 對同一路徑的第二個 mutation 直接退件(不再靜默
            # 互蓋),模型下一步重送被退的那個 edit。
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "edit_file",
                        "id": "call-edit-b-retry",
                        "args": {
                            "file_path": "notes.md",
                            "old_string": "<!-- SLOT_B -->",
                            "new_string": "<div id='panel-b'>B</div>",
                        },
                    },
                ],
            ),
            AIMessage(content="兩個區塊都已補上。"),
        ]
    )
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)
    return scripted


async def test_concurrent_edit_file_calls_both_land_on_notes_md(
    tmp_path, scripted_flow_concurrent_edit
) -> None:
    """同一則 AI message 併發兩個 edit_file 改同一檔案:不得 lost-update——第二個被退件後
    重送,兩個改動 MUST 都在。"""
    await _post_chat(tmp_path)

    workspace = build_workspace_store().prepare("user-1", "sess-1")
    notes_md = (workspace.root / "notes.md").read_text(encoding="utf-8")
    assert "panel-a" in notes_md
    assert "panel-b" in notes_md


# -- DashboardSkillGateMiddleware:/chat 端到端 -----------------------------------------------


@pytest.fixture()
def scripted_flow_skill_not_read(tmp_path, monkeypatch):
    """腳本第一則就直接 write_file dashboard.html,完全不先讀 skill——gate MUST 擋下這次寫檔;
    模型收到退貨 ToolMessage 後(腳本裡)不重試,直接給出一句文字結束這輪。"""
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scripted = ScriptedChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "id": "call1",
                        "args": {"file_path": "dashboard.html", "content": DASHBOARD_HTML_CONTENT},
                    }
                ],
            ),
            AIMessage(content="已完成。"),
        ]
    )
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)
    return scripted


async def test_chat_dashboard_write_blocked_before_skill_is_read(
    tmp_path, scripted_flow_skill_not_read
) -> None:
    events = await _post_chat(tmp_path)

    assert not [event for event in events if event["type"] == "DASHBOARD_HTML"]
    workspace = build_workspace_store().prepare("user-1", "sess-1")
    assert not workspace.dashboard_path.is_file()


async def test_chat_dashboard_write_allowed_after_all_skill_files_read(
    tmp_path, scripted_flow
) -> None:
    """`scripted_flow` 的第一步就是 `_skill_read_step()`(讀完整個 dashboard skill 資料夾)
    才 write_file dashboard.html——這條就是 gate 真的放行時的端到端斷言,與
    `test_chat_full_flow_emits_contracted_events` 是同一份事實的兩個角度。"""
    events = await _post_chat(tmp_path)

    assert [event for event in events if event["type"] == "DASHBOARD_HTML"]


async def test_chat_empty_first_round_does_not_retry_invocation(tmp_path, monkeypatch) -> None:
    # 空回應(無文字、無工具啟動)不再觸發整輪重新 invoke——那條迴圈已刪除,keepalive 降到
    # 傳輸層之後,唯一還會重試的情境是傳輸層錯誤(見 STREAM_RETRY_MAX_RUNS)。釘住「模型只被
    # 呼叫一次」:第二則腳本訊息從未被消費。
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scripted = ScriptedChatModel([AIMessage(content=""), AIMessage(content="不該被用到的第二輪")])
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)

    events = await _post_chat(tmp_path)

    answer_events = [event for event in events if event["type"] == "ANSWER"]
    assert answer_events[-1]["text"] == chat_turn.EMPTY_ANSWER_FALLBACK_MESSAGE
    assert scripted.scripted_messages == [AIMessage(content="不該被用到的第二輪")]


async def test_chat_no_text_and_no_dashboard_falls_back_to_empty_answer_message(
    tmp_path, monkeypatch
) -> None:
    # 空回應且本輪沒發出 DASHBOARD_HTML → ANSWER 走 EMPTY_ANSWER_FALLBACK_MESSAGE。
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    scripted = ScriptedChatModel([AIMessage(content="")])
    monkeypatch.setattr(chat_turn, "build_model", lambda: scripted)

    events = await _post_chat(tmp_path)

    answer_events = [event for event in events if event["type"] == "ANSWER"]
    assert answer_events[-1]["text"] == chat_turn.EMPTY_ANSWER_FALLBACK_MESSAGE


async def test_chat_error_terminates_stream_and_still_closes_connection(
    tmp_path, monkeypatch
) -> None:
    # 釘住兩件事,重構把 chat() 拆成 ChatTurn 之後必須仍成立:
    #   (a) ERROR 是本輪最後一個事件——之後不再有 ANSWER 或任何其他事件
    #   (b) 早退仍會執行 teardown——duckdb 連線確實被關閉
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    opened_connections: list[object] = []
    original_open = chat_turn.open_locked_connection

    def tracking_open(sources):
        connection = original_open(sources)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(chat_turn, "open_locked_connection", tracking_open)
    monkeypatch.setattr(chat_turn, "build_model", lambda: FailingChatModel())

    events = await _post_chat(tmp_path)

    error_indexes = [index for index, event in enumerate(events) if event["type"] == "ERROR"]
    assert error_indexes, "本測試需要一個會觸發 ERROR 的模型"
    assert error_indexes[-1] == len(events) - 1, "ERROR 之後不應再有任何事件"

    assert opened_connections, "本輪應該開過一個 duckdb 連線"
    with pytest.raises(duckdb.ConnectionException):
        opened_connections[0].execute("SELECT 1")


async def test_chat_aenter_unexpected_failure_after_connection_open_emits_clean_error_event(
    tmp_path, monkeypatch
) -> None:
    # __aexit__ 只在 __aenter__ 成功 return self 時才會被呼叫——open_locked_connection 之後、
    # return self 之前的任何一步拋例外(這裡用 build_agent 模擬)時,ChatTurn.__aenter__ 自己的
    # except BaseException 已經關連線/清 scratch/reset identity 再重新拋出(見下方連線斷言)。
    # main.py 的 `/chat` handler 進一步把這個重新拋出的例外攔在 __aenter__ 呼叫點,轉成乾淨的
    # ErrorEvent 後 return——SSE 傳輸層不應再看到裸例外冒出去中斷 stream(終審點名的修正點)。
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    opened_connections: list[object] = []
    original_open = chat_turn.open_locked_connection

    def tracking_open(sources):
        connection = original_open(sources)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(chat_turn, "open_locked_connection", tracking_open)

    def failing_build_agent(*args, **kwargs):
        raise RuntimeError("boom during agent assembly")

    monkeypatch.setattr(chat_turn, "build_agent", failing_build_agent)

    csv_path = tmp_path / "uploads" / "sess-1" / "orders.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("system\nCRM\nCRM\nERP\n", encoding="utf-8")
    payload = {
        "sessionId": "sess-1",
        "userId": "user-1",
        "message": "哪個系統最需要改善?",
        "history": [],
        "sources": [{"alias": "orders", "path": str(csv_path), "fileType": "csv"}],
    }
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_BEARER_TOKEN}"},
    ) as client:
        response = await client.post("/chat", json=payload)
    assert response.status_code == 200

    events = _sse_events(response.text)
    assert events == [
        {
            "type": "ERROR",
            "code": "CHAT_INIT_FAILED",
            "message": "對話初始化失敗：RuntimeError",
        }
    ]

    assert opened_connections, "本輪應該開過一個 duckdb 連線"
    with pytest.raises(duckdb.ConnectionException):
        opened_connections[0].execute("SELECT 1")


def test_build_callbacks_gate_follows_tracing_enabled_flag(monkeypatch):
    """_build_callbacks 的開關看 `tracing.is_tracing_enabled()`,不再直接看 Settings 的
    LANGFUSE_PUBLIC_KEY——runtime 完整接管建構時 client 不一定源自那兩個 key。"""
    import app.agent.tracing as tracing_module

    monkeypatch.setattr(tracing_module, "_tracing_enabled", False)
    assert chat_turn._build_callbacks() == []

    monkeypatch.setattr(tracing_module, "_tracing_enabled", True)
    callbacks = chat_turn._build_callbacks()
    assert len(callbacks) == 1


class _PersistTrackingStore:
    """包一個真的 store,把 persist() 換成可控行為(記次數、視需要拋錯)——用來測
    ChatTurn.finalize() 的 WORKSPACE_PERSIST_FAILED 分支,不必真的接 S3。也記
    cleanup_scratch() 呼叫次數,驗證 ChatTurn.__aexit__ 在任何退出路徑都會呼叫它
    (s3 模式 per-turn scratch 清理的終審修正點)。"""

    def __init__(self, delegate, persist_error: Exception | None = None) -> None:
        self._delegate = delegate
        self._persist_error = persist_error
        self.persist_calls = 0
        self.cleanup_scratch_calls = 0

    def prepare(self, user_id: str, session_id: str):
        return self._delegate.prepare(user_id, session_id)

    def persist(self, workspace) -> None:
        self.persist_calls += 1
        if self._persist_error is not None:
            raise self._persist_error

    def cleanup_scratch(self) -> None:
        self.cleanup_scratch_calls += 1


async def test_chat_persist_failure_emits_error_event_right_after_answer(
    tmp_path, scripted_flow, monkeypatch
) -> None:
    tracking_store = _PersistTrackingStore(
        build_workspace_store(), persist_error=WorkspacePersistError("boom")
    )
    monkeypatch.setattr(chat_turn, "build_workspace_store", lambda: tracking_store)

    events = await _post_chat(tmp_path)

    assert tracking_store.persist_calls == 1
    answer_index = next(index for index, event in enumerate(events) if event["type"] == "ANSWER")
    assert events[answer_index + 1] == {
        "type": "ERROR",
        "code": "WORKSPACE_PERSIST_FAILED",
        "message": "本輪結果未能寫入儲存空間,下一輪可能拿不到這次的變更。",
    }
    assert answer_index + 1 == len(events) - 1
    # persist 失敗(拋 WorkspacePersistError)也是 __aexit__ 的正常退出路徑之一——scratch
    # MUST 照樣被清,不能因為 persist 失敗就洩漏。
    assert tracking_store.cleanup_scratch_calls == 1


async def test_chat_persist_success_emits_no_error_event(
    tmp_path, scripted_flow, monkeypatch
) -> None:
    tracking_store = _PersistTrackingStore(build_workspace_store())
    monkeypatch.setattr(chat_turn, "build_workspace_store", lambda: tracking_store)

    events = await _post_chat(tmp_path)

    assert tracking_store.persist_calls == 1
    assert not [event for event in events if event["type"] == "ERROR"]
    assert tracking_store.cleanup_scratch_calls == 1


async def test_chat_turn_aexit_calls_cleanup_scratch_on_early_error_return(
    tmp_path, monkeypatch
) -> None:
    """stream() 因 FailingChatModel 以 ErrorEvent 提前終止(main.py 的 `/chat` 端點直接
    return,finalize() 從未執行)——這正是終審點名的洩漏路徑之一;`async with ChatTurn(...)`
    保證 __aexit__ 仍會執行,cleanup_scratch() 因此仍要被呼叫到。"""
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    tracking_store = _PersistTrackingStore(build_workspace_store())
    monkeypatch.setattr(chat_turn, "build_workspace_store", lambda: tracking_store)
    monkeypatch.setattr(chat_turn, "build_model", lambda: FailingChatModel())

    events = await _post_chat(tmp_path)

    assert [event for event in events if event["type"] == "ERROR"]
    assert tracking_store.persist_calls == 0
    assert tracking_store.cleanup_scratch_calls == 1


async def test_chat_turn_aenter_failure_calls_cleanup_scratch_before_reraising(
    tmp_path, monkeypatch
) -> None:
    """__aenter__ 在 build_agent 失敗時的 except BaseException 區塊 MUST 自己呼叫
    cleanup_scratch()——__aexit__ 在這條路徑上不會被呼叫(__aenter__ 從未成功 return self),
    這是終審點名「build_agent 失敗也洩漏」的那個分支。main.py 的 `/chat` handler 接住這個
    重新拋出的例外轉成 ErrorEvent(見 test_chat_aenter_unexpected_failure_after_connection_open_
    emits_clean_error_event),本測試只關注 cleanup_scratch 這個資源善後副作用,不重複斷言
    ErrorEvent 內容。"""
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    tracking_store = _PersistTrackingStore(build_workspace_store())
    monkeypatch.setattr(chat_turn, "build_workspace_store", lambda: tracking_store)

    def failing_build_agent(*args, **kwargs):
        raise RuntimeError("boom during agent assembly")

    monkeypatch.setattr(chat_turn, "build_agent", failing_build_agent)

    csv_path = tmp_path / "uploads" / "sess-1" / "orders.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("system\nCRM\nCRM\nERP\n", encoding="utf-8")
    payload = {
        "sessionId": "sess-1",
        "userId": "user-1",
        "message": "哪個系統最需要改善?",
        "history": [],
        "sources": [{"alias": "orders", "path": str(csv_path), "fileType": "csv"}],
    }
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_BEARER_TOKEN}"},
    ) as client:
        response = await client.post("/chat", json=payload)
    assert response.status_code == 200
    error_events = [event for event in _sse_events(response.text) if event["type"] == "ERROR"]
    assert error_events
    assert tracking_store.cleanup_scratch_calls == 1
