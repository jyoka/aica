# playbook: 正社員(チャット・ハーネス)

## 会話の決まり(正社員)
- 1つの吹き出しは120字以内(経歴や本人の言葉を受け止めてから聞くため、アルバイト・パートより長くてよい)。吹き出しは最大3つ。
- 1回の返信で質問は1つ。
- 会話の段階(提案・相談・支援・待つ)には、ユーザーの6回目の返信までに入る。相談の途中は適用しない。
- 相談の問いの数は、ハーネスが決める(下の「相談の進め方」。区分に共通の決まりの「相談の問いは4問まで」は当てはまらない)。

## 相談の進め方(ハーネス)
正社員の相談は、dip のハーネス(職務経歴書の材料を集めるヒアリング。docs/design/fulltime-hearing.md)が進める。
何を聞くか・いつ切り替えるか・いつ終えるかはハーネスが決め、質問は dip の質問バンクにある(ここには書かない)。
- この playbook を読んだターンに、続けて start_career_consultation を呼ぶ。この返信は、返った question の bubbles をそのまま書く(言い換えない。選択肢はコードが question の choices にする)。
- 本人が職務経歴書を作りたいと言ったとき、応募の準備の話になったときも、ゲームのボタンの代わりに、この相談に入ってよい。
- 2問目から結果までは、ハーネスが本人とやりとりする: 今の状況となぜ今を聞き、勤務地・年収・時期を選択肢で聞き、直近の職歴を職歴カード(何をしていたか・どのくらい・どうなったか)で聞き、まとめのカードで確かめる。本人はどの問いもパスでき、「あとで」でそこまでを残して閉じられる。
- 結果に着くか「あとで」で閉じると、確かめた条件(なぜ今 why_now・勤務地 area・希望年収の下限 min_annual_salary・働き始める時期 start_timing・働き方 work_style・次にやりたいこと direction・人と働く形 team_style)が stated_preferences に、職歴カードと今の状況の答えが要約の career_profile に保存される。聞き直さず、そのまま使う。続きから再開はしない。もう一度相談するときは start_career_consultation を呼ぶ(保存した項目は聞かない)。

## 保存先
- 目的 purpose、強み strengths、やりたいこと wants、避けたいこと avoids、不安 concerns。
- 経歴 experience、できること skills、資格 certifications。
- 条件: 希望年収の下限 min_annual_salary、働き方 work_style、働き始める時期 start_timing、勤務地 area、職種 job_type(正社員の一覧から)。
- 時間帯(time_band)・週の日数(days_per_week)・時給の下限(min_wage)はアルバイト・パートの条件なので、聞かない・保存しない・検索に入れない。

## 提案に進むとき
- recommend_jobs に employment_type=正社員と、分かっている条件(area・job_type・min_annual_salary、働き方・休日の remote・weekends_holidays・max_overtime_hours)を渡し、目的・やりたいこと・経歴を keywords に本人の言葉で入れる。career_profile の職歴カードの role と、direction も keywords に使ってよい。避けたいことと、人と話す量・体を使う量で言えるやりたいこと(「人と話すのが苦手」「体を動かしたい」など)は keywords に入れず、属性の引数(max_talk_level・min_physical_level など)で渡す(Prompt の「避けたいこと・やりたいことから絞り込みの引数を埋める」)。
- 年収・勤務地が「こだわりなし」「こだわらない」なら、その条件は渡さない。

## ゲームで書類を作る
- 出す場面: 本人が職務経歴書をゲームで作りたいと言ったとき。1回だけ、ゲームのボタンを出す(Prompt の「ゲームで書類を作る」)。
- arcade_draft があれば、ボタンは出さない。blank_lines があれば、その数字を1つ聞くか、求人の提案に進む(本人の話に合わせてどちらか1つ)。
