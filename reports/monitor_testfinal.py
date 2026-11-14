import os, json, urllib.request, time, datetime
base = os.environ['SPREAD_URL'].rstrip('/'); key = os.environ['SPREAD_KEY']

def snap():
    req = urllib.request.Request(base + '/v1/leaderboard', headers={'Authorization': 'Bearer ' + key, 'Accept': 'text/event-stream'})
    r = urllib.request.urlopen(req, timeout=30); buf = ''
    t0 = time.time()
    while time.time() - t0 < 25:
        c = r.read(4096)
        if not c:
            break
        buf += c.decode('utf-8', 'replace')
        while '\n\n' in buf:
            block, buf = buf.split('\n\n', 1)
            for ln in block.split('\n'):
                if ln.startswith('data: '):
                    try:
                        return json.loads(ln[6:])
                    except Exception:
                        pass
    return None

for i in range(40):
    try:
        d = snap()
    except Exception as e:
        print(datetime.datetime.now().strftime('%H:%M:%S'), 'ERR', type(e).__name__, str(e)[:120], flush=True)
        time.sleep(20); continue
    if not d:
        time.sleep(15); continue
    teams = d.get('teams', []); ranked = [t for t in teams if t.get('rank')]
    me = [t for t in teams if 'alignas' in str(t.get('team', '')).lower()]
    stamp = datetime.datetime.now().strftime('%H:%M:%S')
    if me:
        m = me[0]; ms = sorted(t['marginCents'] / 100 for t in ranked)
        rec = {'at': stamp, 'status': d.get('status'), 'now': d.get('now'), 'nranked': len(ranked),
               'rank': m.get('rank'), 'margin': round(m['marginCents'] / 100, 2), 'rev': round(m['revenueCents'] / 100, 2),
               'won': m['wonOrders'], 'ot': round(m['onTimeShare'], 4), 'share': round(m['marginShare'], 4),
               'leader': ranked[0]['name'] if ranked else None,
               'leader_margin': round(ranked[0]['marginCents'] / 100, 2) if ranked else None,
               'cutoff10': round(ranked[9]['marginCents'] / 100, 2) if len(ranked) >= 10 else None,
               'median': round(ms[len(ms) // 2], 2) if ms else None}
        print(json.dumps(rec), flush=True)
        if d.get('status') != 'running':
            print('FINAL', flush=True); break
    else:
        print(json.dumps({'at': stamp, 'status': d.get('status'), 'no_alignas': True}), flush=True)
    time.sleep(40)
print('MONITOR DONE', flush=True)
