/* NAGAR Studio service worker.
 *
 * Scope: the application SHELL only (HTML, JS, CSS, font, icons).
 * It never touches /api/ (job state, plans and evidence are private and must not be
 * stored on the device), never handles non-GET requests, and never caches anything
 * cross-origin. BUILD is replaced server-side with a hash of the shipped bytes, so a
 * new release gets a new cache and the old one is deleted.
 */
const BUILD = "__BUILD_ID__";
const CACHE = "nagar-shell-" + BUILD;
const SHELL = [
  "/studio/",
  "/studio/app.js",
  "/studio/api.js",
  "/studio/dom.js",
  "/studio/views.js",
  "/studio/i18n.js",
  "/studio/style.css",
  "/studio/manifest.webmanifest",
  "/studio/icon.svg",
  "/studio/icon-192.png",
  "/studio/fonts/vazirmatn.ttf",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL.map((url) => new Request(url, { cache: "reload" }))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key.startsWith("nagar-shell-") && key !== CACHE).map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith("/studio/")) return; // /api/* and everything else: network only
  // The worker itself and the credential exchange are network only, never cached or faked.
  if (url.pathname === "/studio/sw.js" || url.pathname === "/studio/session") return;
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && SHELL.includes(url.pathname)) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() =>
        caches.match(request).then((hit) => hit || caches.match("/studio/")),
      ),
  );
});
