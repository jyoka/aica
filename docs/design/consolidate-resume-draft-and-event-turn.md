# 設計見直し: 履歴書の下書きの契約と、イベントの1ターンをそれぞれ1か所にする

アーキテクチャレビュー(2026-10-11、`improve-codebase-architecture`)が挙げた2つの候補について、直すかどうかと直し方を決める。
このメモは実装のタスク(設計見直しの 2/3 と 3/3)の元になる。このメモではコードを変えない。

レビューは 2026-09-27 時点の古い clone を読んでいたので、行やファイルは今の main(`7521952`、2026-10-04 のコミット)で 2026-10-11 に確かめ直した。

## 結論

| 候補 | 判断 | 主な理由 | 実装 |
|---|---|---|---|
| 1. 履歴書の下書きの契約(スキーマ・envelope・個人情報を消す決まり)を1つにする | 直す | 個人情報を消す決まりが dip の照合より遅れている(全角の電話番号を消せない)。直す場所を1つにしたい。同期の手間そのものはまだ小さい | 1 PR |
| 2. イベントの受け付けとターンの実行を1か所にする | 直す | 2つの入口に手を入れた7回の修正すべてで、両方を同じように直している | 1 PR |

2つの実装は互いに依存しないので、どちらを先にしてもよい。価値が大きいのは候補 2。
ただし `line_runtime/server.py` と `line_runtime/resume_confirm.py` は両方の PR が触る(候補 1 は import・`server.py:740` の呼び出し・docstring)。後に出す PR で衝突を解く。

## レビューとの差(古い clone だった点)

- `line_runtime/server.py` はレビュー時 389 行、今は 898 行。2つの入口は `server.py:330-415`。補助の関数は `server.py:418-470`。
- `docs/adr/` はある(0001 雇用区分と playbook、0002 求人モデル)。どちらもこの2候補には触れない。
- clone の後に入った変更で、レビューの記述より重複が増えている。
  - 音声の下書きは format_version 1.1(`document_kind`)になった(task 108、`45e36e7`)。書類の種類の定数が `voice_resume/schema.py` と `dip_mcp/logic.py` の2か所にある。
  - 会話数の上限に IP ごと・全体の上限が入った(task 112、`d5b093c`)。2つの入口の同じ4か所を同じように直している。

## 候補 1: 履歴書の下書きの契約を1つにする

### 今の main での重複

| もの | `resume_intake/`(アップロード) | `voice_resume/`(音声) | `dip_mcp/logic.py`(保存) |
|---|---|---|---|
| pydantic の型(`Education` ほか4つと `ResumeDraft`) | `schema.py:25-78` | `schema.py:48-101` | `resume_intake.schema` を import(`logic.py:43`) |
| `JST`・`PERSONAL_KEYS` | `schema.py:19, 22` | `schema.py:42, 45` | `PERSONAL_KEYS` だけを import |
| envelope | `schema.py:81-89`(1.0、`document_kind` なし) | `schema.py:104-115`(1.1、`document_kind` あり) | `save_resume_upload`(`logic.py:1017-1042`)で personal を空欄にして組み直す |
| 個人情報を消す処理 `scrub_pii` と正規表現 | `extract.py:41-45, 88-99` | `drafter.py:18-22, 37-48` | `resume_intake.extract` から import(`logic.py:42`) |
| 書類の種類 `DOCUMENT_KINDS`・`DEFAULT_DOCUMENT_KIND` | なし | `schema.py:20-21` | `logic.py:922-923` |
| 書類の種類を読む関数 | なし | `document_kind_of`(`schema.py:36-39`) | `document_kind`(`logic.py:997-1000`)。中身は同じ |

同じところ:

- 型の形(項目・型・既定値・必須)はまったく同じ。`to_strict_json_schema` で2つを比べると、違いは説明文だけだった。
- `scrub_pii` と3つの正規表現は1文字も違わない。
- `PERSONAL_KEYS`・`JST` も同じ。

違うところ:

- **型の説明文が7か所違う。** 生成元に合わせた言い方で、意図した違い。
  説明文は structured output の JSON スキーマとして LLM に渡るので、LLM への指示の一部になっている。

  | 場所 | アップロード | 音声 |
  |---|---|---|
  | `Education`(クラスの docstring) | 書いてある範囲だけ。無ければ空 | 会話に出た範囲だけ。出なければ空 |
  | `Education.school` | 書いてあるまま | 本人が言った呼び方のまま |
  | `WorkExperience.organization` | 書いてあるまま | 本人が言った呼び方のまま |
  | `WorkExperience.achievements` | 書いてあるもののみ | 本人談のみ |
  | `OtherExperience.description` | 書いてあるもののみ | 本人談のみ |
  | `Preferences.notes` | 大事にしたいと書いたこと | 大事にしたいと言ったこと |
  | `ResumeDraft.unconfirmed` | 読み取りきれず | 会話で確認しきれず |

- format_version: アップロードは 1.0、音声は 1.1 で `document_kind` を書く(`resume-draft-schema.md` の決まりどおり)。
- `voice_resume/schema.py` には、音声だけが使う関数もある(`document_kind_for`・`document_name`・`UNDECIDED_DOCUMENT_NAME`)。

### 直す価値(git log の根拠)

同期の手間は、まだ小さい。

- 4つのファイル(2つの `schema.py`・`extract.py`・`drafter.py`)に触れたコミットは4つ(`bde3f6b`・`6caf047`・`2cf85e0`・`45e36e7`)。
  移植の後で両方に手を入れたのは `45e36e7`(task 108)の1回だけ。そのときは音声だけを 1.1 にし、アップロード側は docstring に
  「1.0 のまま」と書き足した。

一方、安全の振る舞いのずれは実際に起きている。2つの複製の間ではなく、dip の個人情報の照合との間で起きている。

- task 99(`98aec97`、2026-09-29)で、dip の `has_pii`・`redact_pii`(`dip_mcp/data.py:38-77`)は、照合の前に全角の数字・ハイフンを
  半角にそろえるようになった(`fold_pii`)。下書きの `scrub_pii` 2つには入っていない。
- 今の main で確かめた結果(`scrub_pii` は `resume_intake.extract` のもの。`voice_resume.drafter` のものも同じ正規表現):

  | 入力 | `scrub_pii` の結果 | `has_pii` |
  |---|---|---|
  | `090-1234-5678` | 消える | True |
  | `０９０－１２３４－５６７８` | **残る** | True |
  | `090ー1234ー5678` | **残る** | True |
  | `ｔａｒｏ＠example．com` | **残る** | True |

- アップロードの読み取りの指示は「文字・記号も書いてあるままにする」(`extract.py:34`)。日本語の履歴書は全角の数字が多いので、
  プロンプトの「書かない」をモデルが守らなかったときに、全角の電話番号が dip に残りうる。
  dip に残った読み取り結果は、会話の要約(`uploaded_resume`)として LINE 側の LLM に渡る。

ほかに、構造の問題もある。

- dip の保存(`save_resume_upload`)が、LLM を呼ぶ読み取りのモジュール `resume_intake.extract` から `scrub_pii` を借りている。
- 書類の種類の定数が `voice_resume/schema.py` と `dip_mcp/logic.py` の2か所にあり、ソースを AST で読むテスト
  (`tests/voice/test_jobs.py:162-167`)で同期を確かめている。

### 判断: 直す

`resume-draft-schema.md` は複製を意図したものとして書いている。レビュー自身も「同期の手間や安全の振る舞いのずれが実際に起きているときだけ見直す」と書いた。
同期の手間だけなら直さない。ただし安全のずれは起きているので、その条件に当たる。
個人情報を消す決まりは、呼び出しが3か所(読み取り・音声の下書き・dip の保存)、実装が2か所ある。全角への対応を入れる前に、直す場所を1つにする。

型の説明文の違い(7か所)は、型を1組にすると残せない。型を2組残せば説明文は保てるが、重複の大半(型の定義)がそのまま残る。
そこで、説明文は生成元によらない言い方にそろえ、生成元ごとの言い方はシステムプロンプトに任せる。
アップロードの指示(`EXTRACT_SYSTEM` の中の `extract.py:22`)はすでに「書いてあることだけを書き写す」と書いている。
音声の指示(`_DRAFTER_RULES` の中の `voice_resume/prompts.py:418`。履歴書と職務経歴書の両方の指示が使う)も「本人が会話の中で言ったこと…だけを書く」と書いている。
LLM に渡る文が変わるのはこの7か所だけで、実装の PR で意図した変更として扱う(下の「意図して変えること」)。

### モジュールの形

新しいパッケージ `resume_draft/` を作る(`resume_draft/__init__.py` は1行の docstring、本体は `resume_draft/schema.py`)。
依存は pydantic と標準ライブラリだけにする。`voice_resume` は今、リポジトリのほかのパッケージを import していない。
dip_mcp にも resume_intake にも依存しない置き場にして、その形を保つ。

`resume_draft/schema.py` が公開するもの:

```python
FORMAT_VERSION = "1.1"           # document_kind を書くときの版
PERSONAL_KEYS = ("name", "name_kana", "birth_date", "address", "phone", "email")
JST = ZoneInfo("Asia/Tokyo")
DOCUMENT_KINDS = ("履歴書", "職務経歴書")
DEFAULT_DOCUMENT_KIND = "履歴書"

class Education / WorkExperience / OtherExperience / Certification / Preferences / ResumeDraft  # 型は1組

def document_kind_for(track: str | None) -> str     # 雇用区分 → 書類の種類(resume-draft-schema.md に書いてある決まり)
def document_kind_of(env: dict | None) -> str       # 下書き・音声の結果の書類の種類。無い・知らない値は履歴書

def scrub_pii(value):                               # 今の実装をそのまま移す(正規表現も変えない)
def scrub_draft(value: ResumeDraft | dict | None) -> ResumeDraft
    # 個人情報を消してから型に通す。読み取り・音声の下書き・dip の保存の3か所が使う
def blank_personal() -> dict[str, str]              # personal の空欄
def envelope(draft: ResumeDraft, *, source: str, document_kind: str | None = None) -> dict
    # document_kind が None なら 1.0(document_kind を書かない。アップロード)。
    # 渡したら DOCUMENT_KINDS のどれかか確かめて 1.1 で書く(音声)。知らない値は ValueError(今と同じ)
```

音声の話し方だけに使うもの(`document_name`・`UNDECIDED_DOCUMENT_NAME`)は契約ではないので、`voice_resume/prompts.py` に移す。
`advisor.py` はすでに `prompts.py` から import している。

### 呼び出し側の変わり方

| ファイル | 今 | 直した後 |
|---|---|---|
| `resume_intake/schema.py` | 型・envelope の複製 | 消す |
| `voice_resume/schema.py` | 型・envelope・書類の種類の複製 | 消す |
| `resume_intake/extract.py` | `_PII_PATTERNS`・`scrub_pii` を持つ。`finalize` は `envelope(draft, source="upload")` | 両方を消し、`draft = scrub_draft(resp.output_parsed)`。`finalize` は `envelope(draft, source="upload")`(1.0 のまま) |
| `voice_resume/drafter.py` | 同じく `_PII_PATTERNS`・`scrub_pii` を持つ | 同じく `scrub_draft` を使う。`finalize` は `envelope(..., document_kind=document_kind)`(1.1 のまま) |
| `dip_mcp/logic.py` | `resume_intake.extract` と `resume_intake.schema` を import。`DOCUMENT_KINDS`・`document_kind` を持つ | `resume_draft.schema` だけを import。`save_resume_upload` は `scrub_draft(...).model_dump()` と `blank_personal()`。定数は `resume_draft.schema` から import する(`record_voice_result` が使う、`logic.py:952`)。`document_kind` は消す |
| `dip_mcp/profile.py:61`・`line_runtime/server.py:740` | `logic.document_kind(r)` | `document_kind_of(r)` |
| `voice_resume/render.py`・`advisor.py`・`outputs.py`・`prompts.py` | `voice_resume.schema` | `resume_draft.schema`(`document_name` は `prompts.py` へ) |
| `scripts/voice/e2e_audio_session.py`・`eval_stt.py` | `voice_resume.schema` の `JST` | `resume_draft.schema` の `JST` |
| `ruff.toml` | `known-first-party` に5つ | `resume_draft` を足す |

docstring とコメントで古い置き場を指しているもの(`line_runtime/server.py:827`、`line_runtime/resume_confirm.py:72`、`evals/scenarios.py:33`、
`dip_mcp/logic.py:920, 1018`、`voice_resume/README.md:43`)も直す。

### 振る舞いを変えないことの確かめ方

実装の PR では、移す前に次のテストを足し、今の main で通ることを確かめてから移す。

1. **型の形が同じこと**(`tests/test_resume_draft.py`)。2つの `ResumeDraft` の `to_strict_json_schema` を、説明文(`description`)を除いて比べ、
   一致することを確かめる。移した後は、1組の型が同じ形を持つことを同じテストで確かめる(比べる相手は、テストに固定した形)。
2. **個人情報を消す結果が同じこと**。入力と期待の表を1つ作り、今は2つの `scrub_pii` の両方に、移した後は1つに通す。
   表には今の2か所のテストにある例を集める(`tests/test_resume_intake.py:108`、`tests/voice/test_drafter.py:87, 103`):
   構造を保つ・空になった要素をリストから落とす・普通の文(時給・年月)を残す・電話番号を郵便番号より先に消す。
   全角の例は `xfail(strict=True)` で今の欠け(上の表)として残す。全角に対応するタスクで外す。
3. **envelope が同じこと**。アップロードは `format_version == "1.0"` で `document_kind` のキーが無い。音声は `"1.1"` で `document_kind` がある。
   キーの集合・`personal` の空欄・`created_at` の `+09:00`・知らない `document_kind` で `ValueError` を確かめる。
4. 既存のテストがそのまま通ること。特に `tests/test_resume_upload.py:153`(保存で personal を空欄にし個人情報を消す)。
   テストは import の置き場だけを変える。消す2つの `schema.py` か、消す2つの `scrub_pii` を import しているのは次の10ファイル:
   `tests/test_resume_upload.py:20`、`tests/test_resume_intake.py:12-13`、`tests/test_track.py:23`、`tests/test_talk_actions.py:198`、
   `tests/test_voice_track.py:37`、`tests/voice/test_drafter.py:8-9`、`tests/voice/test_advisor.py:11`、`tests/voice/test_jobs.py:164`、
   `tests/voice/test_render.py:7`、`tests/voice/test_outputs.py:11`。`tests/test_profile_page.py:543` は残る `finalize` だけを使うので変えない。
5. 同期のテスト `tests/voice/test_jobs.py:162-167` は、定数が1か所になるので消す。

### 意図して変えること

型の説明文の7か所(上の表)だけ。生成元によらない言い方にする(例: `学校名(元の呼び方のまま)`、`工夫・任されたこと・成果(本人が書いた・言ったもののみ)`)。
文言は実装の PR で決め、PR の説明に前後の対応を載せる。

オフラインのテストは LLM をスタブにしているので、この変更では落ちない。実 API での確認は人が任意で行う:
`resume_intake/run_samples.py`(4つのサンプル)で読み取り結果を比べる。API キーと費用が要るので、PR の必須にはしない。

### PR の範囲(1 PR)

コミットの順番:

1. 上の確かめ方 1〜3 のテストを足す(今の main で通る)。
2. `resume_draft/` を作り、呼び出し側を移し、2つの `schema.py` と2つの `scrub_pii` を消す。説明文の7か所をそろえる。
3. ドキュメント(`resume-draft-schema.md`・`voice_resume/README.md`・docstring)を直す。`README.md:313` の「`scrub_pii` で2回除く」は、置き場が変わっても正しいので、関数の置き場を書き足すだけにする。

この PR でしないこと:

- **`scrub_pii` の全角への対応。** 振る舞いの変更なので、別のタスクにする。1か所になった後なら、直すのは `resume_draft/schema.py` だけになる。
  dip の `fold_pii`(`dip_mcp/data.py`)を使うか、全角をそろえる表を依存の無い置き場へ移すかは、そのタスクで決める。
- 音声の結果の `draft_markdown`(`record_voice_result`)の扱い。個人情報は下書きを作るときに消えている前提のままにする。

### `resume-draft-schema.md` の「複製」の記述

実装の PR で、4〜5 行目を次のように書き換える(このメモでは変えない)。

> このドキュメントが正で、実装は `resume_draft/schema.py` の1か所にある(pydantic)。アップロードの読み取り(`resume_intake/`)、
> 音声棚卸し(`voice_resume/`)、dip の保存(`save_resume_upload`)が同じ型と、個人情報を消す同じ決まり(`scrub_draft`)を使う。

あわせて、「履歴書のアップロード(`resume_intake/`)は 1.0 のまま出す」の段落に、`envelope` に `document_kind` を渡さないと 1.0 になることを1行足す。

## 候補 2: イベントの受け付けとターンの実行を1か所にする

### 今の main での重複

`POST /api/event`(`server.py:330-365`)と `POST /api/event/stream`(`server.py:368-415`)は、同じ手順を同じ順で持っている。

| 手順 | `/api/event` | `/api/event/stream` |
|---|---|---|
| 本人のユーザーか(403) | 332 | 372 |
| 知っているイベントか(400) | 333-334 | 373-374 |
| 空の発言を断る(400、`_require_text`) | 335 | 375 |
| アップロードの事実(無ければ 400、`_upload_facts`) | 338 | 376 |
| プッシュ: 上限なら送らない(dip に聞く前) | 341-342 | 385-387 |
| プッシュ: dip の `push_trigger` | 344-347 | 388-393 |
| 別のタブで会話が替わっていたら stale(会話数を使わない) | 348-350 | 394-397 |
| 会話数を1つ使う(`quota.try_take`)。上限なら案内 | 351-353 | 398-401 |
| プロフィールの変更の知らせ(`_profile_notice`) | 354 | 402 |
| `LineRuntime` を呼ぶ | 356 `handle` | 404 `handle_stream` |
| ターンの中で stale になったら会話数を返す | 360-361 | 409-410 |
| 挨拶を開いた記録・知らせを渡し終えた記録(`_record_open`) | 362 | 407 |
| 返信に今の雇用区分と切り替えの状態を足す | 363-365 | 408 |

残す違い(意味がある):

- 返し方。stream は最初に `start` の行、プッシュなら `gate` の行、ターンの途中の `thread`・`llm`・`tool_start`・`tools` の行を出す。JSON は最後の結果だけを返す。
- 送らない・上限の形。stream は `{"kind": "reply", "sent": false, ...}`・`{"kind": "reply", **limit_reply}`。JSON は同じ中身を `kind` 無しで返す。
- stale の形。stream は `{"kind": "stale", "thread": n}`。JSON は `{"stale": true, "thread": n}`。
- 失敗の返し方。stream は応答を返し始めた後なので `{"kind": "error"}` の行にする。JSON は `ValueError` を 400 にし、ほかは 500 のまま。
- stream では、返信の後の記録(`_record_open`)は返信の行を出す直前に行う。画面との接続がその前に切れたら記録しない。
  この場合は、次に `/api/talk/open` を開いたときに補う(`server.py:250-252`)。

なくしてよい違い(意味が無い):

- JSON の `r.get("sent") is not False`(363 行)。`LineRuntime` の返信(`render`)は `sent` を持たないので、いつも真になる。
- JSON は stale の結果にも `_record_open` と `_track_status` を呼ぶ(中で何もしない)。stream は返信のときだけ呼ぶ。

### 直す価値(git log の根拠)

`git log -L` で2つの関数の本体の履歴を見ると、最初の移植(`daba706`)の後に7つのコミットが手を入れている。
7つすべてが両方を同じように直している。

| コミット | 両方に足したもの |
|---|---|
| `e9bed1c` task 58 | 本人のユーザーかの確認(`_require_own`) |
| `57af1bc` task 59 | 空の発言を断る・会話数の上限(`available`・`take`) |
| `438053b` task 98 | stale の照合・会話数を返す・開いた記録 |
| `625987d` task 91 | アップロードの事実 |
| `b171369` task 95 | プロフィールの変更の知らせ |
| `e63e47a` task 96 | 雇用区分と切り替えの状態 |
| `d5b093c` task 112 | IP ごとの上限(`available`・`try_take`・`refund` に ip を渡す。両方で4か所ずつ) |

片方だけを直し忘れた不具合は、git log には見つからなかった。毎回、両方をそろえる手間を払って防いでいる。
テストも、手順ごとにどちらか一方の入口でしか確かめていないものが多い。例: 空の発言は `/api/event` だけ(`tests/test_quota.py:184`)、
プロフィールの変更の知らせは stream だけ(`tests/test_profile_page.py:956-990`)、プッシュの拒否は stream だけ(`tests/test_handle.py:140`)。

### 判断: 直す

手順を1か所に集め、JSON と NDJSON は出力の形だけを受け持つ。
`LineRuntime` に前例がある。`handle` は `handle_stream` の出すイベントを畳んで返信だけを返している(`runtime.py:262-270`)。同じ形にする。

### モジュールの形

新しいモジュール `line_runtime/turn.py` を作る。

```python
LIMIT_REASON = "本日の会話数の上限に達している"   # server.py:168 から移す

@dataclass
class TurnDeps:
    """1ターンが使うもの。server が呼ぶたびに作る(テストは server.state を差し替えるので、import のときに固定しない)。"""
    rt: LineRuntime
    quota: DailyQuota
    state: State
    unread_uploads: dict[str, dict]

@dataclass(frozen=True)
class Turn:
    user_id: str
    event: str
    text: str | None
    thread: int | None
    ip: str
    facts: dict | None          # アップロードの事実(プッシュの事実は run の中で dip から受け取る)

class BadTurn(ValueError):
    """入口が 400 にする誤り。accept の検証と、LineRuntime が出した ValueError だけ(今の 400 と同じ範囲)。"""

def accept(deps, *, user_id, event, text, thread, ip) -> Turn
    """会話数を使う前に断る検証(知らないイベント・空の発言・アップロードの結果が無い)。断るときは BadTurn。"""

async def run(deps, turn) -> AsyncIterator[dict]
    """プッシュの判定 → stale → 会話数 → 知らせ → LineRuntime.handle_stream → 返信の後の記録、の順に進め、
    今の stream と同じイベント(gate / reply / stale / thread / llm / tool_start / tools)を出す。
    ターンの中の stale では会話数を返す。LineRuntime の返信にだけ search_track と切り替えの状態を足してから出す
    (送らない・上限の reply には足さない。今と同じ)。LineRuntime が出した ValueError は BadTurn に包み直す。"""

async def run_to_reply(deps, turn) -> dict
    """run を畳んで、最後の reply か stale だけを返す(LineRuntime.handle と同じ畳み方)。/api/event 用。"""

def track_status(state, user_id, reply) -> dict | None   # server.py:273 から移す。/api/talk/history も使う
```

`server.py:418-470` の補助(`_stale`・`_profile_notice`・`_record_open`・`_upload_facts`・`_require_text`)は `turn.py` の非公開の関数にする。
`_upload_facts` が使う `RESUME_LABEL`(`server.py:751`)は、循環 import を避けるため `line_runtime/resume_confirm.py` に移す(`/api/resume/upload` もそこから使う)。

### 呼び出し側の変わり方

2つの入口は、本人の確認・検証・出力の形だけになる。

```python
@app.post("/api/event")
async def event(body: EventIn, request: Request):
    _require_own(request, body.user_id)
    deps = _turn_deps()
    try:
        t = turn.accept(deps, ..., ip=_client_ip(request))
        return await turn.run_to_reply(deps, t)
    except turn.BadTurn as e:
        raise HTTPException(400, str(e)) from e

@app.post("/api/event/stream")
async def event_stream(body: EventIn, request: Request):
    _require_own(request, body.user_id)
    deps = _turn_deps()
    try:
        t = turn.accept(deps, ..., ip=_client_ip(request))   # 応答を返し始める前に 400 にする
    except turn.BadTurn as e:
        raise HTTPException(400, str(e)) from e

    async def lines():
        yield _line({"kind": "start", ...})
        try:
            async for ev in turn.run(deps, t):
                yield _line(ev)
        except Exception as e:  # noqa: BLE001
            err = (e.__cause__ or e) if isinstance(e, turn.BadTurn) else e   # 包み直す前の例外を出す(今の行と同じ)
            yield _line({"kind": "error", "message": f"{type(err).__name__}: {err}"})

    return StreamingResponse(lines(), media_type="application/x-ndjson")
```

`runtime.py`・`quota.py`・`dip_mcp/logic.py` は変えない。

- 手順を `LineRuntime` に入れない理由: 評価(`evals/run.py:138`)は `LineRuntime.handle` を直接呼び、会話数の上限をかけない(README の「対象は Web の API だけ」)。
  上限・プッシュの判定・プロフィールの知らせは Web の入口の決まりなので、`line_runtime/turn.py` に置く。
- `push_trigger` は dip の決まりのまま、`turn.py` から呼ぶだけにする。

### 振る舞いを変えないことの確かめ方

実装の PR では、移す前に `tests/test_event_turn.py` を足し、今の main で通ることを確かめる。
すべてのテストを2つの入口で parametrize する。stream は最後の行を JSON と同じ形に直して比べる(`kind` を外し、stale は `{"stale": True, "thread": n}`)。
フィクスチャは `tests/test_quota.py` の `_server` の形(台本のモデル・メモリ上の `LineRuntime`)を使う。

| 確かめること | 今あるテスト |
|---|---|
| 知らないイベント・空の発言・アップロードの結果が無いときは 400 で、会話数を使わない | 空の発言は JSON だけ(`test_quota.py:177`) |
| プッシュ: 上限なら送らず、dip に接触を記録しない | JSON だけ(`test_quota.py:116, 218`。接触の記録は確かめていない) |
| プッシュ: dip が断ったら送らない。stream には `gate` の行がある | stream だけ(`test_handle.py:140`) |
| プッシュ: 許可されたら根拠の事実が入力に入る | 入口では無い(`push_trigger` 単体は `tests/test_dip.py:322-339`) |
| 事前の stale: 会話数も LLM も使わない | 両方(`test_talk_page.py:250-254`) |
| ターンの中の stale: 会話数を返す | 両方(`test_talk_page.py:290-310`) |
| 上限: 案内を返し、LLM を呼ばない | 両方(`test_quota.py:96`) |
| プロフィールの変更の知らせ: 発言のターンで渡し、返信が返ったら渡し終える。stale なら渡し終えない | stream だけ(`test_profile_page.py:956-990`)。stale のときは無い |
| 挨拶が返ったら開いた記録が残る(thread つき) | stream だけ(`test_talk_page.py:92-101`) |
| 返信に `search_track` と `track_switch.status` がある。送らない・上限の返信には `search_track` が無い | `track_switch` は両方(JSON は `test_track_switch.py:131-134`)。`search_track` は stream だけ(`test_track_switch.py:114`)。無いことは確かめていない |
| LLM の失敗: stream は `start` の後に `error` の行、JSON は 500。どちらも会話数は返さない | stream だけ(`test_handle.py:162`) |
| LLM の `ValueError`(`OutputParserException` など): JSON は 400。stream の `error` の行は元の例外の名前で始まる(`BadTurn:` にならない) | 無い |

既存のテストで直す必要があるのは、`server._stale` を差し替えている `tests/test_talk_page.py:306` だけ(差し替え先が `turn` になる)。
`server.state`・`server.LineRuntime` の差し替えは、`TurnDeps` を呼ぶたびに作るので今のまま効く。

意図して変えること: stream の `try` が、`handle_stream` の部分だけから `run` 全体に広がる。`push_trigger` などで例外が起きたとき、
今は応答が途中で切れるが、直した後は `error` の行になる。これは上の表の「LLM の失敗」と同じテストで確かめ、PR の説明に書く。
`error` の行の文言は変えない。`run` は `LineRuntime` の `ValueError` を `BadTurn` に包むが、stream の `except` は包む前の例外(`__cause__`)の名前と文言を出す。
`langchain_core` の `OutputParserException` と `pydantic.ValidationError` は `ValueError` の子なので、包んだままだと文言が `BadTurn: ...` に変わってしまう。
JSON の 400 の範囲は広げない。`run_to_reply` を `except ValueError` で包むと、`push_trigger` などの `ValueError` まで 400 になる
(今は 500)。そのため入口は `BadTurn` だけを 400 にする。

### PR の範囲(1 PR)

コミットの順番:

1. `tests/test_event_turn.py` を足す(今の main で通る)。
2. `line_runtime/turn.py` を作り、2つの入口と補助を移す。`RESUME_LABEL` を `resume_confirm.py` へ移す。`tests/test_talk_page.py:306` の差し替え先を直す。
3. ドキュメントを直す: `server.py` の先頭の docstring、`docs/design/line-sim-architecture.md` の会話の入口の節(159 行目のあたり)に、手順の置き場が `turn.py` であることを1行足す。

この PR でしないこと:

- `/api/voice/start`(`server.py:682`)と `/api/resume/upload`(`server.py:783`)も会話数を1つ使うが、イベントではないので移さない。
  `/api/resume/upload` は stale を先に見て(409)、上限なら読み取らない。`/api/voice/start` は上限だけを見る。
  上限の決まりにもう一度手が入ったら、`turn.py` に会話数を使う関数を足して寄せる。
- 失敗したターンで会話数を返すかどうか。今はどちらの入口も返さない。変えるなら別の判断にする。

## 実装の順番

- 2つの PR は振る舞いの上では独立していて、どちらを先にしてもよい。
- ただし触るファイルは重なる。`line_runtime/server.py`(候補 1 は 740 行目の呼び出しと 827 行目の docstring、候補 2 は 330-470 行目と `RESUME_LABEL`)と `line_runtime/resume_confirm.py`(候補 1 は 72 行目の docstring、候補 2 は `RESUME_LABEL` の移し先)。
  並べて進めると、この2つのファイルで衝突する。後に出す PR で衝突を解く。
- 候補 1 の PR の後に、全角の数字・記号に対応する `scrub_pii` の修正を別のタスクにする(`xfail` にした例を通す)。
