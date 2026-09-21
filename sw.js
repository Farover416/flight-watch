/* Offline support for Flight Watch.
 *
 * Network first for the page and the price files, cache only as the fallback.
 * The opposite - cache first - is how a saved web app ends up showing a fare
 * from last week with no sign anything is stale, which for this app would be
 * worse than not working at all. Here, online always means current, and
 * offline means the last prices that were seen, with the run time on screen
 * saying how old they are.
 *
 * GitHub's API is never touched: those are authenticated calls that must not
 * be cached, replayed, or served from anywhere but the network.
 */

const VERSION = "fw-3";
const SHELL = `shell-${VERSION}`;
const DATA = `data-${VERSION}`;

const SHELL_FILES = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL)
      // A missing file must not abort the whole install, or one typo means
      // no offline support at all and nothing says so.
      .then((c) => Promise.allSettled(SHELL_FILES.map((f) => c.add(f))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request, cacheName) {
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.ok) {
      const copy = fresh.clone();
      caches.open(cacheName).then((c) => c.put(request, copy)).catch(() => {});
    }
    return fresh;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw err;
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;   // GitHub API, fonts, etc.

  if (request.mode === "navigate" || url.pathname.endsWith("/index.html")) {
    event.respondWith(networkFirst(request, SHELL));
    return;
  }
  if (url.pathname.includes("/data/")) {
    event.respondWith(networkFirst(request, DATA));
    return;
  }
  // Icons and the manifest never change without a version bump.
  event.respondWith(
    caches.match(request).then((hit) => hit || networkFirst(request, SHELL))
  );
});
