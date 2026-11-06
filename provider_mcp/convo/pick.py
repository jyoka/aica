"""決める 3: 次の問いの選び方と候補(設計メモ docs/design/conversation-layer.md §4.7)。どの入り口でも同じ決まり。

- 群: 問いを群(入り口の定義の groups。正社員は はじめ、条件、職歴、好み)に分ける。固い順(フェーズの順と上限)の代わり
- スコア: 優先度(入り口の今のスコア。正社員は HARD 5 〜 LOW 1)+ 今の話題のおまけ(TOPIC_BONUS)。いちばん高い問いを出す。
  同点は群の順、その次は質問バンクの並び順。おまけは優先度の 1 段より小さいので、低い優先度の問いが HARD の前に来ることはない
- 話題のおまけ: 本人が今の発言で値を言った欄と、同じ話題(入り口の定義の topics)の欄を聞く問いに付く(リモートの話をした人には働き方)
- 本人の言葉の候補: 本人がこの会話で言った言葉(読む段の mentions)を、決めた問いの最初のページの頭に 1 つまで足す(put_first)。
  ボタンのラベルの決まり(合計の字数、数、1 つの字数)に入らなければ、ページの後ろの候補を外して入れる

値と予算と優先度は入り口が持つ。ここは、入り口が数えたスコアと候補のページを受け取って、選んで並べるだけ。
"""

from __future__ import annotations

TOPIC_BONUS = 0.5  # 今の話題のおまけ。優先度の 1 段(1)より小さい


def touched_topics(slots, topics: dict) -> set[str]:
    """本人が今の発言で値を言った欄 -> その欄を含む話題の名前。"""
    slots = set(slots)
    return {name for name, members in topics.items() if slots & set(members)}


def bonus(targets, touched, topics: dict) -> float:
    """問いの欄が、今の話題(touched)のどれかに入っていればおまけ。"""
    return TOPIC_BONUS if any(set(targets) & set(topics[name]) for name in touched) else 0.0


def group_index(phase: str, groups: list[dict]) -> int:
    """問いのフェーズが入る群の番号(群の順)。どの群にも無ければ最後。"""
    return next((i for i, g in enumerate(groups) if phase in g["phases"]), len(groups))


def best(scored):
    """[(スコア, 群の番号, バンクの並び, 問い)] から出す問い。スコアが 0 以下の問いは出さない。無ければ None。"""
    live = [s for s in scored if s[0] > 0]
    if not live:
        return None
    return min(live, key=lambda s: (-s[0], s[1], s[2]))[3]


def put_first(pages: list[list[str]], word: str, tail: list[str], width, total_max: int, count_max: int,
              label_max: int) -> list[list[str]] | None:
    """本人の言葉を最初のページの頭に置いたページ。ほかのページに同じ言葉があれば外す。ボタンのラベル(候補 + tail。tail は
    「他の候補」やスキップ)が決まりに入るまで、最初のページの後ろの候補を外す(外した候補は出さない)。候補が 1 つも残らないか、
    言葉そのものが長すぎるときは None(足さない)。width は字数の数え方。"""
    if not word or width(word) > label_max or not pages:
        return None
    if word in pages[0]:
        return None  # もう最初のページにある
    first = [word, *pages[0]]
    while len(first) > 1 and (sum(width(c) for c in first + tail) > total_max or len(first + tail) > count_max):
        first.pop()
    if len(first) < 2:
        return None
    rest = [[c for c in p if c != word] for p in pages[1:]]
    return [first, *[p for p in rest if p]]
