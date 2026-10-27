"""付録B（規範）: 求人カタログ＝推薦の許可リスト（v4）。PRD_baito_agent_v4.md 付録B が参照する正本（スキーマと照合規則は PRD 付録B・§6-5・§8-7）。
J(id, name, cf, phys, times, keys, min_age=15, remote=False, noise="normal", night_only=False, spot=False)
  cf    : 接客の度合い  high / mid / low / none
  phys  : 体の負担      seated / light / standing / heavy
  times : morning / daytime / evening / late_night の部分集合
  keys  : NG 照合用の語（PRD §6-5）
  min_age: 18 なら未成年モード（年齢不明・未回答を含む）では出さない
  remote: 在宅可 / noise: quiet / normal / lively / night_only: 主に22〜5時 / spot: 単発可
"""
# cf: customer-facing level (high/mid/low/none); phys: seated/light/standing/heavy
# times: subset of morning/daytime/evening/late_night; min_age: 18 => never shown to minors / unknown age
# night_only: work is mainly 22-5 (excluded for minors); keys: words matched against NG strings
def J(id, name, cf, phys, times, keys, min_age=15, remote=False, noise="normal", night_only=False, spot=False):
    return dict(id=id, name=name, cf=cf, phys=phys, times=set(times), keys=keys, min_age=min_age,
                remote=remote, noise=noise, night_only=night_only, spot=spot)

M, D, E, N = "morning", "daytime", "evening", "late_night"
CATALOG = [
    J("conv", "コンビニスタッフ", "high", "standing", [M, D, E, N], ["コンビニ", "レジ", "接客"]),
    J("super_register", "スーパーのレジ", "high", "standing", [M, D, E], ["スーパー", "レジ", "接客"]),
    J("super_stock", "スーパーの品出し", "low", "standing", [M, D, E], ["品出し", "立ち仕事"]),
    J("cafe", "カフェスタッフ", "high", "standing", [M, D, E], ["カフェ", "飲食", "接客"], noise="lively"),
    J("family_rest", "ファミレスのホール", "high", "standing", [D, E, N], ["ファミレス", "飲食ホール", "飲食", "接客"], noise="lively"),
    J("kitchen", "飲食店のキッチン", "low", "standing", [D, E], ["キッチン", "飲食", "立ち仕事"]),
    J("izakaya", "居酒屋ホール", "high", "standing", [E, N], ["居酒屋", "夜の飲食ホール", "飲食ホール", "飲食", "接客"], min_age=18, noise="lively"),
    J("warehouse", "倉庫の仕分け・ピッキング", "none", "heavy", [M, D, E, N], ["倉庫", "倉庫作業", "体力系の作業", "立ち仕事"], spot=True),
    J("moving", "引越しスタッフ", "low", "heavy", [M, D], ["引越し", "重い肉体労働", "体力系の作業"], min_age=18, spot=True),
    J("event_setup", "イベント設営", "none", "heavy", [M, D, E, N], ["イベント設営", "体力系の作業"], min_age=18, spot=True),
    J("event_staff", "イベントスタッフ（案内・誘導）", "mid", "standing", [D, E], ["イベント", "接客", "立ち仕事"], noise="lively", spot=True),
    J("call_center", "コールセンター", "mid", "seated", [D, E], ["コールセンター", "電話対応", "電話"], min_age=18),
    J("data_entry", "データ入力", "none", "seated", [M, D, E], ["データ入力", "黙々PC作業", "PC"], remote=True, noise="quiet"),
    J("tutor", "塾講師・個別指導", "mid", "seated", [E], ["塾", "講師"], min_age=18, noise="quiet"),
    J("apparel", "アパレル販売", "high", "standing", [D, E], ["アパレル", "接客", "立ち仕事"]),
    J("bookstore", "書店スタッフ", "mid", "light", [M, D, E], ["書店", "レジ"], noise="quiet"),
    J("cleaning", "清掃スタッフ", "none", "light", [M, D], ["清掃"], noise="quiet"),
    J("factory_light", "工場の軽作業", "none", "standing", [M, D, E], ["工場", "立ち仕事"], noise="quiet"),
    J("reception", "受付・事務補助", "mid", "seated", [M, D], ["受付", "事務"], min_age=18, noise="quiet"),
    J("nursery_assist", "保育補助", "mid", "standing", [M, D], ["保育"], min_age=18),
    J("posting", "ポスティング", "none", "light", [M, D, E], ["ポスティング"], noise="quiet", spot=True),
    J("web_writer", "Webライター（在宅）", "none", "seated", [M, D, E, N], ["ライター", "黙々PC作業", "PC"], min_age=18, remote=True, noise="quiet"),
    J("karaoke", "カラオケスタッフ", "high", "standing", [D, E, N], ["カラオケ", "接客"], noise="lively"),
    J("pool_reception", "スポーツ施設の受付", "mid", "light", [M, D, E], ["受付", "接客"]),
]
CATALOG_BY_ID = {j["id"]: j for j in CATALOG}
