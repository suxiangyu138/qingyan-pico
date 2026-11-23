#!/usr/bin/env python3
"""轻言 Pico 访问统计：从 nginx 日志累计安装包下载、网页版打开、下载页访问。
只存计数，不存 IP、不存 UA 原文。默认每小时由 cron 调用。
用法：
  python3 pico-stats.py --log '/var/log/nginx/access*.log' \
      --state /var/lib/pico-stats.state.json \
      --out /var/www/html/download/stats.json
"""
import argparse, glob, gzip, hashlib, json, os, re, sys, tempfile

LINE = re.compile(r'"(?P<method>[A-Z]+) (?P<path>[^ ]+) HTTP/[^"]*" (?P<status>\d{3})')
UA = re.compile(r'"([^"]*)"\s*$')
BOT = re.compile(r'bot|crawl|spider|slurp|scan|python-requests|curl|wget|go-http|java/|okhttp|headless', re.I)
APK = re.compile(r'^/download/(?:app-latest|app-(\d+\.\d+\.\d+))\.apk$')

def load(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"files": {}, "totals": {"installs": 0, "versions": {}, "webOpens": 0, "pageViews": 0}, "updated": ""}

def save(path, data):
    d = os.path.dirname(path) or '.'
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def gz_key(path):
    st = os.stat(path)
    return 'gz:' + hashlib.sha1((path + ':' + str(st.st_size)).encode()).hexdigest()[:16]

def handled(path, offsets, st):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if path.endswith('.gz'):
        k = gz_key(path)
        if k in offsets:
            return
        f = gzip.open(path, 'rb')
        k_done = k
        ino = None
        start = 0
    else:
        ino = os.stat(path).st_ino
        k = 'ino:' + str(ino)
        f = open(path, 'rb')
        rec = offsets.get(k) or {}
        start = rec.get('offset', 0)
        if size and start > size:
            start = 0
        k_done = k
    with f:
        if start:
            try:
                f.seek(start)
            except Exception:
                start = 0
        for raw in f:
            if ino is not None:
                try:
                    pos = f.tell()
                except Exception:
                    pos = start
                if not raw.endswith(b'\n'):
                    start = max(0, pos - len(raw))
                    break
                start = pos
            try:
                line = raw.decode('utf-8', 'ignore')
            except Exception:
                continue
            m = LINE.search(line)
            if not m or m.group('method') != 'GET':
                continue
            if m.group('status') not in ('200', '206'):
                continue
            ua = UA.search(line)
            if ua and BOT.search(ua.group(1) or ''):
                continue
            p = m.group('path').split('?', 1)[0]
            t = st['totals']
            if p in ('/app/', '/app/index.html'):
                t['webOpens'] += 1
            elif p in ('/download/', '/download/index.html'):
                t['pageViews'] += 1
            else:
                am = APK.match(p)
                if am:
                    t['installs'] += 1
                    ver = am.group(1) or 'latest'
                    t['versions'][ver] = t['versions'].get(ver, 0) + 1
    if ino is None:
        offsets[k] = {'path': path, 'done': True}
    else:
        offsets[k] = {'path': path, 'offset': start}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', default='/var/log/nginx/access*.log')
    ap.add_argument('--state', default='/var/lib/pico-stats.state.json')
    ap.add_argument('--out', default='/var/www/html/download/stats.json')
    ap.add_argument('--adopt', default='', help='沿用已有的 stats.json 数值，并把当前日志标记为已处理，只统计之后的新请求')
    a = ap.parse_args()
    fresh = not os.path.exists(a.state)
    state = load(a.state)
    offsets = state.get('files') or {}
    totals = state.get('totals') or {"installs": 0, "versions": {}, "webOpens": 0, "pageViews": 0}
    if fresh and a.adopt and os.path.exists(a.adopt):
        try:
            with open(a.adopt, encoding='utf-8') as f:
                old = json.load(f)
            totals = {
                'installs': int(old.get('installs') or 0),
                'versions': old.get('versions') or {},
                'webOpens': int(old.get('webOpens') or 0),
                'pageViews': int(old.get('pageViews') or 0),
            }
            st = {'totals': totals}
            for path in sorted(glob.glob(a.log)):
                if path.endswith('.gz'):
                    offsets[gz_key(path)] = {'path': path, 'done': True}
                else:
                    try:
                        ino = os.stat(path).st_ino
                        offsets['ino:' + str(ino)] = {'path': path, 'offset': os.path.getsize(path)}
                    except OSError:
                        pass
            out = {'installs': totals['installs'], 'versions': totals['versions'],
                   'webOpens': totals['webOpens'], 'pageViews': totals['pageViews'],
                   'updated': __import__('datetime').datetime.now().astimezone().isoformat(timespec='seconds')}
            save(a.out, out)
            save(a.state, {'files': offsets, 'totals': totals, 'updated': out['updated']})
            print(json.dumps(out, ensure_ascii=False))
            return
        except Exception as e:
            print('adopt failed: %s' % e, file=sys.stderr)
    st = {'totals': totals}
    for path in sorted(glob.glob(a.log)):
        try:
            handled(path, offsets, st)
        except Exception as e:
            print(f'skip {path}: {e}', file=sys.stderr)
    out = {
        'installs': totals['installs'],
        'versions': totals['versions'],
        'webOpens': totals['webOpens'],
        'pageViews': totals['pageViews'],
        'updated': __import__('datetime').datetime.now().astimezone().isoformat(timespec='seconds'),
    }
    save(a.out, out)
    save(a.state, {'files': offsets, 'totals': totals, 'updated': out['updated']})
    print(json.dumps(out, ensure_ascii=False))

if __name__ == '__main__':
    main()
