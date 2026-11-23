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
WHEN = re.compile(r'\[(\d{2})/([A-Za-z]{3})/(\d{4}):')
MONTHS = {m: i + 1 for i, m in enumerate(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'])}

def day_of(line):
    m = WHEN.search(line)
    if not m:
        return ''
    d, mon, y = m.group(1), MONTHS.get(m.group(2).title(), 0), m.group(3)
    if not mon:
        return ''
    return f'{y}-{mon:02d}-{d}'

def blank_totals():
    return {"installs": 0, "versions": {}, "webOpens": 0, "pageViews": 0}

def load(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {"files": {}, "totals": blank_totals(), "daily": {}, "updated": ""}
    totals = data.get('totals') or {}
    merged = blank_totals()
    merged.update(totals)
    if not isinstance(merged.get('versions'), dict):
        merged['versions'] = {}
    data['totals'] = merged
    data['files'] = data.get('files') or {}
    data['daily'] = data.get('daily') or {}
    data['heads'] = data.get('heads') or []
    return data

def save(path, data):
    d = os.path.dirname(path) or '.'
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def head_crc(data):
    return '%08x' % (__import__('zlib').crc32(data[:8192]) & 0xffffffff)

def plain_head(path):
    try:
        with open(path, 'rb') as f:
            return head_crc(f.read(8192))
    except Exception:
        return ''

def gz_head(path):
    try:
        with gzip.open(path, 'rb') as f:
            return head_crc(f.read(8192))
    except Exception:
        return ''

def gz_key(path):
    try:
        with open(path, 'rb') as f:
            f.seek(-8, os.SEEK_END)
            trailer = f.read(8)
        if len(trailer) == 8:
            crc = int.from_bytes(trailer[:4], 'little')
            isize = int.from_bytes(trailer[4:], 'little')
            return 'gz:%08x:%d' % (crc, isize)
    except Exception:
        pass
    st = os.stat(path)
    return 'gz:' + hashlib.sha1((path + ':' + str(st.st_size)).encode()).hexdigest()[:16]

def handled(path, offsets, st, heads):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if path.endswith('.gz'):
        k = gz_key(path)
        if k in offsets:
            return
        h = gz_head(path)
        if h and h in heads:
            offsets[k] = {'path': path, 'done': True, 'head': h}
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
        if not rec and path:
            h0 = plain_head(path)
            if h0:
                heads.add(h0)
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
            day = day_of(line)
            bucket = None
            if day:
                bucket = st['daily'].setdefault(day, {'installs': 0, 'webOpens': 0, 'pageViews': 0})
            if p in ('/app/', '/app/index.html'):
                t['webOpens'] += 1
                if bucket is not None:
                    bucket['webOpens'] += 1
            elif p in ('/download/', '/download/index.html'):
                t['pageViews'] += 1
                if bucket is not None:
                    bucket['pageViews'] += 1
            else:
                am = APK.match(p)
                if am:
                    t['installs'] += 1
                    if bucket is not None:
                        bucket['installs'] += 1
                    ver = am.group(1) or 'latest'
                    t['versions'][ver] = t['versions'].get(ver, 0) + 1
    if ino is None:
        offsets[k] = {'path': path, 'done': True, 'head': gz_head(path)}
        heads.add(gz_head(path))
    else:
        offsets[k] = {'path': path, 'offset': start}

def report_text(out, daily, days=10):
    from datetime import datetime
    t = out
    lines = []
    lines.append('轻言 Pico 站点统计')
    lines.append('更新时间  ' + str(t.get('updated', '')).replace('T', ' ')[:16])
    lines.append('')
    lines.append('总计')
    lines.append('  安装包下载   ' + str(t['installs']))
    lines.append('  网页版打开   ' + str(t['webOpens']))
    lines.append('  下载页访问   ' + str(t['pageViews']))
    lines.append('')
    keys = sorted(daily.keys(), reverse=True)[:days]
    if keys:
        lines.append('最近 %d 天' % len(keys))
        lines.append('  日期          下载   网页   下载页')
        for k in keys:
            b = daily[k]
            lines.append('  %s  %5d  %5d  %6d' % (k, b.get('installs', 0), b.get('webOpens', 0), b.get('pageViews', 0)))
        lines.append('')
    vers = t.get('versions') or {}
    if vers:
        lines.append('按版本下载')
        for v, n in sorted(vers.items(), key=lambda x: -x[1]):
            lines.append('  %-10s %5d' % (v, n))
        lines.append('')
    lines.append('口径：只统计请求次数，不记录 IP、UA 与 Cookie；爬虫与 4xx 已剔除。')
    return '\n'.join(lines) + '\n'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', default='/var/log/nginx/access*.log')
    ap.add_argument('--state', default='/var/lib/pico-stats.state.json')
    ap.add_argument('--out', default='', help='可选的公开 stats.json；留空则不写公开文件')
    ap.add_argument('--report-token', default='', help='生成私有报告 <dir>/pico-<token>.txt 与 .json')
    ap.add_argument('--report-dir', default='/var/www/html', help='报告输出目录')
    ap.add_argument('--adopt', default='', help='沿用已有 stats.json 数值，并把当前日志标记为已处理')
    a = ap.parse_args()

    fresh = not os.path.exists(a.state)
    state = load(a.state)
    totals_now = state.get('totals') or blank_totals()
    has_offsets = bool(state.get('files'))
    legacy = (not fresh) and has_offsets is False and (totals_now.get('installs', 0) > 0 or totals_now.get('webOpens', 0) > 0)
    need_adopt = fresh or legacy or (totals_now.get('installs', 0) == 0 and totals_now.get('webOpens', 0) == 0)
    offsets = state.get('files') or {}
    totals = state.get('totals') or {"installs": 0, "versions": {}, "webOpens": 0, "pageViews": 0}
    daily = state.get('daily') or {}
    heads = set(state.get('heads') or [])
    st = {'totals': totals, 'daily': daily}

    if legacy and not (a.adopt and os.path.exists(a.adopt)):
        for path in sorted(glob.glob(a.log)):
            if path.endswith('.gz'):
                offsets[gz_key(path)] = {'path': path, 'done': True}
            else:
                try:
                    ino = os.stat(path).st_ino
                    offsets['ino:' + str(ino)] = {'path': path, 'offset': os.path.getsize(path)}
                except OSError:
                    pass
        print('legacy state detected: logs marked as processed, totals kept', file=sys.stderr)
    elif need_adopt and a.adopt and os.path.exists(a.adopt):
        try:
            with open(a.adopt, encoding='utf-8') as f:
                old = json.load(f)
            totals.update({
                'installs': int(old.get('installs') or 0),
                'versions': old.get('versions') or {},
                'webOpens': int(old.get('webOpens') or 0),
                'pageViews': int(old.get('pageViews') or 0),
            })
            for path in sorted(glob.glob(a.log)):
                if path.endswith('.gz'):
                    offsets[gz_key(path)] = {'path': path, 'done': True}
                else:
                    try:
                        ino = os.stat(path).st_ino
                        offsets['ino:' + str(ino)] = {'path': path, 'offset': os.path.getsize(path)}
                    except OSError:
                        pass
        except Exception as e:
            print('adopt failed: %s' % e, file=sys.stderr)
    else:
        for path in sorted(glob.glob(a.log)):
            try:
                handled(path, offsets, st, heads)
            except Exception as e:
                print('skip %s: %s' % (path, e), file=sys.stderr)

    out = {
        'installs': totals['installs'],
        'versions': totals['versions'],
        'webOpens': totals['webOpens'],
        'pageViews': totals['pageViews'],
        'updated': __import__('datetime').datetime.now().astimezone().isoformat(timespec='seconds'),
    }
    if a.out:
        save(a.out, out)
    save(a.state, {'files': offsets, 'totals': totals, 'daily': daily, 'heads': sorted(heads)[-2000:], 'updated': out['updated']})
    if a.report_token:
        base = os.path.join(a.report_dir, 'pico-' + a.report_token)
        save(base + '.json', dict(out, daily=daily))
        os.makedirs(a.report_dir, exist_ok=True)
        with open(base + '.txt', 'w', encoding='utf-8') as f:
            f.write(report_text(out, daily))
        print('report: %s.txt' % base)
    print(json.dumps(out, ensure_ascii=False))

if __name__ == '__main__':
    main()
