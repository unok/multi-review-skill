# multi-review-skill

Claude Code と Codex の両方から使うスキル。claude（総合＝Fable 5.1 medium、専門役割＝Opus 5.5 high）と codex（総合＝gpt-6.1-sol medium）に並列でコードレビューを依頼し、呼び出し元が指摘を選別して修正を指揮し、未解決の妥当な指摘がなくなるまで再レビューを繰り返す。`--profile codex` で総合の claude 枠を Opus 5.5 high、専門役割を codex（gpt-6.1-sol high）に切り替えられる。

レビュアーは指摘を出すだけで、妥当性の判定はスキルを実行するメインセッションが行う。却下した指摘は理由付きで台帳に記録し、再レビュー時にレビュアーへ提示するため、同じ誤検知でループが空回りしない。本スキルの手順書自体を multi-review で9イテレーション・セルフレビューし、99件の修正を経て全レビュアー「問題なし」まで収束させて検証した。

## 必要なもの

- Claude Code または Codex（実行主体）
- レビュアー CLI（認証済みであること）: [codex](https://github.com/openai/codex) / claude（[cursor-agent](https://cursor.com/cli) は既定で無効。起動時に明示した場合のみ使用）

一部の CLI がなくても動く。未インストールのレビュアーは欠席として最終レポートに記録される。

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
- `references/execution.md` — 実行詳細（レビュアー別コマンド形・スナップショット・品質チェック）
- `references/roles.md` — 役割カタログ（総合 / セキュリティ / バグハント / アーキテクチャなど13役割）
- `CONTEXT.md` — 用語集

## ライセンス

MIT
