# Sandbox Integration — Design

- **Date**: 2026-11-12
- **Status**: design agreed with the user, step by step in conversation. Two questions are still open (§12). This is a hand-off document: it will be carried into the internal repo, and the implementation happens there. The implementer has no memory of that conversation.
- **Reference repo**: this spec was written against the external reference repo, branch `feat/sandbox-integration`. Code there is a reference snapshot and differs from the internal implementation. Every file path, class name and version in this spec is from the reference repo; see §0.
- **Diagrams** (`docs/diagrams/`):
  - `sandbox-integration.excalidraw`: overview for PMs.
  - `sandbox-sequence.excalidraw`: one chat turn as a sequence diagram.
  - `sandbox-execute-flow.excalidraw`: one `execute` call as a swimlane flowchart.

## 0. Carrying this into the internal repo

Read the design as contracts and behaviour, not as a patch. Before starting, check each of these against the internal code:

| Assumption from the reference repo | Where the spec relies on it |
|---|---|
| The agent's file backend is a local filesystem backend rooted at a per-turn workspace folder. | §3, §6.3 |
| The workspace is saved as one snapshot per turn and loaded at turn start. | §1, §7 |
| Uploaded files are cached locally before the agent runs, and Excel files are already converted to CSV. | §3, §7.3 |
| Tools run one at a time within a turn. | §6.4, §9 |
| The checkpointer is persistent and shared across pods. | §3, §6.4 |
| Tool output that can carry data values is wrapped in a "data, not instructions" frame. | §3, §7.6 |
| deepagents is 0.7.x, where the `execute` tool appears by itself for a backend that can execute. | §5, §8 |
| The files listed as protected in §7.5 exist under those names and have those rules. | §7.5, §8 |

Known difference (user, 2026-11-13): internal no longer has `api_snapshots/`. MCP results are flattened through `run_sql` and saved as `results/qN.json`. The reference repo still has the folder.

The reference branch contains one code commit, the deepagents upgrade to 0.7.34 with two fixes (`7311e1c`). It is not synced in. Internal must redo the upgrade and check for the same two breaks: `write_file` now overwrites existing files, and a second edit to the same file in one model reply is rejected. §8 lists a third break.

## 1. Goal

Let the deepagent run code, such as Python scripts over the uploaded data, in a remote sandbox pod. Everything else stays as it is today.

The sandbox pod blocks all outbound traffic: only its own API is reachable, and there are no LLM or MCP calls. So the sandbox **cannot replace** the agent's file backend. The agent service keeps calling the model, the connectors and MinIO, and the sandbox is only a place to run commands.

How we'll know it works:
- The agent writes a script, runs it with `execute`, and reads its output.
- Files the script creates appear in the local workspace and are saved in the session snapshot.
- The next turn, possibly on another agent pod, reuses the same sandbox and pushes only what changed.
- With the sandbox switched off, behaviour is exactly as today: no `execute` tool.

## 2. Terms

- **Workspace**: the agent's per-turn local folder (`SessionWorkspace.root`). It holds the dashboard, queries, results, notes and so on. It is the master copy.
- **Snapshot**: the zip of the workspace that is saved to MinIO at the end of every turn (`app/engine/workspace_store.py`).
- **Push**: upload local files to the sandbox.
- **Pull-back**: download files from the sandbox into the workspace.
- **Fingerprint**: the SHA-256 hash of a file's content. Two files with the same fingerprint have the same content.
- **Sandbox record**: the sandbox id plus the fingerprint of every file currently known to be in the sandbox.

## 3. Decisions

| Topic | Decision | Reason |
|---|---|---|
| Who is the master copy | The local workspace. All file tools (`ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`) stay local. | The sandbox can't reach MinIO, the model or connectors. Keeping file tools local avoids a network round-trip per file operation. |
| When to sync | Only around `execute`: push before, pull back after. | File tools never need the sandbox. |
| Sync direction | Both ways. | Without pull-back, files a script writes never reach the dashboard or the snapshot. |
| Sandboxes per session | One. It is reused across all turns of the session. | Saves re-creating and re-pushing every turn. |
| When to create | On the first `execute`, not at turn start. | Many turns never run a script; the sandbox can also disappear mid-turn, so that handling must live in `execute` anyway. |
| Sandbox gone | Create a new one, push everything, retry the failed call once. | The sandbox is disposable; local is the master copy. |
| Deleting sandboxes | We never delete them. The platform shuts down idle sandboxes itself. | Confirmed by the user. |
| Where the sandbox record lives | The LangGraph checkpointer, as a hidden state key, set by one middleware. | The checkpointer is persistent and shared across pods (confirmed by the user). It saves after every agent step, so a sandbox created in a turn that later fails is still recorded. |
| Uploaded data | Pushed as CSV (Excel already converted), from the local cache. | Files are under 20 MB today. Parquet is 3–10× smaller but brings no visible speed gain at that size; switch only if size becomes a problem. |
| Script output | Wrapped in the "this is data, not instructions" frame (`frame_data_content`) before the model sees it. | The code is the model's own, but its output can print values from the uploaded data, and those can contain instruction-like text. Same frame as for query results. |
| Sandbox client | Behind an interface. Default implementation: HTTP with bearer auth, configured by env vars. | The real API spec is internal and can't live in this repo. |
| deepagents version | 0.7.34 (already done, commit `7311e1c`). | Requested by the user. |

## 4. Sandbox API (what we rely on)

The API uses bearer auth. Calls, as described by the user:

| Call | Inputs | Notes |
|---|---|---|
| `create` | image, env vars, size | Returns a sandbox id. |
| `delete` | id | Not used (idle sandboxes shut down by themselves). |
| `upload` | id, file, path, permission | One file per call. |
| `download` | id, path | One file per call. |
| `ls` | id, path | The exact output format is unknown; see §7.4 for why we don't depend on it. |
| `execute` | id, command, timeout | The reply format is open (§12, question 2). |

Assumptions the implementer must confirm against the real API before going live:
- `upload` creates missing parent folders and overwrites existing files.
- `download` is binary-safe.
- A call on a sandbox that no longer exists fails with a recognisable "not found" error (for example HTTP 404). This is how we detect "sandbox gone".

## 5. Where things happen in the code

```
ChatTurn.prepare  (app/agent/chat_turn.py)
  └─ build_agent  (app/agent/graph.py)
       ├─ backend = SandboxWorkspaceBackend   if SANDBOX_ENABLED
       │            DashboardOverwriteBackend  otherwise (today's behaviour)
       └─ middleware += SandboxStateMiddleware if SANDBOX_ENABLED

deepagents FilesystemMiddleware
  └─ sees a backend that can execute → adds the `execute` tool by itself

Model calls execute(command)
  └─ SandboxStateMiddleware.awrap_tool_call   (only for tool name "execute")
       ├─ hands the saved sandbox record to the backend
       ├─ handler(request) → SandboxWorkspaceBackend.execute(command)
       │     create if needed → push → run → list → pull back → frame output
       └─ if the record changed → returns Command(update={sandbox record, tool message})
```

Nothing in `ChatTurn` talks to the sandbox. Its only new job is to pass the uploaded data files to `build_agent`: alias and local cache path, from the `_resolve_source` results it already has.

## 6. Components

### 6.1 `SandboxClient` interface — `app/agent/sandbox/client.py`

A `Protocol` with one async method per API call:
- `create(image, env, size) -> str`
- `upload(sandbox_id, path, content: bytes, permission)`
- `download(sandbox_id, path) -> bytes`
- `execute(sandbox_id, command, timeout) -> SandboxExecResult`

`SandboxExecResult` holds `output: str`, `exit_code: int | None` and `truncated: bool`.

There is also one exception type, `SandboxGoneError`, which the client raises when the API says the sandbox doesn't exist. Every other API failure raises `SandboxUnavailableError` with the original error as its cause.

`ls` is part of the API but not of this interface for v1 (see §7.4).

### 6.2 `HttpSandboxClient` — same package

- Uses `httpx.AsyncClient`, with the bearer token from `SANDBOX_API_TOKEN`. The token never goes in `one.properties` and is never logged.
- The base address comes from `SANDBOX_API_BASE_URL`. The path of each call comes from settings with sensible defaults, so the internal values stay out of the repo.
- It maps the API's error for a missing sandbox to `SandboxGoneError`.

Internal override: `AgentRuntime` gets an **optional** `build_sandbox_client()` method. Callers use `getattr` with a fallback to `HttpSandboxClient`, the same pattern as `build_langfuse`, so `base.py`'s required interface doesn't change. Document it in `docs/internal-implementation-guide.md`.

### 6.3 `SandboxWorkspaceBackend` — `app/agent/sandbox/backend.py`

- It subclasses `DashboardOverwriteBackend` (so it is still a `FilesystemBackend` and the `AgentRuntime.build_agent` type hint is unchanged) and also implements `SandboxBackendProtocol` (`id`, `execute`, `aexecute`).
- All file methods are inherited and stay local.
- Per-request state: the client, settings, the uploaded data files for this turn, and the current sandbox record. It is created fresh in each `build_agent`, so there is no state shared between requests.
- `aexecute(command, timeout)` runs the algorithm in §7.
- The sync `execute()` runs the async version on the event loop. Check how deepagents 0.7 calls it from the async tool path, and implement whichever it actually uses.

### 6.4 `SandboxStateMiddleware` — `app/agent/middleware.py`

- Its `state_schema` adds `sandbox: NotRequired[Annotated[dict, PrivateStateAttr]]`. `PrivateStateAttr` keeps the key out of the model's input and output.
- For tool calls named `execute`:
  - Before the call: hand `request.state.get("sandbox")` to the backend.
  - After the call: if the backend's record changed, return `Command(update={"sandbox": new_record, "messages": [tool_message]})`; otherwise return the tool message unchanged.
- Every other tool passes through untouched.
- It must wrap inside `SerializedToolCallsMiddleware`, so that a file edit can never run in the middle of a sync.

### 6.5 Pure sync helpers — `app/engine/sandbox_sync.py`

These use only the standard library (engine purity, ruff TID251):
- fingerprinting files;
- deciding what to push (local fingerprint ≠ record);
- deciding what to pull back (§7.5 rules);
- path safety checks.

They are easy to unit-test without any network.

### 6.6 Settings — `app/config.py` and `one.properties`

| Key | Default | Purpose |
|---|---|---|
| `SANDBOX_ENABLED` | `false` | Off means today's behaviour exactly. |
| `SANDBOX_API_BASE_URL` | empty | Required when enabled. |
| `SANDBOX_API_TOKEN` | empty | Secret, env only. Startup fails loudly if enabled and empty. |
| `SANDBOX_IMAGE` | empty | Required when enabled. |
| `SANDBOX_SIZE` | empty | Passed to `create` as is. |
| `SANDBOX_WORKDIR` | `/workspace` | Folder in the sandbox that mirrors the workspace. |
| `SANDBOX_EXECUTE_TIMEOUT_SECONDS` | `300` | Default timeout per command. |
| `SANDBOX_MAX_PULL_FILE_BYTES` | `52428800` (50 MB) | Larger files are not pulled back; the model is told. |
| `SANDBOX_PATH_*` | API defaults | Per-call paths (§6.2). |

### 6.7 Other touch points

- `app/agent/events.py` `step_title_for`: add a Chinese step title for `execute` (for example 「執行程式」). Project rule: every new tool needs one.
- `app/agent/prompts.py`: add a short contract note, only when the sandbox is enabled. It says:
  - commands run from the workspace root, so use relative paths;
  - uploaded data is in `data/{alias}.csv`;
  - there is no network, no package installs and no connector access;
  - results go where §12 question 1 decides.

  How to analyse belongs in a skill, not in the prompt.

### 6.8 Where each kind of file lives

The main copy of every file is on the agent pod. The sandbox only ever holds a copy.

| Files | Pushed to the sandbox | Pulled back | Saved between turns in |
|---|---|---|---|
| `dashboard.html`, `notes.md` | Yes | No (protected) | Session snapshot |
| `queries/qN.sql`, `results/qN.json`, including flattened MCP results | Yes | New files only | Session snapshot |
| `replay/` (records of MCP calls), if internal still has it | Yes | No (protected) | Session snapshot |
| `large_tool_results/`, `conversation_history/` | Yes | No (protected) | Session snapshot |
| Uploaded data files | Yes, as `data/{alias}.csv` | No | MinIO original; local cache |
| Built-in and MCP skills (`.skills/`) | Yes | No (protected) | Nowhere; rebuilt every turn |
| Skills the agent writes for the user | Yes, with the rest of `.skills/` | No | Open; see §12 question 3 |
| Files a script creates | Created there | Yes | Session snapshot |

Because results and MCP data are ordinary workspace files, `ls`, `read_file` and `grep` see them locally, and `execute` sees them because they are pushed before every command. Nothing extra is needed after `run_sql`.

## 7. One `execute` call

### 7.1 Sandbox record shape

```json
{
  "sandbox_id": "…",
  "files": { "scripts/analysis.py": "<sha256>", "data/orders.csv": "<sha256>" }
}
```

Paths are relative to the workspace root. In the sandbox they sit under `SANDBOX_WORKDIR`.

### 7.2 Get a sandbox

- If there is no record, call `create(image, env, size)`. `env` includes `COWORK_USER_ID` and `COWORK_SESSION_ID`, read from `request_context`, so the platform can tell whose sandbox it is. Start with an empty `files` map.
- Otherwise, use the recorded id.
- If any later call raises `SandboxGoneError`: create a new sandbox, reset `files` to empty, and redo the step. Do this at most once per `execute`. A second failure is returned to the model as an error message.

### 7.3 Push

1. Walk the workspace, skipping junk (§7.5). `.skills/` is included, so a script that ships with a skill can run in the sandbox. It is never pulled back (§7.5).
2. Upload each file whose fingerprint differs from `files`.
3. Upload each uploaded data file to `data/{alias}.csv` if its fingerprint differs.
4. Update `files` after each successful upload.

Files deleted locally are not deleted in the sandbox in v1. They are removed from `files`.

### 7.4 Run, then find what changed

1. Call `execute(sandbox_id, "cd {SANDBOX_WORKDIR} && {command}", timeout)`.
2. Then call `execute` again with a small fixed listing script. It prints one JSON line per file under `SANDBOX_WORKDIR`, with path, size and SHA-256, and skips `data/` and junk folders.
   - Why not the API's `ls`: its output format is unknown, and it may not include sizes, times or hashes. The script gives exact fingerprints on any image that has Python.
3. A file is "changed" if it is new or its hash differs from `files`.

### 7.5 Pull-back rules

For each changed file, in this order:

| Rule | Action |
|---|---|
| Path escapes the workspace (`..`, absolute, symlink) | Skip and log a warning. |
| Junk: `__pycache__/`, `.ipynb_checkpoints/`, `*.pyc`, `.cache/`, temp files | Skip. |
| Under `data/` | Skip (inputs only, never pulled). |
| Protected: `dashboard.html`, `notes.md`, `.sources-manifest.json`, `replay/`, `.skills/`, `large_tool_results/`, `conversation_history/` | Skip. Tell the model it was not applied. |
| Already exists locally in `queries/` or `results/` | Skip; these folders are create-only. New files there are allowed. |
| Larger than `SANDBOX_MAX_PULL_FILE_BYTES` | Skip and tell the model. |
| Otherwise | Download, write into the workspace, and update `files`. |

Notes on the protected list:
- `dashboard.html` and `notes.md` are protected because changes to them must go through the model's own `write_file`/`edit_file`, which pass the dashboard-skill gate and the wiring guard. A script can write a draft elsewhere and the model can copy it in.
- `replay/` holds the system's own records of MCP calls. Scripts must not rewrite them.

### 7.6 Return to the model

The output becomes `ExecuteResponse(output=frame_data_content(output) + pull-back summary, exit_code, truncated)`. The summary is a short note listing files pulled back and files skipped, with the reason for each.

Long output: in deepagents 0.7 the "capture output at the source" feature only works for `BaseSandbox` backends. Ours is a local filesystem backend, so long output goes through the generic eviction to `large_tool_results/` instead. That is fine: the model can still `read_file` it.

### 7.7 Errors

`SandboxUnavailableError` and the "retry once" failure are returned to the model as a plain error message. The `execute` tool never raises (same as the `data.py` tool contract), and the turn continues.

## 8. Prerequisite fix from the 0.7 upgrade (do first)

deepagents 0.7 adds a `delete` tool. With today's backend the model could delete:
- `queries/*.sql` and `results/*.json`. These are create-only, and query ids are numbered by counting `queries/*.sql`, so deleting one would make ids repeat.
- `replay/` files, which are the system's own records of MCP calls.

Fix: override `delete` in `DashboardOverwriteBackend`. It refuses everything except the files the model already owns (`dashboard.html`, `notes.md`, and files the model created outside the protected folders). Add a test per protected folder. This is independent of the sandbox and should land as its own commit.

## 9. Concurrency and limits

- Within one turn, `SerializedToolCallsMiddleware` already runs tools one at a time, so no sync overlaps a file edit.
- Two turns of the same session at once, for example from two tabs, can both push to the same sandbox or both create one. The losing sandbox is abandoned and shuts down by itself.
- File conflicts in that case follow today's rule: the last snapshot save wins.
- Blocking concurrent `/chat` for the same session is already a separate backlog item (return 409), and it would fix this too.

## 10. Security

- The sandbox runs model-written code. It has no network, so the worst case inside it is wrong output. The code is the model's own, but what comes back can carry values from the uploaded data, so it is handled as data, never as instructions:
  - output is framed;
  - pull-back has path checks, a protected list and a size cap.
- Uploaded data is pushed decrypted. Excel files are stored encrypted in MinIO; CSV files are not. The sandbox is egress-locked and per-session.
- The bearer token is env-only and never logged.
- Logs record sandbox id, command length, exit code, and file counts and sizes. They never record command text, output or file contents (project logging rule).
- This lifts the `CLAUDE.md` rule "NEVER add a code-execution tool before the sandbox lands" for this path only. Code still never runs on the agent pod itself.

## 11. Tests

All tests use a fake in-memory `SandboxClient` that records calls and simulates files. They never run real commands and never call the real API.

- **Sync helpers:**
  - push only what changed;
  - path escape;
  - each pull-back rule in §7.5, one test per row.
- **Backend:**
  - first call creates and pushes everything;
  - a second call pushes only the changed file;
  - "gone" → create, push everything, retry once;
  - "gone" twice → error message, no raise;
  - output is framed;
  - the pull-back summary lists skipped files.
- **Middleware:**
  - the record is written to state when created or replaced, and not written when unchanged;
  - non-`execute` tools pass through;
  - the record survives into the next turn through the checkpointer (two `/chat` calls, same session).
- **Graph:**
  - with the sandbox off, there is no `execute` tool and today's tests are unchanged;
  - with the sandbox on, the `execute` tool exists.
- **Settings:** enabled with an empty token or base address fails at startup.
- **Prerequisite (§8):** `delete` is refused for each protected folder.

## 12. Open questions

1. **Must scripts save their results as JSON in a fixed place?**
   - Background: there is no number check today. Dashboard charts read only `results/qN.json` through `__ERD_RESULTS__`; text numbers are unchecked.
   - Options:
     - (a) Free-form output under `outputs/`; the dashboard keeps binding to `qN` only.
     - (b) Scripts write `results/s{N}.json` in the same shape as query results, and injection is extended to `sN`. Extending injection means changes to `results.py` (the reference pattern), the dashboard skill's data contract and Java `ArtifactAssembler` detection.
   - Recommendation: (a) for v1; (b) together with the future number check.
2. **What does the API's `execute` return?** Does it include the exit code? Does it return normal output and errors separately or combined? `HttpSandboxClient` maps either shape to `SandboxExecResult`; this only decides the mapping code.

3. **Where do skills that the agent writes for the user go?**
   - Background: in the reference repo, user skills are loaded read-only from a per-user folder in storage and staged into `.skills/` each turn. `.skills/` is rebuilt every turn and left out of the session snapshot, so there is no way to save a skill the agent writes. How internal handles this is not known here.
   - A skill belongs to the user, not to one session. So it should not live in the session snapshot or only in the session's sandbox.
   - Recommendation: the agent writes the skill with `write_file` on the agent pod, into a fixed folder. At turn end, the system saves new or changed skills to the per-user folder in storage. The sandbox never creates or changes skills: `.skills/` is never pulled back.
   - To settle with the user:
     - Is a skill kept across sessions, or only for the session?
     - Internal storage forbids overwriting a key, so changing a skill needs versioned keys.
     - A saved skill is loaded into later sessions. Should saving one need the user's confirmation, so that instruction-like text in data cannot plant a lasting skill?

## 13. Out of scope (v1)

- Deleting sandboxes ourselves, and per-user sandbox limits. These need a searchable store, such as MongoDB or the platform's own listing.
- Parquet conversion.
- The number check.
- Re-enabling the general-purpose subagent. It is still disabled in `graph.py`.
- Propagating deletions from the sandbox to the workspace.

## 14. Docs to update in the same PR

- **`CLAUDE.md`:**
  - the deepagents pin (0.5.5 → 0.7.34, and the internal registry note);
  - sandbox status and the `execute` contract;
  - the workspace file contract: the `data/` and §12 output folder, and the protected pull-back list.
- **`docs/architecture.md`:** add the sandbox to the deepagent section.
- **`docs/internal-implementation-guide.md`:** the optional `build_sandbox_client()` and the `SANDBOX_*` env vars.
- **`one.properties`:** the new keys, with secrets left empty.

## Appendix A. Which code touches the workspace, and what each design costs

Counted from the reference repo on deepagents 0.7.34. Internal may differ (§0).

### A.1 Tools the model calls

"Through the backend" means the call follows whichever file system is the main one. "Agent pod disk" means our own code reads or writes local disk directly, whatever the backend is.

| Tool | Writes | Reads | How |
|---|---|---|---|
| `write_file` | Any allowed path: `dashboard.html`, `notes.md`, new files | Whether the file exists | Through the backend |
| `edit_file` | The target file | The target file | Through the backend |
| `delete` (new in 0.7) | Removes the target | Whether it exists | Through the backend |
| `read_file`, `ls`, `glob`, `grep` | Nothing | Workspace files | Through the backend |
| `execute` (new) | Whatever the script writes | Whatever the script reads | In the sandbox |
| `run_sql` | `queries/qN.sql`, `results/qN.json` | The list of `queries/*.sql`, to pick the next number | Agent pod disk |
| MCP connector tools | `replay/calls.jsonl`, `replay/landings.jsonl` | Nothing | Agent pod disk |
| `check_dashboard` | A temporary file outside the workspace | `dashboard.html`, `replay/*.jsonl` | Agent pod disk |
| `get_schema`, `preview_data` | Nothing | Nothing (DuckDB memory only) | No files |
| `write_todos` | Nothing (agent state only) | Nothing | No files |

### A.2 System code that is not a tool

| When | Writes | Reads | How |
|---|---|---|---|
| Turn start | Unpacks the snapshot (all files); stages `.skills/`; `.sources-manifest.json`; the previous `dashboard.html` | The snapshot; the old source manifest; `replay/` | Agent pod disk |
| Turn start, skills loading | Nothing | Lists each skills folder and reads every `SKILL.md` | Through the backend |
| Every model call | Nothing | All `results/*.json`, to rebuild the list of query ids | Agent pod disk |
| A tool result is too large | `large_tool_results/<id>` | Nothing | Through the backend |
| The conversation gets too long | `conversation_history/<thread>.md` (rare) | The same file | Through the backend |
| Turn end | The snapshot zip | `dashboard.html`, all `results/*.json`, then every workspace file | Agent pod disk |

### A.3 Sandbox API calls per operation

Assumes one file per `upload` or `download` call, as the API was described.

| Operation | Agent pod is main (this spec) | Sandbox is main (`BaseSandbox` as shipped) |
|---|---|---|
| `ls`, `read_file`, `glob`, `grep` | 0 | 1 |
| `write_file` | 0 | 2 (make folders, then upload) |
| `edit_file` | 0 | 1 if old and new text are under 50 KB together; otherwise 4 |
| `delete` | 0 | 2 |
| `execute` | changed files pushed + 1 run + 1 listing + new files pulled | 1 |
| `run_sql` | 0 | 2 uploads, so the model's `ls` sees the new files |
| `check_dashboard` | 0 | 1 download |
| Skills loading at turn start | 0 | 1 per skills folder + 1 per `SKILL.md` |
| A large tool result is saved | 0 | 2 |
| Previous dashboard at turn start | 0 | 1 upload |
| Turn end | 0 | 1 listing + 1 download per file changed in the sandbox |
| Sandbox missing (first use, or shut down while idle) | 1 create + every workspace file + uploaded data. Only when a script runs. | 1 create + every workspace file + uploaded data. On any turn. |

### A.4 Three example turns (sandbox already running)

The base turn: the model reads 2 skill documents, runs 6 queries, writes the dashboard once and edits it twice. The reference repo has one skills folder with 2 skills.

| Turn | Agent pod is main | Sandbox is main |
|---|---|---|
| Base turn, no script | 0 | 24 = 3 skills + 1 previous dashboard + 2 reads + 12 query files + 2 write + 2 edits + 2 turn end |
| Base turn + 1 script that makes 1 file | 16 = 13 pushed (script + 12 query files) + 1 run + 1 listing + 1 pulled | 28 = 24 + 2 write script + 1 run + 1 more download at turn end |
| Base turn + 5 scripts, 1 file each | 32 = 16 for the first + 4 for each later one | 44 = 24 + 5 × 3 + 5 more downloads at turn end |

Reading the numbers:
- A turn with no script costs nothing when the agent pod is main, and about 24 calls when the sandbox is main.
- Each extra script costs about the same in both: 4 calls against 3.
- If many files must move at once, both designs can pack them into one archive: 1 upload plus 1 unpack command. That turns the 13-file push into 2 calls and a full restore into about 3.

### A.5 Costs other than call counts

| Cost | Agent pod is main | Sandbox is main |
|---|---|---|
| Sandboxes running | Only for sessions that run a script | For every active session |
| If the sandbox API is down | Only `execute` fails | The agent cannot read or write any file |
| If the sandbox dies mid-turn | Nothing is lost | The turn's files that were only in the sandbox are lost |
| Speed of file tools | Local disk | One API call plus a command in the pod, each time |
| Code we must write | Push, pull-back rules, change listing, path mapping | Restore at turn start, download at turn end, an upload after every `run_sql`, a download for `check_dashboard`, write rules rebuilt on the sandbox side |
| Paths in scripts | The agent's paths and the sandbox's differ; commands use relative paths | The same everywhere |
| Stale views | None for file tools; scripts see the state as of the push just before them | None |
| Rules on writes (create-only, protected files) | Enforced: scripts cannot overwrite protected files | Scripts can change any file |
