# ハーネスからの補足（どのフェーズでも読む）
- 「これまでにわかった条件」は {"slots": {スロット: {"label": 項目の日本語, "value": 値, "value_ja": 値の日本語, "confidence": "high|low"}}, "ng": [NG]} の JSON。
- 「次に聞く質問」は、質問バンクの1問の JSON（id・phase・text・choices・choice_effects・targets）。質問文（text）と選択肢（choices）は原文のまま使い、言い換えない。choices は、ハーネスが NG で絞った後のもの。slot_updates の slot と value は、choice_effects にあるものを使う。
- 次に聞く質問に "retry": true があるときは、前に答えがはっきりしなかった質問の聞き直し。質問文はそのまま使い、前と同じ聞き方に見えないよう一言添えて、一言で答えてもいいことを伝える。
- review では「次に聞く質問」が {"card": card JSON, "card_text": card_text}。
- result では「次に聞く質問」が {"result": true, "card": card JSON, "card_text": card_text}（最終の条件カード。確認の呼びかけは付かない）。reply は card_text の条件カードと、おすすめのバイトの種類3つ（理由つき）。choices は空でよい（ハーネスが付ける）。
- ユーザーが前に言った NG を「やっぱり大丈夫」と取り消したら、出力の JSON に "ng_removals": ["取り消す NG"] を足してよい（ほかのキーはそのまま返す）。18歳未満の人の「深夜シフト」は取り消せない（ハーネスが戻す）。
