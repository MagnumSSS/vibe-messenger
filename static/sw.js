// VibeBunker Service Worker — Phase micro3
// Phase 7.8: имя кэша = версия фазы. Меняем его КАЖДУЮ фазу: иначе браузер
// продолжает отдавать старый style.css (cache-first по ключу /static/style.css?v=...)
// и новый CSS до пользователя не доходит вообще.
const CACHE = 'vb-7.8';
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

  // Stale-while-revalidate: static assets with ?v=
  // (Phase 7.8: даже если версию в ссылке забыли поднять, фоновая подкачка
  // обновит кэш и следующая загрузка покажет свежие стили)
  if (STATIC_RE.test(path) && url.searchParams.has('v')) {
    e.respondWith(
      caches.open(CACHE).then(cache =>
        cache.match(e.request).then(cached => {
          const network = fetch(e.request).then(resp => {
            if (resp.ok) cache.put(e.request, resp.clone());
            return resp;
          }).catch(() => cached);
          return cached || network;
        })
      )
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
