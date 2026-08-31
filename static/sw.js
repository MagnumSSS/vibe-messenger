// VibeBunker Service Worker — Phase micro3
const CACHE = 'vb-7.5';
const STATIC_RE = /^\/static\//;

self.addEventListener('install', e => {
  e.waitUntil(self.skipWaiting());
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

  // Never intercept API, login, register, admin, WebSocket
  if (path.startsWith('/api/') || path === '/login' || path === '/register' || path === '/admin') return;
  if (e.request.headers.get('upgrade') === 'websocket') return;

  // Cache-first: static assets with ?v= and icons
  if (STATIC_RE.test(path) && url.searchParams.has('v')) {
    e.respondWith(
      caches.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request).then(resp => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE).then(c => c.put(e.request, clone));
          }
          return resp;
        });
      })
    );
    return;
  }

  // Cache-first: icons and manifest
  if (path.endsWith('.png') || path === '/static/manifest.webmanifest') {
    e.respondWith(
      caches.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request).then(resp => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE).then(c => c.put(e.request, clone));
          }
          return resp;
        });
      })
    );
    return;
  }

  // Network-first: navigations (/, /chat, HTML pages)
  e.respondWith(
    fetch(e.request).then(resp => {
      // Cache a copy for offline fallback
      if (resp.ok && e.request.mode === 'navigate') {
        const clone = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
      }
      return resp;
    }).catch(() => {
      // Offline: serve cached navigation or shell
      if (e.request.mode === 'navigate') {
        return caches.match(e.request).then(cached => cached || caches.match('/chat'));
      }
      return new Response('Offline', { status: 503 });
    })
  );
});
