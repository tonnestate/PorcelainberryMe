import test from 'node:test';
import assert from 'node:assert/strict';
import { IPCallClient } from './ip_calls_client.mjs';

test('headless client rings, accepts, connects to room, and ends without PSTN', async () => {
  const received = [];
  const requests = [];
  let connected = false;
  let disconnected = false;
  let subscribed = null;
  const roomFactory = () => ({
    on(event, callback) {
      assert.equal(event, 'trackSubscribed');
      subscribed = callback;
    },
    async connect(url, token) {
      assert.equal(url, 'ws://localhost:7880');
      assert.equal(token, 'scoped-jwt');
      connected = true;
    },
    localParticipant: {
      async setMicrophoneEnabled(enabled) {
        assert.equal(enabled, true);
      },
    },
    async disconnect() { disconnected = true; },
  });
  const fakeFetch = async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith('/v1/ip-calls')) {
      return { ok: true, json: async () => ({ id: 'c1', state: 'RINGING' }) };
    }
    if (url.endsWith('/v1/ip-calls/events?after=0')) {
      return { ok: true, json: async () => ({
        events: [{ cursor: 10, kind: 'RINGING',
                   call: { id: 'c1', direction: 'incoming' } }],
      }) };
    }
    if (url.endsWith('/v1/ip-calls/c1/accept')) {
      return { ok: true, json: async () => ({ id: 'c1', state: 'ACTIVE' }) };
    }
    if (url.endsWith('/v1/ip-calls/c1/media-token')) {
      return { ok: true, json: async () => ({
        url: 'ws://localhost:7880', token: 'scoped-jwt', room: 'pb_c1',
      }) };
    }
    if (url.endsWith('/v1/ip-calls/c1/hangup')) {
      return { ok: true, json: async () => ({ id: 'c1', state: 'ENDED' }) };
    }
    throw new Error('unknown_url_' + url);
  };

  const client = new IPCallClient({
    apiBase: 'http://localhost:8874',
    identityToken: 'signed-human-token',
    roomFactory, fetchImpl: fakeFetch,
    onCallEvent: evt => received.push(evt),
    onRemoteAudio: track => received.push(track),
  });
  assert.equal((await client.dial('bob', 'stable-request-01')).state, 'RINGING');
  await client.pollOnce();
  assert.equal(received[0].kind, 'RINGING');
  assert.equal(client.cursor, 10);
  await client.accept('c1');
  await client.connect('c1');
  assert.equal(connected, true);
  subscribed({ kind: 'audio', id: 'remote-track' });
  assert.equal(received[1].id, 'remote-track');
  await client.hangup('c1');
  assert.equal(disconnected, true);
  assert.equal(client.room, null);
  assert.equal(requests[0].options.headers.Authorization, 'Bearer signed-human-token');
  assert.equal(JSON.parse(requests[0].options.body).to, 'bob');
});

test('media is forbidden before receiver accepts, no silent connect', async () => {
  let roomFactoryCalls = 0;
  const client = new IPCallClient({
    apiBase: 'http://localhost:8874', identityToken: 'client-token',
    roomFactory: () => { roomFactoryCalls++; return {}; },
    fetchImpl: async () => ({
      ok: false, status: 409,
      json: async () => ({ error: 'call_not_accepted' }),
    }),
  });
  await assert.rejects(client.connect('c2'), /call_not_accepted/);
  assert.equal(roomFactoryCalls, 0);
});

test('polling an ended call disconnects the media session', async () => {
  const observed = [];
  let ended = false;
  const client = new IPCallClient({
    apiBase: 'http://localhost:8874',
    identityToken: 'client-token',
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({
        events: [{ cursor: 4, kind: 'ENDED', call: { id: 'c1' } }],
      }),
    }),
    onCallEvent: event => observed.push(event.kind),
  });
  client.callId = 'c1';
  client.room = { async disconnect() { ended = true; } };
  await client.pollOnce();
  assert.equal(ended, true);
  assert.deepEqual(observed, ['ENDED']);
  assert.equal(client.cursor, 4);
});
