"use strict";

// Agent Hub richiede sempre il server e l'identita Tailscale: il service
// worker abilita l'esperienza installabile senza conservare dati/API offline.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
