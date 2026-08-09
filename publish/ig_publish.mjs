#!/usr/bin/env node
/**
 * 오늘 자 카드뉴스를 인스타그램에 게시한다. (GitHub Actions 용, 의존성 없음)
 *
 * 이 파일은 heart-connect-docs 의 `marketing/content_engine/deploy/ig_publish.mjs`
 * 사본이다. **여기서 고치지 말고 원본을 고친 뒤 deploy_schedule.mjs 로 배포**할 것.
 *
 * 환경변수: IG_ACCESS_TOKEN, IG_USER_ID
 */
import { readFileSync, writeFileSync } from 'node:fs';

const GRAPH = 'https://graph.facebook.com/v21.0';
const SCHEDULE = new URL('./schedule.json', import.meta.url).pathname;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const token = process.env.IG_ACCESS_TOKEN;
const igUser = process.env.IG_USER_ID;
if (!token || !igUser) throw new Error('IG_ACCESS_TOKEN·IG_USER_ID 가 없습니다');

const todayKST = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
}).format(new Date());

async function graph(path, params, method = 'GET') {
  const body = new URLSearchParams(params);
  const res = method === 'GET'
    ? await fetch(`${GRAPH}/${path}?${body}`)
    : await fetch(`${GRAPH}/${path}`, { method, body });
  const json = await res.json();
  if (json.error) throw new Error(`[${json.error.code}] ${json.error.message}`);
  return json;
}

async function waitReady(id, label) {
  for (let i = 0; i < 30; i++) {
    const { status_code, status } = await graph(id, { fields: 'status_code,status', access_token: token });
    if (status_code === 'FINISHED') return;
    if (status_code === 'ERROR' || status_code === 'EXPIRED') {
      throw new Error(`${label} 컨테이너 ${status_code} — ${status || ''}`);
    }
    await sleep(2000);
  }
  throw new Error(`${label} 컨테이너 준비 시간 초과`);
}

const sched = JSON.parse(readFileSync(SCHEDULE, 'utf8'));
const entry = sched.posts.find((p) => p.date === todayKST);

if (!entry) { console.log(`오늘(${todayKST}) 자 일정 없음 — 종료`); process.exit(0); }
if (entry.posted_at) { console.log(`${todayKST} 이미 게시함 (${entry.posted_at}) — 건너뜀`); process.exit(0); }

const base = sched.base_url.replace(/\/$/, '');
const urls = Array.from({ length: entry.cards }, (_, i) =>
  `${base}/${entry.slug}/4x5/${String(i + 1).padStart(2, '0')}.png`);

console.log(`📅 ${todayKST}(${entry.weekday}) · ${entry.slug} · 카드 ${urls.length}장`);

// 이미지가 실제로 열리는지 먼저 확인 — Meta 서버가 직접 받아간다
for (const u of urls) {
  const r = await fetch(u, { method: 'HEAD' });
  if (!r.ok) throw new Error(`이미지 접근 불가 (${r.status}): ${u}`);
}
console.log('✅ 이미지 확인');

const children = [];
for (let i = 0; i < urls.length; i++) {
  const { id } = await graph(`${igUser}/media`, {
    image_url: urls[i], is_carousel_item: 'true', access_token: token,
  }, 'POST');
  await waitReady(id, `${i + 1}번`);
  children.push(id);
}
console.log(`✅ 자식 컨테이너 ${children.length}개`);

const parent = await graph(`${igUser}/media`, {
  media_type: 'CAROUSEL', children: children.join(','), caption: entry.caption, access_token: token,
}, 'POST');
await waitReady(parent.id, '캐러셀');

const published = await graph(`${igUser}/media_publish`, {
  creation_id: parent.id, access_token: token,
}, 'POST');

const info = await graph(published.id, { fields: 'permalink', access_token: token }).catch(() => ({}));

entry.posted_at = new Date().toLocaleString('sv-SE', { timeZone: 'Asia/Seoul' }).replace(' ', 'T');
entry.media_id = published.id;
if (info.permalink) entry.permalink = info.permalink;
writeFileSync(SCHEDULE, JSON.stringify(sched, null, 2) + '\n', 'utf8');

console.log(`🚀 게시 완료 — ${info.permalink || published.id}`);
