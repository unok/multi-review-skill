# multi-review-skill

Claude Code 用スキル。codex / cursor-agent / claude に並列でコードレビューを依頼し、呼び出し元が指摘を選別・修正指揮し、未解決の妥当な指摘がなくなるまで再レビューを繰り返す。claude レビュアーは役割ごとにモデルを選定する（既定 sonnet、高難度の役割のみ opus）。

レビュアーは指摘を出すだけで、妥当性の判定はスキルを実行するメインセッションが行う。却下した指摘は理由付きで台帳に記録し、再レビュー時にレビュアーへ提示するため、同じ誤検知でループが空回りしない。本スキルの手順書自体を multi-review で9イテレーション・セルフレビューし、99件の修正を経て全レビュアー「問題なし」まで収束させて検証した。

## 必要なもの

- Claude Code（実行主体）
- レビュアー CLI（認証済みであること）: [codex](https://github.com/openai/codex) / [cursor-agent](https://cursor.com/cli) / claude

一部の CLI がなくても動く。未インストールのレビュアーは欠席として最終レポートに記録される。

## インストール

```bash
git clone https://github.com/unok/multi-review-skill.git ~/git/multi-review-skill
ln -s ~/git/multi-review-skill ~/.claude/skills/multi-review
```

## 使い方

Claude Code で `/multi-review` または「複眼レビューして」と入力する。

- 引数なし: git の未コミット変更を対象にする。なければデフォルトブランチとの差分
- パス指定: `/multi-review src/billing` のようにディレクトリ・ファイルを対象にできる
- レビュアーの除外・限定: 「codex 抜きで」「claude だけで」

1イテレーションは、レビュアー並列起動 → 選別（修正方針サマリを表示）→ 修正サブエージェントへの委譲 → diff 照合と品質チェック → 再レビューの順に進む。上限は5イテレーションで、到達時は残存指摘を報告して停止する。

## 構成

- `SKILL.md` — 制御フロー（分岐・状態管理・終了条件）
- `references/execution.md` — 実行詳細（レビュアー別コマンド形・スナップショット・品質チェック）
- `references/roles.md` — claude レビュアーの役割カタログ（総合 / セキュリティ / バグハント / アーキテクチャなど11役割）
- `CONTEXT.md` — 用語集

## ライセンス

MIT
