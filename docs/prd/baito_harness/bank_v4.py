"""付録A（規範）: v4 質問バンク。PRD_baito_agent_v4.md 付録A が参照する正本。求人カタログは付録B catalog_v4.py。
Q schema: {id, phase, segments, targets, text, alt_text, choices, choice_effects, choice_flags, multi}
choice_effects: choice -> {slot: value, "__ng__": [NG...], "__segment__": seg}
  * applied DETERMINISTICALLY by the harness when the user presses a button (03-A1 / 04-B3)
  * values MUST be canonical enum values of harness_v4.SLOTS (checked by unit_checks_v4.check_bank)
choice_flags: choice -> ["late_night" | "heavy"]  (harness hides flagged choices from minors / unknown age)
multi: True -> several choices may be pressed at once ("朝、夕方〜夜")
v4 rules (02-#5, 03-D3/D4, 02-#11, 02-#15):
  * scenario / reverse_pick / icebreak questions never write HARD slots and never write fact slots (fuyou etc.)
  * multi-aspect slots are split (atmosphere -> teaching_style / social / coworker_age)
"""

def Q(id, phase, segments, targets, text, effects, alt_text=None, flags=None, multi=False):
    return {"id": id, "phase": phase, "segments": segments, "targets": targets, "text": text,
            "alt_text": alt_text or text, "choices": list(effects.keys()), "choice_effects": effects,
            "choice_flags": flags or {}, "multi": multi}

def make_bank():
    B = []
    # ---------------- icebreak ----------------
    B.append(Q("q_money", "icebreak", ["all"], ["purpose"], "バイト代、何に使いたい？", {
        "推し活・趣味": {"purpose": "hobby"},
        "貯金・将来のため": {"purpose": "saving"},
        "生活費・家計の足し": {"purpose": "living_cost"},
        "旅行・遊び": {"purpose": "fun"}}))
    B.append(Q("q_segment", "icebreak", ["all"], ["segment"], "今のあなたに近いのは？", {
        "学生": {"__segment__": "student"},
        "家のこと中心（主婦・主夫など）": {"__segment__": "homemaker"},
        "本業あり副業探し": {"__segment__": "side_job"},
        "フリーター・求職中・その他": {"__segment__": "other"}}))
    # ---------------- hard_filter (direct). hf_age first: tie-break = bank order ----------------
    B += [
        Q("hf_age", "hard_filter", ["all"], ["age_band"], "年齢、ざっくりどれ？（働ける時間が法律で変わるから聞いてるよ）", {
            "中学生以下": {"age_band": "under_15"}, "15〜17歳": {"age_band": "15_17"},
            "18歳以上": {"age_band": "18_plus"}, "ひみつ": {"age_band": "declined"}},
            alt_text="ごめん、これだけ確認させて！年齢はどれに近い？"),
        Q("hf_days", "hard_filter", ["all"], ["days_per_week"], "週に何日くらい入れそう？", {
            "週1〜2日": {"days_per_week": "1-2"}, "週3〜4日": {"days_per_week": "3-4"},
            "週5日以上": {"days_per_week": "5+"}},
            alt_text="ざっくりでOK！週に何日くらいがいい？"),
        Q("hf_time", "hard_filter", ["all"], ["time_slots"], "働けるのはどの時間帯？（いくつでも）", {
            "朝": {"time_slots": ["morning"]}, "昼": {"time_slots": ["daytime"]},
            "夕方〜夜": {"time_slots": ["evening"]}, "深夜もOK": {"time_slots": ["late_night"]}},
            alt_text="一番働きやすい時間帯だけでいいよ！どれ？", flags={"深夜もOK": ["late_night"]}, multi=True),
        Q("hf_commute", "hard_filter", ["all"], ["commute"], "通うなら、どこまでOK？", {
            "家から徒歩・自転車": {"commute": "walk_bike"}, "電車で30分まで": {"commute": "train_30"},
            "1時間くらいまでOK": {"commute": "train_60"}},
            alt_text="通勤、いちばん遠くてどこまでならアリ？"),
        Q("hf_school", "hard_filter", ["student"], ["school_permission"], "学校のバイトのルールは？", {
            "OK": {"school_permission": "ok"}, "届け出すればOK": {"school_permission": "needs_notice"},
            "禁止されてる": {"school_permission": "banned"}, "わからない": {"school_permission": "unknown"}},
            alt_text="学校のバイトのルール、どれが近い？"),
        Q("hf_hours", "hard_filter", ["homemaker"], ["available_hours", "time_slots"], "働ける時間はどれが近い？", {
            "9〜14時くらい": {"available_hours": "9-14", "time_slots": ["morning", "daytime"]},
            "10〜16時くらい": {"available_hours": "10-16", "time_slots": ["daytime"]},
            "夕方以降（家族が帰ってから）": {"available_hours": "evening", "time_slots": ["evening"]},
            "日によってバラバラ": {"available_hours": "varies"}},
            alt_text="いちばん多いパターンだとどれ？"),
        Q("hf_leave", "hard_filter", ["homemaker"], ["sudden_leave"], "急なお休み（家族の用事や体調など）、どのくらいありそう？", {
            "けっこうある": {"sudden_leave": "frequent"}, "たまにある": {"sudden_leave": "sometimes"},
            "ほぼない": {"sudden_leave": "rare"}},
            alt_text="急に休む日、月にどれくらいありそう？"),
        Q("hf_sideok", "hard_filter", ["side_job"], ["side_job_allowed"], "今の会社、副業はOK？", {
            "OK": {"side_job_allowed": "ok"}, "届け出が必要": {"side_job_allowed": "needs_notice"},
            "禁止されてる": {"side_job_allowed": "banned"}, "わからない・グレー": {"side_job_allowed": "unknown"},
            "公務員です": {"side_job_allowed": "civil_servant"}},
            alt_text="副業のルール、どれがいちばん近い？"),
        Q("hf_weekday", "hard_filter", ["side_job"], ["weekday_or_weekend"], "働くなら平日？週末？", {
            "平日の夜": {"weekday_or_weekend": "weekday_evening"}, "土日": {"weekday_or_weekend": "weekend"},
            "どちらでも": {"weekday_or_weekend": "either"}},
            alt_text="平日と週末、どっちが入りやすい？"),
    ]
    # ---------------- scenario_game: common (never HARD / fact slots) ----------------
    S = "scenario_game"
    B += [
        Q("sc_line", S, ["all"], ["customer_facing", "pace"], "お店に長い行列ができた！そのとき思うのは？", {
            "よし、燃えてきた": {"customer_facing": "ok", "pace": "busy_ok"},
            "今すぐ抜けたい": {"customer_facing": "avoid", "pace": "calm"}}),
        Q("sc_shift", S, ["all"], ["shift_length"], "理想の1回のシフトは？", {
            "3時間でサクッと": {"shift_length": "short"}, "6時間でガッツリ稼ぐ": {"shift_length": "long"}}),
        Q("sc_teach", S, ["all"], ["teaching_style"], "仕事の教わり方、どっちがいい？", {
            "マニュアル通りにきっちり": {"teaching_style": "structured"},
            "ざっくり教わって自分で考える": {"teaching_style": "autonomous"}}),
        Q("sc_after", S, ["all"], ["social"], "バイト終わり、どっちがうれしい？", {
            "「おつかれ！ごはん行く？」": {"social": "friendly"}, "各自解散": {"social": "dry"}}),
        Q("sc_noise", S, ["all"], ["environment_noise"], "働く場所、どっちがいい？", {
            "にぎやか": {"environment_noise": "lively"}, "静か": {"environment_noise": "quiet"}}),
        Q("sc_wage_commute", S, ["all"], ["wage_priority"], "時給100円の差。徒歩5分の店と電車30分の店、どっち？", {
            "近いほう": {"wage_priority": "near_first"}, "時給が高いほう": {"wage_priority": "wage_first"}}),
        Q("sc_praise", S, ["all"], ["recognition"], "ほめられてうれしいのは？", {
            "お客さん": {"recognition": "customers"}, "店長": {"recognition": "manager"}}),
    ]
    # ---------------- scenario_game: student ----------------
    B += [
        Q("sc_st_peers", S, ["student"], ["coworker_age"], "バイト仲間、どっちがいい？", {
            "同年代": {"coworker_age": "same_age"}, "大人": {"coworker_age": "mixed_age"}}),
        Q("sc_st_exam", S, ["student"], ["exam_flex"], "テスト前、シフトどうしたい？", {
            "完全休み": {"exam_flex": "full_off"}, "少なめなら": {"exam_flex": "reduced"}}),
        Q("sc_st_summer", S, ["student"], ["long_vacation"], "夏休みは？", {
            "がっつり": {"long_vacation": "heavy"}, "いつも通り": {"long_vacation": "normal"}}),
        Q("sc_st_resume", S, ["student"], ["purpose"], "バイト選び、どっち重視？", {
            "履歴書に書ける": {"purpose": "career"}, "ゆるく": {"purpose": "easygoing"}}),
        Q("sc_st_after", S, ["student"], ["start_time_pref"], "学校のあと、いつから働く？", {
            "放課後すぐ": {"start_time_pref": "right_after_school"}, "部活のあと": {"start_time_pref": "after_club"}}),
        Q("sc_st_perk", S, ["student"], ["purpose"], "うれしいのはどっち？", {
            "まかない": {"purpose": "perk_meal"}, "社割": {"purpose": "perk_discount"}}),
    ]
    # ---------------- scenario_game: homemaker (wording neutral: no "子ども" assumption, 02-#8) ----------------
    B += [
        Q("sc_hm_fixed", S, ["homemaker"], ["shift_flex"], "シフト、どっちがラク？", {
            "曜日・時間を固定": {"shift_flex": "fixed"}, "毎週自分で選ぶ": {"shift_flex": "flexible"}}),
        Q("sc_hm_fever", S, ["homemaker"], ["leave_support"], "家族が急に熱！どっちが安心？", {
            "すぐ休める": {"leave_support": "instant"}, "代わりがいる": {"leave_support": "cover"}}),
        Q("sc_hm_remote", S, ["homemaker"], ["remote"], "働くなら？", {
            "在宅": {"remote": "yes"}, "外に出たい": {"remote": "no"}}),
        Q("sc_hm_peers", S, ["homemaker"], ["coworker_age"], "職場の雰囲気は？", {
            "同じような立場の人が多い": {"coworker_age": "similar_life"}, "気にしない": {"coworker_age": "any"}}),
        Q("sc_hm_money", S, ["homemaker"], ["purpose"], "バイト代は？", {
            "家計の足し": {"purpose": "living_cost"}, "おこづかい": {"purpose": "pocket_money"}}),
        Q("sc_hm_exp", S, ["homemaker"], ["purpose"], "仕事内容は？", {
            "経験を活かす": {"purpose": "use_experience"}, "違うこと": {"purpose": "try_new"}}),
    ]
    # ---------------- scenario_game: side_job ----------------
    B += [
        Q("sc_sj_brain", S, ["side_job"], ["purpose"], "副業に求めるのは？", {
            "頭を使わない": {"purpose": "no_brain"}, "本業にプラス": {"purpose": "skill_up"}}),
        Q("sc_sj_weekend", S, ["side_job"], ["weekend_length"], "週末に働くなら？", {
            "週末1日": {"weekend_length": "full_day"}, "半日": {"weekend_length": "half_day"}}),
        Q("sc_sj_coworker", S, ["side_job"], ["avoid_coworkers"], "バイト先で会社の人に会ったら？", {
            "気にしない": {"avoid_coworkers": "no"}, "絶対イヤ": {"avoid_coworkers": "yes"}}),
        Q("sc_sj_stand", S, ["side_job"], ["physical", "wage_priority"], "どっちを選ぶ？", {
            "時給高め立ちっぱなし": {"physical": "standing_ok", "wage_priority": "wage_first"},
            "ふつう座れる": {"physical": "seated", "wage_priority": "comfort_first"}}),
        Q("sc_sj_goal", S, ["side_job"], ["purpose"], "副業の目的は？", {
            "稼ぐ": {"purpose": "earn"}, "気分転換": {"purpose": "refresh"}}),
        Q("sc_sj_stable", S, ["side_job"], ["work_style"], "どっちがいい？", {
            "安定収入（毎週決まって）": {"work_style": "steady"},
            "スポット（空いた日だけ）": {"work_style": "spot"}}),
    ]
    # ---------------- reverse_pick (always asked once when available, 03-A12) ----------------
    RP = ["customer_facing", "physical"]
    B += [
        Q("rp_student", "reverse_pick", ["student"], RP, "この3つ、一番やりたくないのは？", {
            "コンビニ店員": {"customer_facing": "avoid", "__ng__": ["コンビニ"]},
            "品出し・仕分け（体を動かす）": {"physical": "light", "__ng__": ["体力系の作業"]},
            "コールセンター": {"__ng__": ["電話対応"]}}),
        Q("rp_homemaker", "reverse_pick", ["homemaker"], RP, "この3つ、一番やりたくないのは？", {
            "スーパーのレジ": {"customer_facing": "avoid", "__ng__": ["レジ"]},
            "工場の軽作業（立ち仕事）": {"physical": "seated", "__ng__": ["立ち仕事"]},
            "データ入力（在宅）": {"__ng__": ["黙々PC作業"]}}),
        Q("rp_side_job", "reverse_pick", ["side_job"], RP, "この3つ、一番やりたくないのは？", {
            "居酒屋ホール（夜）": {"customer_facing": "avoid", "__ng__": ["夜の飲食ホール"]},
            "倉庫ピッキング": {"physical": "light", "__ng__": ["倉庫作業"]},
            "イベント設営": {"__ng__": ["イベント設営"]}}),
        Q("rp_general", "reverse_pick", ["other", "unknown"], RP, "この3つ、一番やりたくないのは？", {
            "ファミレスのホール": {"customer_facing": "avoid", "__ng__": ["飲食ホール"]},
            "倉庫ピッキング": {"physical": "light", "__ng__": ["倉庫作業"]},
            "コールセンター": {"__ng__": ["電話対応"]}}),
    ]
    # ---------------- fill_gaps (direct) ----------------
    F = "fill_gaps"
    B += [
        Q("fg_wage", F, ["all"], ["hourly_wage_min"], "時給はいくら以上がいい？", {
            "1,000円台でOK": {"hourly_wage_min": 1000}, "1,200円以上": {"hourly_wage_min": 1200},
            "1,500円以上": {"hourly_wage_min": 1500}}),
        Q("fg_shift", F, ["all"], ["shift_length"], "1回何時間くらい働きたい？", {
            "4時間未満": {"shift_length": "short"}, "4〜6時間": {"shift_length": "mid"},
            "6時間以上": {"shift_length": "long"}}),
        Q("fg_customer", F, ["all"], ["customer_facing"], "接客はどう？", {
            "好き": {"customer_facing": "like"}, "ふつう": {"customer_facing": "ok"},
            "できれば避けたい": {"customer_facing": "avoid"}}),
        Q("fg_physical", F, ["all"], ["physical"], "体力仕事は？", {
            "どんとこい": {"physical": "heavy_ok"}, "少しなら": {"physical": "light"},
            "座り仕事がいい": {"physical": "seated"}}, flags={"どんとこい": ["heavy"]}),
        Q("fg_purpose", F, ["all"], ["purpose"], "いちばんの目的は？", {
            "お金": {"purpose": "money"}, "経験・スキル": {"purpose": "skill"},
            "人とのつながり": {"purpose": "people"}, "気分転換": {"purpose": "refresh"}}),
        Q("fg_exam", F, ["student"], ["exam_flex"], "テスト期間のシフトは？", {
            "休みたい": {"exam_flex": "full_off"}, "減らせればOK": {"exam_flex": "reduced"},
            "気にしない": {"exam_flex": "no_change"}}),
        Q("fg_fuyou", F, ["homemaker"], ["fuyou"], "扶養の範囲内で働きたい？", {
            "はい、扶養内で": {"fuyou": "within"}, "気にしない": {"fuyou": "beyond_ok"},
            "わからない": {"fuyou": "unknown_needs_help"}}),
        Q("fg_remote", F, ["homemaker"], ["remote"], "在宅の仕事も気になる？", {
            "在宅がいい": {"remote": "yes"}, "外がいい": {"remote": "no"}, "どちらでも": {"remote": "either"}}),
        Q("fg_energy", F, ["side_job"], ["energy_left"], "本業のあと、体力はどれくらい残ってる？", {
            "けっこう残ってる": {"energy_left": "high"}, "少しだけ": {"energy_left": "some"},
            "ほぼゼロ": {"energy_left": "none"}}),
    ]
    return B

BANK = make_bank()

# 互換: 求人カタログは catalog_v4.py（付録B）に分離した。従来どおり bank_v4.CATALOG でも参照できる。
from catalog_v4 import CATALOG, CATALOG_BY_ID, J  # noqa: E402,F401
