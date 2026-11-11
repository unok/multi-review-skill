# multi-review-skill

Claude Code と Codex の両方から使うスキル。claude（総合＝Opus 5.5 medium、専門役割＝Opus 5.5 high）と codex（総合＝gpt-6.1-sol medium）に並列でコードレビューを依頼し、呼び出し元が指摘を選別して修正を指揮し、未解決の妥当な指摘がなくなるまで再レビューを繰り返す。`--profile codex` で総合の claude 枠を Opus 5.5 high、専門役割を codex（gpt-6.1-sol high）に切り替えられる。

レビュアーは指摘を出すだけで、妥当性の判定はスキルを実行するメインセッションが行う。却下した指摘は理由付きで台帳に記録し、再レビュー時にレビュアーへ提示するため、同じ誤検知でループが空回りしない。本スキルの手順書自体を multi-review で9イテレーション・セルフレビューし、99件の修正を経て全レビュアー「問題なし」まで収束させて検証した。

## 必要なもの

- Python 3.9 以上（標準ライブラリのみ）と POSIX 環境の git・diff
- node・対象プロジェクトの typescript パッケージ・go は任意。なければ既存の一覧の該当言語を飛ばし、解析不能の理由を記録する
- Claude Code または Codex（実行主体）
- レビュアー CLI（認証済みであること）: [codex](https://github.com/openai/codex) と claude

一部の CLI がなくても動く。未インストールのレビュアーは欠席として最終レポートに記録される。

TypeScript の解析は `createSourceFile` を持つコンパイラ API 版（TypeScript 5.x・6.x）と、7.x のネイティブ API 版に対応する。対象ソースは実行しないが、対象プロジェクトの typescript パッケージは実行するため、利用者が入れた依存を信頼する前提で使う。

テスト実行には node・typescript・go が必要。TypeScript 5.x・6.x は `MR_TEST_TYPESCRIPT`（未設定なら直下の `node_modules/typescript`）、7.x は `MR_TEST_TYPESCRIPT7` に既存のパッケージディレクトリを指定し、`MR_TEST_TYPESCRIPT=/path/to/typescript5 MR_TEST_TYPESCRIPT7=/path/to/typescript7 python3 -m unittest` で実行する。どちらかが見つからなければ、設定方法を表示してテストを失敗させる。

## インストール

```bash
git clone https://github.com/unok/multi-review-skill.git ~/git/multi-review-skill
ln -s ~/git/multi-review-skill ~/.claude/skills/multi-review   # Claude Code
ln -s ~/git/multi-review-skill ~/.codex/skills/multi-review    # Codex
```

## 使い方

Claude Code では `/multi-review` または「複眼レビューして」、Codex では `$multi-review` と入力する（Codex では暗黙の起動を `agents/openai.yaml` で無効にしているので、名前を指定して起動する）。

- 引数なし: git の未コミット変更を対象にする。なければデフォルトブランチとの差分
- パス指定: `/multi-review src/billing` のようにディレクトリ・ファイルを対象にできる
- レビュアーの除外・限定: 「codex 抜きで」「claude だけで」。指定した CLI の全役割に適用
- モデル構成: `$multi-review --profile codex`（パスとの併用例: `$multi-review --profile codex src/billing`）。省略時と `--profile default` は従来構成で、呼び出し元による自動切り替えはしない

1イテレーションは、レビュアー並列起動 → 選別（修正方針サマリを表示）→ 修正サブエージェントへの委譲 → diff 照合と品質チェック → 再レビューの順に進む。上限は5イテレーションで、到達時は残存指摘を報告して停止する。

## 構成

- `SKILL.md` — 制御フロー（分岐・状態管理・終了条件）
- `references/execution.md` — CLI の引数・成果物・終了コードと、呼び出し元の照合・判断
- `references/roles.md` — 役割カタログ（総合 / セキュリティ / バグハント / アーキテクチャなど13役割）
- `scripts/mr.py` — 差分・プロンプト・並列実行・台帳・決定事項・既存の一覧の生成・rules・ゲートの機械的処理
- `scripts/inventory_ts.cjs` — TypeScript コンパイラ API による既存の一覧の解析
- `scripts/inventory_go.go` — Go の構文解析による既存の一覧の解析
- `tests/` — `python3 -m unittest` で実行するテスト（外部レビュアーとゲートは偽 CLI、inventory は実物の node・typescript・go を使用）
- `CONTEXT.md` — 用語集

## ライセンス

MIT
