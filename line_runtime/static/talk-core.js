// トークルームの共通部品。本番のトーク画面(/ の talk.html)と開発者画面(/dev の dev.html)の両方が使う。
// 吹き出し・求人カード・返信の描画(renderReply)・ストリームの読み取り・待ちの表示・入力欄の「+」メニュー・
// 履歴書のアップロード・音声相談のシートをここに1回だけ書き、どちらの画面でも同じ描画にする。
// 画面ごとに違うこと(今のユーザー・忙しさ・ターンの始め方・案内の文言)は createTalk の引数で受け取る。
//
//   const talk = createTalk({
//     log, form,                // トークの #log と入力欄の form(「+」メニューを form の左に付ける)
//     getUser, isBusy, setBusy, // 今のユーザーID / ターンの途中か / ターンの始まりと終わり
//     send(text), fire(event),  // 本人の発言を送る / イベントで1ターン進める(履歴書のアップロードの後に使う)
//     showTalk,                 // (任意)トークの表示に切り替える(本番の下のナビ)
//     getThread, onStale,       // (任意)表示中の会話の番号 / 別のタブで会話が替わっていたとき(本番だけ)
//     afterTurn,                // (任意)アップロード・音声の結果の後に呼ぶ(/dev は dip の状態を読み直す)
//     beforeTurn,               // (任意)本人のターン(発言・アップロード)の前に呼ぶ(本番: 閉じたシートの結果を描き直す)
//   });
function createTalk(o) {
  const log = o.log;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const now = () => new Date().toTimeString().slice(0, 5);
  const scroll = () => { log.scrollTop = log.scrollHeight; };
  const yen = (n) => Number(n).toLocaleString("ja-JP");

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
    o.showTalk?.(); o.setBusy(true);
    try { await o.beforeTurn?.(); } catch (e) { console.error(e); }
    disableChips();
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
    // AI が確認を踏まえて次の1問を返す(読み取れなかったときは、その前提で1問)
    if (d) await o.fire("resume_upload");
  }
  // 読み取り結果の確認(吹き出しの中身=要点だけ・80字以内はサーバーが組み立てる。line_runtime/resume_confirm.py)。
  // 選択肢は付けない(続きは AI の返信)。全文は「読み取り結果を見る」
  function resumeConfirm(u, col, time = now()) {
    renderReply({ bubbles: u.shown.slice(0, 3), cards: [], quick_replies: [], actions: [],
                  buttons: u.view_url ? [{ label: "読み取り結果を見る", url: u.view_url }] : [] }, col, time);
  }

  // ---------- 音声相談(voice-resume との連携) ----------
  // トークの上に重ねる通話中のシートで開く(タブはまたがない)。POST /api/voice/{user}/start で LiveKit の
  // 接続トークンを受け取り(本人の Cookie のユーザーだけ。会話数を1つ使う)、マイクを許可して音声ワーカーと話す。
  // 面談が終わると、ワーカーが /api/voice/result に結果を送り、サーバーが今の会話に AI の返信として続ける。
  // シートは開いている間だけ /api/voice/{user}/wait で待ち、届いたら閉じて同じ返信をトークに描く
  // (システム通知のラベルは出さない。再読み込みしても履歴に残る)。
  const LIVEKIT_CLIENT = "https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js";
  const sheet = el("div", "voice-sheet"); sheet.id = "voice-sheet"; sheet.hidden = true;
  sheet.setAttribute("role", "dialog"); sheet.setAttribute("aria-modal", "true"); sheet.setAttribute("aria-label", "音声相談");
  const panel = el("div", "vs-panel"), vHead = el("div", "vs-head");
  const vClose = el("button", "vs-close", "×"); vClose.type = "button"; vClose.id = "voice-close"; vClose.setAttribute("aria-label", "閉じる");
  vHead.append(el("b", null, "🎤 音声で相談"), vClose);
  const vOrb = el("div", "vs-orb"); vOrb.append(el("i"), el("i"), el("i"));
  const vStatus = el("div", "vs-status"); vStatus.id = "voice-status"; vStatus.setAttribute("aria-live", "polite");
  const vCaps = el("div", "vs-caps"); vCaps.id = "voice-captions";
  const vEnd = el("button", "vs-end", "面談を終える"); vEnd.type = "button"; vEnd.id = "voice-end";
  // 通話中に × を押したときの確認(誤って押して面談が切れないように。会話数は戻らない)
  const vConfirm = el("div", "vs-confirm"); vConfirm.id = "voice-confirm"; vConfirm.hidden = true;
  const vKeep = el("button", "keep", "面談を続ける"); vKeep.type = "button"; vKeep.id = "voice-keep";
  const vQuit = el("button", "quit", "終了して閉じる"); vQuit.type = "button"; vQuit.id = "voice-quit";
  const vBtns = el("div", "vs-confirm-btns"); vBtns.append(vKeep, vQuit);
  vConfirm.append(el("p", null, "通話中です。閉じると面談はここで終わり、使った会話数は戻りません。閉じますか?"), vBtns);
  panel.append(vHead, el("p", "vs-note", "アドバイザーと話しながら、希望と経験を5分ほどで伺います。氏名・住所・電話番号は聞きません。イヤホンの利用がおすすめです。"),
               vOrb, vStatus, vCaps, vConfirm, vEnd);
  sheet.appendChild(panel); document.body.appendChild(sheet);

  let voice = null;  // 開いている間の通話: { user, since, roomName, room, agent, me, working, ended, stalled, done, abort }
  // 結果が届く前にシートを閉じた面談の、次に待つ結果の番号(届いた結果は会話に入るので、次のターンの前に描き直す)
  let voicePendingSince = null;
  const END_WAIT_MS = 120000;  // 「面談を終える」からまとめが届くまでの待ちの上限(過ぎたら、閉じてよいと案内する)
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  function vSet(state, text) { sheet.dataset.state = state; vStatus.textContent = text; }
  // 音声ワーカー(LiveKit Agents)が参加者の属性 lk.agent.state で知らせる状態
  const AGENT_STATE = {
    initializing: ["connecting", "アドバイザーが準備しています…"], listening: ["listening", "お話しください(聞いています)"],
    thinking: ["thinking", "考えています…"], speaking: ["speaking", "アドバイザーが話しています"],
  };
  function vRefresh(v) {
    if (voice !== v || !v.room) return;
    let [state, text] = AGENT_STATE[v.agent] || ["connecting", "接続しました。まもなくアドバイザーが話しかけます。"];
    if (v.me) [state, text] = ["me", "あなたが話しています"];
    if (v.working) [state, text] = ["thinking", v.working];  // 求人検索・調べものの間
    if (v.ended) text = "面談の内容をまとめています。そのままお待ちください…";
    if (v.stalled) [state, text] = ["off", STALLED];
    if (v.done) text = "まとめをトークに送りました。";
    vSet(state, text);
  }
  function caption(id, isUser, text) {
    let c = vCaps.querySelector(`[data-seg="${CSS.escape(id)}"]`);
    if (!c) { c = el("div", "cap " + (isUser ? "user" : "agent")); c.dataset.seg = id; vCaps.appendChild(c); }
    c.textContent = (isUser ? "あなた: " : "アドバイザー: ") + text;
    vCaps.scrollTop = vCaps.scrollHeight;
  }
  let lkLoading = null;
  function loadLiveKit() {  // 音声を使うときだけ読み込む
    if (window.LivekitClient) return Promise.resolve();
    return lkLoading ||= new Promise((ok, ng) => {
      const s = document.createElement("script"); s.src = LIVEKIT_CLIENT; s.onload = ok;
      s.onerror = () => { lkLoading = null; s.remove(); ng(new Error("音声の部品を読み込めませんでした。通信を確かめて、もう一度お試しください。")); };
      document.head.appendChild(s);
    });
  }

  const STALLED = "まとめが届きませんでした。× で閉じてトークに戻れます(あとで届いたら、開き直したトークに出ます)。";

  async function openVoice() {
    const user = o.getUser();
    if (!user || o.isBusy() || voice) return;
    o.showTalk?.();
    const v = voice = { user, since: null, roomName: null, room: null, agent: null, me: false, working: null,
                        ended: false, stalled: false, done: false, abort: new AbortController() };
    let room = null;
    vCaps.replaceChildren(); vEnd.hidden = false; vEnd.disabled = true; vConfirm.hidden = true;
    sheet.hidden = false; vSet("connecting", "接続の準備をしています…");
    try {
      const res = await fetch(`/api/voice/${encodeURIComponent(user)}/start`, { method: "POST" });
      if (!res.ok) { let m = "音声相談を始められませんでした。"; try { m = (await res.json()).detail || m; } catch (_) {} throw new Error(m); }
      const t = await res.json();
      if (voice !== v) return;  // 準備の間に閉じた
      v.since = t.since; v.roomName = t.room; waitResult(v);
      await loadLiveKit();
      if (voice !== v) return;
      room = new LivekitClient.Room();
      room.on("trackSubscribed", (track) => { if (track.kind === "audio") { const a = track.attach(); a.hidden = true; sheet.appendChild(a); } });
      const agentState = (p) => { const s = p?.attributes?.["lk.agent.state"]; if (s && p !== room.localParticipant) { v.agent = s; vRefresh(v); } };
      room.on("participantConnected", agentState);
      room.on("participantAttributesChanged", (_changed, p) => agentState(p));
      room.on("activeSpeakersChanged", (speakers) => { v.me = speakers.includes(room.localParticipant); vRefresh(v); });
      room.on("dataReceived", (payload, _p, _k, topic) => {
        if (topic !== "voice-resume") return;
        try {
          const msg = JSON.parse(new TextDecoder().decode(payload));
          if (msg.type === "status") v.working = msg.done ? null : msg.label;
          if (msg.type === "draft") v.ended = true;  // 下書きができた: まもなく結果がトークに届く
          vRefresh(v);
        } catch (_) { /* 画面の補助なので無視 */ }
      });
      room.registerTextStreamHandler?.("lk.transcription", async (reader, participant) => {
        const isUser = participant.identity === room.localParticipant.identity;
        const id = reader.info.attributes?.["lk.segment_id"] || reader.info.id;
        let text = "";
        for await (const chunk of reader) { text += chunk; caption(id, isUser, text); }
      });
      room.on("disconnected", () => {
        if (voice !== v || v.done) return;
        v.room = null; vEnd.disabled = true;
        vSet("off", v.stalled ? STALLED : v.ended ? "面談の内容をまとめています。そのままお待ちください…" : "通話が切れました。閉じて、もう一度お試しください。");
      });
      vSet("mic", "マイクの利用を許可してください…");
      await room.connect(t.url, t.token);
      v.room = room;
      if (voice !== v) { room.disconnect(); return; }
      // エコー(スピーカーの音をマイクが拾う)対策: echoCancellation を明示的に有効にする
      await room.localParticipant.setMicrophoneEnabled(true, { echoCancellation: true, noiseSuppression: true, autoGainControl: true });
      vEnd.disabled = false; vRefresh(v);
    } catch (e) {
      room?.disconnect();  // マイクが使えないまま、アドバイザーだけが話し始めないように
      if (voice !== v) return;
      v.room = null;
      const denied = e?.name === "NotAllowedError";
      vEnd.hidden = true;
      vSet("error", denied ? "マイクを使えませんでした。ブラウザでマイクを許可してから、もう一度お試しください。" : e.message);
    }
  }
  // 「面談を終える」: ワーカーが下書きを保存して締める(結果はトークに届く)
  vEnd.onclick = async () => {
    const v = voice;
    if (!v?.room || v.ended) return;
    vEnd.disabled = true; v.ended = true; vRefresh(v);
    // まとめが届かない(会話が短すぎた・下書きに失敗した・送れなかった)ときに、待たせたままにしない
    setTimeout(() => { if (voice === v && !v.done) { v.stalled = true; vSet("off", STALLED); } }, END_WAIT_MS);
    try {
      await v.room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({ type: "end" })), { reliable: true, topic: "voice-resume" });
    } catch (e) { console.error(e); }
  };
  function closeVoice() {
    const v = voice;
    if (!v) return;
    voice = null; vConfirm.hidden = true;
    if (!v.done && v.since != null && voicePendingSince == null) voicePendingSince = v.since;
    v.abort.abort();  // 待っている問い合わせもやめる(閉じた後に届いた結果は、会話に入っているので再読み込みで出る)
    v.room?.disconnect();
    sheet.hidden = true; sheet.querySelectorAll("audio").forEach(a => a.remove());
    scroll();
  }
  // 通話中(つながっていて、まとめがまだ届いていない)なら、閉じる前に確かめる
  const inCall = (v) => !!(v && v.room && !v.done && !v.stalled);
  vClose.onclick = () => { if (inCall(voice)) { vConfirm.hidden = false; vKeep.focus(); } else closeVoice(); };
  vKeep.onclick = () => { vConfirm.hidden = true; };
  vQuit.onclick = () => closeVoice();

  // シートが開いている間だけ、この面談(部屋)の結果が届くのを待つ(サーバーが最長 25 秒待って返すのを繰り返す)
  async function waitResult(v) {
    while (voice === v) {
      let d;
      try {
        const q = `since=${v.since}&room=${encodeURIComponent(v.roomName)}`;
        const res = await fetch(`/api/voice/${encodeURIComponent(v.user)}/wait?${q}`, { method: "POST", signal: v.abort.signal });
        if (!res.ok) throw new Error(res.status + "");
        d = await res.json();
      } catch (e) {
        if (voice !== v) return;
        await sleep(3000); continue;  // 再起動中など。開いている間はまた待つ
      }
      if (voice !== v) return;
      v.since = d.next;
      if (d.results.length) { await finishVoice(v, d.results); return; }
    }
  }
  // 結果が届いた: 同じトークに AI の返信として描き、アドバイザーの締めの言葉を待って(最長10秒)シートを閉じる
  async function finishVoice(v, results) {
    for (const r of results) { disableChips(); renderReply(r, botCol()); }
    o.afterTurn?.();
    v.done = true; vEnd.disabled = true; vRefresh(v);
    for (let t = 0; t < 10000 && voice === v && v.room && ["thinking", "speaking"].includes(v.agent); t += 250) await sleep(250);
    if (voice === v) closeVoice();
  }

  // 閉じたシートの面談の結果が、まだ画面に出ていないかもしれない(出ていなければ次に待つ結果の番号、無ければ null)
  const voicePending = () => voicePendingSince;
  // 描き直した履歴に、その結果(voice_result がその番号以上の返信)が入っていたら待つのをやめる
  function voiceShown(turns) {
    if (voicePendingSince != null && turns.some(t => t.bot && t.bot.voice_result >= voicePendingSince)) voicePendingSince = null;
  }

  return { el, now, scroll, yen, sys, me, meFile, botCol, typingCol, disableChips, fail, setWait, onStep, readStream,
           jobCard, renderReply, resumeConfirm, pickResume, openVoice, closeVoice, setEnabled, voicePending, voiceShown };
}
