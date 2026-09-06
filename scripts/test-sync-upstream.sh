#!/usr/bin/env bash
# sync-upstream.sh 的守門驗證：每個情境 MUST 讓腳本以非零碼中止，守門絕不可跳過。
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK_ROOT=$(mktemp -d)
trap 'rm -rf "$WORK_ROOT"' EXIT
FAILURES=0

expect_abort() {
  local caseName=$1
  shift
  if (cd "$WORK_ROOT/clone" && "$@" >/dev/null 2>&1); then
    echo "FAIL: $caseName —— 腳本應中止卻成功了"
    FAILURES=$((FAILURES + 1))
  else
    echo "ok: $caseName"
  fi
}

setup() {
  rm -rf "$WORK_ROOT"/upstream "$WORK_ROOT"/origin "$WORK_ROOT"/clone "$WORK_ROOT"/seed
  git init -q --bare "$WORK_ROOT/upstream"
  git init -q --bare "$WORK_ROOT/origin"

  git clone -q "$WORK_ROOT/upstream" "$WORK_ROOT/seed"
  (
    cd "$WORK_ROOT/seed"
    git config user.email t@t; git config user.name t
    mkdir -p scripts backend
    cp "$SCRIPT_DIR/sync-upstream.sh" scripts/
    cp "$SCRIPT_DIR/internal-owned-paths.txt" scripts/
    cp "$SCRIPT_DIR/manual-merge-paths.txt" scripts/
    echo "<project/>" > backend/pom.xml
    echo shared > shared.txt
    git add -A && git commit -qm "init"
    git push -q origin HEAD:master
  )

  git clone -q "$WORK_ROOT/origin" "$WORK_ROOT/clone"
  (
    cd "$WORK_ROOT/clone"
    git config user.email t@t; git config user.name t
    git remote add gl "$WORK_ROOT/upstream"
    git fetch -q gl
    git checkout -qb develop gl/master
    # 首次同步前置條件：internal 獨佔檔 MUST 先 commit 到 develop 才有東西可還原（見 docs/internal-sync.md）。
    mkdir -p internal backend/src/internal backend/src/main/resources \
      frontend/src/bootstrap deepagent-service/app/agent/runtime deepagent-service/app/engine
    echo "internal owned" > internal/README.md
    echo "# internal owned" > .env.internal.example
    echo "internal owned" > backend/src/internal/Marker.java
    echo "internal.owned=true" > backend/src/main/resources/application.properties
    echo "<html>internal owned</html>" > frontend/index.html
    echo "export {};" > frontend/src/bootstrap/internal.impl.ts
    echo "# internal owned" > deepagent-service/app/agent/runtime/internal_runtime.py
    echo "# internal owned" > deepagent-service/app/engine/upload_decrypt.py
    git add -A && git commit -qm "internal 獨佔檔 bootstrap"
    # bootstrap：第一顆同步 commit，之後的基準點由它提供。
    git commit -q --allow-empty -m "upstream-sync: bootstrap" \
      -m "Upstream-Commit: $(git rev-parse gl/master)"
    git push -q -u origin develop
  )
}

# 情境 ①：獨佔清單外有 internal 改動
setup
(cd "$WORK_ROOT/clone" && echo tampered > shared.txt && git commit -qam "越界" && git push -q origin develop)
expect_abort "① 獨佔清單外有 internal 改動" bash scripts/sync-upstream.sh --official develop gl/master

# 情境 ②：有野生 untracked 檔
setup
(cd "$WORK_ROOT/clone" && echo stray > stray.txt)
expect_abort "② 有野生 untracked 檔" bash scripts/sync-upstream.sh --official develop gl/master

# 情境 ③：不在 develop 上
setup
(cd "$WORK_ROOT/clone" && git checkout -qb feature/x)
expect_abort "③ 不在 develop 上" bash scripts/sync-upstream.sh --official develop gl/master

# 情境 ④：找不到基準同步 commit（未 bootstrap）
setup
(cd "$WORK_ROOT/clone" && git checkout -qB develop gl/master && git push -qf origin develop)
expect_abort "④ 未 bootstrap（無基準同步 commit）" bash scripts/sync-upstream.sh --official develop gl/master

expect_note() {
  local caseName=$1
  local shouldContain=$2
  local body
  body=$(cd "$WORK_ROOT/clone" && git log -1 --format=%B)
  if [ "$shouldContain" = yes ] && ! grep -q "需人工調和：backend/pom.xml" <<<"$body"; then
    echo "FAIL: $caseName —— commit body 少了待辦行"; FAILURES=$((FAILURES + 1)); return
  fi
  if [ "$shouldContain" = no ] && grep -q "需人工調和" <<<"$body"; then
    echo "FAIL: $caseName —— commit body 不該有待辦行"; FAILURES=$((FAILURES + 1)); return
  fi
  echo "ok: $caseName"
}

# 情境 ⑤：上游動過 pom.xml → commit body MUST 列出待辦
setup
(cd "$WORK_ROOT/seed" && echo "<project><!--changed--></project>" > backend/pom.xml \
  && git commit -qam "上游改 pom" && git push -q origin HEAD:master)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --official develop gl/master >/dev/null 2>&1)
expect_note "⑤ 上游動過 pom.xml → 列出待辦" yes

# 附加斷言（同一情境的落地產物）：官方模式 commit subject MUST 與原版 byte-identical
# ——不帶 UPSTREAM_REF 後綴。測試模式為了在 in-place 反覆疊快照時方便辨識來源
# feature branch，subject 會附加（${UPSTREAM_REF}），但官方同步永遠是 gl/master，
# 加了只是雜訊且破壞既有 commit subject 格式相容性；過去沒有測試釘住這個格式，才
# 讓官方模式一度意外沾上這個後綴，這裡補上，往後再漂移會被抓到。
UPSTREAM_SHORT_5=$(cd "$WORK_ROOT/clone" && git rev-parse --short gl/master)
SUBJECT_5=$(cd "$WORK_ROOT/clone" && git log -1 --format=%s)
if [ "$SUBJECT_5" = "upstream-sync: 同步至 ${UPSTREAM_SHORT_5}" ]; then
  echo "ok: ⑤ 官方 commit subject 與原版 byte-identical（不帶 ref 後綴）"
else
  echo "FAIL: ⑤ 官方 commit subject 漂移 —— got=[$SUBJECT_5] want=[upstream-sync: 同步至 ${UPSTREAM_SHORT_5}]"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ⑥：錨點回歸——上游未動 pom 時不得列出待辦（用 $LAST_SYNC 當錨點會誤報）
setup
(cd "$WORK_ROOT/seed" && echo other > other.txt && git add -A && git commit -qm "上游改別的" \
  && git push -q origin HEAD:master)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --official develop gl/master >/dev/null 2>&1)
expect_note "⑥ 上游未動 pom.xml → 不列待辦" no

# 情境 ⑦：基準點取自 origin/develop——同步 branch 已推但 PR 未合併時，基準不得前移
setup
BASE_BEFORE=$(cd "$WORK_ROOT/clone" && git log origin/develop --grep='^upstream-sync: ' -1 --format=%H)
(cd "$WORK_ROOT/seed" && echo more > more.txt && git add -A && git commit -qm "上游再改" \
  && git push -q origin HEAD:master)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --official develop gl/master >/dev/null 2>&1)
BASE_AFTER=$(cd "$WORK_ROOT/clone" && git fetch -q origin \
  && git log origin/develop --grep='^upstream-sync: ' -1 --format=%H)
if [ "$BASE_BEFORE" = "$BASE_AFTER" ]; then
  echo "ok: ⑦ PR 未合併時基準不前移"
else
  echo "FAIL: ⑦ 基準前移了——基準必須取自 origin/develop 而非分支或 tag"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ⑧：--official 指定非 develop 主線——internal 主線非 develop 時腳本仍可跑
# 獨立一組 throwaway repo（override-*），避免污染上面共用 setup() 的 develop 情境。
OVERRIDE_BRANCH="feature/main"
rm -rf "$WORK_ROOT"/override-upstream "$WORK_ROOT"/override-origin \
  "$WORK_ROOT"/override-clone "$WORK_ROOT"/override-seed
git init -q --bare "$WORK_ROOT/override-upstream"
git init -q --bare "$WORK_ROOT/override-origin"

git clone -q "$WORK_ROOT/override-upstream" "$WORK_ROOT/override-seed"
(
  cd "$WORK_ROOT/override-seed"
  git config user.email t@t; git config user.name t
  mkdir -p scripts backend
  cp "$SCRIPT_DIR/sync-upstream.sh" scripts/
  cp "$SCRIPT_DIR/internal-owned-paths.txt" scripts/
  cp "$SCRIPT_DIR/manual-merge-paths.txt" scripts/
  echo "<project/>" > backend/pom.xml
  echo shared > shared.txt
  git add -A && git commit -qm "init"
  git push -q origin HEAD:master
)

git clone -q "$WORK_ROOT/override-origin" "$WORK_ROOT/override-clone"
(
  cd "$WORK_ROOT/override-clone"
  git config user.email t@t; git config user.name t
  git remote add gl "$WORK_ROOT/override-upstream"
  git fetch -q gl
  git checkout -qb "$OVERRIDE_BRANCH" gl/master
  mkdir -p internal backend/src/internal backend/src/main/resources \
    frontend/src/bootstrap deepagent-service/app/agent/runtime deepagent-service/app/engine
  echo "internal owned" > internal/README.md
  echo "# internal owned" > .env.internal.example
  echo "internal owned" > backend/src/internal/Marker.java
  echo "internal.owned=true" > backend/src/main/resources/application.properties
  echo "<html>internal owned</html>" > frontend/index.html
  echo "export {};" > frontend/src/bootstrap/internal.impl.ts
  echo "# internal owned" > deepagent-service/app/agent/runtime/internal_runtime.py
  echo "# internal owned" > deepagent-service/app/engine/upload_decrypt.py
  git add -A && git commit -qm "internal 獨佔檔 bootstrap"
  git commit -q --allow-empty -m "upstream-sync: bootstrap" \
    -m "Upstream-Commit: $(git rev-parse gl/master)"
  git push -q -u origin "$OVERRIDE_BRANCH"
)

# 比照既有成功情境（⑤⑥⑦）的斷言方式：看落地的側面效果，而非腳本自身 exit code。
(cd "$WORK_ROOT/override-clone" && bash scripts/sync-upstream.sh --official "$OVERRIDE_BRANCH" gl/master >/dev/null 2>&1)
UPSTREAM_SHORT=$(cd "$WORK_ROOT/override-clone" && git rev-parse --short gl/master)
if (cd "$WORK_ROOT/override-clone" && git ls-remote --exit-code origin "sync/upstream-${UPSTREAM_SHORT}" >/dev/null 2>&1); then
  echo "ok: ⑧ --official 指定非 develop 主線——同步在非 develop 主線（${OVERRIDE_BRANCH}）上成功產出 sync branch"
else
  echo "FAIL: ⑧ --official 指定非 develop 主線——未推出 sync branch"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ⑨：測試模式產物形狀（in-place）——disposable 的 test/upstream-<sha> 一次性
# branch 已移除，測試模式只剩 in-place：使用者自建 test/mine，`--test gl/feat/x`
# 沒有主線可指定（固定 develop 當擁有路徑還原來源），就地疊一顆快照 commit，斷言
# 雙重隔離標記（test-sync: 前綴、trailer 換名）與擁有路徑正確還原。
setup
(
  cd "$WORK_ROOT/seed"
  git checkout -qb feat/x
  echo "feature marker" > feature-marker.txt
  git add -A && git commit -qm "上游 feature 分支新檔"
  git push -q origin HEAD:feat/x
)
(cd "$WORK_ROOT/clone" && git checkout -qb test/mine)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --test gl/feat/x >/dev/null 2>&1)
FEATURE_FULL=$(cd "$WORK_ROOT/clone" && git rev-parse gl/feat/x)
CURRENT_BRANCH_9=$(cd "$WORK_ROOT/clone" && git rev-parse --abbrev-ref HEAD)
SUBJECT_9=$(cd "$WORK_ROOT/clone" && git log -1 --format=%s)
BODY_9=$(cd "$WORK_ROOT/clone" && git log -1 --format=%B)
HAS_MARKER_9=$(cd "$WORK_ROOT/clone" && git ls-files | grep -c '^feature-marker.txt$' || true)
OWNED_CONTENT_9=$(cd "$WORK_ROOT/clone" && cat internal/README.md 2>/dev/null || true)
REMOTE_HAS_MINE_9=0
(cd "$WORK_ROOT/clone" && git ls-remote --exit-code origin test/mine >/dev/null 2>&1) && REMOTE_HAS_MINE_9=1
if [ "$CURRENT_BRANCH_9" = "test/mine" ] && [[ "$SUBJECT_9" == test-sync:\ * ]] \
  && grep -q "Test-Upstream-Commit: ${FEATURE_FULL}" <<<"$BODY_9" \
  && [ "$HAS_MARKER_9" = "1" ] && [ "$OWNED_CONTENT_9" = "internal owned" ] \
  && [ "$REMOTE_HAS_MINE_9" = "1" ]; then
  echo "ok: ⑨ 測試模式產物形狀（in-place）"
else
  echo "FAIL: ⑨ 測試模式產物形狀 —— current=[$CURRENT_BRANCH_9] subject=[$SUBJECT_9] marker=[$HAS_MARKER_9] owned=[$OWNED_CONTENT_9] remote=[$REMOTE_HAS_MINE_9]"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ⑩：測試 commit 永不成錨（對抗性，in-place 版本）——接續⑨，模擬違規把 in-place
# 測試 branch（test/mine）merge 進 develop，驗證基準查找仍選到 bootstrap（不是
# test-sync），且後續正式同步不受污染、照常成功。用 -s ours 是為了單純模擬
# 「test-sync commit 混進 develop 歷史」這個違規本身，不夾帶內容變動——內容變動會
# 觸發另一道「獨佔清單外有 internal 改動」守門，混淆此情境要驗的重點。
BOOTSTRAP_SHA=$(cd "$WORK_ROOT/clone" && git log origin/develop --grep='^upstream-sync: ' -1 --format=%H)
(
  cd "$WORK_ROOT/clone"
  git checkout -q develop
  git merge -q -s ours test/mine -m "違規：把 in-place 測試同步 merge 進 develop（模擬）"
  git push -q origin develop
)
(
  cd "$WORK_ROOT/seed"
  git checkout -q master
  echo poststate > poststate.txt
  git add -A && git commit -qm "上游再改一次"
  git push -q origin HEAD:master
)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --official develop gl/master >/dev/null 2>&1)
SELECTED_SHA=$(cd "$WORK_ROOT/clone" && git fetch -q origin \
  && git log origin/develop --grep='^upstream-sync: ' -1 --format=%H)
NEW_UPSTREAM_SHORT=$(cd "$WORK_ROOT/clone" && git rev-parse --short gl/master)
if [ "$SELECTED_SHA" = "$BOOTSTRAP_SHA" ] \
  && (cd "$WORK_ROOT/clone" && git ls-remote --exit-code origin "sync/upstream-${NEW_UPSTREAM_SHORT}" >/dev/null 2>&1); then
  echo "ok: ⑩ 測試 commit 永不成錨（對抗性，in-place 版本）"
else
  echo "FAIL: ⑩ 測試 commit 永不成錨 —— selected=$SELECTED_SHA bootstrap=$BOOTSTRAP_SHA"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ⑪：未知的上游 ref（站在使用者自建的 test/* branch 上）——mode 判定在
# fetch／驗證 ref 存在之前就會放行 test/* branch，中止原因只會是 ref 不存在。
setup
(cd "$WORK_ROOT/clone" && git checkout -qb test/mine)
expect_abort "⑪ 未知的上游 ref" bash scripts/sync-upstream.sh --test gl/no-such-branch

# 情境 ⑫：錨點鏈污染守門——偽造一顆錨點 commit，Upstream-Commit trailer 指到不在
# gl/master 祖先鏈上的 sha（另一條 feature branch 的 commit），正式同步 MUST 中止。
setup
(
  cd "$WORK_ROOT/seed"
  git checkout -qb feat/poison
  echo poison > poison.txt
  git add -A && git commit -qm "不在祖先鏈上的 commit"
  git push -q origin HEAD:feat/poison
)
POISON_SHA=$(cd "$WORK_ROOT/clone" && git fetch -q gl && git rev-parse gl/feat/poison)
(
  cd "$WORK_ROOT/clone"
  git commit -q --allow-empty -m "upstream-sync: 污染" -m "Upstream-Commit: ${POISON_SHA}"
  git push -q origin develop
)
expect_abort "⑫ 錨點鏈污染守門" bash scripts/sync-upstream.sh --official develop gl/master

# 情境 ⑬：IN-PLACE 首次同步——使用者站在自建的 test/* branch 上（而非 $MAIN_BRANCH）跑
# 測試模式，MUST 就地疊一顆快照 commit，不新切 test/upstream-<sha> branch。
setup
(
  cd "$WORK_ROOT/seed"
  git checkout -qb feat/x
  echo "in-place marker 1" > feature-marker-1.txt
  git add -A && git commit -qm "上游 feature 分支新檔 #1"
  git push -q origin HEAD:feat/x
)
(cd "$WORK_ROOT/clone" && git checkout -qb test/mine)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --test gl/feat/x >/dev/null 2>&1)
CURRENT_BRANCH_13=$(cd "$WORK_ROOT/clone" && git rev-parse --abbrev-ref HEAD)
SUBJECT_13=$(cd "$WORK_ROOT/clone" && git log -1 --format=%s)
HAS_MARKER_13=$(cd "$WORK_ROOT/clone" && git ls-files | grep -c '^feature-marker-1.txt$' || true)
OWNED_CONTENT_13=$(cd "$WORK_ROOT/clone" && cat internal/README.md 2>/dev/null || true)
NO_NEW_TEST_BRANCH_13=1
if (cd "$WORK_ROOT/clone" && git branch --list 'test/upstream-*') | grep -q .; then
  NO_NEW_TEST_BRANCH_13=0
fi
REMOTE_HAS_MINE_13=0
(cd "$WORK_ROOT/clone" && git ls-remote --exit-code origin test/mine >/dev/null 2>&1) && REMOTE_HAS_MINE_13=1
if [ "$CURRENT_BRANCH_13" = "test/mine" ] && [[ "$SUBJECT_13" == test-sync:\ * ]] \
  && [ "$HAS_MARKER_13" = "1" ] && [ "$OWNED_CONTENT_13" = "internal owned" ] \
  && [ "$NO_NEW_TEST_BRANCH_13" = "1" ] && [ "$REMOTE_HAS_MINE_13" = "1" ]; then
  echo "ok: ⑬ in-place 首次同步"
else
  echo "FAIL: ⑬ in-place 首次同步 —— current=[$CURRENT_BRANCH_13] subject=[$SUBJECT_13] marker=[$HAS_MARKER_13] owned=[$OWNED_CONTENT_13] no_new_test_branch=[$NO_NEW_TEST_BRANCH_13] remote=[$REMOTE_HAS_MINE_13]"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ⑭：重複 in-place 同步疊 commit＋develop 錨點不受影響——接續⑬，上游 feature branch
# 再推一顆 commit，同一條 test/mine 上重跑，確認疊出第二顆 test-sync commit，且 develop
# 基準仍是 bootstrap（比照⑩的對抗性檢查手法：in-place 快照 commit 不該擾動主線錨點）。
BOOTSTRAP_SHA_14=$(cd "$WORK_ROOT/clone" && git log origin/develop --grep='^upstream-sync: ' -1 --format=%H)
(
  cd "$WORK_ROOT/seed"
  git checkout -q feat/x
  echo "in-place marker 2" > feature-marker-2.txt
  git add -A && git commit -qm "上游 feature 分支新檔 #2"
  git push -q origin HEAD:feat/x
)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --test gl/feat/x >/dev/null 2>&1)
TEST_SYNC_COUNT_14=$(cd "$WORK_ROOT/clone" && git log --oneline | grep -c '^[a-f0-9]* test-sync:')
HAS_MARKER2_14=$(cd "$WORK_ROOT/clone" && git ls-files | grep -c '^feature-marker-2.txt$' || true)
ANCHOR_AFTER_14=$(cd "$WORK_ROOT/clone" && git fetch -q origin \
  && git log origin/develop --grep='^upstream-sync: ' -1 --format=%H)
if [ "$TEST_SYNC_COUNT_14" = "2" ] && [ "$HAS_MARKER2_14" = "1" ] \
  && [ "$ANCHOR_AFTER_14" = "$BOOTSTRAP_SHA_14" ]; then
  echo "ok: ⑭ 重複 in-place 同步疊 commit＋錨點不受影響"
else
  echo "FAIL: ⑭ 重複 in-place 同步疊 commit＋錨點不受影響 —— count=[$TEST_SYNC_COUNT_14] marker2=[$HAS_MARKER2_14] anchor=[$ANCHOR_AFTER_14] bootstrap=[$BOOTSTRAP_SHA_14]"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ⑮：非 test/* branch 拒跑——測試模式下站在既非 $MAIN_BRANCH 也非 test/* 的其他
# branch 上，一律中止。用有效的上游 feature ref（比照⑨用 gl/feat/x）而非不存在的
# ref：若用不存在的 ref，就算把分支守門那個 `*)` arm 拿掉，腳本仍會在稍後的
# 「找不到 ${UPSTREAM_REF}」步驟中止，看起來像是守門生效、實則沒測到它。改用有效
# ref 後才會真的測到這道守門：拿掉 `*)` arm，guard-skip 現在直接讀 TEST_MODE（不再
# 經一層獨立的 IN_PLACE 判定），會讓腳本把 feature/other 當成合法測試路線繼續跑到
# push——變成「不該中止卻成功了」，比誤判成別道守門更隱蔽。因此不用 expect_abort
# （它只驗 exit code），改自行捕捉 stderr，同時斷言 non-zero exit 與分支守門訊息
# 本文，才能真正鎖定這道守門。
setup
(
  cd "$WORK_ROOT/seed"
  git checkout -qb feat/x
  echo "feature marker" > feature-marker.txt
  git add -A && git commit -qm "上游 feature 分支新檔"
  git push -q origin HEAD:feat/x
)
(cd "$WORK_ROOT/clone" && git checkout -qb feature/other)
STDERR_15=$(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --test gl/feat/x 2>&1 >/dev/null)
EXIT_15=$?
if [ "$EXIT_15" -ne 0 ] && grep -q "防止把其他 branch 整棵樹替換掉" <<<"$STDERR_15"; then
  echo "ok: ⑮ 非 test/* branch 拒跑（斷言分支守門訊息）"
else
  echo "FAIL: ⑮ 非 test/* branch 拒跑 —— exit=[$EXIT_15] stderr=[$STDERR_15]"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ⑯：--test 站在非 test/* branch（develop）上拒跑——測試模式沒有主線概念，
# 「站在主線上拒跑」已由「不在 test/* 上一律拒跑」這道通用守門涵蓋，不再有專屬的
# in-place 指路訊息。ref 不需要真的存在——mode 判定在 fetch／驗證 ref 之前就會
# 中止，用 gl/feat/x 純粹比照其他情境的命名習慣。比照⑮的技巧：自行捕捉 stderr，
# 同時斷言 non-zero exit 與通用守門訊息本文。
setup
STDERR_16=$(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --test gl/feat/x 2>&1 >/dev/null)
EXIT_16=$?
if [ "$EXIT_16" -ne 0 ] && grep -q "防止把其他 branch 整棵樹替換掉" <<<"$STDERR_16"; then
  echo "ok: ⑯ --test 站在非 test/* branch（develop）上拒跑"
else
  echo "FAIL: ⑯ --test 站在非 test/* branch（develop）上拒跑 —— exit=[$EXIT_16] stderr=[$STDERR_16]"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ⑰：無旗標呼叫拒跑——舊的無旗標寫法全部移除。
setup
expect_abort "⑰ 無旗標呼叫拒跑" bash scripts/sync-upstream.sh

# 情境 ⑱：未知旗標拒跑。
expect_abort "⑱ 未知旗標拒跑" bash scripts/sync-upstream.sh --bogus develop gl/master

# 情境 ⑲：--official 少給一個位置參數拒跑。
expect_abort "⑲ --official 少一個參數拒跑" bash scripts/sync-upstream.sh --official gl/master

# 情境 ⑳：--official 多給一個位置參數拒跑。
expect_abort "⑳ --official 多一個參數拒跑" bash scripts/sync-upstream.sh --official develop gl/master extra

# 情境 ㉑：--official 兩個位置參數都不是 gl/ 開頭，分不出上游 ref，拒跑。
expect_abort "㉑ --official 兩個都不是 gl/ 拒跑" bash scripts/sync-upstream.sh --official develop master

# 情境 ㉒：--official 兩個位置參數都是 gl/ 開頭，分不出主線，拒跑。
expect_abort "㉒ --official 兩個都是 gl/ 拒跑" bash scripts/sync-upstream.sh --official gl/develop gl/master

# 情境 ㉓：--test 缺上游 ref 拒跑。
expect_abort "㉓ --test 缺上游 ref 拒跑" bash scripts/sync-upstream.sh --test

# 情境 ㉔：--test 多給參數拒跑（測試模式沒有主線概念，只接受一個引數）。
expect_abort "㉔ --test 多給參數拒跑" bash scripts/sync-upstream.sh --test gl/feat/x extra

# 情境 ㉕㉖：用 feature 整合分支當主線（對應 docs/internal-sync.md「用 feature 整合
# 分支當主線」一節）——internal 在非 develop 的主線（9E）上正式收 gl/feat/x；上游
# 之後把 feat/x 以 merge commit 併回 master，主線再接一次 gl/master 時錨點祖先守門
# 仍能通過。獨立一組 throwaway repo（integ-*），比照 ⑧ 的 override-* 命名手法。
INTEGRATION_MAINLINE="9E"
rm -rf "$WORK_ROOT"/integ-upstream "$WORK_ROOT"/integ-origin \
  "$WORK_ROOT"/integ-clone "$WORK_ROOT"/integ-seed
git init -q --bare "$WORK_ROOT/integ-upstream"
git init -q --bare "$WORK_ROOT/integ-origin"

git clone -q "$WORK_ROOT/integ-upstream" "$WORK_ROOT/integ-seed"
(
  cd "$WORK_ROOT/integ-seed"
  git config user.email t@t; git config user.name t
  mkdir -p scripts backend
  cp "$SCRIPT_DIR/sync-upstream.sh" scripts/
  cp "$SCRIPT_DIR/internal-owned-paths.txt" scripts/
  cp "$SCRIPT_DIR/manual-merge-paths.txt" scripts/
  echo "<project/>" > backend/pom.xml
  echo shared > shared.txt
  git add -A && git commit -qm "init"
  git push -q origin HEAD:master
  git checkout -qb feat/x
  echo "feature marker" > feature-marker.txt
  git add -A && git commit -qm "GitHub 端 feat/x 收攏功能"
  git push -q origin HEAD:feat/x
  git checkout -q master
)

git clone -q "$WORK_ROOT/integ-origin" "$WORK_ROOT/integ-clone"
(
  cd "$WORK_ROOT/integ-clone"
  git config user.email t@t; git config user.name t
  git remote add gl "$WORK_ROOT/integ-upstream"
  git fetch -q gl
  git checkout -qb "$INTEGRATION_MAINLINE" gl/master
  mkdir -p internal backend/src/internal backend/src/main/resources \
    frontend/src/bootstrap deepagent-service/app/agent/runtime deepagent-service/app/engine
  echo "internal owned" > internal/README.md
  echo "# internal owned" > .env.internal.example
  echo "internal owned" > backend/src/internal/Marker.java
  echo "internal.owned=true" > backend/src/main/resources/application.properties
  echo "<html>internal owned</html>" > frontend/index.html
  echo "export {};" > frontend/src/bootstrap/internal.impl.ts
  echo "# internal owned" > deepagent-service/app/agent/runtime/internal_runtime.py
  echo "# internal owned" > deepagent-service/app/engine/upload_decrypt.py
  git add -A && git commit -qm "internal 獨佔檔 bootstrap"
  git commit -q --allow-empty -m "upstream-sync: bootstrap" \
    -m "Upstream-Commit: $(git rev-parse gl/master)"
  git push -q -u origin "$INTEGRATION_MAINLINE"
)

# 情境 ㉕：--official <非 develop 主線> gl/feat/x 成功——subject 無後綴，
# Upstream-Commit 指向 gl/feat/x 的 sha。
(cd "$WORK_ROOT/integ-clone" && bash scripts/sync-upstream.sh --official "$INTEGRATION_MAINLINE" gl/feat/x >/dev/null 2>&1)
FEAT_X_SHA_25=$(cd "$WORK_ROOT/integ-clone" && git rev-parse gl/feat/x)
FEAT_X_SHORT_25=$(cd "$WORK_ROOT/integ-clone" && git rev-parse --short gl/feat/x)
SUBJECT_25=$(cd "$WORK_ROOT/integ-clone" && git log -1 --format=%s)
BODY_25=$(cd "$WORK_ROOT/integ-clone" && git log -1 --format=%B)
if [ "$SUBJECT_25" = "upstream-sync: 同步至 ${FEAT_X_SHORT_25}" ] \
  && grep -q "Upstream-Commit: ${FEAT_X_SHA_25}" <<<"$BODY_25"; then
  echo "ok: ㉕ --official 非 develop 主線 gl/feat/x 成功（subject 無後綴，錨點指向 feat/x）"
else
  echo "FAIL: ㉕ --official 非 develop 主線 gl/feat/x —— subject=[$SUBJECT_25] body=[$BODY_25]"
  FAILURES=$((FAILURES + 1))
fi

# 模擬「發 PR 進主線」：把 ㉕ 落地的 sync branch 快轉併回 9E，讓它成為下次同步的
# 基準錨點；接著模擬 GitHub 端把 feat/x 以 merge commit 併回 master。
SYNC_BRANCH_25="sync/upstream-${FEAT_X_SHORT_25}"
(
  cd "$WORK_ROOT/integ-clone"
  git checkout -q "$INTEGRATION_MAINLINE"
  git merge -q --ff-only "$SYNC_BRANCH_25"
  git push -q origin "$INTEGRATION_MAINLINE"
)
(
  cd "$WORK_ROOT/integ-seed"
  git checkout -q master
  git merge -q --no-ff feat/x -m "GitHub 端把 feat/x 併回 master"
  git push -q origin HEAD:master
)

# 情境 ㉖：接續㉕——上游 merge 後，對同一主線跑 --official <主線> gl/master，
# 錨點（feat/x 的 sha）是新 gl/master 的祖先，守門通過並成功產出新 sync branch。
(cd "$WORK_ROOT/integ-clone" && bash scripts/sync-upstream.sh --official "$INTEGRATION_MAINLINE" gl/master >/dev/null 2>&1)
NEW_MASTER_SHA_26=$(cd "$WORK_ROOT/integ-clone" && git rev-parse gl/master)
NEW_MASTER_SHORT_26=$(cd "$WORK_ROOT/integ-clone" && git rev-parse --short gl/master)
SUBJECT_26=$(cd "$WORK_ROOT/integ-clone" && git log -1 --format=%s)
BODY_26=$(cd "$WORK_ROOT/integ-clone" && git log -1 --format=%B)
if [ "$SUBJECT_26" = "upstream-sync: 同步至 ${NEW_MASTER_SHORT_26}" ] \
  && grep -q "Upstream-Commit: ${NEW_MASTER_SHA_26}" <<<"$BODY_26" \
  && (cd "$WORK_ROOT/integ-clone" && git ls-remote --exit-code origin "sync/upstream-${NEW_MASTER_SHORT_26}" >/dev/null 2>&1); then
  echo "ok: ㉖ feat/x 併回 master 後，--official 同主線 gl/master 通過錨點守門並成功"
else
  echo "FAIL: ㉖ feat/x 併回 master 後同步 —— subject=[$SUBJECT_26] body=[$BODY_26]"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ㉗（對抗性）：上游 rebase／force-push 後，舊錨點不再是新 ref 的祖先，正式
# 同步 MUST 被錨點守門擋下。先用 --official 把 gl/feat/x 正式收進 develop（錨點指向
# feat/x 的 sha），把落地的 sync branch 快轉併回 develop 模擬 PR 合併；接著在 seed
# 端「重寫」feat/x（force push 出內容不同、歷史不相干的新 sha），舊錨點從此不在
# 新 feat/x 的祖先鏈上。
setup
(
  cd "$WORK_ROOT/seed"
  git checkout -qb feat/x
  echo "v1" > feature-marker.txt
  git add -A && git commit -qm "上游 feature 分支 v1"
  git push -q origin HEAD:feat/x
)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --official develop gl/feat/x >/dev/null 2>&1)
SYNC_BRANCH_27="sync/upstream-$(cd "$WORK_ROOT/clone" && git rev-parse --short gl/feat/x)"
(
  cd "$WORK_ROOT/clone"
  git checkout -q develop
  git merge -q --ff-only "$SYNC_BRANCH_27"
  git push -q origin develop
)
(
  cd "$WORK_ROOT/seed"
  git checkout -q master
  git branch -qD feat/x
  git checkout -qb feat/x
  echo "v2 rewritten" > feature-marker.txt
  git add -A && git commit -qm "上游 feature 分支重寫（模擬 rebase/force-push）"
  git push -qf origin HEAD:feat/x
)
expect_abort "㉗ 上游 rebase 使錨點不再是祖先" bash scripts/sync-upstream.sh --official develop gl/feat/x

# 情境 ㉘：owned 目錄內上游新增檔在還原後被清除（in-place ＋ 官方模式雙覆蓋）——單純
# git checkout <ref> -- owned/ 是聯集，上游在 owned 目錄內新增的檔會殘留；還原改為
# rm -r 全清再 checkout，owned 路徑須嚴格等於主線版本。
setup
(
  cd "$WORK_ROOT/seed"
  git checkout -qb feat/exact-restore
  mkdir -p internal
  echo "upstream injected" > internal/upstream-injected.txt
  git add -A && git commit -qm "上游在 owned 目錄內新增檔（模擬洩漏，in-place）"
  git push -q origin HEAD:feat/exact-restore
)
(cd "$WORK_ROOT/clone" && git checkout -qb test/exact-restore)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --test gl/feat/exact-restore >/dev/null 2>&1)
INJECTED_PRESENT_28=$(cd "$WORK_ROOT/clone" && git ls-files | grep -c '^internal/upstream-injected.txt$' || true)
OWNED_STILL_28=$(cd "$WORK_ROOT/clone" && cat internal/README.md 2>/dev/null || true)
if [ "$INJECTED_PRESENT_28" = "0" ] && [ "$OWNED_STILL_28" = "internal owned" ]; then
  echo "ok: ㉘ owned 目錄內上游新增檔被清除（in-place）"
else
  echo "FAIL: ㉘ owned 目錄內上游新增檔被清除（in-place）—— injected_present=[$INJECTED_PRESENT_28] owned=[$OWNED_STILL_28]"
  FAILURES=$((FAILURES + 1))
fi

# 官方模式同一斷言——script 內已 checkout -qb 到 sync branch，clone 工作樹直接就是
# 產出結果，不需另外 fetch/checkout。
setup
(
  cd "$WORK_ROOT/seed"
  mkdir -p internal
  echo "upstream injected" > internal/upstream-injected.txt
  git add -A && git commit -qm "上游在 owned 目錄內新增檔（模擬洩漏，官方模式）"
  git push -q origin HEAD:master
)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --official develop gl/master >/dev/null 2>&1)
INJECTED_PRESENT_28B=$(cd "$WORK_ROOT/clone" && git ls-files | grep -c '^internal/upstream-injected.txt$' || true)
OWNED_STILL_28B=$(cd "$WORK_ROOT/clone" && cat internal/README.md 2>/dev/null || true)
if [ "$INJECTED_PRESENT_28B" = "0" ] && [ "$OWNED_STILL_28B" = "internal owned" ]; then
  echo "ok: ㉘b owned 目錄內上游新增檔被清除（官方模式）"
else
  echo "FAIL: ㉘b owned 目錄內上游新增檔被清除（官方模式）—— injected_present=[$INJECTED_PRESENT_28B] owned=[$OWNED_STILL_28B]"
  FAILURES=$((FAILURES + 1))
fi

# 情境 ㉙：清單客製在第二輪 in-place 仍生效——清單改讀 origin/develop 而非工作樹。
# 第二輪 in-place 站在 test/* branch 上時，工作樹已是第一輪疊上去的快照（＝
# read-tree --reset 到上游 ref 後的樹，含上游版的 scripts/internal-owned-paths.txt，
# 不含 internal 在 develop 上新增的 custom-owned/ 那行）——若清單來源退回讀工作樹，
# custom-owned/ 這個新擁有路徑會在這一輪就悄悄失效，上游檔案滲入不會被清除。
setup
(
  cd "$WORK_ROOT/seed"
  git checkout -qb feat/custom-owned
  mkdir -p custom-owned
  echo "upstream custom v1" > custom-owned/upstream.txt
  git add -A && git commit -qm "上游在新路徑新增檔 #1"
  git push -q origin HEAD:feat/custom-owned
)
(
  cd "$WORK_ROOT/clone"
  mkdir -p custom-owned
  echo "internal custom" > custom-owned/internal.txt
  echo "custom-owned/" >> scripts/internal-owned-paths.txt
  git add -A && git commit -qm "internal 新增獨佔路徑 custom-owned/"
  git push -q origin develop
)
(cd "$WORK_ROOT/clone" && git checkout -qb test/custom-owned)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --test gl/feat/custom-owned >/dev/null 2>&1)
(
  cd "$WORK_ROOT/seed"
  git checkout -q feat/custom-owned
  echo "upstream custom v2" >> custom-owned/upstream.txt
  git commit -qam "上游在新路徑新增檔 #2"
  git push -q origin HEAD:feat/custom-owned
)
(cd "$WORK_ROOT/clone" && bash scripts/sync-upstream.sh --test gl/feat/custom-owned >/dev/null 2>&1)
INTERNAL_PRESENT_29=$(cd "$WORK_ROOT/clone" && git ls-files | grep -c '^custom-owned/internal.txt$' || true)
UPSTREAM_ABSENT_29=$(cd "$WORK_ROOT/clone" && git ls-files | grep -c '^custom-owned/upstream.txt$' || true)
if [ "$INTERNAL_PRESENT_29" = "1" ] && [ "$UPSTREAM_ABSENT_29" = "0" ]; then
  echo "ok: ㉙ 清單客製在第二輪 in-place 仍生效"
else
  echo "FAIL: ㉙ 清單客製在第二輪 in-place 仍生效 —— internal_present=[$INTERNAL_PRESENT_29] upstream_absent=[$UPSTREAM_ABSENT_29]"
  FAILURES=$((FAILURES + 1))
fi

echo "---"
if [ "$FAILURES" -gt 0 ]; then echo "$FAILURES 項失敗"; exit 1; fi
echo "全部通過"
