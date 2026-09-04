"use strict";

// Agent Hub richiede sempre il server e l'identita Tailscale: il service
// worker abilita l'esperienza installabile senza conservare dati/API offline.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});

function setBadge(count) {
  const value = Math.max(0, Number(count) || 0);
  if (value && "setAppBadge" in self.navigator) return self.navigator.setAppBadge(value);
  if (!value && "clearAppBadge" in self.navigator) return self.navigator.clearAppBadge();
  return Promise.resolve();
}

function closeResolvedNotifications(unreadKeys = null, onlyId = "") {
  const unread = unreadKeys ? new Set(unreadKeys.map(String)) : null;
  return self.registration.getNotifications().then(items => {
    for (const item of items) {
      const sid = String(item.data?.session_id || "");
      const eventKey = String(item.data?.event_key || "");
      if ((onlyId && sid === onlyId) ||
          (unread && (!eventKey || !unread.has(eventKey)))) item.close();
    }
  });
}

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  if (data.type === "dismiss" && data.session_id) {
    event.waitUntil(Promise.all([
      closeResolvedNotifications(null, String(data.session_id)), setBadge(data.badge),
    ]));
    return;
  }
  const title = data.title || "Agent Hub richiede attenzione";
  const options = {
    body: data.body || "Apri Agent Hub per controllare la sessione.",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    tag: data.tag || "agenthub-attention",
    renotify: true,
    data: {
      url: data.url || "/#/", session_id: data.session_id || "",
      lifecycle: data.lifecycle || "",
      event_key: data.event_key || "",
    },
  };
  const work = [self.registration.showNotification(title, options)];
  work.push(setBadge(data.badge));
  event.waitUntil(Promise.all(work));
});

self.addEventListener("message", event => {
  const data = event.data || {};
  if (data.type === "dismiss" && data.session_id) {
    event.waitUntil(Promise.all([
      closeResolvedNotifications(null, String(data.session_id)), setBadge(data.badge),
    ]));
  } else if (data.type === "sync-notifications") {
    event.waitUntil(Promise.all([
      closeResolvedNotifications(data.event_keys || []), setBadge(data.badge),
    ]));
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
