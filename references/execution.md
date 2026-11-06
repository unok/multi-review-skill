# 実行詳細

SKILL.md の Step 2・4・5・6・7・8 から参照される実行手順。制御フロー（分岐・順序・上限・責務）は SKILL.md を正とし、ここには具体的なコマンド形・数値・生成手順を置く。

## 作業ディレクトリと絶対パス（Step 4）

- 各レビュアーの作業ディレクトリは、git 管理下なら共通のリポジトリルート、非 git なら対象ディレクトリ（ファイル指定ならその親）とする。パス指定が複数のルート（リポジトリまたは非 git の対象ディレクトリ）にまたがり、作業ディレクトリを一意に決められない場合は中断してユーザーに確認する。
- プロンプトはシェル引数に直接埋め込まず、イテレーションディレクトリにファイルとして保存し、codex / claude は stdin で、cursor-agent はパス参照で渡す（シェル特殊文字や可変長フラグに壊されるのを防ぐ）。
- プロンプトファイル・出力ファイルは絶対パスで指定する（作業ディレクトリは対象プロジェクトのルートで、スクラッチパッドはその外にあるため）。出力先は当該イテレーションディレクトリ配下（例 `iter-1/claude.md`, `iter-1/role-security.md`）。

## 時間制限（Step 4）

- レビュアーの時間制限は省略しない。`timeout` がなければ `gtimeout` を使い、どちらもなければバックグラウンドジョブを10分で打ち切る代替手順を使う。制限なしでの実行は認めない。
- 以下のコマンド例の `timeout 600`（レビュアーは600秒＝10分）は、この解決した時間制限コマンド（`timeout` / `gtimeout` / バックグラウンド打ち切り）に読み替える。

## 対象埋め込みの生成（Step 4）

全レビュアーのプロンプトに、そのイテレーションのレビュー対象範囲を具体的に埋め込む。パス指定なら対象パス一覧を書く。git 差分の場合、呼び出し元が差分本文と対象ファイル一覧を codex / claude / cursor-agent のプロンプトに埋め込む（claude は `--tools "Read,Glob,Grep"` でコマンドを実行できず、codex は `exec review` の差分指定を使えないため。後述）。差分基準は対象のモードで分岐する。

- 未コミット変更モード: `git diff HEAD` の出力＋untracked ファイルの本文＋`git status --porcelain` 由来のファイル一覧を埋め込む。HEAD が存在しない（コミットが1つもない）場合は全対象ファイルの本文を差分の代わりに埋め込む。
- ベース差分モード: merge-base 起点の `git diff <base>...HEAD` の出力とファイル一覧を埋め込む。再レビューでは、修正で生じた未コミットの差分（`git diff HEAD` の出力と untracked ファイルの本文）も全レビュアーのプロンプトに加える（SKILL.md Step 7 の規定に対応）。

差分が大きすぎる場合は、差分全文をイテレーションディレクトリにファイルとして保存し、その絶対パスをプロンプトに記載して各レビュアーに読ませる（claude は Read で読める）。対象判定・ファイル一覧・全レビュアーで同じ範囲を使う。

## レビュアー別コマンド（Step 4）

### codex（総合1インスタンス。`codex` プロファイルでは専門役割も担当）

- モデルと reasoning effort は `~/.codex/config.toml` の既定に依存せず明示指定する（既定はコーディング委譲用に変わり得るため）。モデルは `gpt-6.1-sol`、effort は総合 `medium`、`codex` プロファイルの専門役割 `high`。専門役割は役割ごとに別インスタンスで起動する（`default` では claude が担当）。
- コマンドは対象のモードによらず `timeout 600 codex -m gpt-6.1-sol -c model_reasoning_effort="<effort>" exec -s read-only --skip-git-repo-check -o <出力ファイル> - < <役割プロンプトファイル>` とする（`-` で stdin からプロンプトを読み、`-o` で最終メッセージをファイル出力する）。`--skip-git-repo-check` は git リポジトリ外での即時失敗を防ぐ。git 差分は前述の対象埋め込みでプロンプトに含める。
- `exec review --uncommitted` / `exec review --base <ブランチ>` は使わない。codex-cli 0.161.0 ではどちらもプロンプトとの併用を受け付けず（`the argument '--uncommitted' cannot be used with '[PROMPT]'`）、役割プロンプトと報告フォーマットを渡せないため。
- 役割プロンプトはスキルのベースディレクトリ配下の `references/roles.md` から取得し、対象（パスまたは差分範囲）を埋め込む。出力ファイル名は総合が `role-general.md`、専門役割が roles.md の各役割見出しの `role-<slug>.md`。台帳の `source` は `codex-general` / `codex-<slug>` とする。

### cursor-agent（汎用レビュアー、1インスタンス。既定で無効 — SKILL.md Step 3 で明示指定時のみ起動）

- cursor-agent は長いプロンプトを引数で渡すと exit 0 のまま空出力になるため、プロンプトファイルのパスを含む短い指示を引数に渡し、cursor-agent 自身にファイルを読ませる: `timeout 600 cursor-agent -p --mode plan --trust --output-format text "まず <プロンプトファイル> を読み、その指示に従ってレビューを実行し、指示された報告フォーマットで結果を出力すること" > <出力ファイル> 2>&1`。plan モードは読み取り専用。
- `--trust` はヘッドレス実行に必要。対象リポジトリの信頼を確認できない場合はユーザーに確認し、許可されなければ cursor-agent を欠席として最終レポートに明記する。

### claude（総合1インスタンス。`default` プロファイルでは専門役割も担当）

- 総合: `default` は Opus 5.5 medium、`codex` は Opus 5.5 high。プロンプトは roles.md の総合役割（codex の総合インスタンスと同一）に共通報告フォーマットと対象埋め込みを連結したもの。出力先は `claude.md`、台帳の `source` は `claude-general`。
- 専門役割（`default` のみ）: roles.md の各役割プロンプトに共通報告フォーマットと対象埋め込みを連結し、`--model claude-opus-5-5 --effort high` で役割ごとに起動する。出力先は `role-<slug>.md`。`cat <プロンプトファイル> | timeout 600 claude -p --model claude-opus-5-5 --effort high --tools "Read,Glob,Grep" > <出力ファイル> 2>&1`。台帳の `source` は `claude-<slug>`。
- 総合のコマンド（`default`）: `cat <プロンプトファイル> | timeout 600 claude -p --model claude-opus-5-5 --effort medium --tools "Read,Glob,Grep" > <出力ファイル> 2>&1`。`--tools "Read,Glob,Grep"` で読み取り専用ツールに制限する。`--permission-mode plan` は `-p` と併用すると ExitPlanMode 呼び出しに失敗して指摘本文が最終メッセージから消えるため使わない。
- 総合のコマンド（`codex`）: `cat <プロンプトファイル> | timeout 600 claude -p --model claude-opus-5-5 --effort high --tools "Read,Glob,Grep" > <出力ファイル> 2>&1`。このプロファイルでは claude の専門役割を起動しない。

### ja-review（日本語校閲レビュアー、条件付き — SKILL.md Step 3 の条件を満たすときだけ起動）

- 起動手順の正は ja-review スキル（`~/.claude/skills/ja-review/SKILL.md`）の Step 1〜3 とする。対象はそのイテレーションのレビュー対象範囲（SKILL.md Step 4）を使い、ja-review の Step 1 で対象を決め直さない。
- バッチのプロンプト（`batch-N.txt`）と出力は、当該イテレーションディレクトリ配下の `ja-review/` に置く（出力は `ja-review/out-N.md`）。agy のコマンド形・時間制限・成功判定・1 回だけの再実行は ja-review スキルに従う。全バッチが失敗したら ja-review を欠席として扱う。
- 他のレビュアーと並べて起動する（起動方式は SKILL.md Step 4）。指摘の選別は SKILL.md Step 5 で行う（ja-review の Step 5 以降の報告・修正は実行しない）。

## 出力の成功判定・リトライ・欠席（Step 4）

- 出力ファイルが共通報告フォーマットの指摘または「問題なし」を含むことを成功条件とする。exit 0 でも空・形式外・締めの挨拶だけの出力は失敗としてリトライ対象にする。
- 失敗・タイムアウトしたレビュアーは1度だけ再起動する。それでも失敗なら欠席として除外し、最終レポートに明記する。

## スナップショット（Step 6）

委譲の直前にスナップショットをイテレーションディレクトリへ保存する（元々未コミット変更が対象の場合に修正分を特定するため）。

- git 管理下で HEAD あり: `git diff HEAD` の出力と untracked ファイルの一覧および内容を保存する。
- HEAD が存在しない（コミット0件）または非 git 対象（複製モード）: レビュー対象範囲と委譲する指摘の位置に含まれるファイルを複製して保存する。作業ツリー全体の複製はしない。スナップショット外のファイルへの修正は diff 照合できないため、実装せずに報告する。

Step 7 の diff 照合は、このスナップショット（複製の場合はその複製）との比較で行う。

## 委譲の共通指示（Step 6・7）

初回委譲・差し戻し・再委譲には、指摘一覧（指摘 ID・全位置・問題・修正案・該当する指摘の【パターン】）、レビュー対象範囲、作業ディレクトリ（「作業ディレクトリと絶対パス」で決定）、git と HEAD の有無、品質チェック、複製モードのスナップショット対象ファイル、失敗内容（差し戻し時）、承認内容（承認済み指摘）、以下の共通指示を渡す。範囲は「対象埋め込みの生成」に従って書き込む。「パターンの指摘」の定義は SKILL.md Step 4 を正とし、「実装前の報告」の基準は Step 5 を正とし、ブロック内はその写しとする。multi-review を実行させないのは、修正サブエージェントが CLAUDE.md / AGENTS.md の規定に従って入れ子で起動するのを防ぐためである。

```text
指摘を解消する最小の変更をする。行を足す前に、既存文の修正・削除で対応できないか検討する。
検索は git 管理下では git grep --untracked（`--no-exclude-standard` は付けず、`.gitignore` の対象は検索しない。ビルド成果物などの生成物を拾わないため）、非 git では grep を使う。検索の実行エラーを一致なしとして扱わない。
既存の部品で済むならそれを使う。新しい関数・ヘルパー・ファイルが必要なら、先に同種の既存のものを探し、見つからないときだけ作る。作るときは最も近い既存の兄弟（同じ層・同じ種類のファイル）と置き場所・名前・形を揃え、公開範囲は使う場所に必要な最小にする。
指摘が挙げた位置（「他:」を含む）はレビュー対象範囲の内外にかかわらず直す。指摘の位置で不備を確認できなかった場合は、指摘 ID・位置・確認結果を呼び出し元に報告する。
パターンの指摘は、複数箇所の指摘、または書き方の不揃い・命名・エラー処理の流儀など、同じ形が他の箇所にもありうる指摘をいう。指摘一覧の【パターン】が目印。修正前にリポジトリ全体（非 git なら渡された作業ディレクトリ全体）を検索し、追加の箇所をプロンプトのレビュー対象範囲の内外に分ける。範囲内は全て直し、範囲外は直さずに「範囲外の同じパターン」として位置を報告する。直した全箇所を指摘 ID とともに報告する。
実装前の報告: 複製モードでスナップショット外のファイル（新規作成を含む）の変更が必要な指摘は、実装せずに指摘 ID・必要な変更・理由を報告する。それ以外で、新しい設定項目の追加、新しい状態管理の導入、外部への作用（ネットワーク・ファイル・DB スキーマ）の追加、公開 API の変更が必要な指摘も、未承認なら同じく実装せずに報告する。
検出した品質チェック（テスト・lint 等）を通す。品質チェックの緩和・スキップ・--no-verify は禁止。
multi-review は呼び出し元が実施するので実行しない。
```

## 台帳 `findings.json`（Step 2・5・6・7）

呼び出し元が書く唯一の正本。`jev-guard ledger render` と `jev-guard rereview` が読む。

```json
{
  "reviewId": "multi-review-<タイムスタンプ>",
  "iteration": 1,
  "findings": [
    {
      "id": "F1",
      "severity": "高",
      "source": "codex-general",
      "file": "src/register.ts",
      "line": 42,
      "problem": "email が未検証のまま DB に保存される",
      "fixSummary": "zod で email 形式を検証し、不正なら 400 を返す",
      "status": "fixed",
      "fixedIteration": 1
    },
    {
      "id": "F2",
      "severity": "低",
      "source": "claude-general",
      "file": "src/register.ts",
      "line": 10,
      "problem": "命名が不統一",
      "status": "rejected",
      "reason": "既存規約に従っている"
    }
  ]
}
```

- `id` は実行内で一意（`F1`, `F2`, …。イテレーションをまたいでも振り直さない）。`severity` は `高` / `中` / `低`。`source` は出所レビュアー（共通指摘は `,` 区切りで列挙）。
- `status` は `unresolved` / `fixed` / `on_hold` / `rejected`。`reason` は `rejected` と `on_hold` で必須、それ以外は書かない（`null` も不可）。`fixSummary` は `fixed` にしたときに書く。`fixedIteration` は修正したイテレーションの正の整数を記録する。省略された修正済み指摘は rereview の対象外。
- `iteration` は現在のイテレーション番号に更新する。
- 複数箇所の指摘は `file` / `line` に代表位置を入れ、残りは `problem` の末尾に「（他: path:line, path:line）」と列挙する。Step 7 で対応を確認した追加の箇所もここに追記し、括弧付きの「他:」がなければ新設する。不備のない位置はこの列挙から削り、残りがなければ括弧ごと削除し、代表位置を外したら `file` / `line` を選び直した位置に差し替える。フィールドや `status` の値は増やさない。`jev-guard ledger render` と `jev-guard rereview` が固定のスキーマで検証し、未知のフィールドや `status` の値をエラーで拒否するため。
- 範囲外の同じパターンはレビューの対象外で、指摘として選別しないため、台帳には記録しない。

## ゲート `jev-guard rereview`（Step 7）

- 修正 diff をイテレーションディレクトリに保存する: git 管理下なら委譲前スナップショットとの差分（`git diff HEAD` の出力に untracked ファイルの本文を連結したもの）を `iter-N/fix.diff` に書く。複製モードなら複製と現在のファイルの `diff -u` を連結して書く。
- 実行: `jev-guard rereview --provider claude --findings <レビューディレクトリ>/findings.json --diff iter-N/fix.diff --base-summary "<Step 1 で確定した対象の1行要約>"`。判定は標準出力の JSON。`verdict`（`needed` / `skippable`）、`reason`、`findings[]`（指摘ごとの `resolved` と `confidence`）、`newRisk`、`verdictId`、`skippable` のときは `reviewPrompt` を持つ。
- 結末の記録: `jev-guard record --id <verdictId> --outcome accepted|overruled [--note "<理由>"]`。`skippable` を採用したか覆したかを判定ログに残す。`needed` には記録しない。
- 判定ログは `~/.local/share/jev-guard/verdicts.jsonl`（`JEV_GUARD_LOG_DIR` で変更可）。集計は `jev-guard stats`。

## 範囲外の記録 `out-of-scope.md`（Step 6・7・8）

呼び出し元は、修正済みの指摘に由来する範囲外の同じパターンだけを、レビューディレクトリ直下の `out-of-scope.md` に「- path:line（指摘 ID、iter-N）」の書式で記録する。同じ位置は行を分けず、指摘 ID とイテレーションを `（F3, F7、iter-2, iter-3）` のようにそれぞれ `,` 区切りで併記する。Step 8 は記録された行をそのまま列挙する。未作成は空として扱う。

## レポートファイル `report.md`（Step 8）

呼び出し元が台帳 `findings.json` と `out-of-scope.md` から書く。`ledger.md` は修正概要を含まないので流用しない。

```markdown
# multi-review レポート（<タイムスタンプ>）

- 対象: <Step 1 で確定した対象の1行要約>
- イテレーション: <N>（打ち切った場合はその理由）
- プロファイル: <default|codex>
- レビュアー: <起動した CLI・役割・モデル・effort> / 欠席: <なし or 一覧>
- ゲート: <イテレーションごとの結果を「iter-1: …」の形で>
- 品質チェック: <Step 8 のラベル>

## 未解決（<件数>）
なし
## 修正済み（<件数>）
- F1 [中]【共通】src/register.ts:42（codex-general, claude-general）email が未検証のまま DB に保存される → 修正概要: zod で email 形式を検証し、不正なら 400 を返す（iter-1）
## 保留（<件数>）
- F3 [中] src/api.ts:10（claude-architecture）… → 保留理由: <reason>
## 却下（<件数>）
- F2 [低] src/register.ts:10（claude-general）命名が不統一 → 却下理由: 既存規約に従っている
## 範囲外の同じパターン（<件数>）
<「範囲外の記録」の書式で列挙>
```

- 台帳の指摘は1件1行。各行は指摘 ID（`F1` など）から始め、各節の中は重要度（高→中→低。同じ重要度内は指摘 ID の昇順）で並べ、複数のレビュアーからの共通指摘には【共通】を付ける。行末ラベルは節ごとに「→ 修正概要:」「→ 保留理由:」「→ 却下理由:」を使う。範囲外の同じパターンの集約は「範囲外の記録」に従う。
- 未解決は打ち切り時にだけ残り、行末は「→ 未解決理由: <差し戻しの失敗内容など>」とする。0件の節は見出しの下に「なし」と書く（上記の未解決を参照）。

### exclude への追記（Step 8）

- 判定: `git check-ignore -q .multi-review/<タイムスタンプ>/report.md` を実行する。終了コード 0（すでに無視されている）なら追記不要でそのまま書き出す。
- 追記: 終了コード 1（無視されていない）なら、`git rev-parse --git-path info/exclude` が指すパスの親ディレクトリ（`info/`）がなければ作成し、`.multi-review/` を1行追記する。追記後にもう一度 `git check-ignore -q` を実行し、無視されるようになったか確かめる。
- 追記できない（`.git` が読み取り専用など）場合、または追記しても無視されない場合（`.gitignore` の `!.multi-review/` のような否定パターンが exclude より優先される場合を含む）は、レビューディレクトリ直下に `report.md` を置き（非 git と同じ扱い）、その理由を会話の最終レポートに書く。

## 品質チェック（Step 6 の基準・Step 7 の判定）

- 実行コマンドはプロジェクトの構成ファイル（package.json / Makefile / pyproject.toml / build.gradle 等）や CLAUDE.md から検出する。検出したコマンドにはレビュアーとは分離した時間制限（1800秒目安）を前置する。品質チェックは対象プロジェクトのコードを実行するため、ユーザー自身が開発している信頼済みプロジェクトを前提とする。
- 時間制限の超過は失敗ではなく「判定不能」とする。
- 基準記録（Step 6・初回イテレーション）: 品質チェックを検出できた場合のみ委譲前に1回実行して基準結果を記録する。基準実行にも同じ時間制限（1800秒目安）を前置し、超過・実行不能なら「基準なし」とする。未検出も「基準なし」とする。
- 失敗の分類（Step 7）: 基準結果と照合して「既存の失敗」か「回帰」かを判定する。基準がない（未検出・判定不能・修正後に初めて検出）場合は回帰と断定せず「判定不能」とする。
- `.multi-review/` 配下のファイル（レポートファイル等）が原因の失敗は、基準記録・回帰判定のいずれからも除外する。
