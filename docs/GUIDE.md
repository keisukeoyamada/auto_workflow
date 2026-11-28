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

起動は `python -m nakama ui`（画面だけ）か `python -m nakama serve --ui`（スケジューラも一緒）です。開くアドレスは http://localhost:8765 です。`--open` を付けるとブラウザも自動で開きます。Windows（タスクスケジューラ）と macOS（launchd）の常駐スクリプトは、最初から `--open` 付きで登録されるので、ログオンすると画面が開きます。

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

### 勤怠（Teams / Loop との二重管理をやめる）

**自分の勤怠は Nakama の `workspace/attendance/mine.md` を正とします。** Teams と Loop には、Kintai が作る文面を貼るだけにします。

1. ダッシュボード「勤怠」タブで、自分の予定を追加します（mine.md に 1 行ずつ追記されます）。
2. 同じタブに、Teams のスレッドや Loop の表をコピーして貼り付け、保存します（`sources/` に保存）。
3. Kintai が次の実行で、かぶり・当番の穴・Teams と Loop のズレ・自分の予定の未反映をチェックします。結果は `outbox/<日付>.md` に出ます。
4. outbox を開いて「Teams 投稿文をコピー」「Loop 貼り付け用をコピー」を押し、それぞれに貼ります。

最初に `knowledge/attendance-rules.md` を自分のチームに合わせて書き換えてください。メンバー、別名、グループ、判定ルール、投稿書式を書くファイルです。

**自動取得するなら**（未検証。テナントの設定しだい）
- **Teams**: Power Automate で、チャネルのメッセージを OneDrive のファイルに書き出します。そのフォルダを同期して `workspace/attendance/sources/` に届くようにします。ファイル先頭に `source: teams` の front matter を付けてください。返信まで取得できるかはトリガー次第なので、実際に試して確認する必要があります。
- **Microsoft Graph API** でも取得できます（[返信一覧 API](https://learn.microsoft.com/en-us/graph/api/chatmessage-list-replies)）。ただし `ChannelMessage.Read.All` に管理者の同意が必要です。
- **Loop**: 公開された書き込み API が見当たらないので、貼り付けで運用します。

同僚の不在理由（体調など）は個人情報です。Kintai は出力に理由を書かない指示になっています。それでも貼り付けた原文は Anthropic に送られるので、社内ルールで問題ないか確認してください。

### Planner 起票パイプライン（Kihyo・任意）

議事メモから Planner のタスクを起票する仕組みを、Claude アプリを立ち上げずに Nakama のサブスクトークンで回せます。

- `config/fleet.yaml` に次を書きます。
  - `ticketing.base`: tickets/・planner_state/ などがある同期フォルダ
  - `ticketing.owner_id`: roster.json の自分の id
  - `agents.kihyo` の `instructions_files`: 手順書（SKILL.md）のパス。手順書は Nakama にコピーせず、実行のたびにそのファイルを読みます。
- Kihyo にシェルは渡しません。代わりに、BASE の外に出られない専用ツールを使います。
  - `ensure_readable`: OneDrive を待つ
  - `planner_state_summary` / `planner_state_grep`: open の抽出と、完了済みカードの逆引き
  - `archive_ticket`: 退避
  - `promote_ticket`: 昇格。検証してから、BOM なしで新規作成だけを行う
- 書き込めるのは `tickets/draft/` と `logs/` だけです。PA を起動する `tickets/` 直下には `promote_ticket` だけが書けます。
- 「タスク」タブには、起票履歴のうち自分が担当のものを最新の planner_state と照合して表示します。planner_state に担当者の情報が無いため、Planner で手作りしたカードは出ません。
- 別の仕組み（Claude アプリのルーチンなど）と同時に動かすと二重起票になります。移行したら、古いほうは止めてください。
- **手動起票**（「起票」タブ）: テキストを貼ると、Kihyo が昇格できない「レビューモード」で draft を作ります。続いて Nakama がコードで次を検証し、あなたがチェックを入れた項目だけを昇格させます。
  - 形式、期日・優先度、roster にない担当者、planner_state にない Close 対象、draft 内での重複 → ✗（昇格不可）
  - 未完了の同名カード、完了済みの同名カード、直近 7 日の起票、未割当 → ⚠（既定では外す）
  - `tickets/` 直下に未処理のファイルがある間は昇格しません。PA が処理したかは、次に書き出される planner_state で確かめます。
- 「今日の am/pm を再現（昇格なし）」は検証専用です。結果を定時の draft と並べて比べられます。

### Personal タブ（個人用の機能・任意）

「Personal」グループには、使う人ごとに好きな機能を持たせられます。最初は「概要」タブだけで、`config/fleet.yaml` で機能を有効にするとタブが増えます。

1. `config/fleet.yaml` に機能名のブロックを書き、`enabled: true` にする。

   ```yaml
   <機能名>:          # 機能ごとのキー（各機能のコードが読む）
     enabled: true    # 必須。false か未記入ならタブは出ない
     # 機能固有の設定はここに
   ```

2. Nakama を再起動すると「Personal」の下にタブが出ます。

- 個人データは `workspace/` の下（Git 対象外）に置かれます。設定ファイル `config/fleet.yaml` もコミットされません。
- 新しい機能を足すときは、画面の `TABS` に `feature: "<機能名>"` を付けたタブを追加し、`GROUPS` の `g-personal` に並べます。

### 家族などに画面を見せる（Tailscale の入口の後ろに置く・任意）

画面を Tailscale 経由でほかの人に使ってもらうときは、Nakama のコンテナはそのままにします。そのうえで、**共有用の Tailscale の入口（別のコンテナ）**から、ホストに公開しているポートへ転送します。入口だけを相手に共有すれば、相手からは入口に並べたアプリしか見えません。ホストやほかのサービスには届きません。

Nakama 側でやることは 2 つだけです。

1. ホストのポートを、`0.0.0.0` に公開する（`compose.override.example.yaml` の `ports: "8766:8765"`）。`127.0.0.1:` で公開しているポートには、ほかのコンテナから届きません。
2. `.env` の `NAKAMA_UI_ALLOW_HOSTS` に、入口の名前（`<入口>.<tailnet名>.ts.net`）と `host.docker.internal` を足して、`docker compose up -d` を実行する。足さないと、画面が 403（host not allowed）を返します。

入口が Tailscale でつないでくるので、画面のパスワードは今までどおり必要です。

**相手に一部のタブだけ見せる（ゲスト用パスワード）**: `.env` に `NAKAMA_UI_GUEST_PASSWORD`（自分のパスワードとは別のもの）と、見せるタブ `NAKAMA_UI_GUEST_TABS`（カンマ区切り。既定は `credit`。使えるのは `credit` と `rongo`）を書いて、`docker compose up -d --force-recreate` を実行します。

- このパスワードでログインした人には、指定したタブだけが表示されます。ナビ、チャット、メモは出ません。
- サーバーも、ほかの API をすべて 403 で拒否します。なので、URL を書き換えても見られません。
- 相手にはゲスト用のパスワードを伝えてください。
- 画面の右上には、ログイン中の人の名前と色が出ます。`config/fleet.yaml` に次のように書きます（書かなければ「オーナー」「ゲスト」と表示されます）。
  ```yaml
  ui_users:
    owner: {name: "自分の名前", color: blue}     # blue | pink | green | orange | purple | gray
    guest: {name: "相手の名前", color: pink}
  ```
- ゲスト用のパスワードがあるときは、クレジット精算の月を確定するのに、2 人とも「承認する」を押す必要があります。承認のあとで振り分けが変わると承認は外れるので、もう一度押してください。

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
