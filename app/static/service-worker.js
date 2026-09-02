"use strict";

// Agent Hub richiede sempre il server e l'identita Tailscale: il service
// worker abilita l'esperienza installabile senza conservare dati/API offline.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});

function closeResolvedNotifications(activeIds = null, onlyId = "") {
  const active = activeIds ? new Set(activeIds.map(String)) : null;
  return self.registration.getNotifications().then(items => {
    for (const item of items) {
      const sid = String(item.data?.session_id || "");
      if ((onlyId && sid === onlyId) || (active && sid && !active.has(sid))) item.close();
    }
  });
}

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  if (data.type === "dismiss" && data.session_id) {
    event.waitUntil(closeResolvedNotifications(null, String(data.session_id)));
    return;
  }
  const title = data.title || "Agent Hub richiede attenzione";
  const options = {
    body: data.body || "Apri Agent Hub per controllare la sessione.",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    tag: data.tag || "agenthub-attention",
    renotify: true,
    data: { url: data.url || "/#/", session_id: data.session_id || "" },
  };
  const work = [self.registration.showNotification(title, options)];
  if (Number(data.badge) > 0 && "setAppBadge" in self.navigator) {
    work.push(self.navigator.setAppBadge(Number(data.badge)));
  }
  event.waitUntil(Promise.all(work));
});

self.addEventListener("message", event => {
  if (event.data?.type === "sync-attention") {
    event.waitUntil(closeResolvedNotifications(event.data.session_ids || []));
  }
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/#/", self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true })
    .then(async windows => {
      const current = windows[0];
      if (current) {
        if ("navigate" in current) await current.navigate(target);
        return current.focus();
      }
      return self.clients.openWindow ? self.clients.openWindow(target) : undefined;
    }));
});
