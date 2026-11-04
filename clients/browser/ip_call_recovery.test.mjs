import test from 'node:test';
import assert from 'node:assert/strict';
import { IPCallClient } from './ip_calls_client.mjs';

function ok(value) {
  return { ok: true, json: async () => value };
}

test('browser: a failed event handler does not discard an incoming ringing event', async () => {
  const urls = [];
  const observed = [];
  let failures = 0;
  const client = new IPCallClient({
    apiBase: 'http://127.0.0.1:8874',
    identityToken: 'test-human',
    fetchImpl: async url => {
      urls.push(url);
      return ok({ events: [
        { cursor: 51, kind: 'RINGING', call: { id: 'voice-call-1' } },
      ] });
    },
    onCallEvent: async event => {
      observed.push(event.kind);
      if (++failures === 1) throw new Error('render failed');
    },
  });
  await assert.rejects(client.pollOnce(), /render failed/);
  assert.equal(client.cursor, 0, 'delivery must not acknowledge a failed handler');
  await client.pollOnce();
  assert.equal(client.cursor, 51);
  assert.deepEqual(observed, ['RINGING', 'RINGING']);
  assert.deepEqual(urls.map(url => url.split('after=')[1]), ['0', '0']);
});

test('browser: overlapping polls share one request and deliver each event once', async () => {
  let resolveFetch;
  let requests = 0;
  const events = [];
  const client = new IPCallClient({
    apiBase: 'http://127.0.0.1:8874',
    identityToken: 'test-human',
    fetchImpl: async () => {
      requests++;
      return await new Promise(resolve => { resolveFetch = resolve; });
    },
    onCallEvent: async event => events.push(event.cursor),
  });
  const first = client.pollOnce();
  const second = client.pollOnce();
  assert.equal(requests, 1);
  resolveFetch(ok({ events: [
    { cursor: 8, kind: 'RINGING', call: { id: 'call-a' } },
  ] }));
  await Promise.all([first, second]);
  assert.equal(requests, 1);
  assert.deepEqual(events, [8]);
  assert.equal(client.cursor, 8);
});

test('browser: ordered partial delivery retries only the failing event', async () => {
  let failSecond = true;
  const seen = [];
  const client = new IPCallClient({
    apiBase: 'http://127.0.0.1:8874',
    identityToken: 'test-human',
    fetchImpl: async url => ok({ events: url.endsWith('after=0')
      ? [{ cursor: 1, kind: 'OUTGOING' }, { cursor: 2, kind: 'RINGING' }]
      : [{ cursor: 2, kind: 'RINGING' }],
    }),
    onCallEvent: async event => {
      if (event.cursor === 2 && failSecond) {
        failSecond = false;
        throw new Error('temporary handler failure');
      }
      seen.push(event.cursor);
    },
  });
  await assert.rejects(client.pollOnce(), /temporary handler failure/);
  assert.equal(client.cursor, 1);
  await client.pollOnce();
  assert.deepEqual(seen, [1, 2]);
  assert.equal(client.cursor, 2);
});

test('browser: an authoritative active-call snapshot resumes an invitation after a sleep', async () => {
  const seen = [];
  const client = new IPCallClient({
    apiBase: 'http://127.0.0.1:8874',
    identityToken: 'test-human',
    fetchImpl: async (url) => {
      assert.ok(url.endsWith('/v1/ip-calls/active'));
      return ok({ server_time: 1800000000, calls: [
        { id: 'missed-invitation', state: 'RINGING', direction: 'incoming' },
      ] });
    },
    onCallEvent: evt => seen.push(evt),
  });
  const snapshot = await client.syncActiveCalls();
  assert.equal(snapshot.calls.length, 1);
  assert.deepEqual(seen.map(x => x.kind), ['ACTIVE_SNAPSHOT']);
  assert.equal(seen[0].call.id, 'missed-invitation');
  assert.equal(client.cursor, 0);
});

test('browser: reconnecting after server-side hangup tears down obsolete media', async () => {
  let disconnected = 0;
  const client = new IPCallClient({
    apiBase: 'http://127.0.0.1:8874',
    identityToken: 'test-human',
    fetchImpl: async () => ok({ calls: [] }),
  });
  client.callId = 'ended-while-asleep';
  client.room = {
    async disconnect() { disconnected++; },
  };
  await client.syncActiveCalls();
  assert.equal(disconnected, 1);
  assert.equal(client.callId, null);
  assert.equal(client.room, null);
});

test('browser: signed human sets own AI opt-in and do-not-disturb only', async () => {
  const received = [];
  const client = new IPCallClient({
    apiBase: 'http://127.0.0.1:8874',
    identityToken: 'human-token',
    fetchImpl: async (url, request) => {
      received.push([url, request]);
      if (request.method === 'POST') {
        return ok({ allow_agent_calls: true, do_not_disturb: true });
      }
      return ok({ allow_agent_calls: false, do_not_disturb: false });
    },
  });
  assert.deepEqual(await client.preferences(), {
    allow_agent_calls: false, do_not_disturb: false,
  });
  assert.deepEqual(await client.updatePreferences({
    allow_agent_calls: true,
    do_not_disturb: true,
  }), { allow_agent_calls: true, do_not_disturb: true });
  assert.equal(received.length, 2);
  assert.equal(received[0][1].headers.Authorization, 'Bearer human-token');
  assert.deepEqual(JSON.parse(received[1][1].body), {
    allow_agent_calls: true,
    do_not_disturb: true,
  });
  await assert.rejects(client.updatePreferences({ tenant: 'another' }),
    /invalid_call_preferences/);
  await assert.rejects(client.updatePreferences({ allow_agent_calls: 1 }),
    /invalid_call_preferences/);
  assert.equal(received.length, 2);
});

test('browser: invalid server snapshot must not disconnect the local call', async () => {
  let disconnected = false;
  const client = new IPCallClient({
    apiBase: 'http://127.0.0.1:8874',
    identityToken: 'human-token',
    fetchImpl: async () => ok({ invalid: 'no calls array' }),
  });
  client.callId = 'known-call';
  client.room = { async disconnect() { disconnected = true; } };
  await assert.rejects(client.syncActiveCalls(), /invalid_call_snapshot/);
  assert.equal(disconnected, false);
});
