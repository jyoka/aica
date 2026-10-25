/* レジュメ・アーケードの共通部品: ゲームの AI と保存を、aica の Web サーバーの API で呼ぶ(task 188、docs/prd/resume-arcade.md)。
   claude.ai の window.claude.use("sample") の代わり。画面は指示の文を送らない。送るのはゲームの名前、段、本人の答え
   (まとめの段では書類の行)だけで、指示の文と出力の形はサーバーが持つ。
   - start(): 遊びの番号をもらう(POST /api/arcade/start)。番号はゲームの進み具合と一緒に localStorage に残し、再読み込みでは
     呼び直さない(呼ぶのは、番号が無いときと、本人が最初から遊ぶときだけ)。Cookie が無い(トークを開いていない)ときは AI なし
   - call(play, stage, data): 段を 1 回進める(POST /api/arcade/ai)。だめなら code つきの Error を投げ、ゲームはその段を
     簡易分析で続ける。code は not_granted(この遊びでは AI を使えない)/ rate_limited(回数の上限)/ cancelled / error
   - save(play, markdown, documentKind): 書類を下書きとして保存する(POST /api/arcade/save)
   流れの途中の文字は送らない(結果をまとめて返す)。待っている間は、ゲームがいつもの「考えている」表示を出す。
   LINE のアプリで開いたゲーム(/liff/line/arcade/…。サーバーが window.AICA_LIFF と LIFF SDK を入れる)は、同じ中身の
   /api/liff/arcade/… を LIFF の本人(Cookie aica_liff)として呼ぶ(task 219)。保存では LIFF の ID トークンも送る(サーバーが
   確かめて、その本人の LINE のトークに知らせを送る)。本人の Cookie の期限(1 時間)が切れていたら、ID トークンで 1 回だけ確かめ直す。 */
(function (root) {
'use strict';

const PERMANENT = new Set(['no_play', 'ai_off', 'expired', 'disabled', 'no_key', 'turn_limit', 'daily_limit']);
const LIMITS = new Set(['reaction_limit', 'summary_limit']);
const TIMEOUT = { reaction: 45000, summary: 150000 };

const LIFF = root.AICA_LIFF || null;
const API = LIFF ? '/api/liff/arcade/' : '/api/arcade/';
let liffReady = null;

/* LIFF の ID トークン(取れなければ空)。liff.init は 1 回だけ呼ぶ。 */
async function idToken() {
  if (!LIFF || !root.liff) return '';
  try {
    liffReady = liffReady || root.liff.init({ liffId: LIFF.liffId });
    await liffReady;
    return root.liff.getIDToken() || '';
  } catch (e) {
    return '';
  }
}

/* LIFF の本人を確かめ直す(POST /api/liff/session)。確かめられたら true。 */
async function renew() {
  const token = await idToken();
  if (!token) return false;
  try {
    const r = await fetch('/api/liff/session', { method: 'POST', credentials: 'same-origin',
      headers: { 'content-type': 'application/json' }, body: JSON.stringify({ id_token: token }) });
    return r.ok;
  } catch (e) {
    return false;
  }
}

function scenarioUser() {
  try { return new URLSearchParams(root.location.search).get('scenario_user') || null; } catch (e) { return null; }
}

function fail(code, message, extra) { const e = new Error(message || code); e.code = code; return Object.assign(e, extra || {}); }

async function post(path, body, signal, retried) {
  let r;
  try {
    r = await fetch(path, { method: 'POST', credentials: 'same-origin', signal,
      headers: { 'content-type': 'application/json' }, body: JSON.stringify(Object.assign({ scenario_user: scenarioUser() }, body)) });
  } catch (e) {
    throw fail(e && e.name === 'AbortError' ? 'cancelled' : 'error', 'ネットワークにつながりません');
  }
  let j = null;
  try { j = await r.json(); } catch (e) { /* JSON でない応答 */ }
  if (r.status === 401 && LIFF && !retried && await renew()) return post(path, body, signal, true);
  if (!r.ok) {
    const code = r.status === 401 || r.status === 403 ? 'not_granted' : r.status === 429 ? 'rate_limited' : 'error';
    throw fail(code, (j && typeof j.detail === 'string') ? j.detail : '保存できませんでした（' + r.status + '）', { status: r.status });
  }
  return j;
}

function ArcadeAI(game) {
  return {
    game,
    /* 遊びの番号: { id, ai, reason, documentKind, track, jobs }。番号をもらえなければ id は null(AI なし、保存もできない)。 */
    async start() {
      try {
        const r = await post(API + 'start', { game });
        return { id: r.play, ai: !!r.ai, reason: r.reason || null, documentKind: r.document_kind || null, track: r.track || null,
          jobs: Array.isArray(r.jobs) ? r.jobs : [] };
      } catch (e) {
        return { id: null, ai: false, reason: e.status === 401 ? 'no_user' : 'error', documentKind: null, track: null, jobs: [] };
      }
    },
    /* 段を 1 回進めて、結果(サーバーで事実を確かめたもの)を返す。kind は待つ長さ(reaction / summary)。 */
    async call(play, stage, data, opts) {
      const o = opts || {};
      if (!play || !play.id || !play.ai) throw fail('not_granted');
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), TIMEOUT[o.kind] || TIMEOUT.reaction);
      const onAbort = () => ctl.abort();
      if (o.signal) { if (o.signal.aborted) ctl.abort(); else o.signal.addEventListener('abort', onAbort, { once: true }); }
      try {
        const r = await post(API + 'ai', { game, play: play.id, stage, data }, ctl.signal);
        if (r && r.ok) return r.result;
        const reason = (r && r.reason) || 'error';
        throw fail(PERMANENT.has(reason) ? 'not_granted' : LIMITS.has(reason) ? 'rate_limited' : 'error', reason, { reason });
      } catch (e) {
        if (e.code === 'cancelled' && o.signal && o.signal.aborted) throw e;
        if (e.code === 'cancelled') throw fail('error', 'timeout', { reason: 'timeout' });
        throw e;
      } finally {
        clearTimeout(timer);
        if (o.signal) o.signal.removeEventListener('abort', onAbort);
      }
    },
    /* 書類を下書きとして保存する: { draft, view_url, document_kind, blanks, talk }(LINE のアプリでは line も)。
       だめなら Error(message は画面に出せる文)。 */
    async save(play, markdown, documentKind) {
      if (!play || !play.id) throw fail('not_granted', 'この遊びの書類は保存できません。「Markdownでコピー」で書き写してください');
      const body = { game, play: play.id, markdown, document_kind: documentKind || null };
      return post(API + 'save', LIFF ? Object.assign(body, { id_token: await idToken() }) : body);
    },
  };
}

/* 送る前に n 字までに切る(サーバーも同じ上限で確かめる)。 */
ArcadeAI.cut = (s, n) => String(s == null ? '' : s).slice(0, n);
ArcadeAI.LIMITS = { answer: 600, line: 120, quote: 40, linesPerJob: 6, summary: 4000 };

root.ArcadeAI = ArcadeAI;
})(typeof window !== 'undefined' ? window : globalThis);
