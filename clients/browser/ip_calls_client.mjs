/**
 * PorcelainberryMe Internet-call browser adapter. No UI framework or paid API.
 * Auth token is supplied by the trusted host application, never minted here.
 * A real media connection requires the open-source livekit-client package and
 * an independent self-hosted LiveKit server.
 */
export class IPCallClient {
  constructor({
    apiBase,
    identityToken,
    onCallEvent,
    onRemoteAudio,
    roomFactory,
    fetchImpl = globalThis.fetch,
    clock = () => Date.now(),
  }) {
    if (!apiBase || !identityToken || typeof fetchImpl !== 'function') {
      throw new Error('api_url_token_and_fetch_required');
    }
    this.base = apiBase.replace(/\/+$/, '');
    this.token = identityToken;
    this.onCallEvent = onCallEvent || (() => {});
    this.onRemoteAudio = onRemoteAudio || (() => {});
    this.roomFactory = roomFactory || null;
    this.fetchImpl = fetchImpl;
    this.clock = clock;
    this.cursor = 0;
    this.room = null;
    this.callId = null;
    this.running = false;
    this.timer = null;
  }

  async request(method, endpoint, data) {
    const response = await this.fetchImpl(this.base + endpoint, {
      method,
      headers: {
        Authorization: 'Bearer ' + this.token,
        ...(data === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      ...(data === undefined ? {} : { body: JSON.stringify(data) }),
      cache: 'no-store',
    });
    const payload = await response.json();
    if (!response.ok) {
      const error = new Error(payload?.error || 'ip_call_http_error');
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  async dial(to, idempotencyKey) {
    // A trusted caller/agent must provide a stable request ID and contact policy.
    if (!to || !idempotencyKey) throw new Error('recipient_and_idempotency_required');
    return this.request('POST', '/v1/ip-calls', {
      to,
      idempotency_key: idempotencyKey,
    });
  }

  async accept(callId) {
    const call = await this.request('POST', '/v1/ip-calls/' + encodeURIComponent(callId) + '/accept');
    if (call.state !== 'ACTIVE') throw new Error('call_not_active');
    return call;
  }

  decline(callId) {
    return this.request('POST', '/v1/ip-calls/' + encodeURIComponent(callId) + '/decline');
  }

  async hangup(callId) {
    try {
      return await this.request('POST', '/v1/ip-calls/' + encodeURIComponent(callId) + '/hangup');
    } finally {
      await this.disconnect();
    }
  }

  async connect(callId) {
    if (this.room) throw new Error('media_room_already_connected');
    const credentials = await this.request(
      'POST', '/v1/ip-calls/' + encodeURIComponent(callId) + '/media-token'
    );
    // A test/integrating host can supply its own roomFactory. Otherwise load
    // the open-source LiveKit browser SDK only when media is actually needed.
    const livekit = this.roomFactory ? null : await import('livekit-client');
    const room = this.roomFactory ? this.roomFactory() : new livekit.Room();
    if (!room || typeof room.connect !== 'function'
        || typeof room.disconnect !== 'function') {
      throw new Error('invalid_livekit_room');
    }
    // LiveKit's public event value is trackSubscribed. Do not auto-attach
    // elements or manipulate the host app's UI. The caller owns playback.
    if (typeof room.on === 'function') {
      room.on(livekit?.RoomEvent?.TrackSubscribed || 'trackSubscribed', (track) => {
        if (track?.kind === 'audio') this.onRemoteAudio(track);
      });
    }
    this.room = room;
    this.callId = callId;
    try {
      await room.connect(credentials.url, credentials.token);
      // Explicitly requested by a user action (for a browser) or an
      // authorized worker. Browser microphone permission is still required.
      await room.localParticipant.setMicrophoneEnabled(true);
      return { call_id: callId, room: credentials.room };
    } catch (error) {
      await this.disconnect();
      throw error;
    }
  }

  async disconnect() {
    const room = this.room;
    this.room = null;
    this.callId = null;
    if (room) await room.disconnect();
  }

  async pollOnce() {
    const data = await this.request(
      'GET', '/v1/ip-calls/events?after=' + this.cursor
    );
    for (const event of data.events || []) {
      if (event.cursor > this.cursor) this.cursor = event.cursor;
      if (event.call?.id === this.callId
          && ['ENDED', 'DECLINED', 'TIMED_OUT'].includes(event.kind)) {
        await this.disconnect();
      }
      await this.onCallEvent(event);
    }
    return data;
  }

  startPolling(intervalMs = 1200) {
    if (!Number.isInteger(intervalMs) || intervalMs < 500) {
      throw new Error('invalid_poll_interval');
    }
    if (this.running) return;
    this.running = true;
    const tick = async () => {
      if (!this.running) return;
      try {
        await this.pollOnce();
      } catch (error) {
        await this.onCallEvent({ kind: 'POLL_ERROR', error: String(error.message) });
      } finally {
        if (this.running) this.timer = setTimeout(tick, intervalMs);
      }
    };
    void tick();
  }

  stopPolling() {
    this.running = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}
