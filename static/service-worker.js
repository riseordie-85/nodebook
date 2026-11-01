// Deliberately minimal. This app's pages are dynamic and login-gated, so
// caching responses would risk showing stale or logged-out content -
// every request just passes straight through to the network. The only
// reason this file exists is that Chrome/Android require a service worker
// with a fetch handler before they'll offer to install a PWA.
self.addEventListener('install', () => {
	self.skipWaiting();
});

self.addEventListener('activate', (event) => {
	event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
	event.respondWith(fetch(event.request));
});
