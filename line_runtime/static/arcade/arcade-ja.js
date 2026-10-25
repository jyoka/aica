/* レジュメ・アーケードの共通部品: 日本語の答えを書類の行にするための、端末の中の簡単な判定(task 187)。
   DOM を使わない。ブラウザでは window.ArcadeJa、node では require() で読む(tests/js/arcade-ja.test.js)。
   ゲームの中身(登場人物、台詞、分類の言葉)はゲームの側に置き、ここには置かない。
   元は各ゲームが別々に持っていた写し(the-case-of-you.logic.js がいちばん新しい)で、違いはここでそろえた。 */
(function (root) {
'use strict';

/* ---------- 文字をそろえる ---------- */

/* NFKC で全角の数字と英字を半角にする(「６１％」を「61%」と読む)。空白はまとめる。 */
function clean(s) { return String(s == null ? '' : s).normalize('NFKC').replace(/\s+/g, ' ').trim(); }
/* 画面に出す文はそのままの字形を残し、空白だけまとめる。 */
function squish(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }
function isJa(s) { return /[぀-ヿ㐀-鿿]/.test(String(s || '')); }
function wordCount(s) { return clean(s).split(' ').filter(Boolean).length; }
/* 答えの長さ。日本語は空白が無いので字数で数え、英語は語の数で数える。 */
function textSize(s) { const t = clean(s); if (!t) return 0; return isJa(t) ? t.replace(/\s/g, '').length : wordCount(t); }
/* 英語の語の数にそろえた長さ(日本語はおよそ 2.5 字で 1 語)。 */
function wordUnits(s) { const t = clean(s); if (!t) return 0; return isJa(t) ? Math.round(t.replace(/\s/g, '').length / 2.5) : wordCount(t); }

/* ---------- 数字の検出(日付は数えない) ---------- */

const UNIT_JA = '(?:%|パーセント|ポイント|割|倍|円|ドル|人|名|件|時間|分|秒|日|週間|か月|ヶ月|カ月|ヵ月|年|社|店舗|回|個|本|ページ|台|か国|カ国|ヶ国|言語)';
const UNIT_EN = '(?:percent\\b|x\\b|×|k\\b|m\\b|bn\\b|million|billion|thousand|ms\\b|s\\b|secs?\\b|seconds?|minutes?|mins?\\b|hours?|hrs?\\b|days?|weeks?|months?|years?|' +
  'users?|customers?|clients?|people|members|engineers|developers|teams?|projects?|tickets?|deals?|leads?|accounts|times|apps|downloads?|installs?|visits?|visitors?|views?|' +
  'sign-?ups?|points?|pts\\b|pt\\b|stars?|reviews?|orders?|bookings?|requests?|countries|languages|issues|bugs?|pages?|stores?|schools?|students?|patients?|calls?|emails?|' +
  'items?|units?|usd|eur|gbp|jpy|yen|dollars?|euros?)';
const MAG = '(?:万|億|千|百)?';
const NUM = '\\d[\\d,.]*';
const METRIC_RE = new RegExp('(?:[$¥€£]\\s?' + NUM + '\\s?(?:k|m|bn|万|億)?' + '|' + NUM + '\\s?' + MAG + UNIT_JA + '|' + NUM + '\\s?' + UNIT_EN + ')', 'gi');
const KANJI_RE = /(?:[一二三四五六七八九十]+|半)(?:割|倍)|[0-9一二三四五六七八九十]+分の[0-9一二三四五六七八九十]+|半分|半減|倍増|\b(?:doubled|tripled|halved|quadrupled)\b/i;
const NUMU = '(?:[$¥]\\s?)?' + NUM + '\\s?' + MAG + '(?:' + UNIT_JA + '|ms\\b|s\\b|k\\b|m\\b|x\\b)?';
const RANGE_RE = new RegExp('(?:from\\s+)?' + NUMU + '\\s*(?:から|→|->|⇒|〜|~|to)\\s*(?:約|およそ|about\\s+)?\\s*' + NUMU, 'i');
const RANK_RE = /\b(?:top|rank(?:ed)?|no\.)\s?\d+|#\d+|\d+\s?位/i;

/* 4 桁の年(2022年、2020s)と、暦の日付(3月、5日、9時)は成果の数字ではない。after は数字のすぐ後ろの文字。 */
function isDateish(m, after) {
  return /^(19|20)\d\d\s?(年|s)?$/.test(m) || /^\d{1,2}\s?(?:月|日|時)$/.test(m) || (/^\d+$/.test(m) && /^[月日時]/.test(after || ''));
}

/* 答えの中の成果の数字を 1 つ返す(無ければ null)。範囲(61%から68%)、単位つきの数、漢数字(3割、半分)、順位の順に探す。
   bareNumbers が true(既定)なら、単位の無い 2 以上の数も数字と数える。 */
function findMetric(text, opts) {
  const bare = !opts || opts.bareNumbers !== false;
  const t = clean(text);
  const rg = t.match(RANGE_RE);
  if (rg && /\d.*\d/.test(rg[0]) && !/^(19|20)\d\d/.test(rg[0])) return rg[0].replace(/^from\s+/i, '').trim();
  for (const m of t.matchAll(METRIC_RE)) {
    const s = m[0].trim();
    if (!isDateish(s, t.slice(m.index + m[0].length))) return s;
  }
  const k = t.match(KANJI_RE);
  if (k) return k[0];
  const r = t.match(RANK_RE);
  if (r) return r[0];
  if (bare) {
    for (const m of t.matchAll(/\d[\d,.]*/g)) {
      const n = m[0];
      if (isDateish(n, t.slice(m.index + n.length))) continue;
      if (parseFloat(n.replace(/,/g, '')) >= 2) return n;
    }
  }
  return null;
}
function hasMetric(text, opts) { return !!findMetric(text, opts); }

/* ---------- あいまいさ(字数で見る) ---------- */

/* あいまいな答えか。minChars は日本語の字数、minWords は英語の語の数の下限。
   metric: 'required' は数字が無ければあいまい、'enough' は短くても数字があればよい、'ignore' は長さだけで見る。 */
function isVague(text, opts) {
  const o = opts || {};
  const t = clean(text);
  if (!t) return true;
  const short = textSize(t) < (isJa(t) ? (o.minChars || 0) : (o.minWords || 0));
  const mode = o.metric || 'required';
  if (mode === 'ignore') return short;
  const num = hasMetric(t);
  if (mode === 'enough') return short && !num;
  return !num;
}

/* ---------- 文を分ける、縮める ---------- */

/* 「。！？」と英語の「. 」で文に分ける。nfkc: false なら字形をそのまま残す(画面に出す文)。 */
function sentences(text, opts) {
  const t = opts && opts.nfkc === false ? squish(text) : clean(text);
  return t.split(/(?<=[。！？!?])\s*|(?<=\.)\s+/).map(s => s.trim()).filter(Boolean);
}
/* n 字までに縮め、なるべく「、」「。」か空白で切る。切ったら「…」を付ける。 */
function trimTo(s, n, opts) {
  s = opts && opts.nfkc === false ? squish(s) : clean(s);
  if (s.length <= n) return s;
  const cut = s.slice(0, n);
  const at = Math.max(cut.lastIndexOf(' '), cut.lastIndexOf('、'), cut.lastIndexOf('。'), cut.lastIndexOf(','));
  return cut.slice(0, at >= n * 0.6 ? at : n).replace(/[、。,;:\s-]+$/, '') + '…';
}

/* ---------- です・ます調を言い切りにする ---------- */

/* 書類の行の語尾: 「刷新しました」は「刷新」、「上がりました」は「上がった」、「作り直しました」は「作り直した」。 */
function plainEnding(p) {
  return String(p)
    .replace(/ませんでした$/, 'なかった').replace(/ません$/, 'ない')
    .replace(/ていました$/, 'ていた').replace(/ています$/, 'ている')
    .replace(/でした$/, 'だった').replace(/です$/, '')
    .replace(/([一-鿿゠-ヿ]{2,})(?:を)?(?:しました|しています|していました|します|した|している|していた)$/, '$1')
    .replace(/しました$/, 'した').replace(/します$/, 'する')
    .replace(/(でき|起き|生き|見|過ぎ|伸び|延び|落ち|浴び)ました$/, '$1た')
    .replace(/([えけげせぜてでねべぺめれへ])ました$/, '$1た')
    .replace(/([あかさたなはまやらわ])いました$/, '$1った')
    .replace(/(人|名)いました$/, '$1いた').replace(/([^一-鿿])いました$/, '$1いた')
    .replace(/(行|い)きました$/, '$1った')
    .replace(/[りちい]ました$/, 'った').replace(/[みびに]ました$/, 'んだ')
    .replace(/きました$/, 'いた').replace(/ぎました$/, 'いだ')
    .replace(/ました$/, 'た');
}
/* 文頭の「えっと」などのつなぎ、「そこで」などの接続、「私は」を外す(英語の so / I / we も)。 */
function stripLead(s) {
  return squish(s)
    .replace(/^(?:えっと|ええと|まあ|うーん|そうですね|正直|実は|あと)[、,\s]*/, '')
    .replace(/^(?:そこで|それで|なので|だから|その結果|結果として|結果的に|また|さらに)[、,\s]*/, '')
    .replace(/^(?:私|わたし|僕|ぼく|俺|自分|我々|私たち)(?:は|が|も|たちは)[、,\s]*/, '')
    .replace(/^(so|well|basically|and|then|honestly),?\s+/i, '').replace(/^(i|we)\s+(personally\s+|then\s+|also\s+|basically\s+)?/i, '');
}
/* 答えの 1 文を、書類の行の言い方(である調・体言止め)にする。 */
function plainLine(s) { return plainEnding(stripLead(s).replace(/[。！？!?.]+$/, '')); }

/* ---------- 数字の空欄 ---------- */

const PLACEHOLDER = '[+X% ← 数字を入れる]';
const PLACEHOLDER_YEN = '[X円 ← 数字を入れる]';
const PLACEHOLDER_COUNT = '[N件 ← 数字を入れる]';
const PLACEHOLDER_RE = /\[[^\]]*(数字を入れる|add your number)[^\]]*\]/;
/* 数字の無い行の終わりに空欄を付ける(数字があればそのまま)。 */
function withPlaceholder(line, placeholder) { return hasMetric(line) ? line : line + ' ' + (placeholder || PLACEHOLDER); }

/* ---------- 職務経歴書の Markdown ---------- */

const NAME_PLACEHOLDER = '[氏名]';
const CONTACT_PLACEHOLDER = '[メール] · [電話] · [居住地] · [リンク]';

/* r = { headline, summary, selfPr, experience: [{ company, title, dates, bullets }], skills: [], education }。
   氏名と連絡先は聞かないので、いつも空欄で出す(本人が後で書く)。自己PR(履歴書のとき)は、あるときだけ出す。 */
function resumeMarkdown(r) {
  const L = ['# ' + NAME_PLACEHOLDER];
  if (r.headline) L.push('**' + r.headline + '**');
  L.push(CONTACT_PLACEHOLDER, '', '## 職務要約', r.summary || '', '');
  if (r.selfPr) L.push('## 自己PR', r.selfPr, '');
  L.push('## 職務経歴');
  for (const x of r.experience || []) {
    L.push('### ' + (x.company || '[会社名]') + '｜' + (x.title || '[役職]'));
    if (x.dates) L.push('*' + x.dates + '*');
    for (const b of x.bullets || []) L.push('- ' + b);
    L.push('');
  }
  if (r.skills && r.skills.length) L.push('## スキル', r.skills.join(' · '), '');
  if (r.education) L.push('## 学歴', r.education, '');
  return L.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n';
}

/* ---------- 自己PR(履歴書のとき) ---------- */

const SELF_PR_PLACEHOLDER = '[ここに自己PRを3〜4文で書く]';
/* AI を使わない自己PR: 本人の書類の行(lines)を 2 つまで引いた、です・ます調の 2〜3 文。強み(strengths)は画面に出した言葉だけ。
   行に無いことは足さない(数字の空欄も行のまま残す)。行が無ければ、見える空欄を 1 つ返す。 */
function selfPrOffline(o) {
  const lines = ((o && o.lines) || []).map(x => squish(x).replace(/[。.]+$/, '')).filter(x => x && !/^\[ここに/.test(x)).slice(0, 2);
  if (!lines.length) return SELF_PR_PLACEHOLDER;
  const S = ['これまでの仕事では、' + lines.map(x => '「' + x + '」').join('や') + 'に取り組んできました。'];
  const st = ((o && o.strengths) || []).map(squish).filter(Boolean).slice(0, 2);
  if (st.length) S.push('特に' + st.join('と') + 'を強みにしています。');
  S.push('この経験を活かし、新しい職場でも任された仕事に責任をもって取り組みます。');
  return S.join('');
}
/* 書類の Markdown の「## 自己PR」の節を、書類の種類に合わせる: selfPr があれば職務要約の後に入れ(本人が書いた節があれば
   そのまま)、selfPr が空なら節を外す。本人が画面で直した Markdown にも使う。 */
function withSelfPr(md, selfPr) {
  const text = String(md || '');
  const has = /^## 自己PR\s*$/m.test(text);
  if (!selfPr) return has ? text.replace(/^## 自己PR\s*\n[\s\S]*?(?=^## |(?![\s\S]))/m, '') : text;
  if (has) return text;
  const at = text.search(/^## 職務経歴\s*$/m);
  const block = '## 自己PR\n' + selfPr + '\n\n';
  return at < 0 ? text.replace(/\s*$/, '\n\n') + block.trim() + '\n' : text.slice(0, at) + block + text.slice(at);
}

const API = {
  clean, squish, isJa, wordCount, textSize, wordUnits,
  isDateish, findMetric, hasMetric, isVague,
  sentences, trimTo, plainEnding, stripLead, plainLine,
  PLACEHOLDER, PLACEHOLDER_YEN, PLACEHOLDER_COUNT, PLACEHOLDER_RE, withPlaceholder,
  NAME_PLACEHOLDER, CONTACT_PLACEHOLDER, resumeMarkdown,
  SELF_PR_PLACEHOLDER, selfPrOffline, withSelfPr,
};
root.ArcadeJa = API;
if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : globalThis);
