// 缓存按版本号隔离：每次发版换一个缓存名，激活时清掉旧的
const VERSION = '2.2.1';
const CACHE = 'pico-' + VERSION;
const ASSETS = [
  './',
  './index.html',
  './manifest.json',
  'icon-192.png?v=' + VERSION,
  'icon-512.png?v=' + VERSION,
  './vendor/katex/katex.min.css',
  './vendor/katex/katex.min.js'
];

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const box = await caches.open(CACHE);
    for (const u of ASSETS) { try { await box.add(u); } catch (err) {} }
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});

async function fresh(req) {
  const res = await fetch(req, { cache: 'no-store' });
  if (res && res.ok && res.type === 'basic') {
    const box = await caches.open(CACHE);
    box.put(req, res.clone()).catch(() => {});
  }
  return res;
}

function offlinePage() {
  return new Response('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    + '<body style="font:15px -apple-system,sans-serif;background:#f5f5f7;color:#1d1d1f;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
    + '<div style="text-align:center"><p style="font-size:16px;margin:0 0 8px">网络没有连上</p>'
    + '<p style="color:#86868b;font-size:12px;margin:0 0 18px">检查网络后重试，对话记录都在本机</p>'
    + '<button onclick="location.reload()" style="padding:9px 20px;border-radius:999px;border:1px solid #d0d0d5;background:#fff;font-size:13px">重试</button></div></body>', { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return; // API 直连，不缓存

  if (req.mode === 'navigate') {
    // 导航严格网络优先：拿不到网才回缓存，保证刷新就能看到新版本
    e.respondWith((async () => {
      try {
        const res = await fetch(req, { cache: 'no-store' });
        if (res && res.ok) {
          const box = await caches.open(CACHE);
          box.put('./index.html', res.clone()).catch(() => {});
          return res;
        }
      } catch (err) {}
      return (await caches.match('./index.html')) || (await caches.match('./')) || offlinePage();
    })());
    return;
  }

  e.respondWith((async () => {
    const hit = await caches.match(req);
    if (hit) { fresh(req).catch(() => {}); return hit; }
    return (await fresh(req).catch(() => null)) || (await caches.match('./index.html')) || Response.error();
  })());
});
