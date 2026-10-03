# 履歴書下書きの JSON スキーマ(共通形)

音声棚卸し(`voice_resume/`)と、履歴書のアップロードと読み取り(`resume_intake/`)で共有する形。
このドキュメントが正で、実装は `resume_intake/schema.py` と `voice_resume/schema.py`(どちらも pydantic、task 62 で移植済み)の
2か所に複製されている。形を変えるときは必ず両方を更新すること。

スキーマは1つだけで、履歴書と職務経歴書の両方をこの形で持つ(aica v2 の G で format_version 1.1)。
どちらの書類かは、envelope の任意項目 `document_kind` で表す(下の「format_version 1.1」)。

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

## format_version 1.1(document_kind を足した)

1.0 の形に、envelope の任意項目 `document_kind` を1つ足しただけ。`resume` の中身は 1.0 と同じ。

```json
{
  "format_version": "1.1",
  "document_kind": "職務経歴書",
  "source": "voice-interview",
  "created_at": "2026-10-03T12:34:56+09:00",
  "personal": { "name": "", "name_kana": "", "birth_date": "", "address": "", "phone": "", "email": "" },
  "resume": {
    "job_summary": "法人営業を6年経験し、うち2年はチームリーダーとして5人のチームをまとめた。…",
    "work_history": [
      {
        "period": "2020年4月〜現在",
        "organization": "みほん商事",
        "employment_type": "正社員",
        "role": "法人営業・チームリーダー",
        "duties": ["法人顧客への提案営業"],
        "achievements": ["5人のチームで、担当売上を2年で1.3倍にした(本人談)"]
      }
    ],
    "...": "ほかの項目は 1.0 と同じ"
  }
}
```

- `document_kind`: `履歴書` か `職務経歴書`。任意項目で、無いときは `履歴書` として読む(1.0 の下書きはすべて履歴書)。
- 音声棚卸しは、面談の終わりの雇用区分で決める: 正社員なら `職務経歴書`、アルバイト・パートと未定なら `履歴書`。
  音声の下書きは常に 1.1 で `document_kind` を書く。
- 職務経歴書のときの書き方(下書きを作る LLM への指示、`voice_resume/prompts.py` の `CAREER_DRAFTER_SYSTEM`):
  - `job_summary` は職務要約。経歴の全体(業界・年数・役割・主な成果)を3〜4文・事実のみで整える。
  - `work_history` は新しい順。`duties` に担当業務、`achievements` に成果を、本人が言った数字(規模・期間・結果)とともに書く。
    本人が数字を言っていなければ作らず、`unconfirmed` に確かめたい点として挙げる。
  - `skills` は活かせる経験・知識・技術。`education` は会話に出たときだけ書く(画面には出さない)。
- 履歴書のアップロード(`resume_intake/`)は 1.0 のまま出す(アップロードされたファイルがどちらの書類かは決めない)。
  1.1 の任意項目なので、読み取り・dip への保存(`save_resume_upload`)・要約の `uploaded_resume` はそのまま動く。

### 読める形(Markdown)の見出しと並び

`voice_resume/render.py` が `document_kind` で変える。トークの「履歴書の下書きを見る」「職務経歴書の下書きを見る」
(`GET /liff/voice/draft`)の題も同じ書類名にする。

- 履歴書(`# 履歴書(下書き)`): 基本情報(本人が記入)→ 職務要約 → 学歴 → 職歴・経験 → その他の経験 → できること・得意なこと →
  資格・免許 → 自己PR → 本人の希望 → 確認したい点
- 職務経歴書(`# 職務経歴書(下書き)`): 氏名(本人が記入)→ 職務要約 → 職務経歴(職歴ごとに「業務内容」と「成果」)→
  活かせる経験・知識・技術 → 資格・免許 → 自己PR → その他の経験 → 本人の希望 → 確認したい点。学歴は出さない
