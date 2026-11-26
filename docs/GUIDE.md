# Nakama 使い方ガイド

このページは、ダッシュボードの「使い方」タブと同じ内容です。

## 1. 全体像

エージェントが **フォルダの中のファイルを読んで、別のフォルダに下書きを書く**。仕組みはこれだけです。

```
[あなたの PC の中]
  workspace/（業務データ）  ⇄  エージェント（決まった時刻に起動）  ⇄  knowledge/（ノウハウ）
                                  ⇅
[外]                          Anthropic API（＋設定した場合のみ Slack / MCP）
```

- 成果物はすべて **下書き** です。メールを送るのも確定するのも、あなた自身です。
- データベースは使っていません。全部ただのファイル（Markdown / JSON）なので、エディタで開いて読めます。

## 2. 毎日の使い方

| タイミング | やること | 画面のタブ |
|---|---|---|
| 朝 | 振り分け結果と、初回メールの下書きを確認する | 割り当て / 下書き |
| 日中 | 返信の下書きを確認し、「本文をコピー」してメーラーから送る | 下書き |
| 随時 | 人の判断が必要な案件を片付け、status を `done` にする | 要対応 |
| 気になったとき | 「〇〇をこう変えて」と依頼する（15 分以内に反映） | 変更依頼 |
| 送ったあと | 送信したメールを `workspace/mail/sent/` に置く（`draft_id:` 付き）。Nokori が下書きとの差分を学習する | — |

起動は `python -m nakama ui`（画面だけ）か `python -m nakama serve --ui`（スケジューラも一緒）です。開くアドレスは http://localhost:8765 です。

## 3. データの保存場所

| 場所 | 中身 | 形式 | 書くのは | git |
|---|---|---|---|---|
| `workspace/crm/` | リード・商談 | JSON | Furiwake / Sabaki / Shoudan | 管理外 |
| `workspace/mail/inbox/` | 受信メール | Markdown | あなた or メールのコネクタ | 管理外 |
| `workspace/mail/drafts/` | 下書き | Markdown | Sabaki / Henji / Nokori | 管理外 |
| `workspace/mail/sent/` | 実際に送ったメール（学習用） | Markdown | あなた | 管理外 |
| `workspace/calendar/<日付>/` | 会議メモ・文字起こし | Markdown | あなた or カレンダーのコネクタ | 管理外 |
| `workspace/action_items/` | 要対応（人の判断待ち） | Markdown | 全エージェント | 管理外 |
| `workspace/reports/` | 毎回の実行レポート | Markdown | ランナー | 管理外 |
| `workspace/requests/` | 変更依頼 | Markdown | あなた（画面 / `request`） | 管理外 |
| `knowledge/` | ルール・文体・製品情報・学び | Markdown | あなた＋エージェント | 管理外 |
| `agents/<名前>/LOCAL.md` | あなた用のチューニング | Markdown | あなた＋Coordinator | 管理外 |
| `agents/<名前>/versions/` | LOCAL.md の過去の版 | Markdown | 自動 | 管理外 |
| `logs/runs/<日付>/` | 1 回ごとの全やり取り（トレース） | JSONL | 自動 | 管理外 |
| `logs/costs.jsonl` / `state.json` | コスト・最終成功時刻 | JSON | 自動 | 管理外 |
| `config/` と `.env` | 設定・API キー・トークン | YAML / env | あなた | 管理外 |

- **git で共有されるのは仕組みと雛形だけです。** 上の表のデータはすべて `.gitignore` で除外しています。
- **エージェントが読めるのは `agents/`・`knowledge/`・`workspace/` だけです。** `.env` と `config/` は読めません。書き込みはエージェントごとに許可したフォルダに限ります（設定は `config/fleet.yaml` の `writes:`、実際に止める処理は `nakama/guard.py`）。
- **バックアップ** は `workspace/`・`knowledge/`・`agents/*/LOCAL.md`・`agents/*/versions/`・`config/` の 5 か所をコピーすれば十分です。`.env` には秘密情報が入っているので、別に保管してください。
- **自動では消えません。** `logs/runs/` にはメール本文も含めて全部残るので、ときどき古い日付のフォルダを消してください。
- Claude Code CLI は通常 `~/.claude/projects/` にも会話を保存しますが、Nakama はこれを止めています（`--no-session-persistence`）。記録は `logs/runs/` の 1 か所だけです。

## 4. データの送り先

外に出ていくのは次の 3 つだけです。

| 送り先 | 何が送られるか | いつ |
|---|---|---|
| **Anthropic**（Claude） | 指示文、エージェントが読んだファイルの中身、書こうとした内容 | エージェントの実行中、毎回 |
| **Slack**（任意） | 実行レポートと要対応の通知 | `.env` に `SLACK_WEBHOOK_URL` があるときだけ |
| **MCP サーバー**（任意） | エージェントが呼んだツールの入力（検索条件など） | `connectors` を `mcp:...` にしたときだけ |

**Anthropic 側でのデータの扱い**（出典: [Claude Code Data usage](https://code.claude.com/docs/en/data-usage)）

| 認証方式 | 学習への利用 | 保存期間 |
|---|---|---|
| API キー（`auth: api_key`） | 使われない（商用規約） | 30 日 |
| サブスクリプション（`auth: subscription`） | [プライバシー設定](https://claude.ai/settings/data-privacy-controls) 次第 | 設定オフで 30 日、オンで 5 年 |

業務データを扱うなら、API キーを使うか、サブスクの「モデル改善に協力」をオフにしてください。会社の Team / Enterprise プランは商用規約が適用されるので、API キーと同じ扱い（学習なし・30日）になります。

### サブスクのトークンを発行する
```powershell
# Claude Code CLI が無い場合は、SDK に同梱されている CLI を使える
.\.venv\Lib\site-packages\claude_agent_sdk\_bundled\claude.exe setup-token
# → ブラウザが開くので、使いたいアカウント（会社のプランならその組織）でログインする
# → 表示されたトークン（有効期間1年）を .env に貼る:
#    CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
.\.venv\Scripts\python -m nakama run furiwake --dry-run   # 最終行が auth=subscription なら OK
```
- トークンは画面に一度だけ表示され、どこにも保存されません。`.env` 以外に書かないでください。
- 1 年で失効します。失効したら同じ手順で発行し直してください。
- 会社のプランで使う前に、自動実行や業務データの扱いが社内ルールで許されているか確認してください。

## 5. 1 回の実行で起きること

例として、Henji が毎時 :05 に起動したときの流れです。

| # | 処理 | 担当するコード |
|---|---|---|
| 1 | 時刻になったら起動する | `nakama/scheduler.py` |
| 2 | 次のどれかに当たれば **API を呼ばずに終了**: 無効 / 日次予算超過 / 前回の成功以降 `trigger_paths` のファイルが増えていない | `nakama/runner.py` |
| 3 | 指示文を組み立てる: `COMMON.md` → `AGENT.md` → `LOCAL.md` ＋ 今日の日付・書き込み可能なパス・新着ファイル | `nakama/prompt.py` |
| 4 | 認証方式を決める（API キー or サブスク） | `runner.resolve_auth()` |
| 5 | Claude を起動する。エージェントは Read / Write などのツールでファイルを読み書きする | Claude Agent SDK |
| 6 | ツールを使うたびに場所をチェックし、許可外なら拒否する | `nakama/guard.py` |
| 7 | 全やり取りを `logs/runs/` に記録し、レポートを `workspace/reports/` に保存し、コストを記録する。Slack が設定されていれば通知する | `runner.py` / `state.py` / `notify.py` |

Coordinator の実行後は、LOCAL.md の変更が `versions/vNNN.md` として自動保存されます（`nakama/versioning.py`）。

## 6. 自分のデータをつなぐ

### ナレッジ（いちばん効果が大きい）
`knowledge/` の Markdown を自分の業務に合わせて書き換えます。長い指示文より、ここに書いた内容の方が効きます。

| ファイル | 書くこと |
|---|---|
| `rulebook.md` | 振り分けルール、エスカレーションの条件、やってはいけないこと |
| `style-guide.md` | 文体・署名・言い回し（JA / EN） |
| `product-facts.md` | 製品・価格・よくある質問（**確認済みの事実だけ**） |
| `accounts.md` | 主要な顧客ごとの注意点 |
| `learnings.md` | 自動で増える学びのログ（手で直してもよい） |

新しいファイル（例: `knowledge/competitors.md`）を足すときは、`rulebook.md` に「〇〇のときは competitors.md を見る」と 1 行書いてください。エージェントは毎回 rulebook と style-guide を読むので、そこから辿ってもらえます。

### メール：2 つの方法

**方法 A：ファイルを置く（簡単・外部連携なし）**
受信メールを `workspace/mail/inbox/<id>.md` として保存します。形式は次のとおりです。
```markdown
---
id: M-0101
thread_id: T-0101
from: someone@example.com
subject: 見積もりについて
date: 2026-11-26T10:00:00+09:00
lang: ja
status: new
---
本文
```
メーラーの書き出し機能や、自作の小さなスクリプトで作れば十分です。

**方法 B：MCP サーバーでつなぐ（自動）** 次の「7. コネクタを追加する」を参照してください。

## 7. コネクタを追加する

コネクタは「エージェントがどこからデータを取るか」の切り替えスイッチです。既定は全部 `local`（`workspace/` のファイル）です。

**手順（例：メールを Gmail の MCP サーバーにする）**

1. **サーバーを登録する**: `config/mcp.json` に追加します。秘密の値は `${VAR}` と書いて、実際の値は `.env` に置きます。
   ```json
   { "mcpServers": {
       "gmail": { "type": "stdio", "command": "npx", "args": ["-y", "<使う MCP パッケージ>"],
                  "env": { "GMAIL_OAUTH_TOKEN": "${GMAIL_OAUTH_TOKEN}" } } } }
   ```
2. **切り替える**: `config/fleet.yaml` を `connectors: { mail: mcp:gmail }` にします。
3. **送信を塞ぐ**: メールを扱うエージェントに `disallowed_tools: ["mcp__gmail__send_email"]` を足します。ツール名はサーバーによって違うので、`logs/runs/` のトレースで確認してください。
4. **確認する**: `python -m nakama run henji --dry-run` を実行し、`mcp=['gmail']` と表示されれば OK です。

- MCP サーバーは、そのコネクタを使うエージェントにだけ渡されます（Henji には mail だけ、Furiwake には crm だけ）。
- `npx` で起動するサーバーには Node.js が必要です（Docker イメージには入っています）。
- サブスクのトークン（`setup-token`）では、claude.ai 側のコネクタは使えません。自分で MCP サーバーを用意してください。

## 8. 困ったとき

| 症状 | 確認すること |
|---|---|
| 何も起きない | 画面の「最終成功」の時刻。`trigger_paths` に新しいファイルがなければスキップされるのは正常です |
| `auth=... (未設定)` | `.env` の `ANTHROPIC_API_KEY` か `CLAUDE_CODE_OAUTH_TOKEN` |
| `organization requires remote managed settings` | 会社が Claude Code を一括管理しています。個人のキーでは動かない可能性が高いので、情シスに相談するか、家の PC / NAS で動かしてください |
| 書き込みが拒否される | `config/fleet.yaml` のそのエージェントの `writes:` |
| 下書きの質がいまいち | まず `knowledge/` を充実させる → それでもダメなら変更依頼 |
| 詳しく知りたい | `logs/nakama.log`（全体）と `logs/runs/<日付>/*.jsonl`（1 回分の全やり取り） |
