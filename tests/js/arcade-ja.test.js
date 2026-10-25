// レジュメ・アーケードの共通部品(line_runtime/static/arcade/arcade-ja.js)のテスト。
// node の標準のテスト(node:test)だけを使い、何もインストールしない。pytest からも動く(tests/test_arcade.py)。
//   node --test tests/js/arcade-ja.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const J = require(path.join(__dirname, '..', '..', 'line_runtime', 'static', 'arcade', 'arcade-ja.js'));

test('範囲の数字を 1 つとして見つける', () => {
  assert.equal(J.findMetric('最初の四半期で、決済完了率が61%から68%に上がりました。'), '61%から68%');
  assert.equal(J.findMetric('ページの表示時間を4.2秒から1.6秒に縮めました。'), '4.2秒から1.6秒');
  assert.equal(J.findMetric('問い合わせが月300件から200件ほどに減りました。'), '300件から200件');
});

test('全角の数字も NFKC で読む', () => {
  assert.equal(J.findMetric('離脱が６１％から４０％に減った'), '61%から40%');
  assert.equal(J.clean('ＡＢテスト　２回'), 'ABテスト 2回');
});

test('単位つきの数と漢数字の割合を見つける', () => {
  assert.equal(J.findMetric('若手2人と毎週ペアプロをしました'), '2人');
  assert.equal(J.findMetric('問い合わせが3割減った'), '3割');
  assert.equal(J.findMetric('作業時間が半分になった'), '半分');
  assert.equal(J.findMetric('売上ランキングで3位に入った'), '3位');
});

test('年と日付は数字と数えない', () => {
  assert.equal(J.findMetric('2022年に入社しました。'), null);
  assert.equal(J.findMetric('3月に全顧客へ公開しています。'), null);
  assert.equal(J.findMetric('2022年3月5日に公開した'), null);
  assert.equal(J.hasMetric('2019年から2022年まで在籍'), false);
  // 日付の横に本当の数字があれば、そちらを取る
  assert.equal(J.findMetric('2022年に、問い合わせを40%減らした'), '40%');
});

test('単位の無い数は、bareNumbers を切ると数えない', () => {
  assert.equal(J.findMetric('チームは5で回していた'), '5');
  assert.equal(J.findMetric('チームは5で回していた', { bareNumbers: false }), null);
});

test('です・ます調を言い切りにする', () => {
  assert.equal(J.plainEnding('作り直しました'), '作り直した');
  assert.equal(J.plainEnding('決済画面を刷新しました'), '決済画面を刷新');
  assert.equal(J.plainEnding('決済完了率が上がりました'), '決済完了率が上がった');
  assert.equal(J.plainEnding('自分で機能をリリースできるようになりました'), '自分で機能をリリースできるようになった');
  assert.equal(J.plainEnding('お客さまから返事をもらいました'), 'お客さまから返事をもらった');
  assert.equal(J.plainEnding('期限までに終えられませんでした'), '期限までに終えられなかった');
  assert.equal(J.plainEnding('担当でした'), '担当だった');
});

test('書類の行は、つなぎと「私は」を外して言い切りにする', () => {
  assert.equal(J.plainLine('そこで、私はStorybookで共通コンポーネントを作り直しました。'), 'Storybookで共通コンポーネントを作り直した');
  assert.equal(J.plainLine('えっと、テストを直しました！'), 'テストを直した');
});

test('あいまいな短い答えに印を付け、具体的な答えには付けない', () => {
  // 数字が要る問い(上司・依頼人など): 数字が無ければあいまい
  assert.equal(J.isVague('いろいろ頑張りました'), true);
  assert.equal(J.isVague('決済完了率が61%から68%に上がりました'), false);
  // 短くても数字があればよい問い(同僚など): 字数で見る
  const coworker = { minChars: 40, minWords: 14, metric: 'enough' };
  assert.equal(J.isVague('不安定なテストを直しました。', coworker), true);
  assert.equal(J.isVague('デザインシステムがばらばらだったので、Storybookで共通コンポーネントのライブラリを作り、ドキュメントも書きました。', coworker), false);
  assert.equal(J.isVague('テストを2件直した', coworker), false);
  // 長さだけで見る問い(弱みなど)
  const rival = { minChars: 18, minWords: 7, metric: 'ignore' };
  assert.equal(J.isVague('人前で話すこと', rival), true);
  assert.equal(J.isVague('人前で話すことです。社内のLT会に申し込んで練習しています。', rival), false);
  assert.equal(J.isVague('', rival), true);
});

test('字数と語の数', () => {
  assert.equal(J.textSize('不安定なテストを直した'), 11);
  assert.equal(J.textSize('fixed the flaky tests'), 4);
  assert.equal(J.wordUnits('不安定なテストを直しました'), 5);
});

test('文を「。」で分け、なるべく区切りで縮める', () => {
  assert.deepEqual(J.sentences('決済画面を作り直しました。3月に公開しています。'), ['決済画面を作り直しました。', '3月に公開しています。']);
  assert.deepEqual(J.sentences('Fixed it. Then shipped！'), ['Fixed it.', 'Then shipped!']);
  assert.deepEqual(J.sentences('全角の！を残す！', { nfkc: false }), ['全角の！', 'を残す！']);
  assert.equal(J.trimTo('短い文', 10), '短い文');
  assert.equal(J.trimTo('決済画面を作り直し、テストで守り、3月に全顧客へ公開した', 20), '決済画面を作り直し、テストで守り…');
});

test('数字の無い行には見える空欄を付ける', () => {
  assert.equal(J.PLACEHOLDER, '[+X% ← 数字を入れる]');
  assert.equal(J.withPlaceholder('不安定なテストを直した'), '不安定なテストを直した [+X% ← 数字を入れる]');
  assert.equal(J.withPlaceholder('問い合わせを40%減らした'), '問い合わせを40%減らした');
  assert.equal(J.withPlaceholder('来館者の対応', J.PLACEHOLDER_COUNT), '来館者の対応 [N件 ← 数字を入れる]');
  assert.ok(J.PLACEHOLDER_RE.test('売上を伸ばした [X円 ← 数字を入れる]'));
  assert.ok(!J.PLACEHOLDER_RE.test('売上を[2倍]にした'));
});

test('職務経歴書の Markdown は氏名と連絡先を空欄で出す', () => {
  const md = J.resumeMarkdown({
    headline: 'フロントエンドエンジニア',
    summary: 'フロントエンドエンジニアとして6年の経験。',
    experience: [
      { company: 'ペーパームーン・ペイメンツ', title: 'シニアフロントエンドエンジニア', dates: '2022年 - 現在', bullets: ['決済完了率を61%から68%に改善'] },
      { company: 'ランタンフィッシュ・ラボ', title: '', dates: '', bullets: ['不安定なテストを直した [+X% ← 数字を入れる]'] },
    ],
    skills: ['React', 'TypeScript'],
    education: '',
  });
  assert.equal(md, [
    '# [氏名]',
    '**フロントエンドエンジニア**',
    '[メール] · [電話] · [居住地] · [リンク]',
    '',
    '## 職務要約',
    'フロントエンドエンジニアとして6年の経験。',
    '',
    '## 職務経歴',
    '### ペーパームーン・ペイメンツ｜シニアフロントエンドエンジニア',
    '*2022年 - 現在*',
    '- 決済完了率を61%から68%に改善',
    '',
    '### ランタンフィッシュ・ラボ｜[役職]',
    '- 不安定なテストを直した [+X% ← 数字を入れる]',
    '',
    '## スキル',
    'React · TypeScript',
    '',
  ].join('\n'));
});
