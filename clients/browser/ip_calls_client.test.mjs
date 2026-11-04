import test from 'node:test';
import assert from 'node:assert/strict';
import { IPCallClient } from './ip_calls_client.mjs';

test('headless client rings, accepts, connects to room, and ends without PSTN', async () => {
  const received = [];
  const requests = [];
  let connected = false;
  let disconnected = false;
  let subscribed = null;
  const microphoneStates = [];
  const roomFactory = () => ({
    on(event, callback) {
      if (event === 'trackSubscribed') subscribed = callback;
    },
    async connect(url, token) {
      assert.equal(url, 'ws://localhost:7880');
      assert.equal(token, 'scoped-jwt');
      connected = true;
    },
    localParticipant: {
      async setMicrophoneEnabled(enabled) {
        microphoneStates.push(enabled);
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
  assert.deepEqual(microphoneStates, [true]);
  assert.deepEqual(await client.setMicrophoneMuted(true), { muted: true });
  assert.deepEqual(await client.setMicrophoneMuted(false), { muted: false });
  assert.deepEqual(microphoneStates, [true, false, true]);
  await assert.rejects(() => client.setMicrophoneMuted('true'),
    /invalid_microphone_muted/);
  subscribed({ kind: 'audio', id: 'remote-track' });
  assert.equal(received[1].id, 'remote-track');
  await client.hangup('c1');
  assert.equal(disconnected, true);
  assert.equal(client.room, null);
  await assert.rejects(() => client.setMicrophoneMuted(true), /media_not_connected/);
  assert.equal(requests[0].options.headers.Authorization, 'Bearer signed-human-token');
  assert.equal(JSON.parse(requests[0].options.body).to, 'bob');
});

test('unexpected LiveKit disconnect leaves the call eligible for explicit reconnect', async () => {
  const rooms = [];
  const observed = [];
  let mediaConnections = 0;
  const client = new IPCallClient({
    apiBase: 'http://localhost:8874',
    identityToken: 'signed-human-token',
    roomFactory: () => {
      const events = {};
      const room = {
        events,
        on(name, callback) { events[name] = callback; },
        async connect() { mediaConnections++; },
        localParticipant: {
          async setMicrophoneEnabled(enabled) {
            assert.equal(enabled, true);
          },
        },
        async disconnect() {},
      };
      rooms.push(room);
      return room;
    },
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({
        token: 'room-token', url: 'wss://media.example.test',
        room: 'verified-room',
      }),
    }),
    onCallEvent: event => observed.push(event.kind),
  });
  await client.connect('a'.repeat(32));
  rooms[0].events.reconnecting();
  rooms[0].events.reconnected();
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(observed, ['MEDIA_RECONNECTING', 'MEDIA_RECONNECTED']);
  assert.equal(mediaConnections, 1);

  rooms[0].events.disconnected();
  await Promise.resolve();
  assert.equal(client.room, null);
  assert.equal(client.callId, null);
  assert.equal(observed.at(-1), 'MEDIA_DISCONNECTED');
  // No automatic rejoin, token or microphone request is performed.
  assert.equal(mediaConnections, 1);

  await client.connect('a'.repeat(32));
  assert.equal(mediaConnections, 2);
  rooms[0].events.disconnected(); // delayed event from previous room
  await Promise.resolve();
  assert.equal(client.room, rooms[1]);
  await client.disconnect();
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

test('employee accepts warm handoff, joins same room, and hangs up independently', async () => {
  const requests = [];
  let remoteConnected = false;
  let remoteDisconnected = false;
  const roomFactory = () => ({
    on(event, handler) {
      assert.ok(['trackSubscribed', 'reconnecting', 'reconnected',
                 'disconnected'].includes(event));
    },
    async connect(url, token) {
      assert.equal(url, 'ws://localhost:7880');
      assert.equal(token, 'staff-media-jwt');
      remoteConnected = true;
    },
    localParticipant: {
      async setMicrophoneEnabled(enabled) {
        assert.equal(enabled, true);
      },
    },
    async disconnect() { remoteDisconnected = true; },
  });
  const fetchImpl = async (url, args) => {
    requests.push([url, args.method]);
    if (url.endsWith('/handoff/accept')) {
      return { ok: true, json: async () => ({
        state: 'ACCEPTED', target: 'employee',
      }) };
    }
    if (url.endsWith('/media-token')) {
      return { ok: true, json: async () => ({
        url: 'ws://localhost:7880',
        token: 'staff-media-jwt',
        room: 'pb_existing_customer_call',
      }) };
    }
    if (url.endsWith('/hangup')) {
      return { ok: true, json: async () => ({
        state: 'ENDED',
      }) };
    }
    throw new Error('unexpected_endpoint_' + url);
  };
  const client = new IPCallClient({
    apiBase: 'http://localhost:8874',
    identityToken: 'verified-employee',
    roomFactory, fetchImpl,
  });
  assert.equal((await client.acceptHandoff('current-call-id')).state, 'ACCEPTED');
  const connected = await client.connect('current-call-id');
  assert.equal(connected.room, 'pb_existing_customer_call');
  assert.equal(remoteConnected, true);
  await client.hangup('current-call-id');
  assert.equal(remoteDisconnected, true);
  assert.deepEqual(
    requests.map(row => row[0].split('/').at(-1)),
    ['accept', 'media-token', 'hangup'],
  );
});

test('declining warm handoff does not hang up original customer call', async () => {
  const requests = [];
  const client = new IPCallClient({
    apiBase: 'http://localhost:8874',
    identityToken: 'verified-employee',
    fetchImpl: async (url, request) => {
      requests.push(url);
      return { ok: true, json: async () => ({ state: 'DECLINED' }) };
    },
  });
  assert.equal((await client.declineHandoff('call-a')).state, 'DECLINED');
  assert.equal(requests[0].endsWith('/handoff/decline'), true);
  assert.equal(client.room, null);
  assert.equal(client.callId, null);
});
