// トークルームの共通部品。本番のトーク画面(/ の talk.html)と開発者画面(/dev の dev.html)の両方が使う。
// 吹き出し・求人カード・返信の描画(renderReply)・ストリームの読み取り・待ちの表示・入力欄の「+」メニュー・
// 履歴書のアップロード・音声相談の入口をここに1回だけ書き、どちらの画面でも同じ描画にする。
// 画面ごとに違うこと(今のユーザー・忙しさ・ターンの始め方・案内の文言)は createTalk の引数で受け取る。
//
//   const talk = createTalk({
//     log, form,                // トークの #log と入力欄の form(「+」メニューを form の左に付ける)
//     getUser, isBusy, setBusy, // 今のユーザーID / ターンの途中か / ターンの始まりと終わり
//     send(text), fire(event),  // 本人の発言を送る / イベントで1ターン進める(履歴書のアップロードの後に使う)
//     showTalk,                 // (任意)トークの表示に切り替える(本番の下のナビ)
//     getThread, onStale,       // (任意)表示中の会話の番号 / 別のタブで会話が替わっていたとき(本番だけ)
//     notice,                   // (任意)案内の吹き出しの頭に付ける文字(/dev は「[音声相談] 」など)
//     afterTurn,                // (任意)アップロード・音声の結果の後に呼ぶ(/dev は dip の状態を読み直す)
//   });
function createTalk(o) {
  const log = o.log;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const now = () => new Date().toTimeString().slice(0, 5);
  const scroll = () => { log.scrollTop = log.scrollHeight; };
  const yen = (n) => Number(n).toLocaleString("ja-JP");
  const notice = (kind, text) => (o.notice?.[kind] || "") + text;

  // ---------- 吹き出し ----------
  function sys(text) { log.appendChild(el("div", "sys", text)); scroll(); }
  // time: 描き直した履歴は送った時刻が分からないので出さない("")
  function meRow(bubble, time) { const r = el("div", "row me"); if (time) r.appendChild(el("span", "time", time)); r.appendChild(bubble); log.appendChild(r); scroll(); }
  function me(text, time = now()) { meRow(el("div", "bubble", text), time); }
  // 本人が送ったファイル(LINE でファイルを送ったときの吹き出し)。ファイル名は出さない(氏名が入りがち)。種類だけ
  function meFile(label, time = now()) { const b = el("div", "bubble file"); b.append(el("span", "fi", "📄"), el("span", null, label)); meRow(b, time); }
  function botCol() {
    const r = el("div", "row bot"), c = el("div", "col");
    r.append(el("div", "avatar", "B"), c); c.appendChild(el("span", "who", "バイトル"));
    log.appendChild(r); scroll(); return c;
  }
  // AI 側の「入力中」(返信・読み取りを待つ間)
  function typingCol() {
    const col = botCol(), typing = el("div", "bubble typing"); typing.append(el("i"), el("i"), el("i")); col.appendChild(typing); scroll();
    return col;
  }
  function disableChips() { log.querySelectorAll(".chips button").forEach(b => b.disabled = true); }
  function fail(col, text) { col.querySelector(".typing")?.remove(); col.appendChild(el("div", "bubble", text)); scroll(); }

  // 時間のかかるツールの待ち時間に出す一言(tool_start で出し、tools / reply / error で消す)。
  // 一覧に無いツールは速いので、通常の「…」のまま
  const WAIT_LABEL = {
    recommend_jobs: (args) => (args?.exclude_ids?.length ? "🔍 ほかの求人を探しています…" : "🔍 ぴったりの求人を探しています…"),
    relax_conditions: () => "🔍 条件を見直して探しています…",
  };
  function setWait(col, label) {
    const typing = col.querySelector(".typing");
    if (!typing) return;
    typing.querySelector(".wait")?.remove();
    if (label) { typing.prepend(el("span", "wait", label)); scroll(); }
  }
  // ストリームの途中経過のうち、トークの表示に関わるもの(どちらの画面も同じ)
  function onStep(ev, col) {
    if (ev.kind === "tool_start") {
      const slow = ev.calls.find(c => WAIT_LABEL[c.name]);
      if (slow) setWait(col, WAIT_LABEL[slow.name](slow.args));
    } else if (ev.kind === "tools") {
      setWait(col, null);  // ツールが終わったら「何をしているか」を消す(続きは通常の「…」)
    }
  }
  // /api/event/stream の応答(1行1件の JSON)を、届いた順に onEv に渡す
  async function readStream(res, onEv) {
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (line.trim()) onEv(JSON.parse(line));
      }
    }
  }

  // ---------- 求人カード(LINE の Flex Message のカルーセル風) ----------
  // 給与を雇用区分に合わせて出す: 時給(アルバイト・パート)は「時給 1,350円」、月給・年収(正社員)は「年収 450万〜650万円」。
  // 金額は dip の pay(円)から作る。v1 の会話の記録(hourly_wage だけのカード)も時給として描く
  const man = (n) => `${Number(n) / 10000}万`;
  function payParts(j) {
    const p = j.pay || { kind: "時給", min: j.hourly_wage, max: null };
    const range = p.max && p.max !== p.min;
    if (p.kind === "時給") return [p.kind, `${yen(p.min)}` + (range ? `〜${yen(p.max)}` : "") + "円"];
    return [p.kind, man(p.min) + (range ? `〜${man(p.max)}` : "") + "円"];
  }
  // 正社員の求人のタグ(働き方・休日)。アルバイト・パートは勤務時間・週の日数
  function fulltimeTags(j) {
    const t = [], w = j.work_style, h = j.holidays;
    if (w && w.remote !== "不可") t.push(w.remote === "可" ? "リモート可" : "一部リモート");
    if (w?.flex) t.push("フレックス");
    if (w) t.push(`残業 月${w.overtime_hours}時間ほど`);
    if (h) t.push(`年間休日${h.annual_days}日` + (h.weekends_holidays ? "(土日祝)" : ""));
    return t;
  }
  // saved: 「保存した求人」の一覧に出すカード(保存ボタンは出さず、トークで聞くだけ)
  function jobCard(j, saved = false) {
    const c = el("div", "jc"), inner = el("div", "in");
    const fulltime = j.employment_type === "正社員";
    const meta = el("div", "meta");
    if (fulltime) meta.appendChild(el("span", "kind", "正社員"));
    meta.appendChild(document.createTextNode(`${j.job_type} · ${j.area}` + (j.saved_at ? ` · ${j.saved_at}に保存` : "")));
    inner.append(meta, el("div", "ttl", j.title), el("div", "emp", j.employer));
    const [kind, amount] = payParts(j);
    const w = el("div", kind === "時給" ? "wage" : "wage long"); w.append(el("small", null, kind), el("b", null, amount)); inner.appendChild(w);
    const tags = el("div", "tags");
    const station = `${j.station.replace(/駅$/, "")}駅 徒歩${j.walk_min}分`;
    const basics = fulltime ? [...fulltimeTags(j), station] : [j.shift, j.days_min ? `週${j.days_min}日〜` : null, station];
    for (const t of basics.filter(Boolean)) tags.appendChild(el("span", "tag", t));
    for (const p of (j.perks || []).slice(0, 3)) tags.appendChild(el("span", "tag perk", p));  // アピールポイント(待遇)
    inner.appendChild(tags);
    if (j.reasons?.length) { const why = el("div", "why"); why.append(el("span", null, "おすすめ"), document.createTextNode(j.reasons.join(" / "))); inner.appendChild(why); }
    const btns = el("div", "btns");
    const more = el("button", "go", saved ? "トークで詳しく聞く" : "詳しく見る"); more.type = "button";
    more.onclick = () => { o.showTalk?.(); o.send(`「${j.title}」について詳しく教えて`); };
    btns.appendChild(more);
    if (!saved) { const keep = el("button", "keep", "保存"); keep.type = "button"; keep.onclick = () => o.send(`「${j.title}」(${j.job_id})を保存して`); btns.appendChild(keep); }
    c.append(inner, btns); return c;
  }

  // ---------- 返信の描画 ----------
  // actions: AI が差し出すトークの操作(返信の actions)。押しても発言は送らず、その操作に進む。
  // quick_replies(押すと本人の発言として送る選択肢)と同じ並びに、操作だと分かる見た目で先に出す
  const ACTIONS = {
    resume_upload: { label: "📄 ファイルを選ぶ", run: () => pickResume() },
    voice_consult: { label: "🎤 話して伝える(音声・約5分)", run: () => openVoice() },
  };
  function renderReply(r, col, time = now()) {
    col.querySelector(".typing")?.remove();
    for (const b of r.bubbles) col.appendChild(el("div", "bubble", b));
    if (r.bubbles.length && time) col.appendChild(el("span", "time", time));
    if (r.cards.length) { const box = el("div", "cards"); r.cards.forEach(j => box.appendChild(jobCard(j))); log.appendChild(box); }
    for (const b of r.buttons) {
      const w = el("div", "liff"), a = el("a", null, b.label);
      a.href = b.url; a.target = "_blank"; a.rel = "noopener"; w.appendChild(a); log.appendChild(w);
    }
    const actions = (r.actions || []).filter(a => ACTIONS[a]);
    if (actions.length || r.quick_replies.length) {
      const box = el("div", "chips");
      for (const a of actions) {
        const b = el("button", "action", ACTIONS[a].label); b.type = "button"; b.dataset.action = a; b.onclick = ACTIONS[a].run; box.appendChild(b);
      }
      for (const q of r.quick_replies) { const b = el("button", null, q); b.type = "button"; b.onclick = () => o.send(q); box.appendChild(b); }
      log.appendChild(box);
    }
    scroll();
  }

  // ---------- 入力欄の左の「+」メニュー(自分から履歴書・音声を使う人のため。LINE と同じ置き場) ----------
  const plus = el("button", "plus", "+"); plus.type = "button"; plus.id = "plus"; plus.title = "履歴書・音声相談";
  plus.setAttribute("aria-label", "履歴書・音声相談のメニュー"); plus.setAttribute("aria-expanded", "false");
  const menu = el("div", "plus-menu"); menu.id = "plus-menu"; menu.hidden = true;
  function menuItem(id, icon, title, desc, run) {
    const b = el("button"); b.type = "button"; b.id = id;
    const t = el("span"); t.append(el("b", null, title), el("small", null, desc));
    b.append(el("span", "mi", icon), t);
    b.onclick = () => { closeMenu(); run(); };
    menu.appendChild(b);
  }
  menuItem("menu-resume", "📄", "履歴書・職務経歴書を送る", "読み取って、経歴を聞き直さずに相談できます(PDF / JPEG / PNG)", () => pickResume());
  menuItem("menu-voice", "🎤", "音声で相談する", "話すと約5分で履歴書の下書きができます", () => openVoice());
  const closeMenu = () => { menu.hidden = true; plus.setAttribute("aria-expanded", "false"); };
  plus.onclick = () => { if (!o.getUser() || o.isBusy()) return; menu.hidden = !menu.hidden; plus.setAttribute("aria-expanded", String(!menu.hidden)); };
  o.form.prepend(plus); o.form.before(menu);
  document.addEventListener("click", (e) => { if (!menu.hidden && !menu.contains(e.target) && e.target !== plus) closeMenu(); });
  // ターンの途中とユーザーが決まる前は押せない(画面の setBusy から呼ぶ)
  function setEnabled(on) { plus.disabled = !on; if (!on) closeMenu(); }

  // ---------- 履歴書のアップロード(resume-intake) ----------
  // アップロードそのものが本人の1ターン: 本人側にファイルの吹き出し(「履歴書(PDF)」。ファイル名は出さない)→
  // AI 側の「入力中」の間に /api/resume/{user}/upload で読み取り(dip の状態に保存。会話数を1つ使う)→
  // 読み取り結果の確認(要点だけ)を AI 側に出す → resume_upload のイベントで会話を続け、AI が次の1問を返す。
  // 定型文を本人の発言として送ることはしない。氏名・連絡先は保存されないので、トークにも出さない。
  const fileInput = el("input"); fileInput.type = "file"; fileInput.id = "resume-file"; fileInput.hidden = true;
  fileInput.accept = ".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png";
  document.body.appendChild(fileInput);
  function pickResume() { if (o.getUser() && !o.isBusy()) fileInput.click(); }
  // 表示名はサーバー(RESUME_LABEL)と同じ。対応していない形式はサーバーが断るので、種類を決めつけない
  const fileLabel = (name) => (/\.pdf$/i.test(name) ? "履歴書(PDF)" : /\.(jpe?g|png)$/i.test(name) ? "履歴書(画像)" : "ファイル");
  fileInput.onchange = async () => {
    const f = fileInput.files[0]; fileInput.value = "";
    const user = o.getUser();
    if (!f || !user || o.isBusy()) return;
    o.showTalk?.(); o.setBusy(true); disableChips();
    meFile(fileLabel(f.name));
    const col = typingCol();
    let d = null;
    try {
      const fd = new FormData(); fd.append("file", f);
      const q = o.getThread?.() != null ? `?thread=${o.getThread()}` : "";  // 本番: 表示中の会話(替わっていたら読み取らない)
      const res = await fetch(`/api/resume/${encodeURIComponent(user)}/upload${q}`, { method: "POST", body: fd });
      if (res.status === 409) { col.parentElement.remove(); await o.onStale?.(); return; }
      if (!res.ok) { let m = res.status + ""; try { m = (await res.json()).detail || m; } catch (_) {} throw new Error(m); }
      d = await res.json();
      resumeConfirm({ shown: d.bubbles, view_url: d.view_url }, col);
    } catch (err) {
      fail(col, "履歴書を読み取れませんでした: " + err.message);
    } finally { o.setBusy(false); o.afterTurn?.(); }
    if (d && !d.empty) await o.fire("resume_upload");  // 読み取れたら、AI が確認を踏まえて次の1問を返す
  }
  // 読み取り結果の確認(吹き出しの中身=要点だけ・80字以内はサーバーが組み立てる。line_runtime/resume_confirm.py)。
  // 選択肢は付けない(続きは AI の返信)。全文は「読み取り結果を見る」
  function resumeConfirm(u, col, time = now()) {
    renderReply({ bubbles: u.shown.slice(0, 3), cards: [], quick_replies: [], actions: [],
                  buttons: u.view_url ? [{ label: "読み取り結果を見る", url: u.view_url }] : [] }, col, time);
  }

  // ---------- 音声相談(voice-resume との連携) ----------
  // LIFF のように別タブで開く(同じ画面で開く作りは次の task)。相談が終わると voice-resume が
  // /api/voice/result に結果(dip の求人・下書き)を送り、pollVoice が受け取ってトークに描く。
  function openVoice() {
    const user = o.getUser();
    if (!user) return;
    window.open("/liff/voice?user=" + encodeURIComponent(user), "_blank", "noopener");
    sys(notice("voice", "別タブで音声アドバイザーが開きます。話すと約5分で履歴書の下書きができ、結果はこのトークに届きます。"));
  }
  async function pollVoice() {
    const user = o.getUser();
    if (!user || o.isBusy()) return;  // ターンの途中には差し込まない(次の周回で受け取る)
    let results;
    try {
      const res = await fetch(`/api/voice/${encodeURIComponent(user)}/pull`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (!res.ok) return;
      results = (await res.json()).results;
    } catch (e) { return; }  // 再起動中など。次の周回でまた試す
    for (const r of results || []) {
      sys(notice("voiceResult", "音声相談の結果が届きました"));
      renderReply({
        bubbles: ["音声の相談でお話しした内容です。ご提案した求人と、履歴書の下書きをご確認ください。"],
        cards: r.cards || [],
        buttons: r.draft_markdown ? [{ label: "履歴書の下書きを見る", url: `/liff/voice/draft?user=${encodeURIComponent(user)}&rid=${r.id}` }] : [],
        quick_replies: [],
      }, botCol());
    }
    if ((results || []).length) o.afterTurn?.();
  }
  setInterval(pollVoice, 4000);

  return { el, now, scroll, yen, sys, me, meFile, botCol, typingCol, disableChips, fail, setWait, onStep, readStream,
           jobCard, renderReply, resumeConfirm, pickResume, openVoice, setEnabled };
}
