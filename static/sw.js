// VibeBunker Service Worker — Phase 7.4b
const CACHE = 'vb-7.4b';
const SHELL = ['/', '/chat'];
const STATIC_RE = /^\/static\//;

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  const path = url.pathname;

  // Network-first: API, login, register — never cache
  if (path.startsWith('/api/') || path === '/login' || path === '/register' || path === '/admin') {
    return;
  }

  // WS upgrade — pass through
  if (e.request.headers.get('upgrade') === 'websocket') return;

  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(resp => {
        // Cache shell and static assets
        if (resp.ok && (path === '/' || path === '/chat' || STATIC_RE.test(path) ||
            path === '/static/manifest.webmanifest' || path.endsWith('.png'))) {
          const clone = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return resp;
      }).catch(() => {
        // Offline fallback: serve cached shell for navigation
        if (e.request.mode === 'navigate') {
          return caches.match('/chat');
        }
        return new Response('Offline', { status: 503 });
      });
    })
  );
});
