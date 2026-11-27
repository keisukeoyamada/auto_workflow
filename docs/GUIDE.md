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
| `workspace/credit/` | カード明細（取り込んだ CSV・振り分け・書き出し） | CSV / JSON | あなた（画面） | 管理外 |
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

### クレジット精算（Personal・任意）

カード明細を「誰が負担するか」で振り分けて、各自と共同からの入金額を出すタブです。エージェントは使わず、明細が外部（Claude を含む）に送られることはありません。

1. `config/fleet.yaml` に `credit:` を書き、`enabled: true` にする（書き方は `config/fleet.example.yaml`）。2 人の表示名・色と、CSV 上のカード会員名（`match`）を設定する。
2. 毎月、カード会社のサイトから明細を CSV でダウンロードし、「Personal > クレジット精算」で取り込む。締め月（YYYYMM）は利用日と `closing_day` から自動で決まる（入力して上書きも可）。
3. 行ごとに 共同 / A / B / 折半 / 指定 を選ぶ（何も押さなければ共同）。見終わった行は「状態」にチェック。複数行を選んでまとめて振り分けることもできる。
4. 引き落とし口座の残高を入れると「共同からの入金額 = 共同の負担 − 残高」が出る。請求額を入れると明細の合計と突き合わせる。
5. 全行が確認済みになったら「確定」。`workspace/credit/exports/<月>.csv` にも書き出される（Excel で開ける）。

- 同じ月の CSV を取り込み直しても、入力済みの振り分け・状態・memo は残ります（日付・内容・金額で照合）。
- **過去の分**：月ごとのシート（シート名 `YYYYMM`、列 日付・誰・内容・金額・2 人分の「〜負担額」）の Excel を同じ場所から取り込むと、確定済みの月として入ります。簡単な式（`=E5/2` など）はこちらで計算します。既にある月は上書きしません。
- **過去の振り分け**：各行に、同じ店を以前どう振り分けたか（直近 6 回のうち何回同じか）が出ます。押すとその振り分けになります。
- **除外**：ポイント充当とその相殺のように、振り分けたくない行は「除外」で計算から外して隠せます。請求額との照合には含めたままなので、合計が合わなくなることはありません。
- 「誰」はどのカード（会員）で払ったかで、振り分けの参考です。CSV に会員番号の列があれば末尾の番号も出ます。
- 前回分の口座振替のように明細でない行は取り込みません（取り込み時に知らせます）。ほかにもあれば `credit.exclude` に足してください。
- CSV の列名が違って読めないときは、`credit.csv` に列名を足してください。PDF は読み込めません。
- 取り込んだ元のファイルは `workspace/credit/raw/` に残ります。リポジトリでは `*.csv` `*.pdf` `*.xlsx` も `.gitignore` しているので、どこに置いてもコミットされません。
- NAS などで LAN に公開するときは、`.env` に `NAKAMA_UI_PASSWORD` と `NAKAMA_UI_ALLOW_HOSTS` を必ず設定してください。

### 論語（Personal・任意）

論語を 1 日 1 章ずつ読むタブです。原文、書き下し文、現代語訳、語句、教訓、今日の問いを読み、「読んだ」を押します。読んだ章の数（全体と篇ごと）と、連続日数を記録します。

1. `config/fleet.yaml` に `rongo:` を書き、`enabled: true` にする（書き方は `config/fleet.example.yaml`）。
2. 解説を作るために、エージェントと同じ Claude の認証が必要です。PC で `claude setup-token` を実行して、出てきたトークンを `.env` の `CLAUDE_CODE_OAUTH_TOKEN` に入れます。
3. 「Personal > 論語」で **原文を取り込む** を押す。著作権の切れた原文（全 20 篇・約 500 章・50KB ほど）を、Chinese Text Project から 1 回だけ取得します。
4. あとは毎朝（`rongo.schedule`、既定 5:15）、まだ読んでいない章のうち先の数章分（`rongo.ahead`、既定 3）の解説を、Claude がまとめて作ります。解説がまだない章は、**今すぐ作る** を押せばその場で作れます。

- Claude に送るのは、その章の原文だけです。1 章につき 1 回の短い呼び出しで、ツールは使いません。費用はコストのタブに `rongo` として記録され、日次予算も守ります。
- データは `workspace/rongo/` に保存します（`source.json` = 原文、`notes/` = 解説、`progress.json` = 読んだ記録）。Git には入りません。
- 解説は Claude が書いたものなので、誤りがあり得ます。気になる章は注釈書で確かめてください。
- 取り込みで「応答の形が想定と違います」と出た場合は、何も保存されていません。表示された内容を開発者に伝えてください。

### 家族などに画面を見せる（Tailscale の入口の後ろに置く・任意）

画面を Tailscale 経由でほかの人に使ってもらうときは、Nakama のコンテナはそのままにします。そのうえで、**共有用の Tailscale の入口（別のコンテナ）**から、ホストに公開しているポートへ転送します。入口だけを相手に共有すれば、相手からは入口に並べたアプリしか見えません。ホストやほかのサービスには届きません。

Nakama 側でやることは 2 つだけです。

1. ホストのポートを、`0.0.0.0` に公開する（`compose.override.example.yaml` の `ports: "8766:8765"`）。`127.0.0.1:` で公開しているポートには、ほかのコンテナから届きません。
2. `.env` の `NAKAMA_UI_ALLOW_HOSTS` に、入口の名前（`<入口>.<tailnet名>.ts.net`）と `host.docker.internal` を足して、`docker compose up -d` を実行する。足さないと、画面が 403（host not allowed）を返します。

入口が Tailscale でつないでくるので、画面のパスワード（`NAKAMA_UI_PASSWORD`）は今までどおり必要です。相手に伝えてください。

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
