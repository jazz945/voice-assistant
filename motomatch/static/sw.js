/* Service worker de MotoMatch.
 *
 * Rôle volontairement limité : rendre l'application installable et permettre
 * son démarrage hors ligne. Il met en cache la coque de l'interface — et rien
 * d'autre.
 *
 * Les réponses de l'API ne sont JAMAIS mises en cache. Elles contiennent des
 * profils, des messages et des positions ; les laisser dans le Cache Storage
 * les rendrait lisibles après une déconnexion, et survivraient à une
 * suppression de compte. Le serveur envoie déjà `Cache-Control: no-store` sur
 * `/api/`, mais on ne s'en remet pas à cela : la règle est appliquée ici aussi.
 */

const CACHE = "motomatch-shell-v2";

// Coque de l'application : statique, sans donnée personnelle.
const SHELL = [
  "/",
  "/static/index.html",
  "/static/styles.css",
  "/static/app.js",
  "/static/avatars.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  // Purge les coques des versions précédentes.
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Hors GET, hors origine, ou API : on laisse passer sans jamais intercepter
  // ni conserver quoi que ce soit.
  if (
    request.method !== "GET" ||
    url.origin !== self.location.origin ||
    url.pathname.startsWith("/api/")
  ) {
    return;
  }

  // Coque : réseau d'abord pour rester à jour, cache en secours hors ligne.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/"))),
  );
});
