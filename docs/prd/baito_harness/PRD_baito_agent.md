# PRD／仕様書：バイト探しの相棒（アルバイト条件整理チャット）v3

| 項目 | 内容 |
|---|---|
| 文書バージョン | v3（`harness_v3.py` + `bank.py` + `prompt_review_ja.txt` に対応） |
| 作成日 | 2026-10-26 |
| 想定読者 | **aica**（アルバイト用プロンプトおよびハーネスを担当する開発エージェント）。task hub manager 経由で引き渡す |
| ねらい | aica の既存アルバイト用プロンプト／ハーネス（「範囲が広すぎる」との指摘あり）を、本書が定める構造かつテスト可能な形へ改修する |
| 正とする資料（Source of truth） | 質問バンク＝`bank.py`、状態遷移・マージ・スコアリング＝`harness_v3.py`、review 用プロンプト＝`prompt_review_ja.txt`、ベースライン数値＝`summary.txt`／`summary_v3.txt`／`results.json` |

> 取り決め：ユーザーに表示する文言、プロンプト、質問文、選択肢は**すべてソースファイルの原文どおり**とし、言い換えや改変を行わない。

---

## 1. 背景と課題（既存プロンプトが広すぎる）

既存のアルバイト用プロンプト／ハーネスの課題（ユーザー評価：範囲が広すぎる）は次のとおりである。

1. **「何を聞くか・いつ終えるか」をモデルに委ねている**：プロンプトに「ユーザーの希望条件を把握する」といった漠然とした目標しかなく、モデルが自由に振る舞う。その結果、質問の順序・数・深さが毎回ばらつき、必須項目（年齢、学校の許可、副業の可否など）の聞き漏れや、際限のない深掘りが起こる。
2. **条件に優先度がない**：「必須条件／好み／NG」が区別されず、結果カードを求人の絞り込みに使えない。
3. **ユーザーの振り分けがない**：高校生、主婦・主夫、副業を探す会社員では重視するハード条件がまったく異なる。共通の汎用質問では、無関係な質問をするか、重要な質問を聞き漏らすかのどちらかになる。
4. **面接（ヒアリング）のようになっている**：自由回答の質問が多く、1回に複数の質問をし、リアクションや楽しさがないため、ユーザーの負担が大きい。
5. **コンプライアンス上の要点がコードにない**：18歳未満の深夜労働、扶養の金額などをモデルが「覚えている」ことに頼っており、信頼できない。
6. **テストできない**：構造化された出力がなく、シミュレーターでペルソナ回帰テストを回せない。

本 PRD の方針：**プロンプトは「どう言うか」だけを担当し、ハーネスが「何を聞くか・いつフェーズを切り替えるか・いつ終えるか」を決める**。モデルは毎ターン決められた JSON を返し、ハーネスがそれを条件表にマージして次の質問を決定する。

---

## 2. 目標と非目標

### 目標
- **楽しく、負担の少ない**チャット（目安は約5分、`MAX_TURNS = 15`）で、ユーザーが**自分の希望するバイトとその条件を整理できる**ようにする。
- **3層の条件カード**（HARD 必須条件／MID・LOW 好み／NG）と、**おすすめ職種3つ＋理由**を出力する。
- シチュエーション二択の「シナリオゲーム」を会話の中心に置く。推測した好みは1件ずつ確認せず、**結果カード上でまとめて確認・修正**してもらう（ユーザーが選んだ折衷案）。
- 重要条件（各セグメントの HARD）は会話の終わりの時点で**必ず埋まっている**こととし、コンプライアンス上のルールはコードで強制する。

### 非目標
- 厳密な面接・アンケート（ヒアリング）ではない。すべてのスロットを high まで確定させることは追求しない。
- 求人検索は行わず、特定の店舗や時給を約束しない（提案するのは「職種」である）。
- 外国人留学生を主な対象とはしない（在留資格、資格外活動許可などは本版の範囲外）。
- 扶養の金額は提示しない（§10 参照）。

---

## 3. 対象ユーザーと振り分け（segment）

| segment | 日本語表示（`SEGMENT_JA`） | 想定ユーザー | セグメント専用 HARD |
|---|---|---|---|
| `student` | 学生 | 高校生・大学生 | `age_under_18`, `school_permission` |
| `homemaker` | 家のこと中心 | 主婦・主夫 | `available_hours`, `sudden_leave` |
| `side_job` | 本業あり・副業 | 副業を探す有職者 | `side_job_allowed`, `weekday_or_weekend` |
| `unknown` | （未回答） | 属性を明かさない／未回答 | `age_under_18`（FIX7：属性が不明でも、法令に関わる年齢の質問は必ず行う） |

- セグメントは icebreak フェーズで `q_segment`「今のあなたに近いのは？」（学生／家のこと中心／本業あり副業探し）により判定する。モデルが自由記述から推定し、どのターンでも `segment` フィールドで返してよい（例：「平日の昼は子どものお迎えがあって…」→ homemaker）。
- セグメントが確定すると、ハーネスはそのセグメントのスロットを条件表に加える（`slots_for(state)` = common + セグメント専用）。

---

## 4. 成功指標（シミュレーション結果をベースラインとする）

ベースラインは `simulate.py` の8つのモックペルソナ（モック LLM。実モデルではない）による。数値はすべて `summary.txt`／`summary_v3.txt`／`results.json` から引用している。

### 4.1 v3 ベースライン（現行の推奨実装）

| persona | turns | result 到達 | 最終 segment | フェーズ別ターン数 ib/hf/sc/rp/fg | review 回数（ターン） | 空の HARD | 空の MID | 備考 |
|---|---|---|---|---|---|---|---|---|
| hs16 | 14 | True | student | 2/4/6/1/0 | 1（T14） | なし | なし | 深夜NG=True |
| univ | 14 | True | student | 2/5/5/1/0 | 1（T14） | なし | なし | |
| homemaker | 14 | True | homemaker | 2/5/5/1/0 | 1（T14） | なし | なし | |
| office | 14 | True | side_job | 2/5/4/1/1 | 1（T14） | なし | なし | 初回カードの矛盾：customer_facing → 最後には解消済み |
| nopref | 14 | True | student | 2/5/5/1/0 | 1（T14） | なし | なし | |
| unclear | 15 | True | side_job | 2/5/2/1/3 | 2（T14, T15） | なし | shift_length, customer_facing, physical | repeat=hf_weekday, hf_time、CUT_BY_MAX_TURNS |
| late_seg | 14 | True | homemaker | 2/5/5/1/0 | 1（T14） | なし | physical | 初回カードの矛盾：purpose → 解消済み |
| no_seg | 15 | True | student | 2/4/6/0/1 | 2（T14, T15） | なし | exam_flex | 初回カードの矛盾：customer_facing → 解消済み、CUT_BY_MAX_TURNS、reverse_pick はスキップ |

v3 の集計（上表から算出）：
- turns ≤ 15：8/8（14ターン×6、15ターン×2）
- review 到達：8/8、result 到達：8/8、loop／crash なし
- 空または low の HARD：0/8
- 会話の終わりで未解消の矛盾：0/8
- 未成年（hs16）への NG「深夜シフト」自動付加：True
- 空の MID が残ったもの：3/8（unclear 3件、late_seg 1件、no_seg 1件）
- CUT_BY_MAX_TURNS：2/8（unclear, no_seg）

### 4.2 バージョン比較（修正の効果）

| バージョン | 主な現象（summary.txt より） |
|---|---|
| v1（`harness.py`） | 8人中7人が最終 segment=`unknown`（自由記述から判明した late_seg のみ例外）。この7人は全員 HARD が空（セグメント専用 HARD が条件表に入らなかった）。hs16 の深夜NG=False |
| v1w（v1 + q_segment に purpose も target させる暫定回避策） | セグメントは判定できるようになったが、nopref／no_seg は unknown のままで HARD が空。多くのペルソナが15ターン。unclear は CUT_BY_MAX_TURNS |
| v2（`harness_v2.py`） | nopref は student に復帰。no_seg は `school_permission` が空のまま。unclear は time_slots が空で CUT。複数のペルソナで low の MID が多く残る |
| v2a（`harness_v2a_confirm_all_low.py`。low をすべて fill_gaps で1件ずつ確認する案） | low の MID はほぼ解消したが、シナリオゲームが2ターンに圧縮され、fill_gaps が5〜6ターン（面接化）、ほとんどが15ターン → **不採用** |
| v3（`harness_v3.py`） | review フェーズとターン予約を追加。§4.1 参照 |

### 4.3 受け入れ指標（aica による改修後の実装に適用）

| 指標 | 目標値 | ベースライン（v3） |
|---|---|---|
| 1会話あたりの turns | ≤ 15（`MAX_TURNS`） | 8/8 達成 |
| review 到達率 | 100% | 8/8 |
| result 到達かつ loop／crash なし | 100% | 8/8 |
| 会話の終わりに空または low の HARD（真の segment 基準） | 0 | 0/8 |
| 未成年 → NG に「深夜シフト」を含む | 100% | hs16 True |
| 会話の終わりに残る未解消の矛盾 | 0（または §12 の項目2で決定した方針に従う） | 0/8 |
| シナリオゲームのターン数（unclear 以外） | 4〜6ターンを目安（会話の中心） | 4〜6 |
| 同一質問の繰り返し | 0（unclear 型は例外。今後改善） | unclear のみ hf_weekday, hf_time を再質問 |
| 空の MID | 少ないほどよい。カード上で「未定」と表示することは許容 | 3/8 で空の MID あり |

> 注意：上記はすべてモック LLM による結果である。実モデル接続後は同じペルソナで再実行し、本表と比較すること。

---

## 5. 会話フロー

```mermaid
flowchart TD
    S([開始]) --> IB[icebreak<br/>q_money → q_segment<br/>セグメント判定]
    IB -->|segment 判明 または phase_turns ≥ 3| HF[hard_filter<br/>HARD を直接質問（high）]
    HF -->|空の HARD なし または phase_turns ≥ 5| SC[scenario_game<br/>シチュエーション二択（low）<br/>会話の中心・楽しい部分]
    SC -->|n ≥ 6 / n ≥ 4 かつ空の MID なし / n ≥ 2 かつ予算逼迫| RP[reverse_pick<br/>3職種から一番やりたくないもの → NG]
    RP --> FG[fill_gaps<br/>空・low の HARD と空の MID を補完（high）]
    FG -->|remaining_gaps が空| RV[review<br/>まとめカード：（推測）・どっち？・未定<br/>一括修正]
    IB & HF & SC & RP & FG -->|turns ≥ MAX_TURNS − REVIEW_RESERVE (=13)| RV
    RV -->|need_more_review かつ review_rounds < 2 かつ turns < 15| RV
    RV -->|それ以外| RS([result<br/>条件カード + おすすめ3職種と理由])
```

補足：
- あるフェーズに出題可能な質問がない場合（`pick_question` が None を返す場合）、`ORDER` の次のフェーズへ自動で進む（例：segment が不明だと reverse_pick の質問がないためスキップされる。no_seg 参照）。
- `turns ≥ MAX_TURNS` になると result へ直行する（review フェーズは独自の終わり方の条件を持つため対象外）。
- 標準のターン配分：icebreak 2 + hard_filter 4〜5 + scenario 4〜6 + reverse_pick 1 +（fill_gaps 0〜1）+ review 1 = 14ターン。

### 5.1 会話設計の原則（プロンプトとハーネスの双方で守る）
1. **1ターンに質問は1つだけ**。
2. **選択肢を優先し、自由回答の質問は減らす**（どの質問にも choices を付ける）。
3. **まずリアクションし、それから次の質問へ**（回答にひとこと共感やツッコミを入れてから質問する）。
4. **3〜4問ごとに途中の小さな気づきを伝える**（例：「ここまでだと、コツコツ型っぽいね！」など）ことで楽しさを保つ。
5. ユーザーが「どっちでもない」と答えたら、軽く1回だけ聞き返す：「お、どのへんがちがう？一言で教えて！」
6. **スキップ／どっちでもいい＝こだわりなし**（`"こだわりなし"`、confidence high として記録し、深追いしない）。
7. **同じ話題を聞くのは最大2回まで**（ハーネスでは `attempts[slot] < 2`）。
8. **面接口調を禁止する**（「〜についてお聞かせください」「ご希望の条件は？」のような聞き方をしない）。

---

## 6. 条件表（3層：HARD／MID・LOW／NG）

優先度の定数：`HARD = 3`, `MID = 2`, `LOW = 1`（`harness_v3.py` の `SLOT_PRIORITY`）。

| slot | 優先度 | 対象 segment | 日本語ラベル（`SLOT_LABEL_JA`） | 取得方法（質問 id） |
|---|---|---|---|---|
| days_per_week | HARD | 全員 | 週の日数 | hf_days（直接） |
| time_slots | HARD | 全員 | 働く時間帯 | hf_time（直接）、sc_st_after（推測） |
| commute | HARD | 全員 | 通勤 | hf_commute（直接）、sc_wage_commute（推測） |
| hourly_wage_min | MID | 全員 | 時給 | fg_wage（直接）、sc_wage_commute, sc_sj_stand（推測） |
| shift_length | MID | 全員 | 1回のシフトの長さ | fg_shift（直接）、sc_shift（推測） |
| customer_facing | MID | 全員 | 接客 | fg_customer（直接）、sc_line, sc_praise（推測）、rp_*（逆選択） |
| physical | MID | 全員 | 体力・立ち仕事 | fg_physical（直接）、sc_sj_stand（推測）、rp_* |
| purpose | MID | 全員 | 「バイトの目的」 | fg_purpose（直接）、q_money, sc_st_resume, sc_st_perk, sc_hm_money, sc_hm_exp, sc_sj_brain, sc_sj_goal（推測） |
| pace | LOW | 全員 | 忙しさ | sc_line（推測） |
| atmosphere | LOW | 全員 | 職場の雰囲気 | sc_teach, sc_after, sc_st_peers, sc_hm_peers（推測） |
| environment_noise | LOW | 全員 | 職場のにぎやかさ | sc_noise（推測）、rp_* |
| age_under_18 | HARD | student, unknown | 18歳未満 | hf_age（直接）、または自由記述（例：「高1です！」） |
| school_permission | HARD | student | 学校のバイト許可 | hf_school（直接） |
| exam_flex | MID | student | テスト期間 | fg_exam（直接）、sc_st_exam（推測） |
| long_vacation | LOW | student | 長期休み | sc_st_summer（推測） |
| available_hours | HARD | homemaker | 働ける時間 | hf_hours（直接）、sc_hm_fixed（推測） |
| sudden_leave | HARD | homemaker | 急なお休み | hf_leave（直接）、sc_hm_fever（推測） |
| fuyou | MID | homemaker | 扶養 | fg_fuyou（直接）、sc_hm_money（推測） |
| remote | MID | homemaker | 在宅/外 | fg_remote（直接）、sc_hm_remote（推測） |
| side_job_allowed | HARD | side_job | 会社の副業OK | hf_sideok（直接） |
| weekday_or_weekend | HARD | side_job | 平日/週末 | hf_weekday（直接）、sc_sj_weekend（推測） |
| energy_left | MID | side_job | 本業後の体力 | fg_energy（直接）、sc_sj_stable（推測） |
| avoid_coworkers | LOW | side_job | 会社の人に会うのは | sc_sj_coworker（推測） |

**NG リスト（`state.ng`）**：
- 入力元：reverse_pick の `__ng__`（例：「重い肉体労働」「レジ」「夜の飲食ホール」）、およびモデルが返す `ng_updates`（例：「夜の仕事」）。
- **強制ルール**：`age_under_18` の値が `TRUTHY` に含まれる場合、コードが「深夜シフト」（22:00〜5:00）を自動で追加する。**モデルには依存しない**。

**確信度（confidence）のルール**：
- `hard_filter`／`fill_gaps`（`DIRECT`）での回答 → `confidence: "high"`
- `icebreak`／`scenario_game`／`reverse_pick` からの推測 → `"low"`
- 自由記述で明言された事実 → `"high"`（例：「高1です！」→ age_under_18=True high）
- review でユーザーが修正した値 → すべて `"high"`, `from: "review"`。修正されなかった推測値 → 自動で high に昇格し、`from: "review_accepted"`

---

## 7. 質問バンク仕様（`bank.py` を正とする）

### 7.1 スキーマ
```python
Q(id, phase, segments, targets, text, effects)
→ {"id": str, "phase": str, "segments": ["all"|"student"|"homemaker"|"side_job", ...],
   "targets": [slot, ...], "text": "日本語の質問文",
   "choices": list(effects.keys()),
   "choice_effects": {"選択肢": {slot: value, "__ng__": [NG...], "__segment__": seg}}}
```
- `choices` は `choice_effects` のキーから自動生成する（選択肢の文言＝ユーザーに見える日本語）。
- `__ng__`：その選択肢が選ばれたら NG に追加する。`__segment__`：その選択肢が選ばれたら segment を確定する（segment はスロット**ではない**）。
- `q_segment` の targets は疑似ターゲット `["segment"]` である（スコアリングで特別扱いする。§8.4 の FIX1 参照）。
- 質問を追加する際の要件：1問1テーマ、選択肢は2〜4個、`choice_effects` の各選択肢を個々のスロット値に対応づけること。新しいスロット値を追加した場合は、`_E`（日本語ラベル表）に読みやすいラベルを1件追加すること。

### 7.2 質問数（計48問）

| phase | all | student | homemaker | side_job | 合計 |
|---|---|---|---|---|---|
| icebreak | 2（q_money, q_segment） | – | – | – | 2 |
| hard_filter | 3（hf_days, hf_time, hf_commute） | 2（hf_age, hf_school） | 2（hf_hours, hf_leave） | 2（hf_sideok, hf_weekday） | 9 |
| scenario_game | 7 | 6 | 6 | 6 | 25 |
| reverse_pick | – | 1 | 1 | 1 | 3 |
| fill_gaps | 5（fg_wage, fg_shift, fg_customer, fg_physical, fg_purpose） | 1（fg_exam） | 2（fg_fuyou, fg_remote） | 1（fg_energy） | 9 |

### 7.3 セグメント別の概要（質問文は原文）

**共通 icebreak**：「バイト代、何に使いたい？」（推し活・趣味／貯金・将来のため／生活費・家計の足し／旅行・遊び）、「今のあなたに近いのは？」（学生／家のこと中心／本業あり副業探し）

**共通 hard_filter**：「週に何日くらい入れそう？」「働けるのはどの時間帯？」（朝／昼／夕方〜夜／深夜もOK）「通うなら、どこまでOK？」

**共通 scenario_game**：「お店に長い行列ができた！そのとき思うのは？」（よし、燃えてきた／今すぐ消えたい）、「理想の1回のシフトは？」、「仕事の教わり方、どっちがいい？」、「バイト終わり、どっちがうれしい？」、「働く場所、どっちがいい？」、「時給100円の差。徒歩5分の店と電車30分の店、どっち？」、「ほめられてうれしいのは？」

**student**：hard「今、18歳未満？」「学校のバイトのルールは？」、scenario「バイト仲間、どっちがいい？」「テスト前、シフトどうしたい？」「夏休みは？」「バイト選び、どっち重視？」「学校のあと、いつから働く？」「うれしいのはどっち？」（まかない／社割）、reverse_pick「この3つ、一番やりたくないのは？」（コンビニ店員／引越しスタッフ／コールセンター）、fill「テスト期間のシフトは？」

**homemaker**：hard「働ける時間はどれが近い？」「急なお休み（子どもの体調など）、どのくらいありそう？」、scenario「シフト、どっちがラク？」「子どもが急に熱！どっちが安心？」「働くなら？」（在宅／外に出たい）「職場の雰囲気は？」「バイト代は？」（家計の足し／おこづかい）「仕事内容は？」、reverse_pick（スーパーのレジ／工場の軽作業（立ち仕事）／データ入力（在宅））、fill「扶養の範囲内で働きたい？」「在宅の仕事も気になる？」

**side_job**：hard「今の会社、副業はOK？」「働くなら平日？週末？」、scenario「副業に求めるのは？」「週末に働くなら？」「バイト先で会社の人に会ったら？」「どっちを選ぶ？」（時給高め立ちっぱなし／ふつう座れる）「副業の目的は？」「どっちがいい？」（安定収入／スポット）、reverse_pick（居酒屋ホール（夜）／倉庫ピッキング／イベント設営）、fill「本業のあと、体力はどれくらい残ってる？」

> 選択肢と choice_effects の全容は `bank.py` を正とする。プロンプト内に質問バンクの複製を書かないこと。

---

## 8. ハーネス仕様（`harness_v3.py` を正とする）

### 8.1 State
```python
phase="icebreak"; segment="unknown"
slots: {slot: {"slot","value","confidence":"high|low","from"}}
ng: [str]; asked: set[qid]; attempts: {slot: int}
turns: int; phase_turns: int
pending: [update]            # FIX3：segment 未確定の間に受け取った更新を一時保持
conflicts: {slot: [values]}  # V3：互いに矛盾する low 値
review_rounds: int; need_more_review: bool; review_log: [dict]
```

### 8.2 定数
| 定数 | 値 | 説明 |
|---|---|---|
| `ORDER` | icebreak, hard_filter, scenario_game, reverse_pick, fill_gaps, review, result | |
| `MAX_TURNS` | 15 | |
| `REVIEW_RESERVE` | 2 | review（1回目＋任意の2回目）のために予約するターン数 |
| `MAX_REVIEW_ROUNDS` | 2 | |
| `DIRECT` | {hard_filter, fill_gaps} | 直接質問 → high |
| `SEGMENT_SCORE` | 1.5 | q_money のスコア 2 より小さくし、楽しい質問を先に出す |
| `TRUTHY` | (True, "true", "True", "yes", "はい") | 年齢判定を寛容に解釈する |

### 8.3 マージ規則 `merge_updates`
1. `out.segment` が None／"unknown" 以外 → segment を更新する。
2. `pending + slot_updates` を1件ずつ処理する。スロットが現在の条件表にない場合は `pending` に戻す（**FIX3**：破棄しない。例：segment 判明前に年齢を言われた場合）。
3. 既存値が high で新しい値が low → 無視する（low で high を上書きしない）。
4. `_track_conflict`：新しい値が high → そのスロットの矛盾を解消する。既存値・新値がともに low で**日本語ラベルが異なる** → 矛盾として記録する（`value_ja` で比較し、living_cost／household のような同義値の誤検知を防ぐ）。
5. `ng_updates` は重複を除いて追加する。
6. `age_under_18 ∈ TRUTHY` → NG に「深夜シフト」を自動で追加する（**FIX8**：「true」「はい」などの文字列にも対応）。

### 8.4 出題スコアリング `pick_question`
- 候補の条件：`_phase_ok`（同じフェーズ。fill_gaps では hard_filter の質問も再利用可、**FIX5**）かつ `_segment_ok`（"all" または現在の segment。segment 不明の場合は、targets がすべて現在の条件表にある直接質問を許可、**FIX7**）。
- 出題済みの質問は再出題しない。例外として、fill_gaps フェーズの直接質問は再出題できる（attempts の上限内）。
- スコア：
  - `q_segment`：segment が不明かつ未出題 → `SEGMENT_SCORE`(1.5)、それ以外は 0（**FIX1**）。
  - その他：`Σ priority[t] × gap_weight(t)`。`attempts[t] < 2` の target のみ加算する。`gap_weight` は空 1.0／low 0.5／high 0。
  - 質問の segments に現在の segment が含まれ、かつスコア > 0 → +0.5（セグメント専用の質問を優先）。
- 最高スコアの質問を選ぶ。すべて 0 なら None を返し、次のフェーズへ進む。
- 出題後：**直接質問のみ**、かつ**まだ空または low の target のみ** `attempts += 1` とする（**FIX4**）。

### 8.5 フェーズ遷移 `next_phase`
| 現在のフェーズ | 遷移条件 |
|---|---|
| review より前の全フェーズ | `turns ≥ MAX_TURNS − REVIEW_RESERVE`（=13）→ review（review を必ず実施する） |
| （review 以外） | `turns ≥ MAX_TURNS` → result |
| icebreak | segment 判明 または `phase_turns ≥ 3` → hard_filter（**FIX2**） |
| hard_filter | 空の HARD なし または `phase_turns ≥ 5` → scenario_game |
| scenario_game | `n ≥ 6`、または `n ≥ 4 かつ空の MID なし`、または `n ≥ 2 かつ budget_tight` → reverse_pick。`budget_tight = turns + 1 + len(空・low の HARD) + REVIEW_RESERVE ≥ MAX_TURNS`（V3：シナリオを短縮できるのは HARD の不足のみ） |
| reverse_pick | → fill_gaps |
| fill_gaps | `remaining_gaps` が空 → review。`remaining_gaps` = 空・low の HARD ∪ 空の MID（**FIX6b**：シナリオで推測した low の MID はここで1件ずつ確認しない） |
| review | `need_more_review かつ review_rounds < 2 かつ turns < 15` → もう1回 review、それ以外 → result |

### 8.6 review のロジック
- `build_card`：優先度順に並べ、HARD を【ゆずれない条件】、MID/LOW を【こだわり・好み】に置く。空の LOW は表示しない。空の HARD/MID は「未定（よければ教えてね）」と表示する。low 値には「（推測）」を付ける。矛盾は「どっち？ A / B」と表示する。
- `render_card_ja` の出力（原文）：
  ```
  📋 あなたのバイト条件まとめ（2回目は「（もう一度だけ確認！）」を付加）
  （あなた：{segment_ja}）
  【ゆずれない条件】… 【こだわり・好み】… 【NG】…（なければ「とくになし」）
  （推測）は会話からの推測です。推測のところ、ちがってたらまとめて教えてね！「どっち？」はどちらか選んでね。ぜんぶ合ってたら「これでOK」で！
  ```
- 値の日本語化：`VALUE_JA` = 明示ラベル表 `_E`（優先）+ `set_value_labels(bank)` による選択肢原文での補完。**reverse_pick の選択肢は「やりたくないもの」なので、そのまま値のラベルにしてはならない**（例：physical=light は「引越しスタッフ」ではなく「軽めの作業がいい」と表示する）。したがって必ず `_E` を経由する。
- `apply_review`：
  1. モデルが返した slot_updates はすべて high とする。
  2. マージ後、修正されず矛盾にも含まれない low 値 → high に昇格し、`from="review_accepted"` とする。
  3. 2回目が必要になる条件：HARD の値が変更された、矛盾が残っている、または segment の変更で新たに空の HARD が生じた（例：no_seg が review で「実は大学生です」と答え、school_permission が不足になった）。
  4. `review_log` に記録する（touched / promoted / hard_changed / open_conflicts / new_hard_gaps / segment_change）。

### 8.7 ターンごとのインターフェース
- モデル出力（厳密な JSON）：
  ```json
  {"reply": "...", "choices": ["..."], "slot_updates": [{"slot": "...", "value": "...", "confidence": "high|low", "from": "<qid>"}], "ng_updates": ["..."], "segment": "student|homemaker|side_job|unknown"}
  ```
- ハーネスがプロンプトに注入する変数：`{{phase}}`, `{{segment}}`, `{{slots_json}}`, `{{next_question}}`（review フェーズでは card JSON + card_text）。
- `step(state, llm_out, bank)`：マージ → turns+1 → `next_phase` → 次の質問／review カード／None（result）を返す。

### 8.8 修正履歴（v1 → v2 → v3、シミュレーションで見つかった不具合）
| # | 問題（v1 での現象） | 修正内容 |
|---|---|---|
| FIX1 | `q_segment` の target が疑似ターゲット "segment" のみでスコアが 0 → **セグメント質問が一度も出題されない**（v1 では 7/8 が unknown） | segment 不明の間は `SEGMENT_SCORE=1.5` とする（q_money の 2 より低く、楽しい質問が先に出る） |
| FIX2 | icebreak が `n ≥ 2` で抜けてしまい、セグメント質問が間に合わない | segment 判明 または `n ≥ 3` に変更 |
| FIX3 | segment 判明前に受け取ったセグメント専用の更新（年齢など）が破棄される | `pending` にバッファし、segment 確定後に再適用 |
| FIX4 | すべての質問で attempts を加算し、シナリオ質問が直接質問の枠を使い切る | 直接質問のみ、かつ空または low のスロットのみ加算 |
| FIX5 | fill_gaps で HARD を補えない（segment の判明が遅い、HARD の回答があいまい） | fill_gaps で hard_filter の質問を再利用可能に（最大2回） |
| FIX6/6b | ターン予算が足りず、シナリオがターンを使い切る | `budget_tight` で予約、`remaining_gaps` = 空・low の HARD + 空の MID |
| FIX7 | segment が最後まで不明だと年齢を聞かない | `unknown` にも `age_under_18` を HARD として設定し、関連する直接質問を許可 |
| FIX8 | 文字列 `"true"` では深夜 NG が付かない | `TRUTHY` による寛容な解釈 |
| V3-a | review がなく、推測値が誰にも確認されない | review フェーズと `REVIEW_RESERVE=2` を追加。review より前のフェーズは turns ≥ 13 で review へ強制移行 |
| V3-b | 同じスロットで矛盾する2つの low 推測があると、後の値が黙って上書きする | 矛盾を検知し、カードに「どっち？ A / B」を表示 |
| V3-c | カードに英語の内部値（ok / light など）が表示される | `SLOT_LABEL_JA` + `_E` + `set_value_labels` による日本語マッピング |
| V3-d | MID の不足によってシナリオが短縮される | budget_tight は HARD の不足のみを数える。空の MID はカードに「未定」と表示 |
| 不採用 | v2a：low をすべて fill_gaps で1件ずつ確認 → シナリオが2ターンしか残らず面接のようになる | 折衷案を採用：シナリオを中心とし、low は結果カードでまとめて確認 |

---

## 9. プロンプト仕様

### 9.1 メインのシステムプロンプト（ベースプロンプト）

> ⚠️ 注記：メインプロンプトは以前の会話で起草したが、**`/workspace/baito_harness/` には保存されていない**。以下は合意済みの構成（役割、ルール1〜9、フェーズごとの動き、出力 JSON）に基づいて**再構成したベースプロンプト**であり、aica が骨格として使うためのものである。review フェーズでは、この後に `prompt_review_ja.txt` の原文を**追加**する（§9.2）。言い回しの微調整は可とするが、ルールと JSON の契約は変更しないこと。

```text
# あなたの役割
あなたは「バイト探しの相棒」です。高校生・大学生、主婦・主夫、副業をさがしている社会人が、
「どんなバイトがしたいか」「ゆずれない条件は何か」を、楽しく・ラクに整理できるように手伝います。
口調はフレンドリーな です・ます 調。面接（ヒアリング）ではなく、友だちとのおしゃべりのように。

# 現在の状態（ハーネスが毎ターン埋めます）
- フェーズ: {{phase}}
- ユーザーの属性: {{segment}}
- これまでにわかった条件: {{slots_json}}
- 次に聞く質問（ハーネスが決定）: {{next_question}}

# ルール
1. 1ターンに質問は1つだけ。聞くのは {{next_question}} だけ。自分で質問を追加・変更しない。
2. 選択肢（choices）は {{next_question}} の選択肢をそのまま使う。自由回答をお願いしない。
3. まずユーザーの答えにひとことリアクションしてから、次の質問へ。
4. 3〜4問に1回、ここまでの答えからわかる「ちょっとした発見」をひとこと伝える。
5. 「どっちでもない」と言われたら、軽く1回だけ聞く：「お、どのへんがちがう？一言で教えて！」
6. 「スキップ」「どっちでもいい」は「こだわりなし」として記録し、追いかけない。
7. 同じ話題を聞くのは最大2回まで。
8. 面接っぽい言い方（「〜についてお聞かせください」など）はしない。
9. ユーザーがはっきり言っていないことを事実として記録しない。推測は confidence:"low"。
   18歳未満の人に深夜（22時〜5時）の仕事をすすめない。扶養の具体的な金額は言わない（希望だけ聞く）。

# フェーズごとの動き
- icebreak：軽い話題で緊張をほぐしつつ、属性（学生／家のこと中心／本業あり副業探し）をつかむ。
- hard_filter：ゆずれない条件をテンポよく選択肢で聞く（ここの回答は confidence:"high"）。
- scenario_game：二択のシチュエーション質問でゲーム感覚に。答えから好みを推測する（confidence:"low"）。
- reverse_pick：3つの仕事から「一番やりたくない」を選んでもらい、NG に入れる。
- fill_gaps：まだ空いている条件だけを直接聞く（confidence:"high"）。
- review：（prompt_review_ja.txt のルールに従う）
- result：条件カードと、おすすめのバイトの種類を3つ、理由つきで出す。NG とゆずれない条件に反するものは出さない。

# 出力（必ずこのJSONだけを返す）
{"reply": "...", "choices": ["..."],
 "slot_updates": [{"slot": "...", "value": ..., "confidence": "high|low", "from": "<質問ID>"}],
 "ng_updates": ["..."], "segment": "student|homemaker|side_job|unknown"}
```

要点（変更不可の契約）：
- モデルは何を聞くか・いつ終えるかを**自分で決めてはならない**。`{{next_question}}` を言葉にするだけである。
- slot 名と value は `bank.py` の `choice_effects` にある値を使うこと。ユーザーが選択肢を選んだ場合は choice_effects に従って対応づける。
- 確信度のルールは §6 を参照。segment が不確かな場合は `"unknown"` を返す。

### 9.2 review フェーズの追加プロンプト
- ファイル：`prompt_review_ja.txt`（原文のままベースプロンプトの後ろに追加する。`phase == review` のときだけ有効にしても、常時含めてもよい）。
- 主なルール（原文の要点）：
  - 冒頭：「おつかれさま！ここまでの話をまとめてみたよ📋」
  - card_text の構成と順序を変えず、項目の追加・削除をしない。`inferred=true` には必ず「（推測）」を付ける。矛盾は「どっち？ A / B」のまま見せる。missing は「未定（よければ教えてね）」と書く。
  - 締めの文言は固定：「推測のところ、ちがってたらまとめて教えてね！『どっち？』はどっちか選んでね。ぜんぶ合ってたら『これでOK』だけで大丈夫🙆」
  - 1項目ずつ確認しない。2回目は「ごめんね、ここだけもう一回確認させて！」を添え、これが最後の確認であることを伝える。
  - ユーザーが修正・回答した項目だけを slot_updates に入れる（`confidence:"high"`, `from:"review"`）。新しい NG は ng_updates に入れる。修正されなかった推測項目はハーネスが自動で確定するので、モデルは入れ直さない。
  - あいまいな返信（「まあ、そんな感じ」）は変更なしとみなす。矛盾が残ったまま「これでOK」と言われた場合は slot_updates を空にする（ハーネスがもう1回だけ確認する）。
  - 18歳未満の人に深夜の時間帯を確定させる修正はせず、reply でやさしく「18歳未満は22時以降は働けないんだ」と伝える。
  - reply：短くお礼を言い、変更点を復唱する（「OK！シフトは4時間くらいにしておくね」）。続けて「じゃあこの条件で、ぴったりのバイトを探してみるね！」と予告する。

### 9.3 result フェーズ
- result ではハーネスは None を返し（出題しない）、モデルが最終の条件カード（3層）と**おすすめ職種3つおよびその理由**を出力する。
- おすすめの要件：HARD と NG に反しないこと（例：未成年には深夜の職種を勧めない。NG に「レジ」があればスーパーのレジを勧めない）。理由ではカード上の条件を引用すること。店舗名、時給、求人などを捏造しないこと。

---

## 10. 安全性とコンプライアンス

| 項目 | ルール | 実装箇所 |
|---|---|---|
| 18歳未満の深夜労働 | `age_under_18` が真 → NG に「深夜シフト」（22:00〜5:00）を自動追加。**コードで強制**し、モデルには依存しない | `merge_updates`（TRUTHY 判定） |
| review での未成年に関する修正 | モデルは未成年者に深夜の時間帯を確定させてはならない。reply で「18歳未満は22時以降は働けないんだ」と説明する | `prompt_review_ja.txt` |
| segment が不明 | それでも `hf_age` を出題する（法令に関わるため） | `SLOT_PRIORITY["unknown"]`（FIX7） |
| 扶養 | **金額の基準値は提示しない**。希望だけを聞く（「扶養の範囲内で働きたい？」→ 扶養内／気にしない／わからない）。「わからない（相談したい）」の場合は、家族・勤務先・専門の窓口に確認するよう勧める | 質問バンク fg_fuyou、プロンプトのルール9 |
| 学校の規則／副業の規定 | ユーザーの回答（OK／届け出／禁止・わからない、OK／届け出が必要／NG・グレー）を記録するだけで、違反かどうかを代わりに判断しない。「禁止 or わからない」「NG・グレー」の場合は、まず規則を確認するよう促す | 質問バンク + プロンプト |
| 捏造の禁止 | ユーザーが明言していない内容を high として記録しない。推測はすべて low とし、カードに（推測）と表示する。おすすめで求人・時給を捏造しない | プロンプトのルール9、review のルール |

---

## 11. テスト計画

### 11.1 ペルソナ（`simulate.py` で定義）
| persona | 説明 | 真の segment | 確認ポイント |
|---|---|---|---|
| hs16 | 高1・16歳 | student | 自由記述からの年齢取得（FIX3）、深夜 NG、review での shift_length 修正 |
| univ | 大学2年 | student | review で「これでOK」と答え、矛盾を解消しない（resolve=False） |
| homemaker | 未就学児2人の母 | homemaker | review での NG「夜の仕事」追加、remote の修正 |
| office | 会社員・副業探し | side_job | 矛盾の検知と解消、review での時給の修正 |
| nopref | 大学生・ほぼ『どっちでもいい』 | student | こだわりなしの扱い |
| unclear | 会社員・あいまい回答多め | side_job | 「どっちでもない…」の多発、HARD の補完、ターン上限 |
| late_seg | 主婦・最初は属性を言わない（5ターン目に判明） | homemaker | segment の遅い判明、pending の再適用 |
| no_seg | 大学生・属性を最後まで言わない(19歳) | student | unknown のままでの年齢質問、review で初めて属性が判明 → 2回目の review |

### 11.2 実行方法
```bash
cd /workspace/baito_harness
python3 simulate.py v3        # v3 のみ実行。v1 | v1w | v2 | v2a | all も指定可
python3 unit_checks.py        # v1 と v2 の比較：segment 確定前の年齢入力、文字列 "true" → 深夜NG
python3 unit_checks_v3.py     # 矛盾が未解消のまま「これでOK」が続く経路
```
- 出力：コンソールの集計表（`summary_v3.txt` と同じ形式）、`results.json`、`transcripts/{mode}_{persona}.txt`。
- モック LLM の挙動：DIRECT フェーズ → high、それ以外 → low、「どっちでもいい」→ こだわりなし(high)、「どっちでもない…」→ 何も抽出しない。

### 11.3 受け入れチェック（プロンプト／ハーネスを変更するたびに必ず実行）
1. 8/8 のペルソナで `reached=True`、`LOOP`／`CRASH` なし、`turns ≤ 15`。
2. 8/8 のペルソナで review を1回以上実施（`review_rounds ≥ 1`）。
3. 真の segment 基準で、空の HARD = 0、low の HARD = 0。
4. hs16：`late_night_ng=True`。
5. `asked_segment_q=True`（セグメント質問が必ず出題される）。
6. `conflicts_open_at_end` がすべて空（または §12 の項目2で決定した方針に従う）。
7. `irrelevant`（他セグメント向けの質問をした）= 0、`dropped`（更新が破棄された）= 0、`wasted`（すでに high のスロットを質問した）= 0。
8. §4.1 のベースラインより劣化しないこと（特にシナリオのターン数が v2a の2ターンまで後退しないこと）。
9. 実 LLM 接続後：transcript を抜き取り確認し、1ターン1問、リアクションあり、途中の気づきあり、面接口調なし、JSON がパース可能であることを確かめる。

---

## 12. 既知の課題と未決事項

| # | 課題 | 現象・根拠 | 状態 |
|---|---|---|---|
| 1 | **ユーザーの修正が原文のまま保存され、正規化されていない** | v3 hs16：shift_length=「4時間くらい」、unclear：time_slots=「平日の夜」（bank の列挙値ではない）。後段の絞り込み・マッチングで使えない | 対応予定：review での修正を列挙値に対応づけるか、`raw_text` と正規化値を分けて保存する |
| 2 | **review を2回行っても解消しない矛盾** | `unit_checks_v3.py`：「これでOK」が続くと result に進み、customer_facing は最後の low 値（avoid, low）のまま矛盾が残る | **未決定**：最後の low 値を残すか、空欄（未定）にするか |
| 3 | **NG を削除できない** | マージは ng を追加するだけで、ユーザーが「やっぱりそれは大丈夫」と言っても取り消せない | 未実装 |
| 4 | **選択肢が NG／HARD で絞り込まれていない** | 例：未成年にも hf_time で「深夜もOK」が表示される | 未実装（NG に基づく choices の絞り込みを推奨） |
| 5 | **あいまいなユーザーに同じ質問を繰り返す** | v3 unclear：hf_weekday、hf_time を fill_gaps でそのまま再質問 | 要改善（2回目は聞き方を変えるか、「一言で」答えられる入口を用意する） |
| 6 | **あいまいなユーザーは MAX_TURNS に達しうる** | v3 unclear、no_seg：CUT_BY_MAX_TURNS。unclear は空の MID が3件残る | カードの「未定」で受け止めており許容範囲だが、経過を観察する |
| 7 | segment 不明時に reverse_pick の質問がない | v3 no_seg：reverse_pick がスキップされた | "all" 向けの reverse_pick の追加を検討 |
| 8 | メインプロンプトが保存されていない | §9.1 は再構成版 | aica は §9.1 の構成に従って実装し、保存すること |
| 9 | シミュレーションはモック LLM による | ベースラインは実モデルの挙動を表さない | 実モデル接続後に再実行する |

---

## 13. aica 向け改修チェックリスト（優先度順）

**P0 — アーキテクチャ（最優先）**
1. 「何を聞くか／いつ切り替えるか／いつ終えるか」をプロンプトから外し、ハーネスに移す。プロンプトは `{{next_question}}` を言葉にするだけにする。
2. モデルの JSON 出力を強制する：`{reply, choices, slot_updates[{slot,value,confidence,from}], ng_updates, segment}`。ハーネスがマージ後に `{{phase}} {{segment}} {{slots_json}} {{next_question}}` を注入する。
3. 3層の条件表（HARD／MID・LOW／NG）とセグメント振り分け（student／homemaker／side_job／unknown）を導入する。スロットと優先度は §6 の `SLOT_PRIORITY` をそのまま使う。
4. 質問バンクをデータとして外出しする（`bank.py` のスキーマ：phase, segments, targets, choices, choice_effects, `__ng__`, `__segment__`）。プロンプト内に質問を書かない。
5. コンプライアンスをコードで強制する：未成年 → NG「深夜シフト」を自動追加（TRUTHY で解釈）、segment 不明でも年齢を聞く、扶養の金額は示さない。

**P1 — フローとスコアリング**
6. フェーズ遷移を実装する：icebreak → hard_filter → scenario_game → reverse_pick → fill_gaps → review → result。遷移条件は §8.5 に従い、`MAX_TURNS=15`、`REVIEW_RESERVE=2`、`MAX_REVIEW_ROUNDS=2` とする。
7. 出題スコアリングを実装する（priority × gap_weight、セグメント専用 +0.5、SEGMENT_SCORE=1.5、attempts<2）。
8. FIX1〜FIX8 をすべて取り込む（セグメント質問を必ず出題、icebreak の抜け条件、pending バッファ、attempts は直接質問のみ加算、fill_gaps での HARD 補完、ターン予算の予約、unknown でも年齢を質問、TRUTHY）。
9. 確信度のルール：直接質問は high、推測は low、low で high を上書きしない。

**P2 — review／結果**
10. review カードを実装する（build_card／render_card_ja の書式と日本語原文）。（推測）・どっち？・未定を表示し、一括で修正を受け付ける。修正されなかった推測は自動で確定し、最大2回まで、2回目の発動条件は §8.6 に従う。
11. 矛盾の検知（日本語ラベルで比較）と値の日本語マッピング表（`SLOT_LABEL_JA`、`_E`。reverse_pick の選択肢原文を値に使わない）。
12. `prompt_review_ja.txt` を追加する。result を実装する：3層カード + おすすめ3職種と理由。HARD／NG に反せず、求人を捏造しない。

**P3 — プロンプトの口調**
13. 範囲の広い既存プロンプトを §9.1 のベースプロンプトに置き換える：役割「バイト探しの相棒」、ルール1〜9（1ターン1問、選択肢優先、まずリアクション、3〜4問ごとの気づき、どっちでもないへの軽い聞き返し、スキップ＝こだわりなし、同じ話題は最大2回、面接口調の禁止、捏造の禁止）。

**P4 — テストと残課題**
14. `simulate.py` の8ペルソナを回帰テストとして組み込み、§11.3 の受け入れチェックを満たしたうえで §4.1 のベースラインと比較する。
15. §12 の既知課題に対応する：修正値の正規化 → NG による choices の絞り込み → あいまいなユーザー向けの聞き方の変更 → NG の削除 →（ユーザーの決定を待って）未解消の矛盾の扱い。

---

## 14. 付録：ファイルパス

| ファイル | 内容 |
|---|---|
| `/workspace/baito_harness/PRD_baito_agent.md` | 本書 |
| `/workspace/baito_harness/bank.py` | 48問の質問バンク（source of truth） |
| `/workspace/baito_harness/harness_v3.py` | 現行の推奨ハーネス（review、矛盾検知、日本語マッピング） |
| `/workspace/baito_harness/harness_v2.py` | v2（FIX1〜FIX8） |
| `/workspace/baito_harness/harness.py` | v1（初版） |
| `/workspace/baito_harness/harness_v2a_confirm_all_low.py` | 不採用となった「low をすべて1件ずつ確認する」案 |
| `/workspace/baito_harness/harness_v2.diff`, `harness_v3.diff` | 各版の変更点と理由（コメント内の FIX 番号） |
| `/workspace/baito_harness/prompt_review_ja.txt` | review フェーズの追加プロンプト（日本語） |
| `/workspace/baito_harness/simulate.py` | モック LLM シミュレーターと8ペルソナ |
| `/workspace/baito_harness/unit_checks.py`, `unit_checks_v3.py` | ユニットチェック |
| `/workspace/baito_harness/summary.txt`, `summary_v3.txt`, `results.json` | シミュレーション結果（ベースライン数値の出典） |
| `/workspace/baito_harness/transcripts/v3_*.txt` | 最新の会話ログ（ほかに v1/v1w/v2/v2a あり） |
