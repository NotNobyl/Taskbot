// sw.js — Service Worker for TaskBot PWA
const CACHE = 'taskbot-v1';
const OFFLINE_URLS = ['/'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(OFFLINE_URLS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  // Network-first for API calls
  if (e.request.url.includes('/chat') || e.request.url.includes('/tasks') || e.request.url.includes('/push')) {
    return;
  }
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});

// ── Push notification handler ─────────────────────────────────────────────
self.addEventListener('push', e => {
  let data = { title: 'TaskBot Reminder', body: 'You have a task waiting.' };
  if (e.data) {
    try { data = e.data.json(); } catch {}
  }

  const options = {
    body: data.body || data.title,
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    vibrate: [200, 100, 200],
    tag: 'taskbot-reminder',
    renotify: true,
    actions: [
      { action: 'open',   title: '📋 Open App' },
      { action: 'done',   title: '✅ Mark Done' },
      { action: 'snooze', title: '⏰ Snooze 15min' },
    ],
    data: { task_id: data.task_id },
  };

  e.waitUntil(
    self.registration.showNotification(data.title || 'TaskBot Reminder', options)
  );
});

// ── Notification click handler ────────────────────────────────────────────
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const { action } = e;
  const { task_id } = e.notification.data || {};

  if (action === 'done' && task_id) {
    fetch(`/tasks/${task_id}/done`, { method: 'POST' });
    return;
  }

  if (action === 'snooze' && task_id) {
    // Re-schedule a reminder 15 minutes from now via the API
    const remind_at = new Date(Date.now() + 15 * 60 * 1000).toISOString();
    fetch('/reminders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id, remind_at }),
    });
    return;
  }

  // Default: focus or open app
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
      const existing = clients.find(c => c.url.includes(self.location.origin));
      if (existing) return existing.focus();
      return self.clients.openWindow('/');
    })
  );
});
