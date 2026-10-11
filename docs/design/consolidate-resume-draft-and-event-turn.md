# 設計見直し: イベントの受け付けと Resume Draft の契約を、それぞれ 1 か所にする

2026-10-11 のアーキテクチャレビューが挙げた 2 つの候補について、直すかどうかと直し方を決めます。
実装のタスク(設計見直しの (2/3) と (3/3))は、このメモを元にします。このメモではコードを変えません。

- 対象は今の main(`7c73893`)です。コードは `7521952` から変わっていないので、行番号は
  [HLD](../architecture/hld.md) と [review-2026-10.md](../architecture/review-2026-10.md) と同じ版のものです
- 語は HLD の 10 章「用語」に従います
- 候補 2 は review-2026-10.md の R2、候補 1 は R3 です。このメモは、その「直し方の案」を実装できる形に詰めたものです

## 結論

| 候補 | ID | 判断 | 理由 | 実装 |
|---|---|---|---|---|
| 2. イベントの受け付けとターンを 1 か所にする | R2 | 直す | 2 つの API に手を入れた 7 回のコミットが、すべて両方を同じように直している | (2/3) jyoka/tasks#163、1 PR |
| 1. Resume Draft の契約を 1 つにする | R3 | 直す | 個人情報の決まりが実際にずれている。全角で書いた電話番号とメールが、読み取り結果と下書きから消えない | (3/3) jyoka/tasks#164、1 PR |

- 2 つの PR は順番に出します。(2/3) を merge してから (3/3) を始めます。
  どちらも `line_runtime/server.py` と `line_runtime/resume_confirm.py` を触るからです(「実装の順番」)
- 候補 2 を先にします。2 つの API をそろえる手間は、受け付けを変えるたびに毎回かかっているからです

### PR で決めてほしいこと

候補 2 の次の 2 点は、このメモでは決めません。両方の案を並べます(「決めてほしい 2 点」)。

- A. stream の `error` の行の文: `BadTurn` に包む前の例外の名前に戻すか(案 A1)、`BadTurn: ...` に変わるのを受け入れるか(案 A2)
- B. stream の受け付けの途中の例外: 応答が途中で切れる今の振る舞いを残すか(案 B1)、`error` の行にする変化を受け入れるか(案 B2)

## 候補 2: イベントの受け付けとターンを 1 か所にする(R2)

### 今の main での重複

`POST /api/event`(JSON)と `POST /api/event/stream`(NDJSON)は、同じ手順を同じ順に持っています。

| 手順 | JSON(`server.py`) | stream(`server.py`) |
|---|---|---|
| Cookie と user_id の照合 `_require_own` | 332 | 372 |
| イベントの検証 | 333-334 | 373-374 |
| 空の発言を断る `_require_text` | 335 | 375 |
| アップロードの事実 `_upload_facts` | 338 | 376 |
| プッシュ: ターン数の上限に空きがあるか | 341-342 | 385-387 |
| プッシュ: `logic.push_trigger` で送ってよいか | 344-347 | 388-393 |
| stale の照合 `_stale` | 348-350 | 394-397 |
| ターンを 1 つ数える `quota.try_take` | 351-353 | 398-401 |
| 知らせ `_profile_notice` | 354 | 402 |
| LineRuntime を呼ぶ | 355-359(`handle`) | 403-405(`handle_stream`) |
| ロックの中で stale になったらターンを返す `quota.refund` | 360-361 | 409-410 |
| 開いた記録 `_record_open` | 362 | 407 |
| `search_track` と切り替えの状態 `_track_status` | 363-365 | 408 |
| 例外 | 358-359(`ValueError` だけ 400) | 412-413(すべて `error` の行) |

補助の関数(`_stale`、`_profile_notice`、`_record_open`、`_upload_facts`、`_require_text`)は `server.py:418-470` にあり、
2 つの API が共有しています。共有しているのは部品だけで、呼ぶ順番と分岐は 2 か所に書いてあります。

- LineRuntime の側は、すでに 1 つです。`handle`(`runtime.py:262-270`)は `handle_stream` を畳んだものです
- `try_take` は Web サーバーに 4 か所あります(`server.py:351`、`:398`、`:682`、`:783`)。直すのはイベントの 2 か所です。
  音声相談の開始(`:682`)と履歴書のアップロード(`:783`)は 429 を返す別の決まりなので、このままにします
- 評価(`evals/run.py:125-153`)もプッシュの判定とアップロードの事実を組み立て直しています。R2 の残りとして後に回します(「PR の範囲」)

### 違い

意味のある違い(残すもの):

- stream は最初に `start` の行を出し、プッシュなら判定の結果を `gate` の行で出す。JSON は結果だけを返す
- stream は LineRuntime の途中経過(`thread`、`llm`、`tool_start`、`tools`)を出す。JSON は出さない
- 例外の扱い。JSON は LineRuntime の `ValueError` を 400 にし、ほかは 500 になる。stream は応答を返し始めた後なので、
  LineRuntime の例外をすべて `error` の行にする

見かけの違い(実際には同じもの):

- JSON の `r.get("sent") is not False`(`server.py:363`)は、いつも真です。`render`(`runtime.py:472-498`)の返信に `sent` は無く、
  `sent: False` の返信は 342 行と 346 行で先に返っているからです。stream の 408 行と同じ振る舞いです
- stale のときの返り値は、JSON が `{"stale": True, "thread": n}`、stream が `{"kind": "stale", "thread": n}` です。中身は同じです

### 直す価値

`git log -L` で 2 つの API の本体の履歴を見ると、最初のコミット `daba706` の後に 7 つのコミットがあります。
JSON の側と stream の側で、コミットの一覧は完全に同じでした。どのコミットも両方に同じ手順を足しています。

| コミット | 両方に足したもの |
|---|---|
| `e9bed1c` task 58 | Cookie と user_id の照合 |
| `57af1bc` task 59 | ターン数の上限、空の発言を断る |
| `438053b` task 98 | stale の照合、ロックの中の stale で返す、開いた記録 |
| `625987d` task 91 | アップロードの事実 |
| `b171369` task 95 | 知らせ |
| `e63e47a` task 96 | `search_track` と切り替えの状態 |
| `d5b093c` task 112 | IP ごとと全体の上限(6 か所を同じように書き換えた) |

今のところ、片方だけを直し忘れたずれは見つかりませんでした。ただし受け付けの決まりを変えるたびに、2 か所を同じように
書く手間が毎回かかっています。次の機能(再訪、プッシュの間隔など、R4)も、この手順に足すことになります。

### 判断: 直す

受け付けとターンの手順を 1 つのモジュールに書き、2 つの API はそれぞれの書き出しだけを受け持つ形にします。
評価が後で同じ手順を呼べるように、FastAPI には依存させません。

### モジュールの形

`line_runtime/turn.py` を足し、受け付けとターンの決まりをここに集めます。
Web サーバーは Cookie の照合と、HTTP と NDJSON への書き出しだけを受け持ちます。

```python
# line_runtime/turn.py
LIMIT_REASON = "本日の会話数の上限に達している"  # server.py:168 から移す


class BadTurn(ValueError):
    """受け付けないイベント。Web サーバーは 400 にする。"""


@dataclass(frozen=True)
class TurnDeps:
    """1 ターンの受け付けに使うもの。Web サーバーが要求ごとに作る。"""
    state: State
    quota: DailyQuota
    rt: LineRuntime
    unread_uploads: dict[str, dict]
    ip: str


@dataclass(frozen=True)
class Turn:
    """受け付けたイベント。"""
    user_id: str
    event: str
    text: str | None
    thread: int | None
    entry: str
    kind: str              # EVENTS の種別(user、upload、greet、push)
    facts: dict | None     # アップロードの事実。プッシュの事実は run が足す


def accept(deps, user_id, event, text, thread) -> Turn:
    """LLM もターンも使わない検証: イベント、空の発言、アップロードの事実。だめなら BadTurn。"""


async def run(deps, turn) -> AsyncIterator[dict]:
    """プッシュの判定、stale、ターン数の上限、知らせ、LineRuntime、開いた記録までを進め、行を順に出す。
    出す行: gate、stale、thread、llm、tool_start、tools、reply(プッシュの見送りと上限の案内も reply)。
    LineRuntime の ValueError は BadTurn に包んで投げ直す(from e)。"""


async def reply_of(deps, turn) -> dict:
    """run を最後まで進め、JSON の API が返す形にする(kind を除いた reply。stale は {"stale": True, "thread": n})。"""


async def stale_thread(deps, turn) -> int | None:      # server._stale を移す
def profile_notice(state, turn) -> tuple[int, str | None]:  # server._profile_notice を移す
def track_status(state, user_id, reply) -> dict | None:  # server._track_status を移す
```

決まりごと:

- 手順の順番は今と同じにします(上の表の順)。プッシュの上限の確かめは `push_trigger` より前です。dip に接触として記録させないためです
- 副作用(開いた記録、知らせの渡し終わり、`search_track`)は、`reply` の行を出す前に済ませます。今の stream と同じ順です
- `run` が `BadTurn` に包むのは LineRuntime の `ValueError` だけです。包む `try` は、`handle_stream` から次の行を受け取る
  ところ(`__anext__`)だけを囲みます。`push_trigger` や、`reply` の行を受け取った後の副作用(開いた記録、`search_track`)の
  `ValueError` は包みません。JSON の API では、今と同じく 500 のままにするためです
- `TurnDeps` の `state` は、Web サーバーが要求のたびに `server.state` から渡します。テストは `server.state` を差し替えているからです
  (`tests/test_quota.py:84` ほか)

### 呼び出し側の変わり方

| ファイル | 変わること |
|---|---|
| `line_runtime/turn.py` | 新しく作る。`server.py:168`(`LIMIT_REASON`)、`:273-278`(`_track_status`)、`:418-470`(補助の関数)をここへ移す |
| `line_runtime/server.py` | `event` と `event_stream` は下の形になる。`_deps(request)` を足す。`talk_history`(`:269`)は `turn.track_status` を呼ぶ |
| `line_runtime/resume_confirm.py` | `server.py:751` の `RESUME_LABEL` をここへ移す。`turn.py` が `server.py` を import しないようにするため |
| `tests/test_talk_page.py:302-306` | 差し替える先を `server._stale` から `turn.stale_thread` に変え、偽の関数 `passes` の引数を `(deps, turn)` に変える |

`_profile_notice` を指すコメントも、`turn.profile_notice` に直します。直さないと、無い関数を指すようになります。

- `line_runtime/server.py:289`(`talk_track_undo` の docstring)
- `line_runtime/server.py:494`(プロフィールの API の前のコメント)
- `line_runtime/static/talk.html:269`(「サーバーの _profile_notice」)

変えないもの: `line_runtime/runtime.py`(`handle` は評価が使う)、`line_runtime/quota.py`(`available`、`try_take`、`refund`、`limit_reply` はそのまま)、
`dip_mcp/logic.py`(`push_trigger`、`record_talk_open`、`search_track` はそのまま)、画面の JS(`talk.html:269` のコメントのほか)。

Web サーバーの 2 つの API は、次のようになります。

```python
@app.post("/api/event")
async def event(body: EventIn, request: Request):
    _require_own(request, body.user_id)
    deps = _deps(request)
    try:
        t = turn.accept(deps, body.user_id, body.event, body.text, body.thread)
        return await turn.reply_of(deps, t)
    except turn.BadTurn as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/event/stream")
async def event_stream(body: EventIn, request: Request):
    _require_own(request, body.user_id)
    deps = _deps(request)
    try:
        t = turn.accept(deps, body.user_id, body.event, body.text, body.thread)
    except turn.BadTurn as e:  # 応答を返し始める前なので 400 にできる
        raise HTTPException(400, str(e)) from e

    async def lines():
        yield _line({"kind": "start", "event": t.event, "entry": t.entry, "event_kind": t.kind, "model": config.MODEL})
        try:
            async for ev in turn.run(deps, t):
                yield _line(ev)
        except Exception as e:  # noqa: BLE001
            yield _line({"kind": "error", "message": _error_text(e)})  # 決めてほしい点 A

    return StreamingResponse(lines(), media_type="application/x-ndjson")
```

`try` が `run` 全体を囲んでいるのが、決めてほしい点 B です。

### JSON と stream で残す違い

| | JSON | stream |
|---|---|---|
| `start` の行 | なし | 最初に出す |
| `gate` の行 | なし(見送りなら `{"sent": False, "reason": ...}` だけ) | プッシュなら出す |
| LineRuntime の途中経過 | なし | `thread`、`llm`、`tool_start`、`tools` |
| 終わりの形 | `reply_of` の dict | `reply` か `stale` の行 |
| `accept` の `BadTurn` | 400 | 400(応答を返し始める前) |
| LineRuntime の `ValueError` | 400 | `error` の行(文は点 A) |
| LineRuntime のほかの例外 | 500 | `error` の行 |
| 受け付けの途中の例外(`push_trigger` など) | 500 | 点 B |

### 決めてほしい 2 点

#### A. stream の `error` の行の文

`run` が LineRuntime の `ValueError` を `BadTurn` に包むので、そのまま `type(e).__name__` を書くと、行の文が変わります。
たとえば構造化出力の失敗は、今は `OutputParserException: ...` ですが、`BadTurn: ...` になります。
`OutputParserException` と `pydantic.ValidationError` は、どちらも `ValueError` の子です(`.venv` で確かめた)。

| 案 | 中身 | 良い点 | 気になる点 |
|---|---|---|---|
| A1: 包む前に戻す | `_error_text` は、`BadTurn` なら `e.__cause__` の名前と文を書く | 文は今と同じ。開発者画面(`dev.html:358-360`)は文をそのまま画面に出すので、原因の名前が見える | stream の側に `BadTurn` の知識が 1 行残る |
| A2: 変化を受け入れる | `_error_text` は `f"{type(e).__name__}: {e}"` のまま | stream の側が単純 | 開発者画面に `BadTurn: ...` と出て、原因の型が見えなくなる |

トーク画面(`talk.html:178-180`)は決まった文を出し、`message` は `console.error` に渡すだけです。どちらの案でも、求職者に見える文は変わりません。

#### B. stream の受け付けの途中の例外

今の stream の `try`(`server.py:403-413`)は LineRuntime の部分だけを囲んでいます。その前の `push_trigger`、`_stale`、
`try_take`、`_profile_notice` が例外を出すと、`start` の行を送った後に応答が途中で切れます。
上の形では `try` が `run` 全体を囲むので、これらも `error` の行になります。

| 案 | 中身 | 良い点 | 気になる点 |
|---|---|---|---|
| B1: 今の振る舞いを残す | `run` が LineRuntime の例外だけに印を付け(たとえば包む例外を分ける)、stream はその例外だけを `error` の行にする。ほかは投げ直す | 振る舞いはまったく同じ | `run` と stream の間の決まりが 1 つ増える |
| B2: 変化を受け入れる | 上の形のまま | 画面は待ちの表示を消せる。トーク画面は「うまく返信できませんでした」を出す | 振る舞いが変わる。今は応答が切れたとき、画面の読み取りが失敗すれば「通信できませんでした」になる(`talk.html:198-201`)。ブラウザでは確かめていない |

どちらの案でも、JSON の API は今と同じく 500 です。

### 振る舞いを変えないことの確かめ方

実装の前に、今の main で通るテストを足します。2 つの API に同じ場面を通し、結果をそろえて比べます。

`tests/test_event_turn.py`(新しく作る。作りは `tests/test_quota.py:36` の `CountingModel`、`:67-87` の `_server`、`:90-93` の `_stream` に合わせる):

| 場面 | JSON | stream の行の種類 |
|---|---|---|
| 本人の発言 | `reply` の行から `kind` を除いたものと同じ。`search_track` がある | `start`、`llm`、`reply` |
| プッシュを dip が見送る(`F-follow-new` の `prepare` で `sim-new` を写した未連携のユーザーに、`form_exit` を送る。dip は「dip ID と未連携」で断る) | `{"sent": False, "reason": ...}` | `start`、`gate`、`reply` |
| 上限のときのプッシュ | `{"sent": False, "reason": LIMIT_REASON}`。dip に接触が残らない | `start`、`reply`(`gate` なし) |
| ロックの外の stale | `{"stale": True, "thread": n}`。ターンを数えない | `start`、`stale` |
| ターン数の上限 | `limited: True` の返信(`limit_reply`) | `start`、`reply` |
| 空の発言、知らないイベント、読み取り結果の無い `resume_upload` | 400。ターンを数えない | 400 |
| 知らせ | 返信の後に渡し終わる。stale なら渡し終わらない | 同じ |
| 挨拶(`thread` つき) | 開いた記録が残る | 同じ |
| 読み取れなかったアップロードの次のターン | `unread_uploads` から消える | 同じ |
| LineRuntime が `ValueError` を出す | 400 と、その文 | `error` の行。文は点 A で決めた形 |
| `reply` を受け取った後の副作用が `ValueError` を出す | 500(400 にならない) | `error` の行 |

- 最後の場面の偽の LineRuntime は、`handle` と `handle_stream` の両方を持たせます。今の JSON の API は `handle` を呼ぶからです
- 点 A と点 B を決めたら、それに合わせた行を足します(点 B は `push_trigger` が例外を出す場面)
- ロックの中の stale は、`tests/test_talk_page.py:290` がすでに両方の API で確かめています

今あるテストは、`tests/test_talk_page.py:302-306`(差し替える先と、偽の関数の引数)のほかは、変えずに通るようにします。
2 つの API を呼ぶテストは `tests/test_quota.py`、`test_handle.py`、`test_talk_page.py`、`test_isolation.py`、`test_multiuser.py`、
`test_profile_page.py`、`test_scenarios.py`、`test_track.py`、`test_track_switch.py`、`test_revisit.py`、`test_talk_actions.py`、
`test_fulltime_jobs.py`(`test_talk_page.py` の `stream` を使う)です。

手で確かめること: Web プロセスを起動し、トーク画面で発言、再読み込み(stale)、履歴書のアップロードを 1 回ずつ通します。
開発者画面ではシナリオ `B-exit` を通し、プッシュが許可される流れ(`gate` の行の後に返信)を確かめます。
開発者画面のシナリオでは、プッシュの見送りは起こせません。`prepare` が接触の記録を消すので(`evals/scenarios.py:49, 54`)、
`B-exit` と `pii` は毎回許可されます。`F-follow-new` は挨拶(`visit`)なので、プッシュになりません。
見送りは API で確かめます。開発者画面で `F-follow-new` を始めたユーザーに、`/api/event/stream` で `form_exit` を送り、
`gate` の行に `"allowed": false` と「dip ID と未連携」の理由が出るのを見ます。

### PR の範囲((2/3) jyoka/tasks#163)

含むもの:

1. `tests/test_event_turn.py` を足す(今の main で通る)
2. `line_runtime/turn.py` を作り、2 つの API をそこへ寄せる。`RESUME_LABEL` を `resume_confirm.py` へ移す。`_profile_notice` を指す 3 か所のコメントを直す
3. 点 A と点 B の決定に合わせて、stream の `except` とテストを書く
4. HLD を直す。3 章の部品の図と表に `turn.py` を足し、受け付けの役割を Web サーバーから移す。4.1 の図の受け付けの手順
   (`S->>S` のプッシュの判定、stale、ターンを数える)を `turn.py` の手順として描き直す。9 章「会話の入口(イベント)」の
   「Web サーバーの受け付け」を `line_runtime/turn.py` にする

含まないもの:

- 評価を `turn.run` に寄せること(R2 の残り)。評価はターン数の上限をかけず、Cookie も通らないので、`TurnDeps` の `quota` を
  任意にする変更が要ります。別のタスクにします
- 画面との接続が途中で切れたとき、stream は `reply` の行を受け取る前に止まり、開いた記録と知らせの渡し終わりが残らない
  (コードを読んだ限りの推測。ターン自体は LineRuntime が最後まで進める)。この弱さは今と同じに残します

## 候補 1: Resume Draft の契約を 1 つにする(R3)

### 今の main での重複

| もの | 読み取り(`resume_intake/`) | 下書き(`voice_resume/`) | dip の保存(`dip_mcp/`) |
|---|---|---|---|
| 型(5 つの部品の型と `ResumeDraft`) | `schema.py:25-78` | `schema.py:48-101` | `resume_intake.schema` を import(`logic.py:43`) |
| `JST`、`PERSONAL_KEYS` | `schema.py:19`、`:22` | `schema.py:42`、`:45` | `PERSONAL_KEYS` を import |
| envelope | `schema.py:81-89`(`1.0`、`document_kind` なし) | `schema.py:104-115`(`1.1`、`document_kind` あり) | `save_resume_upload`(`logic.py:1017-1042`)が組み直す |
| 書類の種類 | なし | `schema.py:20-21`、`:36-39`(`document_kind_of`) | `logic.py:922-923`、`:997-1000`(`document_kind`)。`tests/voice/test_jobs.py:162-167` が同期を確かめる |
| 電話、メール、郵便番号を消す `scrub_pii` | `extract.py:41-45`、`:88-99` | `drafter.py:18-22`、`:37-48` | `resume_intake.extract` から import(`logic.py:42`)し、保存の前にもう一度かける(`:1027`) |
| 電話とメールを見分ける `has_pii`、`redact_pii` | なし | なし | `data.py:30-77`(全角をそろえる `fold_pii` つき) |

2 つの `ResumeDraft` を OpenAI の構造化出力のスキーマ(`to_strict_json_schema`)にして比べると、違いは説明の文の 7 か所だけです。

| 場所 | 読み取り | 下書き |
|---|---|---|
| `Education` の説明 | 書いてある範囲だけ。無ければ空 | 会話に出た範囲だけ。出なければ空 |
| `school` | 学校名(書いてあるまま) | 学校名(本人が言った呼び方のまま) |
| `organization` | 勤務先(書いてあるまま。例: コンビニ) | 勤務先(本人が言った呼び方のまま。例: コンビニ) |
| `achievements` | ...成果(書いてあるもののみ) | ...成果(本人談のみ) |
| `OtherExperience.description` | ...(書いてあるもののみ) | ...(本人談のみ) |
| `notes` | 本人が大事にしたいと書いたこと | 本人が大事にしたいと言ったこと |
| `unconfirmed` | 読み取りきれず、本人に確認したい点 | 会話で確認しきれず、本人に確認したい点 |

### ずれ

2026-10-11 に今の main で確かめた結果です(`scrub_pii` は 2 つとも同じ結果)。

| 文字列 | `has_pii` | `scrub_pii` |
|---|---|---|
| `連絡先 090-1234-5678` | 見つける | 消す |
| `連絡先 ０９０－１２３４－５６７８` | 見つける | 残る |
| `連絡先 090ー1234ー5678` | 見つける | 残る |
| `ｔａｒｏ＠example．com` | 見つける | 残る |
| `〒１５０－０００１` | 見ない(郵便番号は対象外) | 残る |
| `090 12345678` | 見つけない | 消す |

- 全角の電話番号とメールは、読み取り結果と下書きから消えずに残り、dip にもそのまま保存されます
- 逆に `scrub_pii` だけが消すものもあります(区切りが空白 1 つの電話番号、郵便番号)。`scrub_pii` を `has_pii` の決まりに
  置き換えるだけでは、今より守りが弱くなります

### 直す価値

型の同期の手間は、まだ小さいです。4 つのファイル(2 つの `schema.py`、`extract.py`、`drafter.py`)に触れたコミットは、
merge を除くと 3 つです。うち 2 つは移植(`bde3f6b`、`6caf047`)です(`2cf85e0` は main を取り込んだ merge)。移植の後に両方の `schema.py` を触ったのは `45e36e7`(task 108)だけで、
下書きに `document_kind` を足し、読み取り側には「1.0 のまま出す」という説明を足しました。

一方、安全の振る舞いは実際にずれました。`98aec97`(task 99、2026-09-29)は dip 側に `fold_pii` を足し、`has_pii` と `redact_pii` を
全角に対応させました。2 つの `scrub_pii` は、そのとき直されていません。決まりが 3 か所にあり、1 か所だけが直ったためです。

### 判断: 直す

レビューは「同期の手間や安全の振る舞いのずれが実際に起きているときだけ見直す」と書いていました。後者が起きているので、直します。
日本語の履歴書は全角の数字で書かれることが多いので、アップロードの読み取りでは起こりやすいずれだと考えます(推測。サンプルでは測っていない)。

### モジュールの形

新しいパッケージ `resume_draft/` を作ります。`dip_mcp` にも `voice_resume` にも依存させません。
`voice_resume` は `dip_mcp` を import しない決まりがあるからです(HLD 3.2)。

```python
# resume_draft/schema.py  Resume Draft の形(docs/design/resume-draft-schema.md)
JST = ZoneInfo("Asia/Tokyo")
PERSONAL_KEYS = ("name", "name_kana", "birth_date", "address", "phone", "email")
DOCUMENT_KINDS = ("履歴書", "職務経歴書")
DEFAULT_DOCUMENT_KIND = "履歴書"

class Education(BaseModel): ...       # 型は今と同じ形。説明の文は下の表
class WorkExperience(BaseModel): ...
class OtherExperience(BaseModel): ...
class Certification(BaseModel): ...
class Preferences(BaseModel): ...
class ResumeDraft(BaseModel): ...

def envelope(draft: ResumeDraft, *, source: str, document_kind: str | None = None) -> dict:
    """document_kind が None なら format_version 1.0(document_kind を書かない。アップロード)。
    書類の種類を渡したら 1.1 で document_kind を書く(下書き)。知らない種類は ValueError。"""

def document_kind_of(env: dict | None) -> str:
    """下書きの JSON か音声相談の結果の書類の種類。無い、知らないときは履歴書。"""
```

```python
# resume_draft/pii.py  個人情報の決まり(読み取り、下書き、dip で同じものを使う)
PII_VALUE = ...                  # dip_mcp/data.py:30-42 から移す
def fold_pii(text) -> str: ...   # data.py:44-54 から移す(_PII_FOLD も)
def has_pii(text) -> bool: ...   # data.py:57-59 から移す
def redact_pii(text, repl=..., extra=None) -> str: ...  # data.py:62-77 から移す

SCRUB_PATTERNS = [...]           # 今の _PII_PATTERNS(メール、電話、郵便番号)と同じ 3 つを同じ順で
def scrub_pii(value): ...
    """文字列、リスト、辞書の中の電話、メール、郵便番号を消す。照合は fold_pii した文字で行い、
    同じ位置を元の文字列から消す(fold_pii は 1 文字を 1 文字に写すので位置がそろう)。"""
```

- `scrub_pii` の型の扱い(空になった文字列をリストから落とす、前後の空白を落とす)は今と同じです
- `fold_pii` の表に無い文字だけの文字列では、`scrub_pii` の結果は今と変わりません。`fold_pii` がその文字列を変えないからです
- `fold_pii` した文字の中で `PII_VALUE` に当たる範囲は、新しい `scrub_pii` がすべて消すようにします。テストで確かめます(下)。
  ただし消した後に残りの文字がつながり、新しくメールの形になることはあります(今の main でも、半角だけの文字列で起きる)
- `dip_mcp/data.py` の `PII_FIELDS`(項目名の一覧)は dip の画面の決まりなので、`data.py` に残します

`document_kind_for(track)`、`document_name(track)`、`UNDECIDED_DOCUMENT_NAME`(`voice_resume/schema.py:23-33`)は、音声相談で
雇用区分から書類を選ぶ決まりです。Resume Draft の形ではないので、`voice_resume/prompts.py` へ移します。

### 説明の文の扱い

型を 1 つにすると、7 か所の説明の文は 1 つにそろえる必要があります。読み取りと下書きのどちらにも合う文にし、
「書いてあるまま」と「本人が言ったまま」の違いは指示の側に任せます。指示はすでにこの違いを書いています
(`resume_intake/extract.py:18-36` の「書いてあることだけを書き写す」、`voice_resume/prompts.py:465-472` の「本人が言った事実だけを使って」)。

| 場所 | そろえた文(案) |
|---|---|
| `Education` の説明 | 学歴(分かる範囲だけ。無ければ空)。 |
| `school` | 学校名(元の呼び方のまま) |
| `organization` | 勤務先(元の呼び方のまま。例: コンビニ) |
| `achievements` | 工夫・任されたこと・成果(元にある事実のみ) |
| `OtherExperience.description` | 内容・続けた期間・役割など(元にある事実のみ) |
| `notes` | その他、本人が大事にしたいこと |
| `unconfirmed` | 確かめきれず、本人に確認したい点 |

この文は LLM に送るスキーマの一部なので、読み取りと下書きの出力が少し変わるかもしれません。実 API で前後を比べます(下)。

### 呼び出し側の変わり方

| ファイル | 変わること |
|---|---|
| `resume_draft/__init__.py`、`schema.py`、`pii.py` | 新しく作る |
| `resume_intake/schema.py`、`voice_resume/schema.py` | 消す |
| `resume_intake/extract.py:16`、`:38-45`、`:88-99` | `resume_draft` から `ResumeDraft`、`envelope`、`scrub_pii` を import する。`_PII_PATTERNS` と `scrub_pii` を消す。`finalize` は名前も形もそのまま |
| `voice_resume/drafter.py:13`、`:15-22`、`:37-48` | `resume_draft` から `DEFAULT_DOCUMENT_KIND`、`ResumeDraft`、`envelope`、`scrub_pii` を import する。`DEFAULT_DOCUMENT_KIND` は `:52`、`:69` の引数の既定値なので、外すと import が失敗する。`_PII_PATTERNS` と `scrub_pii` を消す。`finalize` は名前も形もそのまま |
| `voice_resume/render.py:11` | `document_kind_of` を `resume_draft.schema` から import する |
| `voice_resume/outputs.py:18` | `DEFAULT_DOCUMENT_KIND`、`JST` を `resume_draft.schema` から import する |
| `voice_resume/prompts.py:20` | `document_name` をここで定義する(`document_kind_for`、`UNDECIDED_DOCUMENT_NAME` も)。`document_kind_for` が使う `DEFAULT_DOCUMENT_KIND` を `resume_draft.schema` から import する |
| `voice_resume/advisor.py:48` | `document_kind_for`、`document_name` を `voice_resume.prompts` から import する |
| `dip_mcp/data.py:30-77` | `PII_VALUE`、`_PII_FOLD`、`fold_pii`、`has_pii`、`redact_pii` を `resume_draft/pii.py` へ移す |
| `dip_mcp/logic.py:32-43` | `fold_pii`、`has_pii`、`redact_pii`、`scrub_pii`、`PERSONAL_KEYS`、`ResumeDraft` を `resume_draft` から import する。`resume_intake` を import しなくなる |
| `dip_mcp/logic.py:920-923`、`:952`、`:997-1000` | 書類の種類の写しと `document_kind` を消し、`resume_draft.schema` の `DOCUMENT_KINDS`、`DEFAULT_DOCUMENT_KIND`、`document_kind_of` を使う |
| `dip_mcp/profile.py:23`、`:61` | `fold_pii`、`has_pii` を `resume_draft.pii` から import する。`logic.document_kind(r)` を `document_kind_of(r)` にする |
| `line_runtime/server.py:740` | `logic.document_kind(r)` を `document_kind_of(r)` にする |
| `line_runtime/voice_reply.py:15`、`:73`、`:96` | `from dip_mcp.logic import ... document_kind ...` から `document_kind` を外し、`resume_draft.schema` の `document_kind_of` を使う。直さないと `voice_reply` の import が失敗し、それを import する `line_runtime/server.py:61` と `line_runtime/runtime.py:34` も読み込めなくなる |
| `evals/checks.py:12-13` | `PII_VALUE`、`fold_pii`、`has_pii` を `resume_draft.pii` から import する |
| `scripts/voice/e2e_audio_session.py:44`、`scripts/voice/eval_stt.py:37` | `JST` を `resume_draft.schema` から import する |
| `ruff.toml:4` | `known-first-party` に `resume_draft` を足す |
| テストの import | `tests/test_resume_intake.py:12-13`、`test_resume_upload.py:20`、`test_track.py:23`、`test_talk_actions.py:198`、`test_voice_track.py:37`、`test_isolation.py:22`、`test_profile_memory.py:19`、`tests/voice/test_drafter.py:8-9`、`test_advisor.py:11`、`test_render.py:7`、`test_outputs.py:11` |
| `tests/voice/test_jobs.py:162-167` | 写しが無くなるので、同期のテストを消す |

古い置き場を指す docstring とコメントも直します。

- `line_runtime/server.py:827`、`line_runtime/resume_confirm.py:72`、`dip_mcp/logic.py:1018`、`evals/scenarios.py:33`、`voice_resume/render.py:28`
- `line_runtime/static/talk-core.js:189`(「`voice_resume/schema.py` の `document_name` と同じ」を `voice_resume/prompts.py` に)
- `voice_resume/README.md:43`

`README.md:314`(読み取りの直後と dip への保存のときの 2 回、消す)は、移した後も正しいので変えません。

### 意図して変えること、変えないこと

変えること:

- 全角の数字、長音記号やマイナスのハイフン、全角の `＠` と `．` で書かれた電話、メール、郵便番号を、読み取り結果、下書き、dip の保存から消す
- LLM に送るスキーマの説明の文(7 か所)

変えないこと:

- 型の形(項目、型、既定値)、`PERSONAL_KEYS`、`personal` を空で保存すること
- envelope の形。アップロードは `1.0` で `document_kind` なし、下書きは `1.1` で `document_kind` あり
- `has_pii`、`redact_pii` の結果(場所を移すだけ)
- `fold_pii` の表に無い文字だけの文字列での `scrub_pii` の結果

### 振る舞いを変えないことの確かめ方

実装の前に、今の main で次のテストを足します(`tests/test_resume_draft.py`)。

| テスト | 今の main | 実装の後 |
|---|---|---|
| 2 つの `ResumeDraft` のスキーマから説明の文を除いた形が同じで、テストに置いた固定値と同じ | 通る | 1 つの `ResumeDraft` で通る |
| envelope の形(キーと順番、`format_version`、`document_kind` の有無、知らない種類は `ValueError`) | 通る | 通る |
| `scrub_pii` の今の結果の固定値(`tests/test_resume_intake.py:108` と `tests/voice/test_drafter.py:87-107` の文字列に、上の「ずれ」の半角の行を足す) | 通る | 通る |
| 全角の電話、メール、郵便番号を消す(上の「ずれ」の全角の行) | `xfail(strict=True)` | `xfail` を外して通る |
| テストの文字列すべてで、`fold_pii(s)` の中の `PII_VALUE` に当たる範囲が `scrub_pii(s)` で消えている | 全角の行で `xfail(strict=True)` | 通る |
| `save_resume_upload` に全角の電話番号を渡すと、保存した読み取り結果に残らない | `xfail(strict=True)` | 通る |
| `document_kind_of`: 無い、知らない種類は履歴書 | 下書き側の関数で通る | 通る |

- `strict=True` の `xfail` にするのは、意図して変える振る舞いを、変える前から見えるようにするためです。直り忘れがあれば、テストが落ちます
- 今あるテストは、import の行のほかは変えずに通るようにします(`tests/voice/test_jobs.py:162-167` は消す)

手で確かめること(実 API):

- 読み取り: `python -m resume_intake.run_samples` を実装の前と後で回し、`data/resume_intake/` の比較を見る。
  正解のある `01_clean`、`02_photo`、`03_sparse` を正解と比べる(`resume_intake/samples/expected/`)。正解の無い `04_unreadable` は、
  CLI で読めなかったときの結果(全項目が空)を確かめる
- 下書き: `tests/voice/test_drafter.py:11` の `TRANSCRIPT` を、実 API の `build_draft` に前と後で 1 回ずつ通し、出力を目で比べる(使い捨てのスクリプトで)

### PR の範囲((3/3) jyoka/tasks#164)

1. `tests/test_resume_draft.py` を足す(今の main で通る)
2. `resume_draft/` を作り、呼び出し側を移し、2 つの `schema.py` と 2 つの `scrub_pii` を消す
3. 全角の `xfail` を外す
4. 文書を直す(下)

### 文書の直し方

`docs/design/resume-draft-schema.md:3-5` は今こう書いています。

> このドキュメントが正で、実装は `resume_intake/schema.py` と `voice_resume/schema.py`(どちらも pydantic、task 62 で移植済み)の
> 2か所に複製されている。形を変えるときは必ず両方を更新すること。

(3/3) で、次の趣旨に書き換えます。

- このドキュメントが正で、実装は `resume_draft/schema.py` の 1 か所。読み取り、下書き、dip の保存が同じものを使う
- 電話、メール、郵便番号を消す決まりは `resume_draft/pii.py` の 1 か所。全角をそろえてから照合する
- 説明の文は読み取りと下書きで共通。「書いてあるまま」と「本人が言ったまま」の違いは、それぞれの指示に書く

設計の文書化の (2/3) で、この文書が LLD に移っていれば、移った先を直します。HLD も直します。

- 3.2 の依存の図: `dm -- "scrub_pii、ResumeDraft" --> ri` を、`dm`、`ri`、`vr` から `resume_draft` への矢印にする
- 6 章「個人情報」: 「個人情報を見分ける決まりは 2 種類あります」を、1 か所になったことに書き換える
- 9 章「Resume Draft の形」: 一緒に変えるものを「`resume_intake/schema.py` と `voice_resume/schema.py` の両方」から `resume_draft/schema.py` にする

`docs/research/apply-flow.md:14` は時点の記録なので、`dip_mcp/data.py` の `PII_VALUE` を指したままにします。

## 実装の順番

(2/3) を merge してから、(3/3) を main から始めます。2 つの PR は振る舞いの上では独立していますが、同じファイルを触ります。

| ファイル | (2/3) 候補 2 | (3/3) 候補 1 |
|---|---|---|
| `line_runtime/server.py` | `:168`、`:273-278`、`:330-470`、`:751` を移すか書き換える。`:289`、`:494` のコメント | `:740` の呼び出し、`:827` の docstring |
| `line_runtime/resume_confirm.py` | `RESUME_LABEL` を足す | `:72` の docstring |
| `docs/architecture/hld.md` | 3 章、4.1、9 章 | 3.2、6 章、9 章 |

並べて進めると、後に出す PR が `server.py` で衝突します。(2/3) が `server.py` の行を大きく動かすので、(3/3) の行番号はそのとき引き直します。
`line_runtime/static/talk.html` は (2/3) だけ、`line_runtime/static/talk-core.js` は (3/3) だけが触ります(どちらもコメント)。
`line_runtime/voice_reply.py`、`line_runtime/runtime.py` は (3/3) だけが触ります(`runtime.py` は import が通ることの確かめだけで、書き換えない)。

## 確かめたこと、確かめていないこと

確かめたこと(2026-10-11、今の main):

- 2 つの API の行と補助の関数の行(`server.py` は 898 行)。`render` に `sent` が無いこと
- `git log -L` で 2 つの API の本体の履歴。`daba706` の後の 7 つのコミットは両方で同じ一覧
- 2 つの `ResumeDraft` の `to_strict_json_schema` を比べ、違いは説明の文の 7 か所だけ
- 「ずれ」の表の 6 つの文字列を、`has_pii` と 2 つの `scrub_pii` に通した結果。`fold_pii` してから今の 3 つの型で消すと、全角の行も消えること
- `OutputParserException` と `pydantic.ValidationError` が `ValueError` の子であること。`raise BadTurn(...) from e` の後に `__cause__` から元の文が取れること
- 未連携のユーザーへのプッシュの見送り。`F-follow-new` の `prepare` で写したユーザーに `logic.push_trigger(..., "form_exit")` を呼ぶと、
  「dip ID と未連携なので行動イベントがない」で断られる。`B-exit` で写したユーザーは許可される(`.venv` で確かめた)
- `logic.document_kind` を使う場所(`dip_mcp/profile.py:61`、`line_runtime/server.py:740`、`line_runtime/voice_reply.py:73`、`:96`)

確かめていないこと:

- 新しいテスト(`tests/test_event_turn.py`、`tests/test_resume_draft.py`)が今の main で書いたとおりに通るか。実装のタスクで書きます
- 点 B の案 B1 で、応答が途中で切れたときにブラウザの画面がどう見えるか
- そろえた説明の文で、読み取りと下書きの出力がどれだけ変わるか
- 全角の電話番号が、実際の履歴書の読み取りでどのくらい出るか
