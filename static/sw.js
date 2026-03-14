const CACHE_NAME = "civicbot-v1";
const STATIC_ASSETS = ["/", "/static/css/app.css", "/static/js/app.js", "/manifest.json"];

// Install — cache static assets
self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

// Activate — clean old caches
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch — network first for API, cache first for static
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Never cache POST requests
  if (e.request.method !== "GET") return;

  if (url.pathname.startsWith("/api/")) {
    // Network first for API GET requests — fall back to cache
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const clone = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(e.request, clone));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
  } else {
    // Cache first for everything else
    e.respondWith(
      caches.match(e.request).then((cached) => cached || fetch(e.request))
    );
  }
});

// Push notification received
self.addEventListener("push", (e) => {
  let data = { title: "📰 New Civic Update", body: "Your civic digest is ready.", tag: "civic-digest" };
  try {
    data = e.data.json();
  } catch (_) {}

  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      
      
      tag: data.tag || "civic-digest",
      renotify: true,
      vibrate: [200, 100, 200],
      data: { url: "/" },
    })
  );
});

// Notification click — open app
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({ type: "window" }).then((clientList) => {
      for (const client of clientList) {
        if (client.url === "/" && "focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow("/");
    })
  );
});
