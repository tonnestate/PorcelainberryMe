import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import { IPCallClient, decodeVapidKey, pushDeviceId } from './ip_calls_client.mjs';

const key = Buffer.concat([Buffer.from([4]), Buffer.alloc(64, 22)])
  .toString('base64url');
const endpoint = 'https://web.push.apple.com/v1/device/test-device-id';

test('browser push requires explicit subscription and registers only this device', async () => {
  const saved = [];
  const browserSubscription = {
    endpoint,
    toJSON: () => ({
      endpoint, keys: {
        p256dh: key,
        auth: Buffer.alloc(16, 7).toString('base64url'),
      },
    }),
    async unsubscribe() { saved.push('unsubscribed'); return true; },
  };
  const prompts = [];
  let installed = false;
  const registration = {
    pushManager: {
      async getSubscription() { return installed ? browserSubscription : null; },
      async subscribe(options) {
        prompts.push(options);
        installed = true;
        return browserSubscription;
      },
    },
  };
  const requests = [];
  const client = new IPCallClient({
    apiBase: 'https://calls.example.test',
    identityToken: 'server-issued-person-token',
    fetchImpl: async (url, options) => {
      requests.push({ url, options });
      if (url.endsWith('/config')) {
        return { ok: true, json: async () => ({ vapid_public_key: key }) };
      }
      return { ok: true, json: async () =>
        JSON.parse(options.body)
          ? { enabled: true, removed: true } : { enabled: false } };
    },
  });
  assert.equal((await client.pushConfig()).vapid_public_key, key);
  const activated = await client.enableCallPush({
    registration, vapidPublicKey: key,
  });
  const device = createHash('sha256').update(endpoint).digest('hex').slice(0, 32);
  assert.deepEqual(activated, { device_id: device, enabled: true });
  assert.deepEqual(prompts, [{
    userVisibleOnly: true,
    applicationServerKey: Uint8Array.from(Buffer.concat([
      Buffer.from([4]), Buffer.alloc(64, 22)
    ])),
  }]);
  assert.equal(requests[1].options.method, 'POST');
  const submitted = JSON.parse(requests[1].options.body);
  assert.equal(submitted.device_id, device);
  assert.equal(submitted.endpoint, endpoint);
  assert.equal(submitted.keys.p256dh, key);
  assert.equal(saved.length, 0);

  const disabled = await client.disableCallPush({ registration });
  assert.equal(disabled.removed, true);
  assert.equal(requests[2].options.method, 'DELETE');
  assert.deepEqual(JSON.parse(requests[2].options.body), { device_id: device });
  assert.deepEqual(saved, ['unsubscribed']);
});

test('browser push rejects malformed keys and unsupported secure context', async () => {
  assert.throws(() => decodeVapidKey('garbage'), /invalid_vapid_public_key/);
  assert.equal(decodeVapidKey(key).length, 65);
  await assert.rejects(
    () => pushDeviceId('http://127.0.0.1/private'),
    /invalid_push_endpoint/
  );
  const client = new IPCallClient({
    apiBase: 'https://calls.example.test', identityToken: 'identity',
    fetchImpl: async () => { throw new Error('must_not_request'); },
  });
  await assert.rejects(
    () => client.enableCallPush({ registration: {} }),
    /push_service_worker_required/
  );
});

test('disable refuses to claim success when backend unavailable', async () => {
  const subscription = {
    endpoint,
    unsubscribe() { throw new Error('must_not_unsubscribe_without_revoke'); },
  };
  const client = new IPCallClient({
    apiBase: 'https://calls.example.test', identityToken: 'identity',
    fetchImpl: async () => ({ ok: false, status: 503,
      json: async () => ({ error: 'backend_offline' }) }),
  });
  await assert.rejects(() => client.disableCallPush({
    registration: { pushManager: {
      getSubscription: async () => subscription,
    } },
  }), /backend_offline/);
});

function workerHarness() {
  const callbacks = {};
  const shown = [];
  const opened = [];
  const received = [];
  const scoped = 'https://calls.example.test/pwa/';
  let currentWindows = [];
  const self = {
    registration: {
      scope: scoped,
      showNotification: async (name, options) => {
        shown.push({ name, options });
      },
    },
    clients: {
      matchAll: async () => currentWindows,
      openWindow: async url => { opened.push(url); },
    },
    addEventListener: (event, callback) => { callbacks[event] = callback; },
  };
  const script = fileURLToPath(new URL('./pwa/push-sw.js', import.meta.url));
  runInNewContext(readFileSync(script, 'utf8'), {
    self, URL,
  });
  return {
    callbacks, shown, opened, received,
    windows: v => { currentWindows = v; },
  };
}

async function fire(callback, payload) {
  const pending = [];
  await callback({
    data: { json: () => payload },
    waitUntil: job => pending.push(job),
  });
  await Promise.all(pending);
}

test('service worker displays only call alerts with opaque call IDs', async () => {
  const h = workerHarness();
  const id = 'a'.repeat(32);
  await fire(h.callbacks.push, {
    type: 'incoming_ip_call', call_id: id, kind: 'RINGING',
  });
  assert.equal(h.shown.length, 1);
  assert.equal(h.shown[0].name, 'Eingehender Anruf');
  assert.equal(h.shown[0].options.tag, 'pb-incoming-' + id);
  assert.equal(h.shown[0].options.requireInteraction, true);
  assert.equal(h.shown[0].options.data.callId, id);
  await fire(h.callbacks.push, {
    type: 'incoming_ip_call', call_id: id, kind: 'HANDOFF_OFFERED',
  });
  assert.equal(h.shown[1].name, 'Gesprächsübernahme');
  for (const payload of [
    { type: 'incoming_ip_call', call_id: '../..', kind: 'RINGING' },
    { type: 'incoming_ip_call', call_id: id, kind: 'UNKNOWN' },
    { type: 'marketing', call_id: id, kind: 'RINGING' },
  ]) await fire(h.callbacks.push, payload);
  assert.equal(h.shown.length, 2);
});

test('service worker notification click opens same-origin PWA only', async () => {
  const h = workerHarness();
  const id = 'b'.repeat(32);
  let closed = false;
  const pending = [];
  h.callbacks.notificationclick({
    notification: {
      data: { callId: id },
      close() { closed = true; },
    },
    waitUntil(promise) { pending.push(promise); },
  });
  await Promise.all(pending);
  assert.equal(closed, true);
  assert.equal(h.opened.length, 1);
  const url = new URL(h.opened[0]);
  assert.equal(url.origin, 'https://calls.example.test');
  assert.equal(url.searchParams.get('incoming_call'), id);
  assert.equal(url.pathname, '/');

  let focused = false;
  let message = null;
  h.windows([{ url: 'https://calls.example.test/pwa/home',
    postMessage: x => { message = x; },
    focus: async () => { focused = true; },
  }]);
  const promises = [];
  h.callbacks.notificationclick({
    notification: { data: { callId: id }, close() {} },
    waitUntil: p => promises.push(p),
  });
  await Promise.all(promises);
  assert.equal(focused, true);
  assert.equal(message.type, 'PB_INCOMING_CALL_CLICK');
  assert.equal(message.callId, id);
  assert.equal(h.opened.length, 1);
});
