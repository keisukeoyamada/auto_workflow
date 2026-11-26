# knowledge/ — 共有ナレッジベース（メモリストア）

全エージェントが読み書きする場所です。中身は個人・業務データなので **git 管理対象外**（このREADMEのみコミット）。

初回は `python -m nakama init` で `templates/knowledge/` から雛形がコピーされます。

| ファイル | 役割 | 主な書き手 |
|---|---|---|
| rulebook.md | 振り分け・トリアージ・エスカレーションのルール | オーナー / Coordinator |
| style-guide.md | オーナーの文体 | Nokori / Coordinator |
| product-facts.md | 製品・価格の確認済み事実 | Coordinator / 各エージェント |
| accounts.md | アカウント単位の注意点 | 各エージェント |
| learnings.md | 下書き→修正の差分から得た学び | Nokori |

バックアップしたい場合は、このフォルダだけ private リポジトリやクラウドストレージに同期してください。
