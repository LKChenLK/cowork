"""deepagents assembly -- wires the chat model, data tools, staged skills, and workspace
filesystem backend into a compiled LangGraph graph the event layer drives via
`astream_events`."""

import threading

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import WriteResult
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from duckdb import DuckDBPyConnection
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from app.agent import session_state
from app.agent.middleware import (
    DashboardSkillGateMiddleware,
    SerializedToolCallsMiddleware,
    WiringManifestMiddleware,
)
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.runtime import load_runtime
from app.agent.tools.data import build_data_tools
from app.agent.tools.recording import ToolResultRecorder
from app.engine.workspace import SessionWorkspace

# write() 允許整份覆寫的檔案集合:dashboard.html 與記錄用的 notes.md。
_OVERWRITABLE_FILE_NAMES = frozenset({"dashboard.html", "notes.md"})

# 關掉 general-purpose subagent——會委派子任務寫 Python 腳本但無執行機制。key="openai"
# 對應這裡唯一會建的模型類別 ChatOpenAI。
register_harness_profile(
    "openai",
    HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
)


class DashboardOverwriteBackend(FilesystemBackend):
    """write 維持 create-only,僅 `_OVERWRITABLE_FILE_NAMES`(dashboard.html/notes.md)可整份
    覆寫。deepagents 0.7 起 parent write 預設覆寫既有檔案,故在此自行擋下其餘已存在檔案——
    queries/results/api_snapshots 等落檔與 hash 驗證依賴「模型不能直接覆寫」。局部編輯走
    parent 的 edit()(大改動改用 write_file 由 prompt 引導)。"""

    def write(self, file_path: str, content: str) -> WriteResult:
        try:
            resolved_path = self._resolve_path(file_path)
        except (OSError, RuntimeError) as error:
            return WriteResult(error=f"Error writing file '{file_path}': {error}")

        overwritable_paths = {(self.cwd / name).resolve() for name in _OVERWRITABLE_FILE_NAMES}
        if resolved_path.exists() and resolved_path not in overwritable_paths:
            return WriteResult(
                error=(
                    f"Cannot write to {file_path} because it already exists. "
                    "Read and then make an edit, or write to a new path."
                )
            )

        return super().write(file_path, content)


def build_model() -> BaseChatModel:
    return load_runtime().build_model()


def build_agent(
    model: BaseChatModel,
    connection: DuckDBPyConnection,
    workspace: SessionWorkspace,
    staged_skill_paths: list[str],
    recorder: ToolResultRecorder,
    extra_tools: list[BaseTool] | None = None,
    connection_lock: "threading.Lock | None" = None,
    extra_system_section: str | None = None,
    *,
    dashboard_skill_root: str = ".skills/builtin/dashboard",
) -> CompiledStateGraph:
    tools = build_data_tools(connection, workspace, recorder, connection_lock=connection_lock)
    if extra_tools:
        tools = [*tools, *extra_tools]
    system_prompt = (
        SYSTEM_PROMPT
        if extra_system_section is None
        else f"{SYSTEM_PROMPT}\n\n{extra_system_section}"
    )
    return load_runtime().build_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        backend=DashboardOverwriteBackend(root_dir=str(workspace.root), virtual_mode=True),
        skills=staged_skill_paths,
        checkpointer=session_state.checkpointer,
        middleware=[
            SerializedToolCallsMiddleware(),
            WiringManifestMiddleware(workspace),
            DashboardSkillGateMiddleware(workspace, skill_relative_root=dashboard_skill_root),
        ],
    )
