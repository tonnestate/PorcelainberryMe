import test from 'node:test';
import assert from 'node:assert/strict';
import { CallConsoleController, mountCallConsole } from './call_console.mjs';

const CID = 'a'.repeat(32);
const NEXT = 'b'.repeat(32);

function mockedClient({ calls = {}, identity = {
  tenant: 'demo', subject: 'human1', role: 'human',
}, failConnect = false, push = false } = {}) {
  const log = [];
  let fail = failConnect;
  const mock = {
    log,
    callId: null,
    onCallEvent: () => {},
    onRemoteAudio: () => {},
    async request(method, path) {
      log.push([method, path]);
      if (path === '/v1/ip-calls/whoami') return identity;
      if (path === '/v1/ip-calls/push/config') {
        if (!push) throw new Error('webpush_not_configured');
        return { vapid_public_key: 'dummy-public-key' };
      }
      if (path.startsWith('/v1/ip-calls/') && method === 'GET') {
        const id = path.split('/').at(-1);
        if (!Object.hasOwn(calls, id)) {
          const error = new Error('call_not_found');
          error.status = 404;
          throw error;
        }
        return calls[id];
      }
      throw new Error('unexpected_http_request');
    },
    async pushConfig() {
      return mock.request('GET', '/v1/ip-calls/push/config');
    },
    startPolling(interval) { log.push(['polling_started', interval]); },
    stopPolling() { log.push(['polling_stopped']); },
    async pollOnce() { return { events: [] }; },
    async dial(recipient, idempotencyKey) {
      log.push(['dial', recipient, idempotencyKey]);
      assert.match(idempotencyKey, /^[a-f0-9]{32}$/);
      return { id: CID, state: 'RINGING' };
    },
    async accept(id) { log.push(['accepted', id]); return { state: 'ACTIVE' }; },
    async decline(id) { log.push(['declined', id]); return { state: 'DECLINED' }; },
    async acceptHandoff(id) {
      log.push(['handoff_accepted', id]); return { state: 'ACCEPTED' };
    },
    async declineHandoff(id) {
      log.push(['handoff_declined', id]); return { state: 'DECLINED' };
    },
    async connect(id) {
      log.push(['media_connect', id]);
      if (fail) { fail = false; throw new Error('microphone_denied'); }
      mock.callId = id;
      return { room: 'pb_' + id };
    },
    async setMicrophoneMuted(muted) {
      if (!mock.callId) throw new Error('media_not_connected');
      log.push(['microphone_muted', muted]);
      return { muted };
    },
    async disconnect() {
      log.push(['media_disconnected']);
      mock.callId = null;
    },
    async hangup(id) {
      log.push(['hung_up', id]);
      mock.callId = null;
      return { state: 'ENDED' };
    },
    async enableCallPush({ registration, vapidPublicKey }) {
      log.push(['enabled_push', registration, vapidPublicKey]);
      return { device_id: 'c'.repeat(32), enabled: true };
    },
    async disableCallPush({ registration }) {
      log.push(['disabled_push', registration]);
      return { enabled: false, removed: true };
    },
  };
  return mock;
}

function swMock() {
  const handlers = new Map();
  const registration = { pushManager: {} };
  return {
    registration,
    addEventListener(name, handler) { handlers.set(name, handler); },
    removeEventListener(name, handler) {
      if (handlers.get(name) === handler) handlers.delete(name);
    },
    async register(path) {
      assert.equal(path, '/push-sw.js');
      return registration;
    },
    emit(id) { handlers.get('message')?.({
      data: { type: 'PB_INCOMING_CALL_CLICK', callId: id },
    }); },
    handlerCount: () => handlers.size,
  };
}

function incoming(id = CID) {
  return {
    id, state: 'RINGING', peer: 'ai_assistant', direction: 'incoming',
    created_at: 1800000000, expires_at: 1800000045,
  };
}
function active(id = CID) {
  return { ...incoming(id), state: 'ACTIVE' };
}
function handoff(id = CID, state = 'OFFERED') {
  return {
    ...active(id), peer: 'customer',
    handoff: { state, target: 'staff1' },
  };
}

test('push and URL can reveal a pending call but never answer or open microphone', async () => {
  const calls = { [CID]: incoming() };
  const client = mockedClient({ calls, push: true });
  const sw = swMock();
  const controller = new CallConsoleController({
    client, serviceWorkerContainer: sw,
    location: { search: '?incoming_call=' + CID },
  });
  assert.deepEqual(await controller.start(), {
    tenant: 'demo', subject: 'human1', role: 'human',
  });
  assert.equal(controller.state.phase, 'incoming');
  assert.equal(controller.state.kind, 'call');
  assert.equal(controller.state.pushAvailable, true);
  assert.equal(sw.handlerCount(), 1);
  assert.equal(client.log.some(x => x[0] === 'accepted'), false);
  assert.equal(client.log.some(x => x[0] === 'media_connect'), false);
  await controller.destroy();
  assert.equal(sw.handlerCount(), 0);
  assert.equal(client.log.some(x => x[0] === 'polling_stopped'), true);
});

test('caller explicitly accepts, connects and sees remote hangup', async () => {
  const calls = { [CID]: incoming() };
  const client = mockedClient({ calls });
  const controller = new CallConsoleController({
    client, serviceWorkerContainer: null,
  });
  await controller.start();
  await client.onCallEvent({
    kind: 'RINGING', call: incoming(),
  });
  assert.equal(controller.state.phase, 'incoming');
  assert.equal(await controller.accept(), true);
  assert.equal(controller.state.phase, 'active');
  assert.deepEqual(client.log.filter(x => x[0] === 'accepted'), [['accepted', CID]]);
  assert.deepEqual(client.log.filter(x => x[0] === 'media_connect'), [
    ['media_connect', CID],
  ]);
  await client.onCallEvent({
    kind: 'ENDED', call: { ...active(), state: 'ENDED' },
  });
  assert.equal(controller.state.phase, 'idle');
  assert.equal(controller.state.callId, null);
  assert.equal(client.callId, null);
  await controller.destroy();
});

test('explicit decline never creates a microphone or ends other call', async () => {
  const client = mockedClient({ calls: { [CID]: incoming() } });
  const consoleUI = new CallConsoleController({
    client, serviceWorkerContainer: null,
  });
  await consoleUI.start();
  await consoleUI.resumeCall(CID);
  assert.equal(await consoleUI.decline(), true);
  assert.equal(consoleUI.state.phase, 'idle');
  assert.deepEqual(client.log.filter(x => x[0] === 'declined'), [
    ['declined', CID],
  ]);
  assert.equal(client.log.some(x => x[0] === 'media_connect'), false);
  await consoleUI.destroy();
});

test('failed microphone after acceptance retains safe explicit reconnect', async () => {
  const client = mockedClient({
    calls: { [CID]: active() }, failConnect: true,
  });
  const controller = new CallConsoleController({
    client, serviceWorkerContainer: null,
  });
  await controller.start();
  await controller.resumeCall(CID);
  assert.equal(controller.state.phase, 'reconnect');
  assert.equal(await controller.reconnect(), false);
  assert.equal(controller.state.phase, 'reconnect');
  assert.equal(await controller.reconnect(), true);
  assert.equal(controller.state.phase, 'active');
  assert.equal(client.log.some(x => x[0] === 'hung_up'), false);
  await controller.destroy();
});

test('staff offer requires explicit acceptance; cancelled offer cannot rejoin', async () => {
  const calls = { [CID]: handoff() };
  const client = mockedClient({
    calls, identity: { tenant: 'demo', subject: 'staff1', role: 'human' },
  });
  const controller = new CallConsoleController({
    client, serviceWorkerContainer: null,
  });
  await controller.start();
  await client.onCallEvent({
    kind: 'HANDOFF_OFFERED', call: incoming(),
  });
  assert.equal(controller.state.kind, 'handoff');
  assert.equal(await controller.accept(), true);
  assert.equal(controller.state.phase, 'active');
  assert.deepEqual(client.log.filter(x => x[0] === 'handoff_accepted'), [
    ['handoff_accepted', CID],
  ]);
  assert.equal(client.log.some(x => x[0] === 'accepted'), false);
  await controller.destroy();

  calls[CID] = handoff(CID, 'CANCELLED');
  const another = new CallConsoleController({
    client, serviceWorkerContainer: null,
  });
  await another.start();
  assert.equal(await another.resumeCall(CID), false);
  assert.equal(another.state.phase, 'idle');
  await another.destroy();
});

test('customer can rejoin during staff offer, departed AI cannot rejoin', async () => {
  const calls = { [CID]: handoff() };
  const customer = mockedClient({
    calls, identity: { tenant: 'demo', subject: 'human1', role: 'human' },
  });
  const c = new CallConsoleController({ client: customer, serviceWorkerContainer: null });
  await c.start();
  assert.equal(await c.resumeCall(CID), true);
  assert.equal(c.state.phase, 'reconnect');
  await c.destroy();

  calls[CID] = { ...handoff(CID, 'CONNECTED'), direction: 'outgoing' };
  const agent = mockedClient({
    calls, identity: { tenant: 'demo', subject: 'ai', role: 'agent' },
  });
  const a = new CallConsoleController({ client: agent, serviceWorkerContainer: null });
  await a.start();
  assert.equal(await a.resumeCall(CID), false);
  assert.equal(a.state.phase, 'idle');
  await a.destroy();
});

test('outgoing Internet call requires user click for audio after recipient answers', async () => {
  const calls = { [CID]: { ...active(), peer: 'friend', direction: 'outgoing' } };
  const client = mockedClient({ calls });
  const controller = new CallConsoleController({
    client, serviceWorkerContainer: null,
  });
  await controller.start();
  assert.equal(await controller.dial('friend'), true);
  assert.equal(controller.state.phase, 'outgoing');
  assert.equal(client.log.some(x => x[0] === 'media_connect'), false);
  await controller.onEvent({ kind: 'ACTIVE', call: calls[CID] });
  assert.equal(controller.state.phase, 'reconnect');
  assert.equal(await controller.reconnect(), true);
  assert.equal(controller.state.phase, 'active');
  assert.equal(await controller.hangup(), true);
  assert.equal(controller.state.phase, 'idle');
  assert.deepEqual(client.log.filter(x => x[0] === 'hung_up'), [
    ['hung_up', CID],
  ]);
  await controller.destroy();
});

test('stale or unauthorized push never opens microphone and cannot displace ongoing call', async () => {
  const client = mockedClient({ calls: { [CID]: incoming() } });
  const controller = new CallConsoleController({
    client, serviceWorkerContainer: null,
  });
  await controller.start();
  await assert.rejects(() => controller.resumeCall('../wrong'),
    /invalid_call_id/);
  assert.equal(await controller.resumeCall(NEXT), false);
  assert.equal(controller.state.phase, 'idle');
  await controller.resumeCall(CID);
  assert.equal(await controller.accept(), true);
  assert.equal(await controller.resumeCall(NEXT), false);
  assert.equal(controller.state.callId, CID);
  assert.equal(controller.state.phase, 'active');
  await controller.destroy();
});

test('push subscription is only invoked on explicit user action', async () => {
  const sw = swMock();
  const client = mockedClient({ push: true });
  const controller = new CallConsoleController({
    client, serviceWorkerContainer: sw,
  });
  await controller.start();
  assert.equal(client.log.some(x => x[0] === 'enabled_push'), false);
  assert.equal(await controller.enablePush().then(x => x.enabled), true);
  assert.equal(controller.state.pushEnabled, true);
  assert.equal(await controller.disablePush().then(x => x.removed), true);
  assert.equal(controller.state.pushEnabled, false);
  await controller.destroy();
});

class FakeElement {
  constructor(tag, document) {
    this.tagName = tag;
    this.ownerDocument = document;
    this.textContent = '';
    this.attrs = {};
    this.children = [];
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.value = '';
  }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(event, handler) { this.listeners[event] = handler; }
  fire(event, extra = {}) { this.listeners[event]?.(extra); }
  remove() { /* fake removed element */ }
}

test('lost LiveKit connection offers manual reconnect without automatic microphone access', async () => {
  const calls = { [CID]: incoming() };
  const client = mockedClient({ calls });
  const controller = new CallConsoleController({
    client, serviceWorkerContainer: null,
  });
  await controller.start();
  await controller.resumeCall(CID);
  assert.equal(await controller.accept(), true);
  calls[CID] = active();

  await controller.onEvent({ kind: 'MEDIA_RECONNECTING', call: { id: CID } });
  assert.match(controller.state.message, /automatisch wiederhergestellt/);
  await controller.onEvent({ kind: 'MEDIA_RECONNECTED', call: { id: CID } });
  assert.equal(controller.state.phase, 'active');
  await controller.onEvent({ kind: 'MEDIA_DISCONNECTED', call: { id: CID } });
  assert.equal(controller.state.phase, 'reconnect');
  assert.equal(controller.state.microphoneMuted, false);
  assert.equal(client.log.filter(x => x[0] === 'media_connect').length, 1);
  assert.equal(await controller.reconnect(), true);
  assert.equal(controller.state.phase, 'active');
  assert.equal(client.log.filter(x => x[0] === 'media_connect').length, 2);
  await controller.destroy();
});

test('active call microphone can be muted and unmuted without reconnecting', async () => {
  const calls = { [CID]: incoming() };
  const client = mockedClient({ calls });
  const controller = new CallConsoleController({
    client, serviceWorkerContainer: null,
  });
  await controller.start();
  await controller.resumeCall(CID);
  assert.equal(await controller.toggleMicrophoneMute(), false);
  assert.equal(controller.state.microphoneMuted, false);
  assert.equal(await controller.accept(), true);
  assert.equal(await controller.toggleMicrophoneMute(), true);
  assert.equal(controller.state.microphoneMuted, true);
  assert.equal(await controller.toggleMicrophoneMute(), true);
  assert.equal(controller.state.microphoneMuted, false);
  assert.deepEqual(client.log.filter(x => x[0] === 'microphone_muted'), [
    ['microphone_muted', true],
    ['microphone_muted', false],
  ]);
  assert.equal(client.log.filter(x => x[0] === 'media_connect').length, 1);
  await controller.hangup();
  assert.equal(controller.state.microphoneMuted, false);
  assert.equal(await controller.toggleMicrophoneMute(), false);
  await controller.destroy();
});

test('unstyled accessible call controls bind real accept/hangup handlers', async () => {
  const doc = { createElement: tag => new FakeElement(tag, doc) };
  const root = new FakeElement('main', doc);
  const client = mockedClient({ calls: { [CID]: incoming() } });
  const mounted = mountCallConsole({
    root, client, serviceWorkerContainer: null,
  });
  await mounted.ready;
  assert.equal(root.children[0].textContent,
    'PorcelainberryMe Internetanrufe');
  const status = root.children[1];
  assert.equal(status.attrs.role, 'status');
  const incomingPanel = root.children[3];
  assert.equal(incomingPanel.hidden, true);
  await mounted.controller.resumeCall(CID);
  assert.equal(incomingPanel.hidden, false);
  assert.equal(incomingPanel.children[0].textContent, 'Annehmen');
  incomingPanel.children[0].fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(mounted.controller.state.phase, 'active');
  const inCall = root.children[4];
  assert.equal(inCall.hidden, false);
  assert.equal(inCall.children[1].textContent, 'Auflegen');
  const mute = inCall.children[2];
  assert.equal(mute.textContent, 'Stummschalten');
  assert.equal(mute.attrs['aria-pressed'], 'false');
  mute.fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(mute.textContent, 'Mikrofon aktivieren');
  assert.equal(mute.attrs['aria-pressed'], 'true');
  assert.equal(mounted.controller.state.microphoneMuted, true);
  inCall.children[1].fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(mounted.controller.state.phase, 'idle');
  await mounted.destroy();
  assert.deepEqual(root.children, []);
});
