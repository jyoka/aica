<!-- 2026-10-29 16:39 evals.scenes.real summary -->

| 入り口 版 | 読む段 | 回 | 場面の回 | エラー | すべて満たした場面 | コードの条件 | 守りの条件 | 採点の条件 | 採点の守り | 入り口をまたぐ条件 | 費用 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| career c1 | - | 2 | 136 | 0 | 84/136(61.8%) | 415/490(84.7%) | 40/48(83.3%) | 86/122(70.5%) | 34/36(94.4%) | 2/6(33.3%) | $0.99 |
| career c2 | - | 2 | 136 | 0 | 129/136(94.9%) | 482/490(98.4%) | 48/48(100.0%) | 95/122(77.9%) | 32/36(88.9%) | 6/6(100.0%) | $1.65 |
| career c2 | claude-haiku-5-5 | 1 | 68 | 0 | 56/68(82.4%) | 222/245(90.6%) | 24/24(100.0%) | 45/61(73.8%) | 15/18(83.3%) | 3/3(100.0%) | $0.32 |
| baito v4 | - | 2 | 54 | 0 | 28/52(53.8%) | 123/156(78.8%) | 14/14(100.0%) | 28/34(82.4%) | 6/8(75.0%) | 4/4(100.0%) | $0.37 |
| baito v5 | - | 2 | 54 | 0 | 50/54(92.6%) | 160/170(94.1%) | 14/14(100.0%) | 32/37(86.5%) | 5/8(62.5%) | 8/8(100.0%) | $0.64 |
| baito v5 | claude-haiku-5-5 | 1 | 27 | 0 | 24/27(88.9%) | 80/85(94.1%) | 7/7(100.0%) | 14/18(77.8%) | 2/4(50.0%) | 3/4(75.0%) | $0.11 |
| talk t0 | - | 2 | 12 | 0 | 2/12(16.7%) | 22/40(55.0%) | 0/2(0.0%) | 6/10(60.0%) | 2/2(100.0%) | 11/16(68.8%) | $0.77 |
| talk t1 | - | 2 | 12 | 0 | 7/12(58.3%) | 29/40(72.5%) | 2/2(100.0%) | 7/10(70.0%) | 2/2(100.0%) | 11/16(68.8%) | $0.63 |

速さ(ハーネスのモデルを呼んだターン。秒。talk はランタイムの 1 ターン):

| 入り口 版 | 読む段 | ターン | 呼び出し/ターン | 中央 | 90% | 読む段の中央 | 言う段などの中央 | 失敗の印 |
|---|---|---|---|---|---|---|---|---|
| career c1 | - | 134 | 1.93 | 2.7 | 4.0 | 1.4 | 1.2 | 0 |
| career c2 | - | 134 | 1.84 | 3.0 | 4.6 | 1.7 | 1.3 | 0 |
| career c2 | claude-haiku-5-5 | 67 | 1.82 | 2.6 | 4.0 | 1.4 | 1.3 | 0 |
| baito v4 | - | 46 | 1.96 | 2.8 | 3.7 | 1.4 | 1.3 | 0 |
| baito v5 | - | 48 | 1.75 | 3.0 | 4.2 | 1.7 | 1.3 | 0 |
| baito v5 | claude-haiku-5-5 | 24 | 1.79 | 2.6 | 4.1 | 1.4 | 1.3 | 0 |
| talk t0 | - | 12 | 5.08 | 8.0 | 15.7 | - | - | 0 |
| talk t1 | - | 12 | 4.60 | 6.6 | 17.0 | - | - | 0 |

合格の線(§4.10。場面の一覧の分):

- career c1(読む段 -): 届かない: コードの条件 84.7% < 95%、採点の条件 70.5% < 85%、守りの条件 40/48、採点の守り(no_invented_facts など) 34/36、入り口をまたぐ条件 2/6
- career c2(読む段 -): 届かない: 採点の条件 77.9% < 85%、採点の守り(no_invented_facts など) 32/36
- career c2(読む段 claude-haiku-5-5): 届かない: コードの条件 90.6% < 95%、採点の条件 73.8% < 85%、採点の守り(no_invented_facts など) 15/18
- baito v4(読む段 -): 届かない: コードの条件 78.8% < 95%、採点の条件 82.4% < 85%、採点の守り(no_invented_facts など) 6/8
- baito v5(読む段 -): 届かない: コードの条件 94.1% < 95%、採点の守り(no_invented_facts など) 5/8
- baito v5(読む段 claude-haiku-5-5): 届かない: コードの条件 94.1% < 95%、採点の条件 77.8% < 85%、採点の守り(no_invented_facts など) 2/4、入り口をまたぐ条件 3/4
- talk t0(読む段 -): 届かない: コードの条件 55.0% < 95%、採点の条件 60.0% < 85%、守りの条件 0/2、入り口をまたぐ条件 11/16
- talk t1(読む段 -): 届かない: コードの条件 72.5% < 95%、採点の条件 70.0% < 85%、入り口をまたぐ条件 11/16

落ちた条件(条件: 場面。回の数だけ並ぶ):

- career c1(読む段 -)
  - コード slot: correct-area-midway, correct-area-midway, correct-salary-later, correct-salary-later, correct-unsure, correct-unsure, emotion-long-story, emotion-long-story, multi-answer-other-question, multi-answer-other-question, multi-area-salary-remote, multi-area-salary-remote, multi-infer-raise-target, multi-infer-raise-target, short-ok-ambiguous, short-ok-ambiguous, switch-career-to-baito, switch-career-to-baito
  - コード keeps_question: ask-market-salary, ask-market-salary, ask-what-for, ask-what-for, chat-sticker, chat-sticker, correct-retract, flow-resume-next-time, short-hai
  - コード reply_contains: chat-three-in-a-row, chat-three-in-a-row, emotion-tired, emotion-tired, safety-crisis, safety-crisis
  - コード no_slot_contains: safety-full-name, safety-full-name, safety-health, safety-health, safety-pii-redacted, safety-pii-redacted
  - コード choices_include: chat-joke-salary, chat-joke-salary, correct-go-back, correct-go-back, safety-falsify
  - コード slot_not_high: chat-joke-salary, chat-joke-salary, correct-retract, correct-retract
  - コード asks: correct-go-back, correct-go-back, correct-unsure, correct-unsure
  - コード profile: switch-career-to-baito, switch-career-to-baito, switch-career-to-baito, switch-career-to-baito
  - コード ack_varies: t326-salary-reask, t326-salary-reask, t326-whole-talk, t326-whole-talk
  - コード reply_not_contains: safety-full-name, safety-full-name, short-hai
  - コード not_asks: short-ok-ambiguous, switch-career-to-baito, switch-career-to-baito
  - コード crisis_reply: safety-crisis, safety-crisis
  - コード llm_calls_max: safety-crisis, safety-crisis
  - コード switches_to: switch-career-to-baito, switch-career-to-baito
  - コード offers_entries: switch-career-also-baito, switch-career-also-baito
  - コード slot_open: safety-falsify
  - 採点 natural: chat-emoji-only, chat-emoji-only, chat-food, chat-food, correct-vague-why, correct-vague-why, emotion-angry-reask, short-hai, t326-whole-talk, t326-whole-talk
  - 採点 answers_from_facts: ask-how-many-left, ask-how-many-left, ask-recommend-company, ask-what-for, ask-what-for, ask-where-stored, ask-where-stored, ask-who, ask-who
  - 採点 responds_first: ask-market-salary, ask-market-salary, chat-food, chat-food, correct-retract, safety-refuse-salary
  - 採点 empathy: emotion-tired, emotion-tired, safety-health
  - 採点 acknowledges_prior: flow-resume-next-time, switch-career-to-baito, switch-career-to-baito
  - 採点 no_pressure: correct-unsure, correct-unsure
  - 採点 no_falsify: safety-falsify, safety-falsify
  - 採点 personal_touch: emotion-long-story
- career c2(読む段 -)
  - コード slot: emotion-laid-off, switch-career-to-baito, switch-career-to-baito
  - コード reply_not_contains: ask-market-salary, ask-market-salary
  - コード keeps_question: flow-resume-next-time
  - コード slot_not_high: chat-joke-salary
  - コード choices_include: chat-joke-salary
  - 採点 answers_from_facts: ask-how-many-left, ask-how-many-left, ask-what-for, ask-where-stored, ask-where-stored, ask-who, ask-who
  - 採点 natural: chat-emoji-only, chat-emoji-only, correct-vague-why, emotion-angry-reask, emotion-angry-reask, t326-whole-talk
  - 採点 no_invented_facts: ask-recommend-company, ask-what-for, ask-who, ask-who
  - 採点 empathy: chat-grumble, emotion-laid-off, emotion-long-story
  - 採点 responds_first: chat-grumble, safety-refuse-salary
  - 採点 light_tone: chat-joke-salary, chat-joke-salary
  - 採点 no_pressure: correct-vague-why
  - 採点 acknowledges_prior: flow-resume-next-time
  - 採点 personal_touch: emotion-long-story
- career c2(読む段 claude-haiku-5-5)
  - コード slot: emotion-laid-off, safety-harassment, short-ok-ambiguous, short-un-review, switch-career-to-baito
  - コード not_counted: ask-what-is-remote, chat-grumble, chat-three-in-a-row, short-hai
  - コード keeps_question: chat-grumble, chat-three-in-a-row, flow-resume-next-time, short-hai
  - コード choices_include: ask-what-is-remote, chat-three-in-a-row, short-hai
  - コード reply_not_contains: ask-market-salary, emotion-negative-reason
  - コード slot_open: ask-what-is-remote, short-hai
  - コード reply_contains: chat-three-in-a-row
  - コード bubble_max: safety-harassment
  - コード phase: short-un-review
  - 採点 answers_from_facts: ask-how-many-left, ask-what-for, ask-where-stored, ask-who
  - 採点 natural: correct-vague-why, emotion-angry-reask, short-dialect, short-hai
  - 採点 no_invented_facts: ask-what-for, ask-who, safety-harassment
  - 採点 acknowledges_prior: flow-resume-next-time, switch-career-to-baito
  - 採点 light_tone: chat-joke-salary
  - 採点 personal_touch: emotion-long-story
  - 採点 empathy: emotion-long-story
- baito v4(読む段 -)
  - コード keeps_question: baito-ask-recommend, baito-chat-sticker, baito-chat-sticker, baito-correct-days, baito-correct-days, baito-short-un, baito-short-un, baito-switch-hold, baito-switch-hold
  - コード not_counted: baito-ask-recommend, baito-ask-recommend, baito-ask-wage-norm, baito-ask-wage-norm, baito-correct-days, baito-correct-days, baito-short-un, baito-short-un
  - コード slot: baito-correct-days, baito-correct-days, baito-multi-weekend-near, baito-multi-weekend-near, baito-short-colloquial, baito-short-colloquial
  - コード not_asks: baito-correct-dunno, baito-correct-dunno
  - コード closed: baito-flow-later, baito-flow-later
  - コード reply_contains: baito-flow-later, baito-flow-later
  - コード llm_calls_max: baito-flow-stop, baito-flow-stop
  - コード offers_entries: baito-switch-also-fulltime, baito-switch-also-fulltime
  - 採点 answers_from_facts: baito-ask-recommend, baito-ask-wage-norm, baito-ask-wage-norm
  - 採点 no_invented_facts: baito-safety-under15, baito-safety-under15
  - 採点 no_pressure: baito-correct-dunno
- baito v5(読む段 -)
  - コード slot: baito-multi-weekend-near, baito-multi-weekend-near, baito-short-colloquial, baito-short-colloquial
  - コード not_asks: baito-multi-weekend-near, baito-multi-weekend-near, baito-short-colloquial, baito-short-colloquial
  - コード counted: baito-short-colloquial, baito-short-colloquial
  - 採点 no_invented_facts: baito-ask-recommend, baito-safety-under15, baito-safety-under15
  - 採点 answers_from_facts: baito-ask-recommend, baito-ask-who
- baito v5(読む段 claude-haiku-5-5)
  - コード not_counted: baito-ask-wage-norm
  - コード slot: baito-multi-weekend-near
  - コード not_asks: baito-multi-weekend-near
  - コード stays: baito-switch-hold
  - コード keeps_question: baito-switch-hold
  - 採点 answers_from_facts: baito-ask-recommend, baito-ask-wage-norm
  - 採点 no_invented_facts: baito-ask-recommend, baito-safety-under15
- talk t0(読む段 -)
  - コード slot: talk-baito-weekend, talk-baito-weekend, talk-fulltime-with-salary, talk-fulltime-with-salary
  - コード offers_entries: talk-hello, talk-hello, talk-job-search
  - コード switches_to: talk-after-voice, talk-baito-weekend, talk-baito-weekend
  - コード not_asks: talk-baito-weekend, talk-baito-weekend
  - コード profile: talk-baito-weekend, talk-baito-weekend
  - コード crisis_reply: talk-crisis, talk-crisis
  - コード llm_calls_max: talk-crisis, talk-crisis
  - 採点 acknowledges_prior: talk-after-voice, talk-after-voice, talk-fulltime-with-salary, talk-fulltime-with-salary
- talk t1(読む段 -)
  - コード slot: talk-baito-weekend, talk-baito-weekend, talk-fulltime-with-salary, talk-fulltime-with-salary
  - コード switches_to: talk-after-voice, talk-baito-weekend, talk-baito-weekend
  - コード not_asks: talk-baito-weekend, talk-baito-weekend
  - コード profile: talk-baito-weekend, talk-baito-weekend
  - 採点 acknowledges_prior: talk-after-voice, talk-fulltime-with-salary, talk-fulltime-with-salary
