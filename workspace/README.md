# workspace/ — エージェントが扱う業務データ

`config/fleet.yaml` の `connectors` が `local` のとき、エージェントはここを CRM / メール / カレンダー / チャットの代わりに使います。中身は **git 管理対象外**（顧客データのため）。

```
workspace/
├── crm/
│   ├── leads/<id>.json            リード（受信キュー = status:"new"）
│   └── opportunities/<id>.json    商談
├── mail/
│   ├── inbox/<id>.md              受信メール（Henji が処理）
│   ├── sent/<id>.md               オーナーが実際に送ったメール（学習用）
│   └── drafts/<id>.md             エージェントが作った下書き（ここをレビューして送る）
├── calendar/YYYY-MM-DD/<slug>.md  会議メタ情報＋トランスクリプト
├── assignments/YYYY-MM-DD.md      Furiwake の割り当て表
├── reports/                       各エージェントの実行レポート（Slack チャンネルの代わり）
├── action_items/                  人間 / Claude Tag へ引き渡す案件
├── requests/                      Coordinator への変更依頼（`python -m nakama request "..."`）
├── briefs/                        製品・価格の朝のブリーフ
└── attendance/                    勤怠（Kintai）
    ├── mine.md                    自分の勤怠のマスター（ダッシュボード「勤怠」から追記）
    ├── sources/<日時>-teams|loop.md  Teams スレッド / Loop 表の貼り付け（読み取り専用の入力）
    └── outbox/YYYY-MM-DD.md       かぶりチェック結果と Teams / Loop への貼り付け文
```

## リード `crm/leads/<id>.json`
```json
{
  "id": "L-0001", "created_at": "2026-11-25T22:14:00+09:00",
  "company": "...", "contact_name": "...", "email": "...", "language": "ja",
  "employees": 40, "existing_customer": false, "source": "web_form",
  "message": "問い合わせ本文",
  "status": "new",            // new | assigned | triaged | nurture | disqualified | escalated
  "owner": null, "priority": null, "category": null, "next_step": null,
  "history": []
}
```

## メール（inbox / sent / drafts 共通の Markdown + front matter）
```markdown
---
id: M-0001
thread_id: T-0001
from: sato@example.jp
to: me@example.com
subject: ...
date: 2026-11-25T17:20:00+09:00
lang: ja
status: new          # inbox のみ: new | drafted | skipped | escalated
draft_id: D-...      # sent のみ: 元にした下書き（あれば）
agent: henji         # drafts のみ: 作成したエージェント
---
本文
```

下書き（drafts）は本文の先頭に、送信されない要約ブロックを置きます:
```markdown
> **要約（送信しない）**
> - 相手が聞いていること: ...
> - スレッドの状況: ...
> - この下書きの方針: ...
> - 意図的に書いていないこと: ...
> - 出典: knowledge/product-facts.md, スレッド T-0001
```

## 商談 `crm/opportunities/<id>.json`
```json
{ "id": "O-0001", "account": "...", "stage": "discovery", "amount": null,
  "close_date": null, "next_step": "...", "next_step_date": null,
  "last_activity": "2026-11-25", "notes": [] }
```

## 勤怠 `attendance/`
`mine.md`（自分の予定。これが正）:
```markdown
| 日付 | 種別 | 時間 | メモ |
|---|---|---|---|
| 2026-11-27 | 有休 | 終日 | |
| 2026-12-03 | 時間変更 | 9:30-18:30 | |
```
`sources/*.md` は front matter に `source: teams` か `source: loop` を付け、本文はコピーした内容そのまま。メンバー・グループ・判定ルール・投稿書式は `knowledge/attendance-rules.md` に書く。
