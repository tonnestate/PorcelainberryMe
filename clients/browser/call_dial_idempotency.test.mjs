import test from 'node:test';
import assert from 'node:assert/strict';
import { CallConsoleController } from './call_console.mjs';

const cid = 'a'.repeat(32);
function storage() {
  const data = new Map();
  return {
    getItem: key => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value),
    removeItem: key => data.delete(key),
    entries: () => [...data.values()],
  };
}
function fakeClient(dial) {
  return {
    onCallEvent: () => {},
    request: async (method, path) => {
      assert.equal(method, 'GET');
      assert.equal(path, '/v1/ip-calls/whoami');
      return { tenant: 'company', subject: 'agent', role: 'agent' };
    },
    dial, startPolling() {}, stopPolling() {}, async pollOnce() {
      return { events: [] };
    }, async disconnect() {},
  };
}
async function controller(client, store) {
  const session = new CallConsoleController({
    client, serviceWorkerContainer: null, pendingDialStorage: store,
    location: { search: '' },
  });
  await session.start();
  return session;
}

test('uncertain outbound invitation retains same key across retries', async () => {
  const keys = [];
  const store = storage();
  let failures = 1;
  const client = fakeClient(async (to, key) => {
    keys.push([to, key]);
    if (failures--) {
      const e = new Error('network_timeout');
      e.outcomeUncertain = true;
      throw e;
    }
    return { id: cid, state: 'RINGING' };
  });
  const consoleUI = await controller(client, store);
  assert.equal(await consoleUI.dial('bob'), false);
  assert.equal(consoleUI.state.phase, 'idle');
  assert.equal(store.entries().length, 1);
  assert.equal(await consoleUI.dial('eve'), false);
  assert.equal(keys.length, 1);
  assert.equal(await consoleUI.dial('bob'), true);
  assert.deepEqual(keys[0], keys[1]);
  assert.equal(store.entries().length, 0);
  assert.equal(consoleUI.state.phase, 'outgoing');
  await consoleUI.destroy();
});

test('PWA reload recovers a timed-out request ID without storing any bearer', async () => {
  const store = storage();
  const keys = [];
  let attempts = 0;
  const dial = async (to, key) => {
    keys.push(key);
    if (!attempts++) throw new Error('socket_closed_without_ack');
    return { id: cid, state: 'ACTIVE' };
  };
  const old = await controller(fakeClient(dial), store);
  assert.equal(await old.dial('bob'), false);
  const saved = JSON.stringify(store.entries());
  assert.doesNotMatch(saved, /token|bearer|secret/i);
  assert.equal(JSON.parse(store.entries()[0]).recipient, 'bob');
  await old.destroy();
  const restored = await controller(fakeClient(dial), store);
  assert.equal(await restored.dial('bob'), true);
  assert.equal(restored.state.phase, 'reconnect');
  assert.equal(keys[0], keys[1]);
  assert.equal(store.entries().length, 0);
  await restored.destroy();
});

test('replaying a terminal invitation does not reopen a dead call', async () => {
  const store = storage();
  let attempts = 0;
  const session = await controller(fakeClient(async () => {
    if (!attempts++) throw new Error('transport_failed');
    return { id: cid, state: 'TIMED_OUT' };
  }), store);
  assert.equal(await session.dial('bob'), false);
  assert.equal(await session.dial('bob'), false);
  assert.equal(session.state.phase, 'idle');
  assert.equal(store.entries().length, 0);
  await session.destroy();
});

test('explicit 4xx policy rejection allows a new independent call intent', async () => {
  const keys = [];
  let first = true;
  const session = await controller(fakeClient(async (to, key) => {
    keys.push([to, key]);
    if (first) {
      first = false;
      const error = new Error('recipient_do_not_disturb');
      error.status = 409;
      throw error;
    }
    return { id: cid, state: 'RINGING' };
  }), storage());
  assert.equal(await session.dial('bob'), false);
  assert.equal(await session.dial('eve'), true);
  assert.notEqual(keys[0][1], keys[1][1]);
  await session.destroy();
});
