# Project Rules

全部程式碼 MUST 遵守以下規則。撰寫 Backend Java 程式前，先讀 `.claude/skills/java/SKILL.md`（收合後的 Java skill；依任務讀對應 `references/*.md`——spring-boot、spring-web、spring-data、spring-testing、code-quality、design-patterns；`jpa-patterns` 為 Oracle/JPA 時期遺留，DB 已是 MongoDB，僅 N+1 概念仍適用）；
撰寫 Frontend 程式前，先讀 `.claude/skills/frontend-dev-guidelines/SKILL.md`；
撰寫 Python/FastAPI（`deepagent-service/`）程式前，先讀 `.claude/skills/fastapi/SKILL.md`（FastAPI 官方 skill；SSE 用 `fastapi.sse.EventSourceResponse`、參數/依賴一律 `Annotated`，其餘見該 skill 與其 `references/`）＋本檔「deepagent-service」節。
架構全貌一律以 `docs/architecture.md` 為權威（本檔只放規則與狀態摘要，不重複架構細節）。

## 專案脈絡

- **產品**：Cowork · Data Studio——使用者上傳 CSV/Excel + prompt，agent 產出 self-contained HTML dashboard（Tailwind + ECharts，serve 期由 `ArtifactCdnRewriter` 改寫成 repo 自帶 `/vendor/` 資產，瀏覽器不連外部 CDN）。UI 完整還原 `docs/mockup/eRDWorkspaceonline.html` 的 Cowork tab（僅此畫面）。
- **兩條 provider 線**（`ERD_AGENT_PROVIDER`，pluggable，統一回傳 AgentEvent 流：STEP/TOKEN/CODE/TABLE/THINKING/QUESTION/ANSWER/ARTIFACT/ERROR）：
  - `openai-compatible`（**llm api 線**，未設時預設）：`OpenAICompatibleProvider`，LLM 直寫 HTML；後端 `ArtifactAssembler` 注入**全量原始資料**到 `window.__ERD_DATA__[alias]`，統計由瀏覽器 JS 現算；生成品質管線（GraalJS 語法驗證、省略偵測、生成期修復）全在 `agent/provider/openai/`；auth-mode `bearer|token-exchange`（j1→j2，TTL 快取，401 單次重試）。現況 **csv-only**（xlsx 改原樣直存後此線不再解析 xlsx，accepted trade-off）。
  - `langgraph-analysis`（**analysis 線＝主線**）：`LangGraphAnalysisProvider` 橋接 `deepagent-service`（FastAPI + deepagents 0.5.5 harness + DuckDB），模型用 `get_schema`/`run_sql`/`preview_data` 查資料、以 `write_file`/`edit_file` 直寫 `dashboard.html`；只注入**被引用到的查詢結果**到 `window.__ERD_RESULTS__["qN"]`（物件列 + Proxy 攔截未知欄名），瀏覽器只做笨渲染，文字結論與圖表數字同源。沒有生成期驗證層，品質防線＝使用者確認制的瀏覽器錯誤修復（`POST /api/artifacts/{id}/repair` → deepagent `POST /repair`）。
  - `InternalCodegenProvider` 已移除；deepagent-service 的 internal 差異走 `AgentRuntime` 接縫（`AGENT_RUNTIME=internal`），非另一個 Java provider。
- **Artifact 契約**：兩線 HTML 都是自足式單檔；迭代修改——llm api 線回餵前版 raw HTML + 最小變更指令，deepagent 線以 `previousDashboardHtml` 進場、`strip_injected_blocks` 剝掉舊注入後由模型 `edit_file`/`write_file`。進度顯示——llm api 線由模型 `[[step:]]` 標記驅動，deepagent 線由工具呼叫 `tool_{name}_{runId}` STEP 驅動。前端以 axios 抓 HTML → 注入 CSP `<meta>` → `sandbox="allow-scripts"` iframe `srcdoc` 呈現（opaque origin；NEVER `allow-same-origin`、NEVER `window.open` blob/原始 HTML）；artifact 讀取已補 ownership 檢查（他人 → 404）。
- **模型（2026-09 起）**：internal 與外部皆為 **deepseek-v4-flash**（dev 經 OpenRouter）。**設計前提已反轉**：舊前提「模型弱且不可升級，品質策略走確定性結構」不再成立——模型只會愈來愈強，harness MUST 以此假設設計，見下方「Harness 設計前提」。設定面：`ERD_AGENT_OPENAI_COMPATIBLE_MODEL`（backend）／`AGENT_MODEL`（deepagent）皆由 env 指定；repo 內的 committed 預設值（`application.properties`、`one.properties`、`config.py`、compose、`.env.example`）已一律 `deepseek-v4-flash`（OpenRouter 上為 `deepseek/deepseek-v4-flash`）；換模型時 MUST 五處同步改。
- **Multi-user**：一律 `X-User-Id` header（v1 前端 localStorage 匿名 UUID 由 axios interceptor 附加）→ `CurrentUserFilter` 填入 `CoworkContextHolder`（ThreadLocal）；internal 環境 `TSSO_ENABLED=true` 時主線 filter 不註冊，由 internal 側同層 filter 填入。所有 session 查詢按 userId 過濾，存取他人資源一律 404。
- **檔案**：限 5 檔/session 共 5GB（CSV 單檔 2GB 串流解析；xlsx 上限 200MB、**原樣直存不解讀 bytes**，解密＋xlsx→CSV 轉檔移交 deepagent-service 下載時在 `source_cache` 惰性做）；儲存雙路線（`erd.storage.type`／`STORAGE_BACKEND`＝`local|s3`，committed 預設 `local`；s3 為 internal 現行路線，本機/compose 接 MinIO）；deepagent workspace 走 write-once generation 快照（**zip-only**：`gen-{epoch13}-{hex8}.zip` 單物件，local/s3 同一 code path，只留最新兩代）；分級保留（artifact HTML 2 年、workspace 與上傳原始檔依 session 最後活動 180 天，`ERD_STORAGE_RETENTION_*` 可調，`ERD_STORAGE_CLEANUP_DRY_RUN` 首次上線 MUST 先 `true` 跑滿一輪）。
- **Internal 環境同步**（權威：`docs/internal-sync.md`、`docs/internal-implementation-guide.md`）：GitHub 為唯一權威寫入者，單向鏡像進 internal（GitLab 鏡像**非純鏡像**，`gl/*` 上會多出 GitHub 沒有的 commit）；`scripts/sync-upstream.sh --official gl/<ref> <GitHub sha>`（站在 internal 主線 branch 上執行，主線＝目前 branch；錨點與快照樹都取人工帶入的 GitHub sha，經三道 sha 守門：可解析、是 `gl/<ref>` 祖先、`gl/<ref>` 相對 sha 只能新增檔案）replace-then-restore；`--test gl/<ref>` 為 in-place 測試模式（站在自建 `test/*` 上，NEVER merge 進主線）；改腳本 MUST 同步跑 `bash scripts/test-sync-upstream.sh`（拋棄式 repo 上 38 個情境）。`scripts/internal-owned-paths.txt` 列 internal 獨佔檔（`backend/src/internal/**`、`frontend/src/bootstrap/internal.impl.ts`、`deepagent-service/app/agent/runtime/internal_runtime.py`、`app/engine/upload_decrypt.py` 等），`manual-merge-paths.txt` 列雙邊擁有檔（`backend/pom.xml`、`application.properties`、`frontend/index.html`）。三個接縫（AgentRuntime／前端 bootstrap／身分 filter）刻意「壞掉就大聲壞掉」，NEVER 靜默 fallback。家裡側 NEVER 把獨佔路徑加進 `.gitignore`；改動接縫介面時 MUST 同步更新指引文件。
- **關鍵文件**：架構 `docs/architecture.md`；spec `docs/superpowers/specs/`（產品原始 spec `2026-07-05-cowork-data-studio-design.md`、deepagent 主線 `2026-07-29-deepagent-dashboard-design.md`、Mongo 遷移 `2026-08-11-*`、S3 回歸 `2026-08-06-*`、internal 接縫 `2026-08-03-*`、xlsx 密文直存＋zip-only `2026-08-26-*`、**API datasource `2026-08-08-api-datasource-design.md`（設計定案、未實作）**）；實作計畫 `docs/superpowers/plans/`；進度 ledger `.superpowers/sdd/progress.md`（gitignored）。
- **狀態（2026-09-10）**：master HEAD `fe3eb2c`（PR #80；本 fork `LKChenLK/cowork` 由上游 `Michelle12369/cowork` 同步，兩者 master 同 sha）。2026-07-29 當時 open 的 `feat/data-insight-agent` 路線已整條演進為 `deepagent-service` 並全數 merge；其後 master 依序納入：deepagent 結構重構與 typed wire events、xlsx→csv 正規化、internal 接縫三件組、Oracle→MongoDB（含 `MongoTransactionManager` 交易）、S3 儲存回歸＋key prefix、本地 S3 parity、pydantic-settings 集中設定、dashboard write-file-only／edit_file 重啟用、QUESTION wire event、Langfuse tracing、log annotation AOP、deepagent inbound bearer、artifact srcdoc 認證交付＋CSP meta、xlsx 密文直存＋workspace zip-only 快照、sync-upstream `--official`/`--test` 旗標與 GitHub sha 錨點。遠端目前只有 `master`，無 open PR、無 open 實驗臂（`exp/*` 皆已 merge 或關閉）。三側測試以 `cd backend && ./mvnw test`／`cd frontend && npm test`／`cd deepagent-service && uv run pytest` 為準（PR gate）。前端 :3000、backend :${BACKEND_PORT:-8080}、deepagent-service :8000。
- **待辦積壓**（未實作，勿當現況）：API datasource（spec 已定案）、MCP connector 資料進 dashboard（見下）、agent 端 sandbox（見下）、artifact `/repair` rate limit、同 session 併發 `/chat` 409、CSP 全螢幕導出隔離收尾。
- **開發流程**：subagent-driven（superpowers plugin；`.claude/agents/` 有 `code-reviewer`＝opus、`spring-boot-engineer`＝sonnet）——implementer/task reviewer 用 sonnet、整支 branch 最終審查用 opus；主迴圈（任何模型）只負責規劃、架構與驗收，不寫 code（小型直接修正除外）。格式由 `.claude/settings.json` 的 PostToolUse hook 自動跑（google-java-format／prettier+eslint／ruff），勿手動改格式風格。ledger `.superpowers/sdd/progress.md` 為跨 session 恢復地圖，任務完成即記帳。
- **多人協作**（多條 session/多人同時開發時）：每人一條 branch（建議各自 worktree）；同一條 branch NEVER 同時有兩個 session 或兩個 implementer。合併一律走 PR（`gh pr create`，遠端 session 無 gh 時用 GitHub MCP），gate＝後端 `./mvnw test`＋前端測試＋deepagent `uv run pytest` 全綠＋opus 全分支終審 Ready to merge，終審結論寫進 PR 描述。**進 `master` 一律 merge commit，NEVER squash／rebase merge**（internal 同步錨點靠 `Upstream-Commit:` trailer 與 GitHub sha 祖先鏈：squash 讓 trailer 消失、rebase 讓錨點不在新歷史裡）；GitHub 端的整合分支（如 `feat/9E`）一旦有 internal 側錨點指向它就 NEVER rebase／force-push。跨人進度追蹤用 plan 檔的 `- [ ]` checkbox（隨 branch commit）與 PR，NEVER 依賴他人的 `.superpowers/sdd/progress.md`（gitignored 個人恢復地圖，不共享）。分工以 spec/plan 為單位認領，plan 之間檔案不重疊；無法避免時以 PR 順序序列化、後者 rebase（僅限尚未被錨點指向的個人 feature branch）。

## Harness 設計前提（2026-09 起，優先於舊有「弱模型」取捨）

- **模型只會更強**：現行 deepseek-v4-flash，之後只升不降。新增任何「替模型代勞」的確定性 scaffolding（canned steps、硬編碼分析劇本、gate 掉模型能力的白名單、為特定弱點加的 prompt 補丁）前 MUST 先問：這是**契約/安全**（保留）還是**補模型能力**（不做，或做成可整段拔除的獨立模組）。既有的弱模型補丁在觸碰時優先移除而非擴充；移除前用當前模型實測。
- **保留的確定性層是契約與安全，不是模型能力補丁**：`__ERD_RESULTS__` 物件列 Proxy 契約、literal-scan 注入白名單、DuckDB 先掛後鎖、filesystem jail、`</`→`<\/` escape、`frame_data_content` 包裝不可信內容、iframe sandbox＋CSP——這些與模型強弱無關，NEVER 因模型變強而拆。
- **知識放 skills，不塞 system prompt**：system prompt 維持薄（`app/agent/prompts.py`），圖表/dashboard 知識在 `deepagent-service/skills/dashboard/SKILL.md` 漸進揭露；新增領域知識一律走 skill 檔，且以「給更強模型的參考」語氣寫（說明契約與理由），NEVER 寫成逐字模板強迫模型照抄。
- **Sandbox 即將可用**（agent 可在 agent-service 側執行任意程式碼）：目前 master **尚無** code-execution 工具，現行隔離模型＝鎖門 DuckDB＋fs jail＋engine 純度。在 sandbox 落地前 NEVER 在 deepagent-service 加任意程式碼執行工具（`exec`/subprocess/未鎖 DuckDB）。設計新工具時以「未來會跑在 sandbox 內」為前提：工具介面與 sandbox 執行器解耦（tool 只定義輸入/輸出契約，執行後端可替換）、輸出一律經 `frame_data_content`、結果落 workspace 而非只留在對話。sandbox 落地時，先評估哪些現行確定性限制（如「NEVER 在瀏覽器 JS 現算統計」）可交給模型在 sandbox 內算好再注入。
- **MCP connector 資料進 dashboard（規劃中，master 未實作）**：agent 產出的 HTML 未來可含 JavaScript，對「來自 MCP connector 呼叫、以 JS 變數形式注入」的資料做轉換。契約走向：connector 呼叫在服務端完成、結果以 JS 變數注入 HTML（同 `__ERD_RESULTS__` 的注入哲學：資料由系統注入、模型只寫引用），瀏覽器 NEVER 直接打 connector（維持 CSP `connect-src 'none'` 與 self-contained artifact 契約）；模型寫的 JS 可對這些變數做轉換/彙整——這是對現行「JS 只笨渲染」規則的**刻意放寬**，僅適用 connector 資料。實作時 MUST：① 沿用 `2026-08-08-api-datasource-design.md` 的 sources manifest diff 通知模型資料變動；② 注入前 escape 與現行 `results.py` 一致；③ connector 回應視為不受信內容；④ 更新 dashboard skill 的資料契約節與本檔。動工前先開 spec，NEVER 直接寫進 `prompts.py`。

## General

- 變數/參數/lambda 參數 NEVER 用 1–2 字元名稱（`id` 等 domain 語彙除外）；一律描述性單詞（domain 語彙優先）；迴圈計數器用 `index`/`rowIndex`/`columnIndex` 等
- google-java-format（由 Claude hook 自動執行，勿手動改格式風格）
- Entity ID 用 Mongo `@Id`（String UUID）：null id 由 `PersistenceConfig` 的 `BeforeConvertCallback<T>` 在 save 前補 `UUID.randomUUID().toString()`（取代 JPA `@UuidGenerator`——Spring Data Mongo 對 null String `@Id` 預設賦 24 字元 ObjectId hex，不符 36 字元 UUID 契約，新 entity MUST 一併補這道掛鉤）；時間戳一律 Mongo Auditing（`@EnableMongoAuditing` + `@CreatedDate`/`@LastModifiedDate`，語意同 JPA Auditing）。例外：`ChatSession` 採 client 指定 id（session upsert 設計），無 generator、實作 `Persistable<String>`，建立時 MUST 先 `setId()`——理由見該 entity class Javadoc
- 多文件寫入的原子性**已採 Branch 3 交易方案（`feat/oracle-to-mongodb-txn`）**：`MongoTransactionManager` ＋ `@Transactional`/`TransactionTemplate`，全 backend 三處多文件寫入（`AgentConversationWriter.persistHtmlResult`、`ArtifactRepairService`、`FileService.upload` 批次）皆受交易保護。**standalone Mongo 不支援交易**——本機/測試/compose 皆 MUST 是單成員以上 replica set（`rs.initiate`），NEVER 對著 standalone Mongo 跑會觸發交易的路徑；曾評估的 standalone 補償方案（孤兒 reaper＋DB 補償）未採用，見 `feat/oracle-to-mongodb-compensation` 分支歷史
- Health 檢查用 Spring Boot Actuator，不自寫 health controller
- 前端 API 一律相對路徑 `/api`；api/hooks/utils 頂層維持；components 可依內聚分子資料夾（不做 features 分層）
- DTO 一律 Java record；例外統一走 `@RestControllerAdvice`
- Secrets NEVER 放入 `application.properties`；一律用 env vars
- 多行文字輸出（email、report）用 Velocity template（`.vm` 放 `src/main/resources/templates/`）；NEVER 用 String 拼接

## Backend (Java / Spring Boot)

- Java 17（internal 環境；NEVER 用 18+ API）

### 注入與結構

- 一律 constructor injection；NEVER 使用 `@Autowired` field injection
- 例外類與 `GlobalExceptionHandler` 一律放 `com.erd.cowork.exception` package
- 使用者身分由 `CurrentUserFilter`（`tsso.enabled=false` 時註冊；internal 環境由 internal 側同層 filter 取代）自 `X-User-Id` 填入 `CoworkContextHolder`（`com.erd.cowork.context`，ThreadLocal，比照 `SecurityContextHolder`；NEVER 換成 InheritableThreadLocal）；service 以 `CoworkContextHolder.userId()` 取值，method 簽名 NEVER 傳 userId。**async/SSE/boundedElastic 邊界前 MUST 先把 userId 取成值**（進入點如 `MessageController` 取一次值、往下顯式傳 `*As(userId, ...)` 變體，或用 `CoworkContextHolder.wrap` capture/restore），非請求執行緒（排程/背景）讀到的是 `null`。sessionId 屬資源位址，維持顯式參數
- 使用 `@RequiredArgsConstructor` 產生 constructor；不手寫 constructor boilerplate
- 分層順序：Controller → Service → Repository；不得跨層直接呼叫
- Config binding 用 `@ConfigurationProperties`；NEVER hardcode URL、credentials、環境值
- **類別命名分類法**（命名即契約，code review 強制）：

  | 後綴／位置 | 類別 | 結構要求 |
  |---|---|---|
  | `*Service` / `*Controller` / `*Provider` / `*Assembler` / `*Validator` / `*Rewriter` / `*Repairer` / `*Guard` / `*Repository` / `*Mapper` / `*Config` / `*Properties` / `*Handler` / `*Interceptor` / `*Writer` / `*Normalizer` / `*Decryptor` | Spring bean | 有 Spring stereotype（`@Component`/`@Service`/`@RestController`/`@Repository`/`@Configuration`/`@ConfigurationProperties`/`@RestControllerAdvice`）或為 MapStruct `@Mapper` interface；絕不用 `new` 建立 |
  | `*Utils` | static utility | `final` class、僅 `private` 建構子（拋 `UnsupportedOperationException`）、全 `static` 方法、無實例欄位、無 Spring 註解 |
  | `*Helper` | per-use 有狀態 helper | 無 Spring 註解；有實例狀態；class Javadoc MUST 標記 `non-bean: instantiate per <context>.`；MUST 用 `new` 建立 |
  | `*Dto` | API record | `record`；位於 `..web.dto..` package |
  | `..parsing.model..` 內 | domain record | 全 `record`；無 Spring 註解 |
  | `*Exception` | 例外 | 位於 `..exception..` package |

### Lombok

- 使用 `@Slf4j`；NEVER 手寫 `private static final Logger log = LoggerFactory.getLogger(...)`
- Entity NEVER 用 `@Data`；改用 `@Getter` + `@Setter` + `@EqualsAndHashCode(of = "id")` + `@ToString(exclude = {lazy collections})`
- Immutable DTO/response 用 Lombok `@Value` 或 Java record；request 用 Java record + Bean Validation
- Entity 使用 `@Builder` 時 MUST 一併加 `@NoArgsConstructor` + `@AllArgsConstructor`

### MapStruct

- Entity↔DTO 轉換一律用 MapStruct `@Mapper(componentModel = "spring")`；NEVER 手寫 mapping 或用 ModelMapper
- Mapper MUST 加 `unmappedTargetPolicy = ReportingPolicy.ERROR`，防止欄位靜默遺失
- `toEntity()` 中 DB-managed 欄位（`id`、`createdAt` 等）MUST 加 `@Mapping(target = "...", ignore = true)`

### API 設計

- 每個 Controller class MUST 加 `@Tag`；每個 endpoint MUST 加 `@Operation` + `@ApiResponse`
- 每個 DTO 欄位 MUST 加 `@Schema(description, example)`
- 所有 `@RequestBody` MUST 加 `@Valid`；Controller class MUST 加 `@Validated`
- GET NEVER 改變狀態；POST 建立資源回傳 201；DELETE 回傳 204；資源不存在回傳 404；衝突回傳 409
- NEVER 在 API response 直接暴露 `@Document` entity；一律用 DTO
- 若引入 Spring Security，config MUST whitelist `/v3/api-docs/**`、`/swagger-ui/**`、`/actuator/health`（v1 無認證，不引入 Security）
- 每個專案 MUST 加入 `springdoc-openapi-starter-webmvc-ui`；NEVER 用已棄用的 Springfox

### Exception Handling

- NEVER 空的 catch block；NEVER 吞掉例外不處理
- 拋出新例外 MUST 包裝原始 cause：`throw new XxxException("msg", e)`
- 所有 IO 資源 MUST 用 try-with-resources；NEVER 手動 `.close()` 放在 finally

### 日誌規範

- 關鍵路徑 MUST 在 controller 進入點記 request 參數摘要（sessionId、長度、計數等）；service 進出 log 用 `@LogAnnotation`（`com.erd.cowork.logging`，AOP 自動附 userId／耗時／例外類名；參數預設不印，開 `args` 時 MUST 設 `maxArgsLength`）；NEVER log API key、token、完整 prompt/HTML、使用者資料內容

### Null Safety

- NEVER 做 chained call 而不做 null check；NEVER `Optional.get()` 不先確認 `isPresent()`
- Public API NEVER 回傳 `null`；改用 `Optional<T>` 或空 collection
- null/empty 檢查優先用 Spring `StringUtils` / `ObjectUtils` / `CollectionUtils`；NEVER 手寫 `x == null || x.isEmpty()` 鏈

### Transaction

- 多步驟寫入 MUST 加 `@Transactional`；讀取 service method 加 `@Transactional(readOnly = true)`——**Mongo 現況例外**：Mongo 純讀不需要交易保護（無跨文件一致性問題），`readOnly` 交易對 Mongo 是 no-op，`SessionService`/`ArtifactService` 等讀取 method 已全數移除 `@Transactional(readOnly = true)`；交易只用在下方「多文件寫入的原子性」列出的三處多文件寫入，NEVER 因為這條規則對純讀 method 誤加
- MongoDB 交易 MUST 搭配單成員以上 replica set（standalone 不支援交易，`MongoTransactionManager` 會直接失敗）；本機/測試/compose 皆已切 replica set，見下方「MongoDB / Database」
- 交易範圍內 NEVER 包慢 IO／遠端呼叫（Mongo 交易 server-side 存活上限約 60s，超時會被中止）：遠端 LLM 呼叫、全量資料組裝等 MUST 在進交易前先做完或留在 `transactionTemplate.execute` 之外，交易內只留需要原子性保護的快寫入——範例見 `ArtifactRepairService.repairFromBrowserErrors`（LLM 呼叫留在交易外）、`AgentConversationWriter.persistHtmlResult`（資料組裝在交易前完成）

### Design Patterns

- Observer pattern 用 Spring Events（`ApplicationEventPublisher` + `@EventListener`）；NEVER 用 raw Singleton 做全域狀態
- Runtime 多實作選擇用 Spring Map-based Factory（inject `List<BeanType>`，build `Map<String, BeanType>`）

## deepagent-service（Python / FastAPI / deepagents）

- **一律 `uv run`**（`uv sync` 安裝；NEVER `pip install`）；lint `uv run ruff check .`、測試 `uv run pytest`。internal 走 `requirements.txt`（不讀 `uv.lock`），改依賴後 MUST `uv export --no-dev --no-hashes --format requirements-txt -o requirements.txt`（`tests/test_requirements_sync.py` 攔漂移）；`deepagents==0.5.5` 釘死（internal registry 只有 0.5.x），版本上限見 `pyproject.toml` `constraint-dependencies`
- **分層純度（ruff TID251 強制）**：`app/engine/**` 只准 stdlib＋boto3＋duckdb＋openpyxl，NEVER import langchain*/langgraph/langfuse/deepagents——`app/agent/**` 是唯一知道 LLM 存在的地方；HTTP 呼叫外部 API 的程式放 `app/agent/tools/`
- **設定**：`app/config.py` 的 pydantic `Settings` 為 key 清單／型別／預設的權威，優先序 env > `one.properties`（`ONE_PROPERTIES_PATH`，本機預設 `one-local.properties`，gitignored）> 欄位預設；新增欄位 MUST 同步更新進版控的 `one.properties` 範本；secrets 留空。`AGENT_API_BEARER_TOKEN` 空字串啟動即炸，NEVER 放行未驗證的 `/chat`／`/repair`（`/health` 豁免）
- **工具契約**（`app/agent/tools/data.py`）：never-raise——錯誤以 `SQL_ERROR:` 字串回給模型；一切不可信內容（cell 值、欄名、外部回應）進 LLM 視野前 MUST 經 `frame_data_content` 包裝；`@tool("bare_name")`；模型看的 markdown 表截 `LLM_VIEW_MAX_ROWS=200`、落檔截 `STORE_MAX_ROWS=5000`；新工具 MUST 在 `app/agent/events.py` `step_title_for` 補中文步驟標題
- **DuckDB 先掛後鎖**：`open_locked_connection` mount 全部完成後 `SET enable_external_access=false` + `lock_configuration=true`（不可逆）；NEVER 為輪中追加資料解鎖或重開連線（只能記憶體 `CREATE TABLE`＋`INSERT`）；NEVER 安裝 `httpfs`——s3 模式資料先下載到 `.sources-cache/` 再交 DuckDB 讀本地路徑
- **Workspace 檔案契約**：`dashboard.html` 可整份 `write_file` 或針對性 `edit_file`（主 agent 掛三個 middleware：`SerializedToolCallsMiddleware` 序列化 tool call、`WiringManifestMiddleware` 每次 model call 重建 qN 綁定、`DashboardSkillGateMiddleware` 未讀 skill 前擋寫；`DashboardWriteFileOnlyMiddleware` 是已拔除的弱模型補丁，保留於 `middleware.py` 但不再掛上——這正是「模型變強即移除補丁」的既有範例）；`queries/*.sql`／`results/*.json` create-only、`qN` 跨輪遞增不重號；`sources.md`／`.skills/`／`.sources-manifest.json` 每輪由系統重建，非模型可寫；是否發 `DASHBOARD_HTML` 由 mtime 快照比對決定，NEVER 靠模型宣告。`ChatTurn`（`app/agent/chat_turn.py`）是 turn 生命週期唯一入口（`prepare`→`stream`→`finalize`→`cleanup_scratch`），ErrorEvent 提前終止的輪 NEVER persist
- **注入契約**（`app/engine/results.py`、`theme_rewrite.py`）：只注入「HTML literal 引用到 ∩ workspace 現有落檔」的 `qN`；JSON 內 `</` MUST 轉義 `<\/`；`<script id="erd-results-data">` 標記供 `strip_injected_blocks` 確定性剝除；`apply_erd_theme` 冪等。改注入格式時 MUST 同步更新 `skills/dashboard/SKILL.md` 的 Data contract 節與 Java `ArtifactAssembler` 的偵測邏輯
- **AgentRuntime 接縫**（`app/agent/runtime/base.py`，共用檔）：`build_agent` 七個參數（model/tools/system_prompt/backend/skills/checkpointer/middleware）一個都不能少；改介面時 MUST 同 PR 更新 `docs/internal-implementation-guide.md`。`internal_runtime.py`、`upload_decrypt.py` 為 internal 獨佔檔，家裡 NEVER 動其實作內容；解密接縫只吞「模組不存在」，impl 壞掉一律炸出
- **Skills**：`skills/dashboard/SKILL.md` 單檔契約（每輪 stage 進 workspace `.skills/`，user 個人 skill 覆寫同名 builtin）；prompt 知識優先放 skill，`prompts.py` 維持薄
- **Langfuse** 一律自架；三個 `LANGFUSE_*` 皆未設即 no-op；NEVER 指向雲端 SaaS

## MongoDB / Database

- Entity↔collection 用 `@Document(collection = "...")`；跨 entity 參照一律純 String 欄位（`sessionId`、`artifactId` 等），無 DB 強制外鍵——ownership／關聯查詢靠索引直查，NEVER 假設有 join
- 需要關聯資料時分開查（`findBySessionId` 等 derived query），NEVER 在 loop 中對每筆結果再各自查一次關聯（N+1 同樣適用於 document store）
- 有並發修改需求的 Entity MUST 加 `@Version`（optimistic locking，Spring Data Mongo 原生支援）
- 大量結果查詢 MUST 用 `Pageable`/`Page<T>`；NEVER 無限制 `findAll()` 無分頁
- Schema 無 migration 工具（Mongo schema-less）：collection shape 由 entity class 本身權威定義；索引改由 `MongoIndexInitializer`（`@Component` + `@EventListener(ApplicationReadyEvent.class)`）以 `MongoTemplate.indexOps()` 顯式建立（不用 auto-index-creation），新增查詢模式前先確認對應索引已建
- 測試走 flapdoodle 嵌入式 mongod（`de.flapdoodle.embed.mongo`，test scope）：`EmbeddedReplicaSetMongo`（JVM 存活期間共用，static）啟動**單成員 replica set**（自動 `rs.initiate()` 並等到 PRIMARY），`ReplicaSetMongoTestInitializer`（`ApplicationContextInitializer`，透過 `spring.factories` 全域生效）把連線字串注入每個 `@SpringBootTest`/`@DataMongoTest` context——交易在測試才成立，standalone 無交易；本機直跑（`./mvnw spring-boot:run`）不含嵌入式 Mongo，需另起真實 Mongo（單成員 replica set，見 README）

## Frontend (React / TypeScript)

### 版本

- React 鎖 **^18.x**（react/react-dom/@types 一律 18，不升 React 19）；antd **6.x**（原生支援 React 18，不需相容補丁）

### 元件規範

- 所有元件使用 `React.FC<Props>` + TypeScript props interface
- 元件結構順序：Props interface → Hooks → Handlers（useCallback） → Render → default export
- 獨立路由或重型第三方元件才用 `React.lazy(() => import('...'))`，並以 `<SuspenseLoader>` 包覆；單頁現況不強制
- NEVER 用 loading spinner early return（`if (isLoading) return <Spin />`）；一律用 `<SuspenseLoader>` 包圍內容

### 資料抓取

- 主要資料抓取 MUST 用 `useSuspenseQuery`；NEVER 用 `useQuery` + `isLoading` 模式做 loading 判斷
- API 呼叫集中在 `src/api/`，使用共用 `apiClient`（axios instance）；route 一律相對路徑 `/api/...`
- Mutation 失敗用 `onError` callback 處理；`useSuspenseQuery` 錯誤用 `<ErrorBoundary>` 接住

### 樣式

- 所有樣式用 Tailwind CSS utility classes；條件 class 組合建議 `cn()`（clsx/tailwind-merge）；引入前用模板字串亦可
- Class string 超過 100 行時抽成獨立 `.classes.ts`
- 使用者通知一律用 antd `message` 或 `App.useApp()`；NEVER 自訂 toast
- **字型 stack 常數**：`src/theme/fonts.ts` export `FONT_FAMILY`（`'Inter Variable', 'Noto Sans TC', -apple-system, 'PingFang TC', 'Microsoft JhengHei', sans-serif`）；三處落地：`index.css` body/root、Tailwind `@theme --font-sans`、antd ConfigProvider `theme.token.fontFamily`。NEVER hardcode font stack 字串，一律引用常數

### TypeScript

- 嚴格模式；NEVER 使用 `any` 型別
- Function MUST 有明確 return type
- Type-only import MUST 用 `import type { ... }`

### 效能

- 傳給子元件的 event handler MUST 用 `useCallback` 包裝
- 昂貴計算用 `useMemo`；昂貴元件用 `React.memo`
- 搜尋輸入 debounce 300–500ms
- `useEffect` MUST 回傳 cleanup function 避免 memory leak

### Import Aliases

- 使用 `@/` alias（指向 `src/`，定義於 vite.config + tsconfig）；不使用其他自訂 alias

## Testing

- Controller 測試用 `@WebMvcTest` + `@MockitoBean`（Spring Boot 3.4+）；NEVER 在 controller slice test 啟動完整 Spring context
- 測試方法命名格式：`methodName_condition_expectedBehavior`（例：`createUser_duplicateEmail_returns409`）
- 每個新功能 MUST 有對應單元測試；多步驟流程 MUST 有 integration test
- PR 合併前 MUST 確認 `./mvnw test` 全部通過
- deepagent-service 測試用 pytest（`asyncio_mode=auto`）＋ `tests/fake_model.py` 假 chat model，NEVER 打真 LLM；FastAPI 契約測試用 httpx；`tests/` 豁免 TID251，但 engine 測試 NEVER 藉此把 LLM 框架依賴帶進 engine 實作
- 前端測試用 Vitest + React Testing Library；斷言元素級行為（NEVER snapshot-only）；fetch mock 用 `vi.stubGlobal`；每個互動元件 MUST 有行為測試
- **Backend Mongo 測試共享單一 JVM-wide DB**：全域 `ApplicationContextInitializer`（`ReplicaSetMongoTestInitializer`，經 `spring.factories` 生效）把所有 `@SpringBootTest`/`@DataMongoTest` context 導向同一個 static 共用的單成員 replica set 嵌入式 mongod、同一個 `cowork` database，**無 per-test 清理**。斷言 MUST 按唯一 id/session scope 或 before/after delta 計數；NEVER 用全域絕對計數（如 `collection.count() == 1`）——會被其他測試留下的資料污染，跑到不確定的順序依賴失敗
