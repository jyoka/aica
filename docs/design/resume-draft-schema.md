# 履歴書下書きの JSON スキーマ(共通形)

音声棚卸し(`voice-resume/`)と、履歴書のアップロードと読み取り(`resume_intake/`)で共有する形。
このドキュメントが正で、実装は `resume_intake/schema.py`(pydantic)と、旧リポジトリ `jyoka/aica_ra_a2a_poc` の
`voice-resume/voice_resume/schema.py`(このリポジトリには未移植)にある。形を変えるときは両方を更新すること。

## 決まりごと

- `personal`(氏名・連絡先など)は**常に空欄**で出力する。会話では聞かない・保存しない
  (line-sim で個人情報を dip に保存しない方針と同じ)。本人が記入する。
- `resume` の中身は**本人が言ったこと・確認できた事実だけ**。性格・感情などの推定フィールドは持たない。
- 曖昧だったこと(期間・正式名称など)は推測で埋めず `unconfirmed` に入れる。
- 職歴が少ない人のために、職歴(`work_history`)とは別に `other_experience`
  (部活・委員会・家事・育児・ボランティア・趣味など)を持つ。
- 不明な文字列フィールドは空文字、無いリストは空配列(null は使わない)。
- `created_at` はオフセット付き ISO 8601。ユーザーは日本にいるので日本時間(Asia/Tokyo、`+09:00`)で書く。
- `self_pr` は `job_summary` の言い換え・繰り返しにしない。材料(工夫・任されたこと・成果など)が
  会話に無ければ空文字にし、`unconfirmed` に本人へ確認したい点として挙げる。

## 形(format_version 1.0)

```json
{
  "format_version": "1.0",
  "source": "voice-interview | upload",
  "created_at": "2026-09-25T12:34:56+09:00",
  "personal": {
    "name": "", "name_kana": "", "birth_date": "", "address": "", "phone": "", "email": ""
  },
  "resume": {
    "job_summary": "職務要約(2〜3文・事実のみ)",
    "self_pr": "自己PR(3〜4文・事実のみ)",
    "education": [
      { "period": "2018年4月〜2021年3月", "school": "○○高校", "status": "卒業" }
    ],
    "work_history": [
      {
        "period": "2024年6月〜2026年1月",
        "organization": "コンビニ",
        "employment_type": "アルバイト",
        "role": "レジ・品出し担当",
        "duties": ["レジ", "品出し"],
        "achievements": ["お菓子の発注を任され、廃棄を減らした(本人談)"]
      }
    ],
    "other_experience": [
      { "kind": "部活動", "title": "吹奏楽部", "description": "3年間継続。パートリーダー" }
    ],
    "skills": ["接客"],
    "certifications": [ { "name": "普通自動車運転免許", "acquired": "" } ],
    "preferences": {
      "desired_work": ["カフェのホール"],
      "conditions": ["平日夕方と土日"],
      "notes": ["家から近い店がよい"]
    },
    "unconfirmed": ["勤務先の正式名称"]
  }
}
```

- `source`: 生成元。音声棚卸しは `voice-interview`(E2E スクリプトは `voice-interview-e2e`)、
  アップロード読み取りは `upload` を想定。
- 読める形(Markdown)への変換は `voice-resume/voice_resume/render.py`(旧リポジトリ)。
  アップロードの読み取り結果は、トークの「読み取り結果を見る」(`GET /liff/resume`)で表示する。
