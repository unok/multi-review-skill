# 実行詳細

SKILL.md の Step 4・6・7 から参照される実行手順。制御フロー（分岐・順序・上限・責務）は SKILL.md を正とし、ここには具体的なコマンド形・数値・生成手順を置く。

## 作業ディレクトリと絶対パス（Step 4）

- 各レビュアーの作業ディレクトリは、git 管理下なら共通のリポジトリルート、非 git なら対象ディレクトリ（ファイル指定ならその親）とする。パス指定が複数のルート（リポジトリまたは非 git の対象ディレクトリ）にまたがり、作業ディレクトリを一意に決められない場合は中断してユーザーに確認する。
- プロンプトはシェル引数に直接埋め込まず、イテレーションディレクトリにファイルとして保存し、codex / claude は stdin で、cursor-agent はパス参照で渡す（シェル特殊文字や可変長フラグに壊されるのを防ぐ）。
- プロンプトファイル・出力ファイルは絶対パスで指定する（作業ディレクトリは対象プロジェクトのルートで、スクラッチパッドはその外にあるため）。出力先は当該イテレーションディレクトリ配下（例 `iter-1/claude.md`, `iter-1/role-security.md`）。

## 時間制限（Step 4）

- レビュアーの時間制限は省略しない。`timeout` がなければ `gtimeout` を使い、どちらもなければバックグラウンドジョブを10分で打ち切る代替手順を使う。制限なしでの実行は認めない。
- 以下のコマンド例の `timeout 600`（レビュアーは600秒＝10分）は、この解決した時間制限コマンド（`timeout` / `gtimeout` / バックグラウンド打ち切り）に読み替える。

## 対象埋め込みの生成（Step 4）

codex（パス指定時）/ cursor-agent / claude 用プロンプトには対象を具体的に埋め込む。パス指定なら対象パス一覧を書く。git 差分の場合、claude は `--tools "Read,Glob,Grep"` でコマンドを実行できないため、呼び出し元が差分本文と対象ファイル一覧をプロンプトに埋め込む。cursor-agent 用プロンプトにも同じ差分本文とファイル一覧を埋め込む。差分基準は対象のモードで分岐し、codex のコマンドと対応させる。

- 未コミット変更モード（codex は `--uncommitted`）: `git diff HEAD` の出力＋untracked ファイルの本文＋`git status --porcelain` 由来のファイル一覧を埋め込む。HEAD が存在しない（コミットが1つもない）場合は全対象ファイルの本文を差分の代わりに埋め込む。
- ベース差分モード（codex は `--base <ブランチ>`）: merge-base 起点の `git diff <base>...HEAD` の出力とファイル一覧を埋め込む。再レビューでは、修正で生じた未コミットの差分（`git diff HEAD` の出力と untracked ファイルの本文）を claude / cursor-agent に加えて codex のプロンプトにも埋め込む（`--base` は未コミット修正を見ないため、codex には埋め込まない一般規定の例外。SKILL.md Step 7 の規定に対応）。

差分が大きすぎる場合は、差分全文をイテレーションディレクトリにファイルとして保存し、その絶対パスをプロンプトに記載して各レビュアーに読ませる（claude は Read で読める）。対象判定・ファイル一覧・全レビュアーで同じ範囲を使う。

## レビュアー別コマンド（Step 4）

### codex（総合レビュアー、1インスタンス。専門役割は claude で起動する）

- モデルと reasoning effort は `~/.codex/config.toml` の既定に依存せず明示指定する（既定は別用途＝コーディング委譲のために変わり得るため）。共通プレフィックスを `codex -m <モデル> -c model_reasoning_effort="<effort>"` とし、`<モデル>` は総合の `gpt-6-luna`（codex は総合のみ。専門役割は claude の Opus 5.5 で起動する。後述）、`<effort>` は `max` 固定とする。
- git 差分対象: `timeout 600 codex -m gpt-6-luna -c model_reasoning_effort="<effort>" exec review --uncommitted -o <出力ファイル> - < <役割プロンプトファイル>`、デフォルトブランチ差分なら同プレフィックスで `exec review --base <ブランチ> -o <出力ファイル> - < <役割プロンプトファイル>`（`-` で stdin からプロンプトを読み、`-o` で最終メッセージをファイル出力する）。
- パス指定対象: `timeout 600 codex -m gpt-6-luna -c model_reasoning_effort="<effort>" exec -s read-only --skip-git-repo-check -o <出力ファイル> - < <役割プロンプトファイル>`（`-` で stdin からプロンプトを読む）。`--skip-git-repo-check` は git リポジトリ外での即時失敗を防ぐ。
- 役割プロンプトはスキルのベースディレクトリ配下の `references/roles.md` から取得し、対象（パスまたは差分範囲）を埋め込む。出力ファイル名は roles.md の各役割見出しの `role-<slug>.md` に従う。

### cursor-agent（汎用レビュアー、1インスタンス。既定で無効 — SKILL.md Step 3 で明示指定時のみ起動）

- cursor-agent は長いプロンプトを引数で渡すと exit 0 のまま空出力になるため、プロンプトファイルのパスを含む短い指示を引数に渡し、cursor-agent 自身にファイルを読ませる: `timeout 600 cursor-agent -p --mode plan --trust --output-format text "まず <プロンプトファイル> を読み、その指示に従ってレビューを実行し、指示された報告フォーマットで結果を出力すること" > <出力ファイル> 2>&1`。plan モードは読み取り専用。
- `--trust` はヘッドレス実行に必要。対象リポジトリの信頼を確認できない場合はユーザーに確認し、許可されなければ cursor-agent を欠席として最終レポートに明記する。

### claude（総合レビュアー＝Fable 5.1 の 1 インスタンス、専門役割＝Opus 5.5（effort high）の役割ごとのインスタンス）

- 総合: プロンプトは roles.md の総合役割（codex の総合インスタンスと同一）に共通報告フォーマットと対象埋め込みを連結したもの。出力先は `claude.md`。
- 専門役割: roles.md の各役割プロンプトに共通報告フォーマットと対象埋め込みを連結し、`--model claude-opus-5-5 --effort high` で役割ごとに起動する。出力先は `role-<slug>.md`。コマンド形は総合と同じで、`--model` を置き換えて `--effort high` を加える: `cat <プロンプトファイル> | timeout 600 claude -p --model claude-opus-5-5 --effort high --tools "Read,Glob,Grep" > <出力ファイル> 2>&1`（2026-09-11 に gpt-6-luna から変更）。
- 総合のコマンド: `cat <プロンプトファイル> | timeout 600 claude -p --model claude-fable-5-1 --tools "Read,Glob,Grep" > <出力ファイル> 2>&1`。`--tools "Read,Glob,Grep"` で読み取り専用ツールに制限する。`--permission-mode plan` は `-p` と併用すると ExitPlanMode 呼び出しに失敗して指摘本文が最終メッセージから消えるため使わない。

## 出力の成功判定・リトライ・欠席（Step 4）

- 出力ファイルが共通報告フォーマットの指摘または「問題なし」を含むことを成功条件とする。exit 0 でも空・形式外・締めの挨拶だけの出力は失敗としてリトライ対象にする。
- 失敗・タイムアウトしたレビュアーは1度だけ再起動する。それでも失敗なら欠席として除外し、最終レポートに明記する。

## スナップショット（Step 6）

委譲の直前にスナップショットをイテレーションディレクトリへ保存する（元々未コミット変更が対象の場合に修正分を特定するため）。

- git 管理下で HEAD あり: `git diff HEAD` の出力と untracked ファイルの一覧および内容を保存する。
- HEAD が存在しない（コミット0件）または非 git 対象: 対象ファイルを複製して保存する。作業ツリー全体の複製はしない。これらのモードでは対象外ファイルへの修正を照合できないため、修正委譲の指示に「対象外ファイルは変更しない。変更が必要になった場合は中断して呼び出し元に報告する」を含める。

Step 7 の diff 照合は、このスナップショット（複製の場合はその複製）との比較で行う。

## 台帳 `findings.json`（Step 2・5・7）

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
      "status": "fixed"
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
- `status` は `unresolved` / `fixed` / `on_hold` / `rejected`。`reason` は `rejected` と `on_hold` で必須、それ以外は書かない（`null` も不可）。`fixSummary` は `fixed` にしたときに書く。
- `iteration` は現在のイテレーション番号に更新する。

## ゲート `jev-guard rereview`（Step 7）

- 修正 diff をイテレーションディレクトリに保存する: git 管理下なら委譲前スナップショットとの差分（`git diff HEAD` の出力に untracked ファイルの本文を連結したもの）を `iter-N/fix.diff` に書く。複製モードなら複製と現在のファイルの `diff -u` を連結して書く。
- 実行: `jev-guard rereview --findings <レビューディレクトリ>/findings.json --diff iter-N/fix.diff --base-summary "<Step 1 で確定した対象の1行要約>"`。判定は標準出力の JSON。`verdict`（`needed` / `skippable`）、`reason`、`findings[]`（指摘ごとの `resolved` と `confidence`）、`newRisk`、`verdictId`、`skippable` のときは `reviewPrompt` を持つ。
- 結末の記録: `jev-guard record --id <verdictId> --outcome accepted|overruled [--note "<理由>"]`。`skippable` を採用したか覆したかを判定ログに残す。`needed` には記録しない。
- 判定ログは `~/.local/share/jev-guard/verdicts.jsonl`（`JEV_GUARD_LOG_DIR` で変更可）。集計は `jev-guard stats`。

## 品質チェック（Step 6 の基準・Step 7 の判定）

- 実行コマンドはプロジェクトの構成ファイル（package.json / Makefile / pyproject.toml / build.gradle 等）や CLAUDE.md から検出する。検出したコマンドにはレビュアーとは分離した時間制限（1800秒目安）を前置する。品質チェックは対象プロジェクトのコードを実行するため、ユーザー自身が開発している信頼済みプロジェクトを前提とする。
- 時間制限の超過は失敗ではなく「判定不能」とする。
- 基準記録（Step 6・初回イテレーション）: 品質チェックを検出できた場合のみ委譲前に1回実行して基準結果を記録する。基準実行にも同じ時間制限（1800秒目安）を前置し、超過・実行不能なら「基準なし」とする。未検出も「基準なし」とする。
- 失敗の分類（Step 7）: 基準結果と照合して「既存の失敗」か「回帰」かを判定する。基準がない（未検出・判定不能・修正後に初めて検出）場合は回帰と断定せず「判定不能」とする。
