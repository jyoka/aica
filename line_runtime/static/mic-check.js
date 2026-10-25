// LIFF の中でマイクを試す画面(/liff/line/mic-check、task 205)。音声相談に要るマイク(getUserMedia)と WebRTC と
// LiveKit の部品が、この画面(LINE のアプリの LIFF ブラウザ、または外のブラウザ)で使えるかを確かめて、画面に出す。
// サーバーには結果(使えたか、エラーの名前、音の大きさ、LIFF の環境)だけを送る(POST /api/liff/mic-check)。
// 声は送らず、残さない: 音は手元で大きさを測るだけで、録らない。WebRTC は手元の候補を数えるだけで、どこにもつながない。
(() => {
  const conf = window.MIC_CHECK;
  const $ = (id) => document.getElementById(id);
  const start = $("mic-start"), status = $("mic-status"), meter = $("mic-level"), out = $("mic-result");
  const TRY_MS = 5000;  // 音の大きさを測る長さ
  const ICE_MS = 3000;  // 手元の ICE の候補を待つ長さ
  const WAIT_MS = 5000;  // liff.init と AudioContext.resume を待つ上限(止まったまま、結果を送れなくならないように)
  const AUDIO_STATES = ["running", "suspended", "closed", "interrupted"];  // interrupted は iOS の Safari だけ
  const VIEW_TYPES = ["compact", "tall", "full", "frame", "full-flex"];

  const errName = (e) => {
    // LIFF のエラーは code(INIT_FAILED など)、DOMException は name(code は古い番号なので使わない)
    const n = (typeof e?.code === "string" && e.code) || e?.name || "";
    return typeof n === "string" && /^[A-Za-z0-9_]{1,40}$/.test(n) ? n : "Error";
  };
  const version = (v) => (typeof v === "string" && /^[0-9A-Za-z._-]{1,32}$/.test(v) ? v : "");
  const call = (f) => { try { return f(); } catch (_) { return undefined; } };
  const within = (promise, ms) => Promise.race([promise.then(() => true), new Promise((r) => setTimeout(() => r(false), ms))]);

  // ---------- LIFF の環境 ----------
  const env = { os: "", in_client: null, line_version: "", liff_version: "", view_type: "", liff_error: "" };
  let initLate = false;  // liff.init が WAIT_MS より遅れて通った(押したときに Timeout にしたものを、後から消した)
  let last = null;  // 最後に出した結果。liff.init が後から終わっても、画面から消さない
  const ready = (async () => {
    if (!window.liff) { env.liff_error = "SdkNotLoaded"; return; }
    try {
      await liff.init({ liffId: conf.liffId });  // 外のブラウザでもログインは求めない(本人を決めない画面)
      if (env.liff_error === "Timeout") { env.liff_error = ""; initLate = true; }
    } catch (e) {
      env.liff_error = errName(e);
    }
    const os = call(() => liff.getOS());
    env.os = ["ios", "android", "web"].includes(os) ? os : "";
    const inClient = call(() => liff.isInClient());
    env.in_client = typeof inClient === "boolean" ? inClient : null;
    env.line_version = version(call(() => liff.getLineVersion()));
    env.liff_version = version(call(() => liff.getVersion()));
    const view = call(() => liff.getContext()?.viewType);
    env.view_type = VIEW_TYPES.includes(view) ? view : "";
    // liff.init が失敗したら openWindow も使えないので、ボタンを出さない(画面の URL を外のブラウザに貼って開く)
    if (env.in_client && !env.liff_error) $("mic-external-wrap").hidden = false;
  })().finally(() => show(last));

  $("mic-external").onclick = () => {
    // 外のブラウザ(iPhone は Safari、Android は Chrome など)で、同じ確認の画面を開く
    call(() => liff.openWindow({ url: location.origin + location.pathname, external: true }));
  };

  // ---------- マイク ----------
  async function tryMic(ctx) {
    if (!navigator.mediaDevices?.getUserMedia) return { result: { mic: "unsupported" }, stream: null };
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e) {
      const name = errName(e);
      return { result: { mic: name === "NotAllowedError" ? "denied" : "error", mic_error: name }, stream: null };
    }
    status.textContent = "マイクを使えました。5 秒ほど声を出してください…";
    return { result: { mic: "allowed", level: await peakLevel(stream, ctx) }, stream };
  }

  // 音の入力の大きさの最大(0 から 1)。手元で測るだけで、音そのものはどこにも送らない。
  // ctx は押したときに作った AudioContext(iOS は、押した操作の中で作らないと音が流れないことがある)
  async function peakLevel(stream, ctx) {
    if (!ctx) return 0;
    try {
      if (ctx.state !== "running" && ctx.resume) await within(ctx.resume(), WAIT_MS);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const buf = new Float32Array(analyser.fftSize);
      let peak = 0;
      await new Promise((resolve) => {
        const timer = setInterval(() => {
          analyser.getFloatTimeDomainData(buf);
          let now = 0;
          for (const v of buf) now = Math.max(now, Math.abs(v));
          now = Number.isFinite(now) ? Math.min(1, now) : 0;
          meter.value = now;
          peak = Math.max(peak, now);
        }, 50);
        setTimeout(() => { clearInterval(timer); resolve(); }, TRY_MS);
      });
      return Math.round(peak * 1000) / 1000;
    } catch (_) {
      return 0;
    } finally {
      meter.value = 0;
    }
  }

  // ---------- WebRTC(手元だけ。ICE のサーバーは使わず、どこにもつながない) ----------
  async function tryWebRTC(stream) {
    if (typeof window.RTCPeerConnection !== "function") return { webrtc: "unsupported" };
    let pc;
    try {
      pc = new RTCPeerConnection({ iceServers: [] });
      const tracks = stream ? stream.getAudioTracks() : [];
      if (tracks.length) for (const t of tracks) pc.addTrack(t, stream);
      else pc.addTransceiver("audio");
      let n = 0;
      const gathered = new Promise((resolve) => {
        pc.onicecandidate = (ev) => { if (ev.candidate) n++; else resolve(); };
        setTimeout(resolve, ICE_MS);
      });
      await pc.setLocalDescription(await pc.createOffer());
      await gathered;
      return { webrtc: "ok", candidates: Math.min(n, 99) };
    } catch (e) {
      return { webrtc: "error", webrtc_error: errName(e) };
    } finally {
      pc?.close();
    }
  }

  // ---------- LiveKit の部品(音声相談と同じもの)がこの画面に対応しているか ----------
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src; s.onload = resolve; s.onerror = reject;
      document.head.appendChild(s);
    });
  }
  async function tryLiveKit() {
    if (!window.LivekitClient) {
      try { await loadScript(conf.livekitClient); } catch (_) { return "not_loaded"; }
    }
    if (!window.LivekitClient?.isBrowserSupported) return "not_loaded";
    return call(() => window.LivekitClient.isBrowserSupported()) ? "supported" : "unsupported";
  }

  // ---------- 画面に出す ----------
  const MIC_TEXT = { allowed: "使えた", denied: "断られた", error: "エラー", unsupported: "この画面には無い" };
  const RTC_TEXT = { ok: "使える", error: "エラー", unsupported: "この画面には無い" };
  const LK_TEXT = { supported: "対応している", unsupported: "対応していない", not_loaded: "読み込めなかった" };
  const yesNo = (v) => (v == null ? "不明" : v ? "はい" : "いいえ");
  function show(r) {
    last = r;
    const lines = [];
    if (r) {
      lines.push(`マイク: ${MIC_TEXT[r.mic]}${r.mic_error ? `(${r.mic_error})` : ""}`);
      if (r.mic === "allowed") {
        lines.push(`音の大きさ(最大、0 から 1): ${r.level}${r.level < 0.01 ? "(音が届いていません)" : ""}`);
        lines.push(`音を測る部品(AudioContext): ${r.audio_state || "不明"}`);
      }
      lines.push(`通話の部品(WebRTC): ${RTC_TEXT[r.webrtc]}${r.webrtc_error ? `(${r.webrtc_error})` : ""}${r.webrtc === "ok" ? `(手元の候補 ${r.candidates})` : ""}`);
      lines.push(`LiveKit: ${LK_TEXT[r.livekit]}`);
    }
    lines.push(`OS: ${env.os || "不明"}`);
    lines.push(`LINE のアプリの中: ${yesNo(env.in_client)}`);
    lines.push(`LINE の版: ${env.line_version || "不明"}`);
    lines.push(`LIFF SDK の版: ${env.liff_version || "不明"}`);
    lines.push(`画面のサイズ: ${env.view_type || "不明"}`);
    if (env.liff_error) lines.push(`LIFF の初期化のエラー: ${env.liff_error}`);
    if (initLate) {
      lines.push(`LIFF の初期化: ${WAIT_MS / 1000} 秒より遅れて終わった`
                 + (r?.liff_error === "Timeout" ? "(サーバーの記録は、終わる前の値)" : ""));
    }
    lines.push(`https(安全な画面): ${yesNo(window.isSecureContext)}`);
    out.textContent = lines.join("\n");
  }

  const DONE_TEXT = {
    allowed: "マイクを使えました。",
    denied: "マイクを断られました。LINE か端末の設定でマイクを許可してから、もう一度お試しください。",
    error: "マイクを使えませんでした。",
    unsupported: "この画面ではマイクを使えません。",
  };
  start.onclick = async () => {
    start.disabled = true;
    // AudioContext は押した操作の中で(await の前に)作って始める
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = Ctx ? call(() => new Ctx()) : undefined;
    call(() => ctx?.resume?.().catch(() => {}));
    status.textContent = "マイクの利用を許可してください…";
    if (!(await within(ready, WAIT_MS)) && !env.liff_error) env.liff_error = "Timeout";
    const { result: mic, stream } = await tryMic(ctx);
    const audioState = AUDIO_STATES.includes(ctx?.state) ? ctx.state : "";
    call(() => ctx?.close?.().catch(() => {}));
    status.textContent = "通話の部品を確かめています…";
    const rtc = await tryWebRTC(stream);
    stream?.getTracks().forEach((t) => t.stop());
    const r = { ...mic, audio_state: audioState, ...rtc, livekit: await tryLiveKit(), ...env,
                secure_context: !!window.isSecureContext, user_agent: String(navigator.userAgent || "").slice(0, 300) };
    show(r);
    let sent = false;
    try {
      const res = await fetch("/api/liff/mic-check", { method: "POST", headers: { "Content-Type": "application/json" },
                                                       body: JSON.stringify(r) });
      sent = res.ok;
    } catch (_) { /* 送れなくても、画面の結果は見られる */ }
    status.textContent = DONE_TEXT[r.mic] + (sent ? "結果をサーバーに記録しました。" : "結果をサーバーに記録できませんでした(画面の結果をそのまま報告してください)。");
    status.dataset.done = r.mic;
    start.disabled = false;
  };
})();
