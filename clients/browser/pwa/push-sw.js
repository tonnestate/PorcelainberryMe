/* PorcelainberryMe standalone Web Push service worker.
 *
 * Serve THIS FILE from the root of the customer's HTTPS PWA origin as
 * /push-sw.js. It does not auto-answer, auto-enable a microphone, or
 * silently record/track calls. iOS requires an installed Home Screen PWA.
 */
self.addEventListener('push', event => {
  // Safari requires a visible user notification immediately after push.
  // No silent push, notification suppression or invisible background fetch.
  let payload;
  try {
    payload = event.data?.json();
  } catch {
    return;
  }
  const id = payload?.call_id;
  const kind = payload?.kind;
  if (payload?.type !== 'incoming_ip_call'
      || !/^[0-9a-f]{32}$/.test(id)
      || !['RINGING', 'HANDOFF_OFFERED'].includes(kind)) {
    return;
  }
  const isHandoff = kind === 'HANDOFF_OFFERED';
  const target = new URL('/', self.registration.scope);
  target.searchParams.set('incoming_call', id);
  const options = {
    body: isHandoff
      ? 'Ein Gespräch wartet auf Ihre Übernahme.'
      : 'Ein Internetanruf wartet auf Ihre Annahme.',
    tag: 'pb-incoming-' + id,
    renotify: true,
    requireInteraction: true,
    data: { callId: id, url: target.href },
  };
  // The push endpoint carries no contact identity, transcript or audio.
  event.waitUntil(self.registration.showNotification(
    isHandoff ? 'Gesprächsübernahme' : 'Eingehender Anruf', options
  ));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const info = event.notification?.data;
  const callId = info?.callId;
  if (typeof callId !== 'string' || !/^[0-9a-f]{32}$/.test(callId)) return;
  const target = new URL('/', self.registration.scope);
  target.searchParams.set('incoming_call', callId);
  event.waitUntil((async () => {
    // Reuse the already running first-party PWA where available. The host
    // app must fetch the current call state before showing accept/decline:
    // the push may arrive AFTER the ring timeout or a remote hangup.
    const windows = await self.clients.matchAll({
      type: 'window', includeUncontrolled: true,
    });
    for (const client of windows) {
      if (new URL(client.url).origin === target.origin
          && client.url.startsWith(self.registration.scope)) {
        client.postMessage({ type: 'PB_INCOMING_CALL_CLICK', callId });
        if ('focus' in client) {
          await client.focus();
          return;
        }
      }
    }
    await self.clients.openWindow(target.href);
  })());
});
