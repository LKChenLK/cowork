#!/usr/bin/env bash
# 單向同步：把上游（GitHub 經 internal GitLab 鏡像）整棵樹取代進來，再還原 internal 獨佔路徑。
# 產出一條 sync/upstream-<sha> branch 供人工適配後發 PR，NEVER 直接推 develop。
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# 用法（模式一律由旗標明確指定，見下方模式判定與 docs/internal-sync.md）：
#   --official <gl/上游ref> <GitHub sha>   正式同步；站在要同步進去的主線 branch 上
#                                          執行，主線＝目前所在 branch；兩個引數順序
#                                          固定，第一個 MUST 以 gl/ 開頭、第二個
#                                          MUST 是 7-40 位 hex（GitHub 上該 commit
#                                          的 sha）
#   --test <gl/上游ref>                    測試同步；只接受一個以 gl/ 開頭的引數，沒有
#                                          主線可指定，MUST 站在自建的 test/* branch 上跑
# 其他寫法（不帶旗標、旗標打錯、參數數量不對）一律印用法並以非零碼結束。
# NEVER 直接改本腳本字面值——本腳本是上游檔,同步會把修改蓋回預設,下一次執行就拒跑。
usage() {
  cat >&2 <<'USAGE'
用法：
  sync-upstream.sh --official <gl/上游ref> <GitHub sha>
  sync-upstream.sh --test <gl/上游ref>
USAGE
}

if [ "$#" -lt 1 ]; then
  usage
  exit 1
fi

MODE="$1"
shift

case "$MODE" in
  --official)
    if [ "$#" -ne 2 ]; then
      usage
      exit 1
    fi
    UPSTREAM_REF="$1"
    UPSTREAM_SHA_INPUT="$2"
    case "$UPSTREAM_REF" in
      gl/*) ;;
      *)
        usage
        exit 1
        ;;
    esac
    if ! [[ "$UPSTREAM_SHA_INPUT" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
      usage
      exit 1
    fi
    MAIN_BRANCH=$(git rev-parse --abbrev-ref HEAD)
    TEST_MODE=0
    ;;
  --test)
    if [ "$#" -ne 1 ]; then
      usage
      exit 1
    fi
    case "$1" in
      gl/*) ;;
      *)
        usage
        exit 1
        ;;
    esac
    UPSTREAM_REF="$1"
    MAIN_BRANCH="develop" # 測試模式沒有主線概念：擁有路徑固定從 develop 還原
    TEST_MODE=1
    ;;
  *)
    usage
    exit 1
    ;;
esac

# 測試模式判定（現在只剩 in-place 一種路線；disposable 的 test/upstream-<sha>
# 一次性 branch 已移除——測試同步 MUST 先由使用者自建 test/* branch，反覆站在上面
# 疊快照，不再每次都新切一條）：
#   站在使用者自建的 test/* 上 → in-place：就地疊一顆快照 commit，branch 不變
#   站在其他 branch 上         → 拒跑，無法判斷意圖，防止整棵樹替換波及不相干的 branch
#（沒有主線概念，站在主線上一樣落在「其他 branch」這支，一律拒跑）
if [ "$TEST_MODE" = "1" ]; then
  CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
  case "$CURRENT_BRANCH" in
    test/*) ;;
    *)
      echo "測試模式 MUST 站在 test/* branch（就地疊快照）上執行——防止把其他 branch 整棵樹替換掉。" >&2
      exit 1
      ;;
  esac
fi

# --multiple 讓每個引數各自當一個 remote 抓；沒有它 `gl origin` 會被解成 gl 底下的 refspec。
git fetch -q --multiple gl origin

# 官方模式的主線就是目前站的 branch，舊的「MUST 在 MAIN_BRANCH 上執行」守門因此沒有
# 意義，改成直接檢查目前 branch 本身：不能是 detached HEAD、不能是 test/* 或
# sync/* branch、且 origin/$MAIN_BRANCH 要存在。放在 fetch 之後、其餘步驟之前，
# 讓後面依賴 origin/$MAIN_BRANCH 的步驟不會用不相干的錯誤訊息蓋掉這裡的問題。
if [ "$TEST_MODE" = "0" ]; then
  if [ "$MAIN_BRANCH" = "HEAD" ]; then
    echo "目前是 detached HEAD，正式同步 MUST 站在具名主線 branch 上執行。" >&2
    exit 1
  fi
  case "$MAIN_BRANCH" in
    test/*|sync/*)
      echo "目前站在 ${MAIN_BRANCH} 上，正式同步不能站在 test/* 或 sync/* branch 上執行。" >&2
      exit 1
      ;;
  esac
  if ! git rev-parse --verify -q "origin/${MAIN_BRANCH}" >/dev/null; then
    echo "origin/${MAIN_BRANCH} 不存在，確認目前 branch 已推上 origin。" >&2
    exit 1
  fi
fi

if ! git rev-parse --verify -q "${UPSTREAM_REF}^{commit}" >/dev/null; then
  echo "找不到 ${UPSTREAM_REF}——GitLab 鏡像可能未帶 feature branches，檢查鏡像設定或手動推入。" >&2
  exit 1
fi

# 官方模式新增三道 sha 守門：GitLab 鏡像不是純鏡像（每條 gl/* branch 上會多出
# GitHub 沒有的 commit），錨點與快照樹改從人工帶入的 GitHub sha 拿，GitLab 多出來
# 的東西完全不進 internal。三道都通過才把 UPSTREAM/UPSTREAM_SHORT 定案。
if [ "$TEST_MODE" = "0" ]; then
  if ! git rev-parse --verify -q "${UPSTREAM_SHA_INPUT}^{commit}" >/dev/null; then
    echo "找不到 ${UPSTREAM_SHA_INPUT} 這個 commit：可能 ${UPSTREAM_REF} 尚未包含它，或短碼有歧義（改帶完整 sha）。" >&2
    exit 1
  fi
  if ! git merge-base --is-ancestor "$UPSTREAM_SHA_INPUT" "$UPSTREAM_REF"; then
    echo "${UPSTREAM_REF} 不包含這個 GitHub commit（${UPSTREAM_SHA_INPUT}）。" >&2
    exit 1
  fi
  # --no-renames: 改名要拆成刪除加新增, 不然會被當成純新增放行.
  if ! git diff --no-renames --diff-filter=MD --quiet "$UPSTREAM_SHA_INPUT" "$UPSTREAM_REF"; then
    echo "GitLab 端相對這個 GitHub commit 修改或刪除了檔案，或帶的 sha 太舊：" >&2
    git diff --no-renames --name-status --diff-filter=MD "$UPSTREAM_SHA_INPUT" "$UPSTREAM_REF" >&2
    exit 1
  fi

  UPSTREAM=$(git rev-parse "$UPSTREAM_SHA_INPUT")
  UPSTREAM_SHORT=$(git rev-parse --short "$UPSTREAM_SHA_INPUT")
fi

# 清單權威來源＝origin/${MAIN_BRANCH}（fetch 之後），NEVER 讀工作樹：in-place 測試
# 模式站在 test/* branch 上時，工作樹是前一輪疊上去的快照（＝上游版清單），讀工作樹
# 會讓 internal 客製清單（例如新增的擁有路徑）從第二輪起悄悄失效。還原與守門排除
# 範圍共用同一份清單，避免兩者失同步而漏守或誤報。
OWNED_LIST_CONTENT=$(git show "origin/${MAIN_BRANCH}:scripts/internal-owned-paths.txt" 2>/dev/null) || {
  echo "找不到 origin/${MAIN_BRANCH}:scripts/internal-owned-paths.txt——主線缺少獨佔清單，確認 MAIN_BRANCH 正確且已推上 origin。" >&2
  exit 1
}
OWNED=(); EXCLUDES=()
while read -r ownedPath; do
  [ -n "$ownedPath" ] || continue
  OWNED+=("$ownedPath"); EXCLUDES+=(":(exclude)$ownedPath")
done <<< "$OWNED_LIST_CONTENT"

# 基準點＝origin/develop 上最後一顆已落地的同步 commit；用 commit 而非 tag，因為 tag 可能
# 隨分支移動，指向從未真正落地的狀態。
LAST_SYNC=$(git log "origin/${MAIN_BRANCH}" --grep='^upstream-sync: ' -1 --format=%H || true)
if [ -z "$LAST_SYNC" ]; then
  echo "找不到基準同步 commit。首次同步 MUST 先人工 bootstrap（見 docs/internal-sync.md）。" >&2
  exit 1
fi
LAST_UPSTREAM=$(git log -1 --format=%B "$LAST_SYNC" | sed -n 's/^Upstream-Commit: //p')
if [ -z "$LAST_UPSTREAM" ]; then
  echo "基準 commit $LAST_SYNC 缺少 Upstream-Commit trailer——同步 PR 被 squash 了？" >&2
  exit 1
fi

# 正式同步的錨點鏈 MUST 單調前進：上一個錨點必須是本次同步目標（GitHub sha）的祖先。
# 不是＝錨點被污染（測試/feature 同步誤入主線）或上游 force-push，先人工修錨再同步。
if [ "$TEST_MODE" = "0" ] && ! git merge-base --is-ancestor "$LAST_UPSTREAM" "$UPSTREAM"; then
  echo "基準錨點 ${LAST_UPSTREAM} 不是 ${UPSTREAM} 的祖先——錨點鏈回退或被污染，MUST 人工修復。" >&2
  exit 1
fi

# 前置守門——全部 MUST 通過，NEVER 為了讓同步跑完而跳過。
if [ -n "$(git status --porcelain)" ]; then
  echo "worktree 不乾淨；read-tree --reset 會吃掉未提交的修改。" >&2
  exit 1
fi
if [ "$TEST_MODE" = "0" ]; then
  if [ -n "$(git diff --name-only "$LAST_SYNC" "$MAIN_BRANCH" -- . "${EXCLUDES[@]}")" ]; then
    echo "獨佔清單外有 internal 改動，同步會無聲抹掉它們：" >&2
    git diff --name-only "$LAST_SYNC" "$MAIN_BRANCH" -- . "${EXCLUDES[@]}" >&2
    exit 1
  fi
fi
if [ -n "$(git ls-files --others --exclude-standard -- . "${EXCLUDES[@]}")" ]; then
  echo "有野生 untracked 檔，git add -A 會把它們永久收編成同步 commit 的一部分：" >&2
  git ls-files --others --exclude-standard -- . "${EXCLUDES[@]}" >&2
  exit 1
fi

if [ "$TEST_MODE" = "1" ]; then
  UPSTREAM=$(git rev-parse "$UPSTREAM_REF")
  UPSTREAM_SHORT=$(git rev-parse --short "$UPSTREAM_REF")
fi

# 雙邊擁有檔：列出上游動過的交給人工調和。錨點 MUST 是 $LAST_UPSTREAM，不是 $LAST_SYNC
# ——後者是 internal 版，拿它比上游永遠有差、每次都誤報。
MANUAL_NOTES=""
while read -r mergePath; do
  [ -n "$mergePath" ] || continue
  if ! git diff --quiet "$LAST_UPSTREAM" "$UPSTREAM" -- "$mergePath"; then
    MANUAL_NOTES="${MANUAL_NOTES}需人工調和：${mergePath}"$'\n'
  fi
done < scripts/manual-merge-paths.txt

if [ "$TEST_MODE" = "1" ]; then
  # 測試模式雙重隔離：test-sync: 前綴、trailer 換名——就算被違規 squash 進主線，
  # 基準 grep 與 trailer 解析也都讀不到它。branch 前綴那重隔離已隨 disposable
  # test/upstream-<sha> 一併移除：測試模式現在只在使用者自建的 test/* branch 上
  # 就地執行，不再由本腳本命名/建立任何 branch。
  COMMIT_PREFIX="test-sync"
  TRAILER_NAME="Test-Upstream-Commit"
  # 測試模式在 subject 附加上游 ref——in-place 反覆疊快照時，一眼就能看出這顆
  # commit 疊的是哪個 feature branch。
  COMMIT_SUBJECT="${COMMIT_PREFIX}: 同步至 ${UPSTREAM_SHORT}（${UPSTREAM_REF}）"
else
  SYNC_BRANCH="sync/upstream-${UPSTREAM_SHORT}"
  COMMIT_PREFIX="upstream-sync"
  TRAILER_NAME="Upstream-Commit"
  # sha 是人工輸入、認不出是哪條上游 ref，subject 附加 gl/ref 方便辨認。
  COMMIT_SUBJECT="${COMMIT_PREFIX}: 同步至 ${UPSTREAM_SHORT}（經 ${UPSTREAM_REF}）"
fi
if [ "$TEST_MODE" = "1" ]; then
  # in-place：不切新 branch，直接在使用者自建的 test/* branch 上疊一顆快照
  # commit。擁有路徑改從 origin/$MAIN_BRANCH（而非 $MAIN_BRANCH）撈——使用者站在
  # test/* branch 上，本機 $MAIN_BRANCH 可能是舊的，fetch 後的 origin 版本才新鮮。
  # 允許上游 sha 未變就重跑——照樣疊一顆（可能是空的）快照 commit。
  git read-tree -u --reset "$UPSTREAM_REF"                    # 整棵樹換成指定上游 ref，含其刪除
  # 還原＝先刪後取，owned 路徑嚴格等於主線版本——單純 checkout 是聯集，上游新增檔會殘留。
  # 來源＝新鮮的 origin，而非可能過期的本機 $MAIN_BRANCH。
  for ownedPath in "${OWNED[@]}"; do
    git rm -rfq --ignore-unmatch -- "$ownedPath"
    git checkout "origin/${MAIN_BRANCH}" -- "$ownedPath"
  done
  git add -A
  # --allow-empty：理由同官方模式——擁有路徑還原後淨變更常是零，但此 commit MUST 落地。
  git commit -q --allow-empty -m "${COMMIT_SUBJECT}" \
    -m "${MANUAL_NOTES}" -m "${TRAILER_NAME}: ${UPSTREAM}"
  git push -q -u origin HEAD
else
  git checkout -qb "$SYNC_BRANCH"
  git read-tree -u --reset "$UPSTREAM"            # 整棵樹換成 GitHub sha（不是 gl/ref），含其刪除
  # 還原＝先刪後取，owned 路徑嚴格等於主線版本——單純 checkout 是聯集，上游新增檔會殘留。
  # 相對切出點淨變更為零。
  for ownedPath in "${OWNED[@]}"; do
    git rm -rfq --ignore-unmatch -- "$ownedPath"
    git checkout "$MAIN_BRANCH" -- "$ownedPath"   # 官方模式清單讀 origin、還原讀本機——分歧方向由守門①(EXCLUDES 同源 origin)fail-close 擋下
  done
  git add -A
  # --allow-empty：雙邊擁有檔還原後淨變更常是零，但此 commit MUST 落地——它是下次同步的
  # 基準點，也是 MANUAL_NOTES 待辦的唯一落地處。
  git commit -q --allow-empty -m "${COMMIT_SUBJECT}" \
    -m "${MANUAL_NOTES}" -m "${TRAILER_NAME}: ${UPSTREAM}"
  git push -q -u origin "$SYNC_BRANCH"
fi

if [ "$TEST_MODE" = "1" ]; then
  echo "已在 ${CURRENT_BRANCH} 上就地疊一顆快照 commit（來源 ${UPSTREAM_REF}），branch 未變。"
  echo "  本模式整棵樹替換——此 branch 上非 test-sync 的手工改動會被覆蓋；internal 接縫改動請進 ${MAIN_BRANCH} 獨佔路徑。"
  echo "  NEVER merge 進 ${MAIN_BRANCH}——測完即刪（本地與 origin 都刪）。"
else
  echo "已推出 ${SYNC_BRANCH}。接著人工完成："
  echo "  1. 檢視 diff，確認上游改動"
  echo "  2. 調和 commit body 列出的雙邊擁有檔"
  echo "  3. 接縫適配（上游若改了 AgentRuntime 等介面，internal 實作要跟著改）"
  echo "  4. 發 PR 進 ${MAIN_BRANCH}，CI 綠燈後合併（MUST NOT squash）"
fi
