# Internal 環境同步流程

同一份程式碼同時活在 GitHub（家裡，權威）與 internal 環境的內網。兩邊的連通是**單向**的：
GitHub 經 internal GitLab 鏡像流入，internal 側的 commit 永遠推不回 GitHub。策略不是「把
衝突解好」，而是讓重疊面積為零——每個檔案只有一側能寫，任何「兩邊都想改」的地方
都先在家裡開一個接縫。本文件是這條同步流程的權威說明；設計脈絡見
`docs/superpowers/specs/2026-08-03-internal-env-seams-design.md`。

---

## 1. 拓撲

```
① 家裡 push ─────────▶ GitHub master              （家裡，權威）
                              │ 自動鏡像
② 　　　　　　　　　　  ▼
                       internal GitLab  gl/master     （唯讀上游）
                              │
③ 有人跑 sync-upstream.sh ────┤  ← 唯一的人工動作
                              ▼
④                      Azure 工作 repo  develop     （internal 主線，可推）
```

四步裡只有第三步需要人；`scripts/sync-upstream.sh` 在 **Azure 工作 repo 的 clone**
裡執行，remote 設定：

```bash
origin  https://dev.azure.com/.../cowork      # internal 工作 repo，可推
gl      https://gitlab.<internal>/.../cowork      # GitHub 鏡像，只讀
```

「上游（upstream）」指 GitHub 那份，經 GitLab 鏡像被 internal 消費——唯讀、不可修改、
只能整批接收。文件、腳本、commit 訊息一律用 upstream 一詞，不用 vendor（vendor
branch 是同一套做法的業界術語，但這裡的上游是自家程式碼，用 vendor 易誤導）。

---

## 2. 首次 bootstrap

`scripts/internal-owned-paths.txt` 列出的 internal 獨佔路徑在剛建立時，`develop` 上還
不存在——同步腳本靠 `git checkout develop -- <path>` 還原這些檔案，若它們從未被
commit 過，這一步會直接失敗。因此**首次同步 MUST 先由 internal 側把各獨佔檔案 commit 到
`develop`**，之後才可能有東西可還原。

獨佔檔就緒後，人工建立第一顆同步 commit 作為之後所有同步的基準點：

```bash
git commit --allow-empty -m "upstream-sync: bootstrap" \
  -m "Upstream-Commit: $(git rev-parse gl/master)"
git push -u origin develop
```

`Upstream-Commit:` trailer 是腳本找基準點的唯一依據——之後每次同步都從
`origin/develop` 的歷史找最後一顆 `^upstream-sync: ` commit，讀出它的
`Upstream-Commit:` 作為「上次同步到哪」。

---

## 3. 每次同步

在**專用 clone 或 worktree**（不與任何人的工作區共用）的 `develop` 上執行：

```bash
bash scripts/sync-upstream.sh --official develop gl/master
```

`--official` 後面兩個參數順序不拘：以 `gl/` 開頭的是上游 ref，另一個是主線名稱；
兩個都要給，沒有預設值。

腳本會做 replace-then-restore：`git read-tree -u --reset gl/master` 把整棵樹換成
上游（含上游的刪除），再用獨佔路徑清單把 internal 檔案撈回來，最後在一條新切出的
`sync/upstream-<shorthash>` branch 上落一顆 commit 並推上 `origin`。**腳本
NEVER 直接推 `develop`**——落地永遠是 feature branch → 人工確認與適配 → PR。

腳本結束後，人在 `sync/upstream-<shorthash>` branch 上完成：

1. **檢視 diff**，確認上游改動的範圍與內容
2. **調和雙邊擁有檔**——commit body 裡標了「需人工調和」的路徑（目前是
   `backend/pom.xml`、`backend/src/main/resources/application.properties`、
   `frontend/index.html`）需要人工比對上游版本與 internal 版本，決定怎麼合併
3. **接縫適配**——上游若改動了接縫介面（例如 `AgentRuntime` 增加方法），
   `internal_runtime.py` 等 internal 獨佔實作 MUST 在**同一個 PR**裡跟著改，否則
   `develop` 會從同步落地那刻起壞掉，直到有人補救為止
4. **發 PR 進 `develop`**，internal CI 綠燈後合併

腳本執行前會做四道前置守門（在 `develop` 上、worktree 乾淨、獨佔清單外沒有 internal
改動、沒有野生 untracked 檔），任何一道不過就中止，不會往下跑。這是整個流程唯一
的安全裝置，**NEVER 為了讓同步跑完而跳過它們**——若真的擋到你，先解決守門指出的
問題（多半是清單漏列了新的 internal 獨佔檔），而不是繞過檢查。

若 internal 側的主線不叫 `develop`，把主線名稱換成實際的 branch，例如
`bash scripts/sync-upstream.sh --official feature/main gl/master`；上游 ref 也
不一定要是 `gl/master`，見下方「用 feature 整合分支當主線」一節。**NEVER 直接改
腳本裡的字面值**——`scripts/sync-upstream.sh` 是上游檔，同步會把改動蓋回預設，
下一次執行就拒跑。

---

## 3.1 用 feature 整合分支當主線

GitHub 端有時會先把幾個 feature 合進一條整合分支（例如 `feat/9E`），internal 側
短期只能在自己對應的主線（例如 `9E`）上收，還不能動 `develop`。這種情況下，上游
ref 換成該整合分支即可，用法不變：

```bash
bash scripts/sync-upstream.sh --official 9E gl/feat/9E
```

`9E` 這條主線建議從 `develop` 切出（這樣它天生就帶著 `develop` 已有的所有同步
錨點，不需要另外 bootstrap）。收尾方式：

1. GitHub 端把 `feat/9E` 以 **merge commit**（不是 squash）併回 `master`
2. internal 側把 `9E` merge 進 `develop`
3. 之後 `develop` 上跑 `bash scripts/sync-upstream.sh --official develop gl/master`，
   錨點祖先守門會發現 `9E` 帶進來的錨點是新 `gl/master` 的祖先，照常通過並繼續往下同步

兩條鐵律：

- **GitHub 端的整合分支（`feat/9E`）永遠不 rebase、不 force-push**——一旦重寫，
  internal 側已經記下的錨點就不再是它的祖先，下次同步會被錨點守門擋下，MUST 人工
  修錨才能繼續
- **`9E` 併進 `master` 不 squash**——squash 會讓 internal 側記下的 `Upstream-Commit`
  錨點從 `master` 的歷史裡消失，錨點祖先守門會誤判成錨點被污染

---

## 4. 測試模式（in-place：反覆疊上游尚未進 master 的 feature branch 快照）

有時需要在 upstream master 還沒收到某個 feature 之前，先把它同步到 internal 測試
（例如驗證某個接縫改動能不能跑）。測試模式只有一種路線——**in-place**：自己建一條
`test/*` branch，站上去反覆執行同一條指令，每次疊一顆新的快照 commit，腳本不會
替你創建或丟棄任何 branch：

測試模式的錨點查找與獨佔路徑還原固定用 `origin/develop`. 主線不是 develop 的站台, 測試模式
驗到的會是 develop 的組合, 結果不可信; 沒有 develop 的站台會拿到「找不到基準同步 commit」.
這種環境請直接用 `--official <主線> <gl/ref>` 在主線上正式同步.

```bash
git checkout -b test/mine develop              # 只做一次
bash scripts/sync-upstream.sh --test gl/feat/<name>  # 之後每次上游推進都重跑這行，站在 test/mine 上原地執行
```

`--test` 只接受一個以 `gl/` 開頭的參數，沒有主線可以指定——擁有路徑固定從
`develop` 還原。

模式判定表（腳本用「目前站在哪條 branch」判斷）：

| 目前站的位置 | 結果 |
|---|---|
| 自建的 `test/*` | in-place：就地疊一顆快照 commit，branch 不變 |
| 其他 branch | 拒跑，無法判斷意圖，防止整棵樹替換波及不相干的 branch |

雙重隔離讓測試產物在基準機制眼裡完全隱形：

- **commit 前綴**：`test-sync: `（正式模式是 `upstream-sync: `）——基準查找只 grep
  `^upstream-sync: `，測試 commit 無論流到哪裡都掃不到
- **trailer 換名**：`Test-Upstream-Commit:`（正式模式是 `Upstream-Commit:`）——就算
  trailer 解析邏輯改了，換名也讓兩者不會被誤認

上游 sha 沒變時重跑也沒關係，會照樣疊一顆（可能是空的）快照 commit，不必先確認
有沒有新進度。

鐵律：

- **本模式整棵樹替換**——`test/mine` 上任何不是 `test-sync:` 這條路徑產生的手工
  改動，下次重跑都會被覆蓋；internal 接縫改動照舊只能進 `develop` 的獨佔路徑，
  絕不要指望 in-place 測試 branch 能保留它
- **NEVER merge 進 `develop`**——就算違規 merge 了，雙重隔離仍能保證它不會被
  誤選為下次同步的基準錨點，但它會把未經上游正式收錄的內容留在主線上，仍是需要
  人工清理的污染
- **用完刪掉**——驗證告一段落後，`test/mine` 本地與 `origin` 都刪，不要留著佔位
- **正式進場路徑固定**：上游把該 feature merge 進 master 後，走正常同步（不帶測試
  ref）把它收進來，測試 branch 不能取代這條路徑

in-place 中途失敗（如獨佔路徑尚未存在於 `origin/develop`）會留下已被 `read-tree`
改寫的 worktree，用 `git reset --hard` 復原即可（branch 本身用完即棄）。

前置：GitLab 鏡像 MUST 帶上該 feature branch（`--mirror` 鏡像預設會帶，若鏡像設定
被改成只同步 `master`，`gl/feat/<name>` 就抓不到——腳本會在 fetch 後立刻驗證 ref
存在，找不到會直接中止並提示檢查鏡像設定）。

另外，正式同步新增了錨點單調守門：上一次同步的基準 commit MUST 是本次同步目標的
祖先，否則中止並要求人工修錨——這道守門防的是錨點被污染（例如測試/feature 同步
誤入主線）或上游 force-push 之後盲目往下跑。

---

## 5. 硬規則

- 同步 PR **MUST NOT squash 合併**：squash 會丟掉 commit 上的 `Upstream-Commit:`
  trailer，下一次同步就找不到基準點。這條規則 MUST 寫進 internal 側的 PR 流程說明。
- `git remote set-url --push gl no_push`，從物理上擋掉誤推鏡像——`gl` 是唯讀
  上游，不該有人往它推東西。
- **GitLab MUST 是真鏡像（`--mirror`），不是重新匯入**。同步 commit 訊息記的是
  `gl/master` 的 short hash，整個流程的稽核能力全靠它——若鏡像是真 mirror，
  SHA 與 GitHub 完全相同，可直接拿去 GitHub 對照；若改成重新匯入、squash 或
  重打包，SHA 會全部變成 GitLab 自己的，對照能力當場歸零，而且不會有任何錯誤
  訊息。鏡像設定變更 MUST 視為破壞性變更。
- 基準點用 **commit 而非 tag**：PR 可能被放棄或擱置，推分支時就移動的 tag 會
  指向從未落地的狀態；改從 `origin/develop` 的歷史找最後一顆同步 commit，基準
  因此只反映真正合併進 `develop` 的同步。

---

## 6. 四類檔案與兩份清單

| 類別 | 誰能寫 | 同步時 | 例子 |
|---|---|---|---|
| **共用權威檔** | 只有家裡 | 整檔取代 | `pyproject.toml`、`uv.lock`、`requirements.txt`、`main.tsx`、`app/agent/**`（`runtime/internal_runtime.py` 除外）、`.env.example` |
| **internal 獨佔檔** | 只有 internal | 取代後還原 | `backend/src/internal/**`、`internal.impl.ts`、`internal_runtime.py` |
| **雙邊擁有檔** | 兩邊都寫 | 還原＋偵測上游變更後人工調和 | `backend/pom.xml`、`backend/src/main/resources/application.properties`、`frontend/index.html` |
| **不在 repo 內** | 各自 | 不受影響 | `.env`（gitignored）、`~/.m2/settings.xml` |

兩份清單都在 `scripts/`，是還原與守門共用的唯一事實來源：

- **`scripts/internal-owned-paths.txt`**——internal 獨佔路徑，同步時先被上游整棵樹
  蓋掉、再從 `develop` 撈回來；也是守門檢查「獨佔清單外有沒有 internal 改動」的排除
  範圍。**新增 internal 獨佔檔時 MUST 同步更新這份清單**，否則下次同步會把它當成
  「越界改動」擋下，或者更糟——清單沒列到但也沒被上游覆蓋的檔案不會出現在守門
  裡，但一旦上游剛好新增同名路徑，該檔案會被無聲蓋掉。
- **`scripts/manual-merge-paths.txt`**——雙邊擁有檔，內容 MUST 是上面清單的
  子集：先被還原保住 internal 版，再由上游變更偵測攔下需要人工調和的情況（同步時若
  上游也動過該路徑，commit body 會多一行「需人工調和：<path>」）。

`uv.lock` 不在清單內——internal 走 `requirements.txt`，不讀 lock；`requirements.txt`
漂移由 `deepagent-service/tests/test_requirements_sync.py` 在家裡攔截，避免忘記
重新匯出（`uv export --no-dev --no-hashes --format requirements-txt -o
requirements.txt`）而讓 internal 裝到舊依賴。`.env` 也不在清單內，它兩側皆 gitignored、
不在 index，`read-tree` 不會碰它。

家裡側的配合：這些獨佔路徑 **NEVER 加進家裡的 `.gitignore`**。internal 側必須能把獨佔檔
commit 到 `develop`，同步時 `git checkout develop -- <path>` 才有東西可還原；若
家裡把這些路徑寫進 `.gitignore`，該 `.gitignore` 會隨同步傳進 internal 側，使這些檔案變成
被忽略，internal 端得靠 `git add -f` 才追蹤得到，是一個沒必要的陷阱。

---

## 7. 守門的限制

守門的觀察範圍僅限 `develop`：若 internal 側的越界改動還躺在未合併的 feature branch 上，
本次同步看不到，要等它 merge 進 `develop` 之後才會在**下一次**同步被攔下。這不是
漏洞（最終仍會被抓到），但延遲是真的——攔下的時間點可能離犯錯的當下很遠。

因此 internal 側的 code review MUST 一併把關「共用檔不得修改」，不能只依賴同步時的
守門作為唯一防線。

---

## 相關檔案

- `scripts/sync-upstream.sh` — 同步腳本本體（internal 側執行、家裡維護）
- `scripts/internal-owned-paths.txt` / `scripts/manual-merge-paths.txt` — 兩份清單
- `scripts/test-sync-upstream.sh` — 守門行為的自動化驗證，在拋棄式 git repo 上跑
  一系列情境，`bash scripts/test-sync-upstream.sh` 即可執行
- `deepagent-service/app/agent/runtime/base.py` — `AgentRuntime` 接縫（本流程要
  搬運的主體）
