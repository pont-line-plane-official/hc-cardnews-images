# -*- coding: utf-8 -*-
"""댓글 키워드 → 자동 DM.

    python3 src/dm_bot.py --check                 권한·연결 점검 (제일 먼저)
    python3 src/dm_bot.py --media                 최근 게시물 ID 조회 (triggers.json 채우기용)
    python3 src/dm_bot.py --test-send <IGSID>     테스트 발송 1건
    python3 src/dm_bot.py --run                   폴링 1회 (실제 발송)
    python3 src/dm_bot.py --run --dry             발송 안 하고 뭘 보낼지만 출력

🔴 웹훅 서버가 없다. **댓글을 폴링**해서 처리한다 — 예약 발행에 쓰는
   GitHub Actions 에 얹으면 새 인프라가 필요 없다.

🔴 2단계 발송이 기본이다.
   1통 = 링크 없음 · 답장 요청  →  답장이 오면  →  2통 = 링크
   첫 자동 메시지에 링크를 넣는 패턴이 스팸 판정을 부른다(2026 실측 기준).

⚠️ 토큰은 `~/.hc-marketing-env` 의 `IG_ACCESS_TOKEN` 에서만 읽는다. 코드에 적지 않는다.
"""
import argparse, hashlib, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Actions(공개 레포)에서는 dm/ 아래 평평하게 놓인다. 로컬에서는 content_engine 구조.
# 🔴 `dm/` 은 GitHub Pages 가 공개로 서빙하는 폴더다. 봇 파일을 거기 두면
#    state.json(=요청자 인스타 ID)이 웹에 그대로 열린다. 반드시 `dmbot/` 에 둔다.
if os.path.exists(os.path.join(ROOT, 'dmbot', 'triggers.json')):
    CFG = os.path.join(ROOT, 'dmbot', 'triggers.json')
    STATE = os.path.join(ROOT, 'dmbot', 'state.json')
else:
    CFG = os.path.join(ROOT, 'content', 'dm', 'triggers.json')
    STATE = os.path.join(ROOT, 'out', 'dm', 'state.json')
API = 'https://graph.facebook.com/v21.0'
KST = timezone(timedelta(hours=9))

NEED = ['instagram_basic', 'instagram_manage_comments',
        'instagram_manage_messages', 'pages_messaging']


# ── 기본 ────────────────────────────────────────────────────
def env_get(key):
    """환경변수 우선. GitHub Actions 에는 ~/.hc-marketing-env 가 없고 Secrets 로 들어온다."""
    if os.environ.get(key):
        return os.environ[key].strip()
    p = os.path.expanduser('~/.hc-marketing-env')
    if not os.path.exists(p):
        return None
    for ln in open(p, encoding='utf-8'):
        if ln.startswith(key + '='):
            return ln.split('=', 1)[1].strip()
    return None


TOKEN = env_get('IG_ACCESS_TOKEN')      # 시스템 사용자 토큰
IG_USER_ID = env_get('IG_USER_ID')
PAGE_ID = env_get('IG_PAGE_ID')
_PAGE_TOKEN = None


def page_token():
    """🔴 메시징은 **페이지 토큰 + 페이지 엔드포인트**로만 된다 (2026-08-21 실측).
    시스템 사용자 토큰으로 `/{ig-user-id}/conversations` 를 부르면 `(#3)` 이 난다."""
    global _PAGE_TOKEN
    if _PAGE_TOKEN is None:
        _PAGE_TOKEN = api('%s' % PAGE_ID, {'fields': 'access_token'})['access_token']
    return _PAGE_TOKEN


def papi(path, params=None, post=None):
    """페이지 토큰으로 호출."""
    global TOKEN
    old, TOKEN = TOKEN, page_token()
    try:
        return api(path, params, post)
    finally:
        TOKEN = old


def api(path, params=None, post=None):
    params = dict(params or {})
    params['access_token'] = TOKEN
    url = '%s/%s?%s' % (API, path.lstrip('/'), urllib.parse.urlencode(params))
    data = json.dumps(post).encode() if post is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={'Content-Type': 'application/json'} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')
        try:
            err = json.loads(body)['error']
            raise RuntimeError('%s (code %s%s)' % (
                err.get('message'), err.get('code'),
                '/' + str(err['error_subcode']) if err.get('error_subcode') else ''))
        except (KeyError, ValueError):
            raise RuntimeError(body[:400])


def uid(igsid):
    """🔴 인스타 사용자 ID 를 **그대로 저장하지 않는다.**
    state.json 은 공개 레포에 커밋되므로, 유출돼도 사람을 특정할 수 없어야 한다.
    중복 발송 판정에는 해시만 있으면 충분하다.
    DM_SALT 를 Secrets 에 넣으면 대조 공격까지 막힌다(선택)."""
    salt = os.environ.get('DM_SALT', 'hc')
    return hashlib.sha256((salt + '|' + str(igsid)).encode()).hexdigest()[:20]


def load_cfg():
    return json.load(open(CFG, encoding='utf-8'))


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding='utf-8'))
    return {'seen_comments': [], 'stage': {}, 'sent_log': []}


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(s, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)


def now():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M')


# ── 점검 ────────────────────────────────────────────────────
def cmd_check():
    if not TOKEN:
        print('❌ ~/.hc-marketing-env 에 IG_ACCESS_TOKEN 이 없습니다')
        return 1
    d = api('debug_token', {'input_token': TOKEN})['data']
    scopes = sorted(d.get('scopes', []))
    print('앱 %s · %s · 만료 %s' % (
        d.get('app_id'), d.get('type'),
        '무기한' if d.get('expires_at') == 0 else d.get('expires_at')))
    print()
    ok = True
    for n in NEED:
        hit = n in scopes
        ok = ok and hit
        print('  %s %s' % ('✅' if hit else '❌', n))
    extra = [s for s in scopes if s not in NEED]
    print('\n  그 외: %s' % ', '.join(extra))

    if not ok:
        print('\n🔴 권한이 빠졌습니다. 시스템 사용자 → [토큰 생성] 에서 다시 발급하세요.')
        print('   ⚠️ 기존 권한도 전부 다시 체크해야 합니다(안 그러면 예약 발행이 깨집니다).')
        return 1

    print('\n권한 OK. 실제 발송 가능한지는 --test-send 로 확인하세요.')
    print('(Standard Access 로 되는지 여기서 갈립니다 — 안 되면 앱 심사 필요)')
    return 0


def cmd_media():
    """최근 게시물 → triggers.json 의 media_id 채우기용."""
    r = api('%s/media' % IG_USER_ID,
            {'fields': 'id,caption,media_type,permalink,timestamp', 'limit': 15})
    for m in r.get('data', []):
        cap = (m.get('caption') or '').split('\n')[0][:44]
        ts = m.get('timestamp', '')[:10]
        print('%s  %-11s %s  %s' % (m['id'], m.get('media_type', ''), ts, cap))
    return 0


# ── 발송 ────────────────────────────────────────────────────
def send_dm(recipient, text, quick=None, button=None):
    """recipient = {'id': IGSID} 또는 {'comment_id': 댓글ID}.

    🔴 **댓글당 비공개 답장은 평생 1회**, 댓글은 **7일 이내**만 가능하다(Meta 제약).
    🔴 타이핑을 시키지 않는다 — **빠른 답장 버튼**(quick)과 **URL 버튼**(button)을 쓴다.
       타계정 실사례(2026-08-21 라일라 수집)가 전부 버튼 방식이다. 타이핑은 이탈이 크다.
    """
    if button:
        # URL 버튼 — 제목 20자 이내, 본문 640자 이내
        msg = {'attachment': {'type': 'template', 'payload': {
            'template_type': 'button',
            'text': text[:640],
            'buttons': [{'type': 'web_url',
                         'url': button['url'],
                         'title': button['title'][:20]}]}}}
    else:
        msg = {'text': text}
        if quick:
            msg['quick_replies'] = [
                {'content_type': 'text', 'title': q[:20], 'payload': 'QR_%d' % i}
                for i, q in enumerate(quick)]
    return papi('%s/messages' % PAGE_ID, post={'recipient': recipient, 'message': msg})


def user_profile(igsid):
    """팔로우 여부 등. 대화가 열려 있어야 조회된다."""
    try:
        return papi('%s' % igsid, {
            'fields': 'name,username,is_user_follow_business,is_business_follow_user'})
    except RuntimeError:
        return {}


def cmd_test_send(igsid, text):
    print('→ %s' % igsid)
    print('─' * 46)
    print(text)
    print('─' * 46)
    r = send_dm({'id': igsid}, text)
    print('✅ 발송됨:', json.dumps(r, ensure_ascii=False))
    return 0


def fetch_comments(media_id):
    """⚠️ 7일 넘은 댓글에는 비공개 답장을 보낼 수 없다(Meta 제약)."""
    out, after = [], None
    while True:
        p = {'fields': 'id,text,username,timestamp,from', 'limit': 50}
        if after:
            p['after'] = after
        r = papi('%s/comments' % media_id, p)
        out += r.get('data', [])
        after = r.get('paging', {}).get('cursors', {}).get('after')
        if not after or not r.get('data'):
            break
    return out


def replied_igsids():
    """답장이 온 대화의 상대 IGSID 집합 — 2통째(링크)를 보낼 대상."""
    got = set()
    try:
        r = papi('%s/conversations' % PAGE_ID,
                 {'platform': 'instagram', 'fields': 'participants,updated_time',
                  'limit': 50})
    except RuntimeError as e:
        print('   ⚠️ 대화 조회 실패:', e)
        return got
    for c in r.get('data', []):
        try:
            msgs = papi('%s' % c['id'], {'fields': 'messages.limit(8){from,created_time}'})
            for m in msgs.get('messages', {}).get('data', []):
                fid = str(m.get('from', {}).get('id', ''))
                if fid and fid != str(IG_USER_ID):
                    got.add(fid)
        except RuntimeError:
            continue
    return got


def cmd_run(dry):
    cfg = load_cfg()
    st = load_state()
    two = cfg.get('two_step', True)
    cap = int(cfg.get('daily_cap', 60))
    today = datetime.now(KST).strftime('%Y-%m-%d')
    sent_today = sum(1 for x in st['sent_log'] if x['at'].startswith(today))

    print('[%s] 2단계=%s · 오늘 발송 %d/%d' % (now(), two, sent_today, cap))

    def log(igsid, kind, tid):
        st['sent_log'].append({'at': datetime.now(KST).isoformat(),
                               'uid': uid(igsid), 'kind': kind, 'trigger': tid})

    # ── 1단계: 키워드 댓글 → 1통 ──
    for t in cfg['triggers']:
        mid = t.get('media_id')
        if not mid:
            print('  · %s — media_id 비어 있음 (게시 후 --media 로 채우세요)' % t['id'])
            continue
        try:
            comments = fetch_comments(mid)
        except RuntimeError as e:
            print('  · %s — 댓글 조회 실패: %s' % (t['id'], e))
            continue

        for c in comments:
            if c['id'] in st['seen_comments']:
                continue
            if t['keyword'] not in (c.get('text') or ''):
                continue
            igsid = str(c.get('from', {}).get('id', '') or c['id'])
            key = uid(igsid)
            if st['stage'].get(key, {}).get(t['id']):
                st['seen_comments'].append(c['id'])
                continue
            if sent_today >= cap:
                print('  🔴 일일 상한 %d 도달 — 중단' % cap)
                break

            text = t['dm1'] if two else t['dm_solo']
            print('  → [%s] @%s 1통' % (t['id'], c.get('username', '?')))
            if dry:
                print('     (dry) %s' % text.replace('\n', ' / ')[:70])
            else:
                try:
                    # 🔴 댓글 작성자는 우리에게 DM 을 보낸 적이 없다 →
                    #    IGSID 가 아니라 **comment_id** 를 수신자로 쓴다(비공개 답장).
                    if two:
                        # 2단계: 링크 없이 빠른 답장 버튼만 (답장을 받아야 링크가 나간다)
                        send_dm({'comment_id': c['id']}, text,
                                quick=[t.get('quick', '팔로우했어요')])
                    else:
                        # 1단계: 비공개 답장 1통에 링크 버튼까지.
                        # 🔴 심사 전에는 **답장을 읽을 수 없어** 2단계가 불가능하다(2026-08-21 실측).
                        send_dm({'comment_id': c['id']}, text,
                                button={'url': t['link'],
                                        'title': t.get('button', '자료 받기')})
                except RuntimeError as e:
                    print('     ❌ %s' % e)
                    continue
                sent_today += 1
                log(igsid, 'dm1' if two else 'dm_only', t['id'])
                time.sleep(2)   # 연속 발송 간격
            st['stage'].setdefault(key, {})[t['id']] = 'dm1' if two else 'done'
            st['seen_comments'].append(c['id'])

    # ── 2단계: 답장 온 사람 → 2통(링크) ──
    if two:
        waiting = {i: s for i, s in st['stage'].items()
                   if any(v == 'dm1' for v in s.values())}
        if waiting:
            got = replied_igsids()
            got_keys = {uid(g): g for g in got}
            for key, stages in list(waiting.items()):
                if key not in got_keys:
                    continue
                igsid = got_keys[key]   # 발송에만 쓰고 저장하지 않는다
                for tid, stage in list(stages.items()):
                    if stage != 'dm1':
                        continue
                    t = next((x for x in cfg['triggers'] if x['id'] == tid), None)
                    if not t:
                        continue
                    prof = user_profile(igsid)
                    follows = prof.get('is_user_follow_business')
                    if cfg.get('require_follow', True) and follows is False:
                        # 🔴 팔로우 게이트 — 링크 대신 안내를 보내고 상태는 dm1 로 둔다.
                        #    다음 폴링에서 다시 확인해 팔로우했으면 그때 링크를 보낸다.
                        if st['stage'].get(key, {}).get('_nudged_' + tid):
                            continue
                        print('  → [%s] %s 팔로우 안내' % (tid, igsid[:10] + '…'))
                        if not dry:
                            try:
                                send_dm({'id': igsid}, t['nudge'],
                                        quick=[t.get('quick', '팔로우했어요')])
                            except RuntimeError as e:
                                print('     ❌ %s' % e)
                                continue
                            log(igsid, 'nudge', tid)
                            time.sleep(2)
                        st['stage'].setdefault(key, {})['_nudged_' + tid] = True
                        continue
                    text = t['dm2']
                    print('  → [%s] %s 2통(링크, 팔로우=%s)' % (tid, igsid[:10] + '…', follows))
                    if dry:
                        print('     (dry) %s' % text.replace('\n', ' / ')[:70])
                    else:
                        try:
                            send_dm({'id': igsid}, text,
                                    button={'url': t['link'],
                                            'title': t.get('button', '자료 받기')})
                        except RuntimeError as e:
                            print('     ❌ %s' % e)
                            continue
                        log(igsid, 'dm2', tid)
                        st.setdefault('follow', {})[key] = follows
                        time.sleep(2)
                    stages[tid] = 'done'

    if not dry:
        save_state(st)
    print('완료.')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--media', action='store_true')
    ap.add_argument('--test-send', metavar='IGSID')
    ap.add_argument('--text', default='하트커넥트 자동 DM 연결 테스트입니다 🙂')
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()

    if not TOKEN:
        print('❌ IG_ACCESS_TOKEN 없음 (~/.hc-marketing-env)')
        return 1
    if a.check:
        return cmd_check()
    if a.media:
        return cmd_media()
    if a.test_send:
        return cmd_test_send(a.test_send, a.text)
    if a.run:
        return cmd_run(a.dry)
    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
