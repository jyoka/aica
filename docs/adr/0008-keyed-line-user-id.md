# LINE のユーザーID(`l-…`)は、aica 専用の鍵つきのハッシュで作る

task 177 では、LINE のユーザーID を `"l-" + sha256("aica-line:" + userId)` の先頭 16 桁にした。鍵は使わなかった。LINE の userId は推測できない乱数なので、ハッシュから userId には戻せない。しかし、ある人の userId を知っている人(LINE の bot を持つほかのサービス、その人のトークを見た人)は、同じ計算で `l-…` を作れる。データ(`data/state.json`、`data/dip_accounts.db` の連携の表、会話、費用台帳)が漏れたとき、`l-…` から誰のものかを突き止められる。

2026-10-21 に、LINE のユーザーID を鍵つきのハッシュにすると決めた(ユーザーの決定)。実ユーザーのデータができる前(task 179 の前)に変える。次のように決めた。

- **作り方**: `line_user(userId)` = `"l-" + HMAC-SHA256(AICA_ID_KEY, userId)` の先頭 16 桁(hex)。形(`l-<16桁hex>`)は前と同じ
- **鍵**: aica 専用の `AICA_ID_KEY`(`.env` に置く)。32 バイト(UTF-8)以上。作り方の例は `python -c "import secrets; print(secrets.token_hex(32))"`(64 字)
- **チャネルシークレットは鍵に使わない**: LINE Developers でチャネルシークレットを作り直すと、全員の ID が変わり、それまでのデータとつながらなくなるからである
- **作る所は 1 つ**: LINE のユーザーID を作るのは `line_runtime/line_channel.py` の `line_user` だけ。webhook(`line_runtime/server.py` の `_line_user_events`)も、アカウント連携の表(ADR-0007。`l-…` を鍵にする)も、この ID を使う
- **起動の検査**: LINE の設定(`LINE_CHANNEL_SECRET` と `LINE_CHANNEL_ACCESS_TOKEN`)があるのに `AICA_ID_KEY` が無い、または 32 バイトより短いときは、Web サーバーの起動の途中で止める(`line_channel.check_id_key`。何が足りないかと鍵の作り方をメッセージに出す)。LINE の設定が無ければ、鍵は要らない(Web のトーク画面だけなら今までと同じ)。鍵が無いまま `line_user` を呼んでも ID は作らない(`IdKeyError`)

## Considered Options

- 案 A(鍵なしの sha256 のまま): 変えるところは無いが、userId を知っている人が `l-…` を作れる
- 案 B(チャネルシークレットを鍵にする): 新しい設定は要らないが、シークレットを作り直す(漏れたときの対処)と全員の ID が変わる
- 案 C(aica 専用の鍵で HMAC-SHA256): 採った。鍵は aica だけが持ち、LINE のチャネルの設定とは別に変えられる
- 案 D(userId ごとに乱数の ID を出し、対応の表を持つ): 表に userId を持つことになり、task 177 の「userId は保存しない」を守れない

## Consequences

- **鍵を変えると、全員の `l-…` が変わる。それまでのデータ(会話、プロフィール、行動、アカウント連携)とはつながらなくなる**。鍵は一度決めたら変えない。なくすと戻せないので、`.env` とは別に控えておく
- 鍵が漏れたら、前の鍵なしの sha256 と同じ強さに戻る(userId を知っている人が `l-…` を作れる)。そのときは鍵を変え、データはつながらなくなることを受け入れる
- 手元にすでにある鍵なしの `l-…` のデータは移し替えない(まだ実ユーザーがいないため)。開発者の手元の LINE のユーザーは、鍵を入れた後は新しいユーザーとして始まる。前の `l-…` のデータは `data/` に残るが、どこからも読まれない
- LINE で試す人は、`.env` に `AICA_ID_KEY` を足す必要がある
