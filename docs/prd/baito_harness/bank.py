"""Full question bank. choice_effects: choice -> {slot: value}, plus optional
'__ng__': [NG items] and '__segment__': segment (segment is NOT a slot)."""
import copy

def Q(id, phase, segments, targets, text, effects):
    return {"id": id, "phase": phase, "segments": segments, "targets": targets,
            "text": text, "choices": list(effects.keys()), "choice_effects": effects}

def make_bank(segment_q_targets=("segment",)):
    B = []
    # ---------------- icebreak ----------------
    B.append(Q("q_money", "icebreak", ["all"], ["purpose"], "バイト代、何に使いたい？", {
        "推し活・趣味": {"purpose": "hobby"},
        "貯金・将来のため": {"purpose": "saving"},
        "生活費・家計の足し": {"purpose": "living_cost"},
        "旅行・遊び": {"purpose": "fun"}}))
    B.append(Q("q_segment", "icebreak", ["all"], list(segment_q_targets), "今のあなたに近いのは？", {
        "学生": {"__segment__": "student"},
        "家のこと中心": {"__segment__": "homemaker"},
        "本業あり副業探し": {"__segment__": "side_job"}}))
    # ---------------- hard_filter (direct) ----------------
    B += [
        Q("hf_days", "hard_filter", ["all"], ["days_per_week"], "週に何日くらい入れそう？", {
            "週1〜2日": {"days_per_week": "1-2"}, "週3〜4日": {"days_per_week": "3-4"},
            "週5日以上": {"days_per_week": "5+"}}),
        Q("hf_time", "hard_filter", ["all"], ["time_slots"], "働けるのはどの時間帯？", {
            "朝": {"time_slots": "morning"}, "昼": {"time_slots": "daytime"},
            "夕方〜夜": {"time_slots": "evening"}, "深夜もOK": {"time_slots": "late_night_ok"}}),
        Q("hf_commute", "hard_filter", ["all"], ["commute"], "通うなら、どこまでOK？", {
            "家から徒歩・自転車": {"commute": "walk_bike"}, "電車で30分まで": {"commute": "train_30"},
            "1時間くらいまでOK": {"commute": "train_60"}}),
        Q("hf_age", "hard_filter", ["student"], ["age_under_18"], "今、18歳未満？", {
            "はい": {"age_under_18": True}, "いいえ": {"age_under_18": False}}),
        Q("hf_school", "hard_filter", ["student"], ["school_permission"], "学校のバイトのルールは？", {
            "OK": {"school_permission": "ok"}, "届け出すればOK": {"school_permission": "needs_notice"},
            "禁止 or わからない": {"school_permission": "banned_or_unknown"}}),
        Q("hf_hours", "hard_filter", ["homemaker"], ["available_hours"], "働ける時間はどれが近い？", {
            "9〜14時くらい": {"available_hours": "9-14"}, "10〜16時くらい": {"available_hours": "10-16"},
            "夕方以降（家族が帰ってから）": {"available_hours": "evening"},
            "日によってバラバラ": {"available_hours": "varies"}}),
        Q("hf_leave", "hard_filter", ["homemaker"], ["sudden_leave"], "急なお休み（子どもの体調など）、どのくらいありそう？", {
            "けっこうある": {"sudden_leave": "frequent"}, "たまにある": {"sudden_leave": "sometimes"},
            "ほぼない": {"sudden_leave": "rare"}}),
        Q("hf_sideok", "hard_filter", ["side_job"], ["side_job_allowed"], "今の会社、副業はOK？", {
            "OK": {"side_job_allowed": "ok"}, "届け出が必要": {"side_job_allowed": "needs_notice"},
            "NG・グレー": {"side_job_allowed": "ng_or_gray"}}),
        Q("hf_weekday", "hard_filter", ["side_job"], ["weekday_or_weekend"], "働くなら平日？週末？", {
            "平日の夜": {"weekday_or_weekend": "weekday_evening"}, "土日": {"weekday_or_weekend": "weekend"},
            "どちらでも": {"weekday_or_weekend": "either"}}),
    ]
    # ---------------- scenario_game: common ----------------
    S = "scenario_game"
    B += [
        Q("sc_line", S, ["all"], ["customer_facing", "pace"], "お店に長い行列ができた！そのとき思うのは？", {
            "よし、燃えてきた": {"customer_facing": "ok", "pace": "busy_ok"},
            "今すぐ抜けたい": {"customer_facing": "avoid", "pace": "calm"}}),
        Q("sc_shift", S, ["all"], ["shift_length"], "理想の1回のシフトは？", {
            "3時間でサクッと": {"shift_length": "short"}, "6時間でガッツリ稼ぐ": {"shift_length": "long"}}),
        Q("sc_teach", S, ["all"], ["atmosphere"], "仕事の教わり方、どっちがいい？", {
            "マニュアル通りにきっちり": {"atmosphere": "structured"},
            "ざっくり教わって自分で考える": {"atmosphere": "autonomous"}}),
        Q("sc_after", S, ["all"], ["atmosphere"], "バイト終わり、どっちがうれしい？", {
            "「おつかれ！ごはん行く？」": {"atmosphere": "friendly"}, "各自解散": {"atmosphere": "dry"}}),
        Q("sc_noise", S, ["all"], ["environment_noise"], "働く場所、どっちがいい？", {
            "にぎやか": {"environment_noise": "lively"}, "静か": {"environment_noise": "quiet"}}),
        Q("sc_wage_commute", S, ["all"], ["commute", "hourly_wage_min"],
          "時給100円の差。徒歩5分の店と電車30分の店、どっち？", {
            "近いほう": {"commute": "walk_bike", "hourly_wage_min": "wage_less_important"},
            "時給が高いほう": {"commute": "train_30", "hourly_wage_min": "wage_priority"}}),
        Q("sc_praise", S, ["all"], ["customer_facing"], "ほめられてうれしいのは？", {
            "お客さん": {"customer_facing": "ok"}, "店長": {"customer_facing": "backoffice_ok"}}),
    ]
    # ---------------- scenario_game: student ----------------
    B += [
        Q("sc_st_peers", S, ["student"], ["atmosphere"], "バイト仲間、どっちがいい？", {
            "同年代": {"atmosphere": "same_age"}, "大人": {"atmosphere": "mixed_age"}}),
        Q("sc_st_exam", S, ["student"], ["exam_flex"], "テスト前、シフトどうしたい？", {
            "完全休み": {"exam_flex": "full_off"}, "少なめなら": {"exam_flex": "reduced"}}),
        Q("sc_st_summer", S, ["student"], ["long_vacation"], "夏休みは？", {
            "がっつり": {"long_vacation": "heavy"}, "いつも通り": {"long_vacation": "normal"}}),
        Q("sc_st_resume", S, ["student"], ["purpose"], "バイト選び、どっち重視？", {
            "履歴書に書ける": {"purpose": "career"}, "ゆるく": {"purpose": "easygoing"}}),
        Q("sc_st_after", S, ["student"], ["time_slots"], "学校のあと、いつから働く？", {
            "放課後すぐ": {"time_slots": "after_school"}, "部活のあと": {"time_slots": "after_club"}}),
        Q("sc_st_perk", S, ["student"], ["purpose"], "うれしいのはどっち？", {
            "まかない": {"purpose": "perk_meal"}, "社割": {"purpose": "perk_discount"}}),
    ]
    # ---------------- scenario_game: homemaker ----------------
    B += [
        Q("sc_hm_fixed", S, ["homemaker"], ["available_hours"], "シフト、どっちがラク？", {
            "週2午前固定": {"available_hours": "fixed_morning"}, "毎週選ぶ": {"available_hours": "flexible"}}),
        Q("sc_hm_fever", S, ["homemaker"], ["sudden_leave"], "子どもが急に熱！どっちが安心？", {
            "すぐ休める": {"sudden_leave": "need_instant"}, "代わりがいる": {"sudden_leave": "cover_ok"}}),
        Q("sc_hm_remote", S, ["homemaker"], ["remote"], "働くなら？", {
            "在宅": {"remote": "yes"}, "外に出たい": {"remote": "no"}}),
        Q("sc_hm_peers", S, ["homemaker"], ["atmosphere"], "職場の雰囲気は？", {
            "子育て中の人が多い": {"atmosphere": "parents"}, "気にしない": {"atmosphere": "any"}}),
        Q("sc_hm_money", S, ["homemaker"], ["fuyou", "purpose"], "バイト代は？", {
            "家計の足し": {"fuyou": "check_needed", "purpose": "household"},
            "おこづかい": {"fuyou": "within", "purpose": "pocket_money"}}),
        Q("sc_hm_exp", S, ["homemaker"], ["purpose"], "仕事内容は？", {
            "経験を活かす": {"purpose": "use_experience"}, "違うこと": {"purpose": "try_new"}}),
    ]
    # ---------------- scenario_game: side_job ----------------
    B += [
        Q("sc_sj_brain", S, ["side_job"], ["purpose"], "副業に求めるのは？", {
            "頭を使わない": {"purpose": "no_brain"}, "本業にプラス": {"purpose": "skill_up"}}),
        Q("sc_sj_weekend", S, ["side_job"], ["weekday_or_weekend"], "週末に働くなら？", {
            "週末1日": {"weekday_or_weekend": "weekend_full_day"}, "半日": {"weekday_or_weekend": "weekend_half_day"}}),
        Q("sc_sj_coworker", S, ["side_job"], ["avoid_coworkers"], "バイト先で会社の人に会ったら？", {
            "気にしない": {"avoid_coworkers": "no"}, "絶対イヤ": {"avoid_coworkers": "yes"}}),
        Q("sc_sj_stand", S, ["side_job"], ["physical", "hourly_wage_min"], "どっちを選ぶ？", {
            "時給高め立ちっぱなし": {"physical": "standing_ok", "hourly_wage_min": "high"},
            "ふつう座れる": {"physical": "seated", "hourly_wage_min": "normal"}}),
        Q("sc_sj_goal", S, ["side_job"], ["purpose"], "副業の目的は？", {
            "稼ぐ": {"purpose": "earn"}, "気分転換": {"purpose": "refresh"}}),
        Q("sc_sj_stable", S, ["side_job"], ["energy_left"], "どっちがいい？", {
            "安定収入（毎週決まって）": {"energy_left": "steady_ok"},
            "スポット（空いた日だけ）": {"energy_left": "limited"}}),
    ]
    # ---------------- reverse_pick ----------------
    RP = ["customer_facing", "physical", "environment_noise"]
    B += [
        Q("rp_student", "reverse_pick", ["student"], RP, "この3つ、一番やりたくないのは？", {
            "コンビニ店員": {"customer_facing": "avoid", "__ng__": ["コンビニ"]},
            "引越しスタッフ": {"physical": "light", "__ng__": ["重い肉体労働"]},
            "コールセンター": {"environment_noise": "lively_ok", "__ng__": ["電話対応"]}}),
        Q("rp_homemaker", "reverse_pick", ["homemaker"], RP, "この3つ、一番やりたくないのは？", {
            "スーパーのレジ": {"customer_facing": "avoid", "__ng__": ["レジ"]},
            "工場の軽作業（立ち仕事）": {"physical": "seated", "__ng__": ["立ち仕事"]},
            "データ入力（在宅）": {"environment_noise": "lively_ok", "__ng__": ["黙々PC作業"]}}),
        Q("rp_side_job", "reverse_pick", ["side_job"], RP, "この3つ、一番やりたくないのは？", {
            "居酒屋ホール（夜）": {"customer_facing": "avoid", "__ng__": ["夜の飲食ホール"]},
            "倉庫ピッキング": {"physical": "light", "__ng__": ["倉庫作業"]},
            "イベント設営": {"environment_noise": "quiet", "__ng__": ["イベント設営"]}}),
    ]
    # ---------------- fill_gaps (direct, every MID slot) ----------------
    F = "fill_gaps"
    B += [
        Q("fg_wage", F, ["all"], ["hourly_wage_min"], "時給はいくら以上がいい？", {
            "1,000円台でOK": {"hourly_wage_min": 1000}, "1,200円以上": {"hourly_wage_min": 1200},
            "1,500円以上": {"hourly_wage_min": 1500}}),
        Q("fg_shift", F, ["all"], ["shift_length"], "1回何時間くらい働きたい？", {
            "2〜4時間": {"shift_length": "2-4h"}, "4〜6時間": {"shift_length": "4-6h"},
            "6時間以上": {"shift_length": "6h+"}}),
        Q("fg_customer", F, ["all"], ["customer_facing"], "接客はどう？", {
            "好き": {"customer_facing": "like"}, "ふつう": {"customer_facing": "ok"},
            "できれば避けたい": {"customer_facing": "avoid"}}),
        Q("fg_physical", F, ["all"], ["physical"], "体力仕事は？", {
            "どんとこい": {"physical": "heavy_ok"}, "少しなら": {"physical": "light"},
            "座り仕事がいい": {"physical": "seated"}}),
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
