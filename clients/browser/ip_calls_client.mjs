/**
 * PorcelainberryMe Internet-call browser adapter. No UI framework or paid API.
 * Auth token is supplied by the trusted host application, never minted here.
 * A real media connection requires the open-source livekit-client package and
 * an independent self-hosted LiveKit server.
 */
/**
 * Stable opaque browser-device key derived from this push endpoint, not
 * stored in tracking cookies. Requires a secure context/WebCrypto.
 */
export async function pushDeviceId(endpoint) {
  if (typeof endpoint !== 'string' || !endpoint.startsWith('https://')) {
    throw new Error('invalid_push_endpoint');
  }
  if (!globalThis.crypto?.subtle) throw new Error('secure_webcrypto_required');
  const hash = new Uint8Array(await globalThis.crypto.subtle.digest(
    'SHA-256', new TextEncoder().encode(endpoint)
  ));
  return Array.from(hash.slice(0, 16), v => v.toString(16).padStart(2, '0')).join('');
}

export function decodeVapidKey(publicKey) {
  if (typeof publicKey !== 'string' || !/^[A-Za-z0-9_-]{86,90}$/.test(publicKey)) {
    throw new Error('invalid_vapid_public_key');
  }
  const binary = atob(publicKey.replace(/-/g, '+').replace(/_/g, '/'));
  const bytes = Uint8Array.from(binary, char => char.charCodeAt(0));
  if (bytes.length !== 65 || bytes[0] !== 4) {
    throw new Error('invalid_vapid_public_key');
  }
  return bytes;
}

/**
 * Deterministic, PII-free CSV for the existing user's call report.
 * Only server-supplied aggregate dates and nonnegative integers are exported.
 * No spreadsheet formulas, transcripts, caller IDs, auth tokens or CRM fields.
 */
export function callReportDailyCsv(report) {
  if (report?.schema !== 'porcelainberryme.reporting.calls.v1'
      || report?.scope !== 'authenticated_participant_only'
      || !Array.isArray(report.daily) || report.daily.length > 31) {
    throw new Error('invalid_call_reporting_response');
  }
  const fields = [
    'day', 'calls', 'incoming', 'outgoing', 'accepted_confirmed',
    'declined', 'timed_out', 'transferred_confirmed',
    'control_session_seconds',
  ];
  const lines = [fields.join(';')];
  for (const entry of report.daily) {
    if (!entry || typeof entry.day !== 'string'
        || !/^\d{4}-\d{2}-\d{2}$/.test(entry.day)
        || fields.slice(1).some(key => !Number.isSafeInteger(entry[key])
                                || entry[key] < 0)) {
      throw new Error('invalid_call_reporting_response');
    }
    lines.push(fields.map(key => entry[key]).join(';'));
  }
  return lines.join('\n') + '\n';
}

export class IPCallClient {
  constructor({
    apiBase,
    identityToken,
    onCallEvent,
    onRemoteAudio,
    roomFactory,
    fetchImpl = globalThis.fetch,
    clock = () => Date.now(),
    requestTimeoutMs = 10000,
  }) {
    if (typeof apiBase !== 'string' || typeof identityToken !== 'string'
        || !identityToken || typeof fetchImpl !== 'function') {
      throw new Error('api_url_token_and_fetch_required');
    }
    if (!Number.isSafeInteger(requestTimeoutMs)
        || requestTimeoutMs < 1000 || requestTimeoutMs > 30000) {
      throw new Error('invalid_call_request_timeout');
    }
    let parsed;
    try {
      parsed = new URL(apiBase);
    } catch {
      throw new Error('invalid_call_api_origin');
    }
    // Signed identity credentials may never traverse cleartext off-host.
    // Reject URL userinfo/query/fragment rather than sending a bearer to
    // a redirect or untrusted origin; HTTPS reverse proxies are supported.
    const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname);
    if ((parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && loopback))
        || parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error('invalid_call_api_origin');
    }
    this.base = apiBase.replace(/\/+$/, '');
    this.token = identityToken;
    this.requestTimeoutMs = requestTimeoutMs;
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
    this._pollInFlight = null;
    this._didInitialSync = false;
  }

  async request(method, endpoint, data) {
    if (!['GET', 'POST', 'DELETE'].includes(method)
        || typeof endpoint !== 'string' || !endpoint.startsWith('/')
        || endpoint.startsWith('//') || endpoint.includes('#')) {
      throw new Error('invalid_call_api_request');
    }
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, this.requestTimeoutMs);
    try {
      const response = await this.fetchImpl(this.base + endpoint, {
        method,
        headers: {
          Authorization: 'Bearer ' + this.token,
          ...(data === undefined ? {} : { 'Content-Type': 'application/json' }),
        },
        ...(data === undefined ? {} : { body: JSON.stringify(data) }),
        cache: 'no-store',
        redirect: 'error',
        signal: controller.signal,
      });
      // A saturated local control server legitimately replies with HTTP 503
      // and an EMPTY body. Do not misreport that as a JSON syntax error.
      let payload;
      try {
        payload = await response.json();
      } catch {
        if (response.ok) throw new Error('ip_call_invalid_json_response');
        payload = {};
      }
      if (!response.ok) {
        const error = new Error(
          typeof payload?.error === 'string'
            ? payload.error : 'ip_call_http_error'
        );
        error.status = response.status;
        throw error;
      }
      return payload;
    } catch (error) {
      if (timedOut) {
        const timeout = new Error('ip_call_request_timeout');
        // A timed-out mutation may have succeeded server-side. Upstream
        // callers must reconcile by ID, never mint a fresh dial ID.
        timeout.outcomeUncertain = method !== 'GET';
        throw timeout;
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Retrieve the PUBLIC VAPID key ahead of a user click. HTTPS/PWA
   * installation and explicit notification consent remain mandatory.
   */
  async pushConfig() {
    return this.request('GET', '/v1/ip-calls/push/config');
  }

  /**
   * IMPORTANT: Call this directly from a user click/tap handler after
   * a service worker has already been registered. Never request push
   * permission automatically during app startup.
   */
  async enableCallPush({ registration, vapidPublicKey }) {
    if (!registration?.pushManager
        || typeof registration.pushManager.subscribe !== 'function') {
      throw new Error('push_service_worker_required');
    }
    // IMPORTANT: subscription must be initiated synchronously within the
    // click handler. Awaiting getSubscription() first can lose transient
    // user activation on iOS/WebKit. subscribe() returns the existing
    // subscription when the application-server key is unchanged.
    const request = registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: decodeVapidKey(vapidPublicKey),
    });
    const subscription = await request;
    const wire = subscription.toJSON();
    if (!wire?.endpoint || !wire.keys?.p256dh || !wire.keys?.auth) {
      throw new Error('invalid_browser_push_subscription');
    }
    const deviceId = await pushDeviceId(wire.endpoint);
    const stored = await this.request('POST',
      '/v1/ip-calls/push/subscriptions', {
        device_id: deviceId,
        endpoint: wire.endpoint,
        keys: wire.keys,
      });
    return { device_id: deviceId, enabled: stored.enabled };
  }

  async disableCallPush({ registration }) {
    if (!registration?.pushManager) {
      throw new Error('push_service_worker_required');
    }
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription) return { enabled: false, removed: false };
    const deviceId = await pushDeviceId(subscription.endpoint);
    // Revoke on the authenticated server FIRST. If it is unavailable,
    // do not falsely claim delivery has been disabled there.
    const result = await this.request(
      'DELETE', '/v1/ip-calls/push/subscriptions',
      { device_id: deviceId }
    );
    await subscription.unsubscribe();
    return result;
  }

  /**
   * Retrieve or change only this authenticated human's own consent/DND.
   * No tenant, recipient, account or user ID may be supplied.
   */
  async preferences() {
    return this.request('GET', '/v1/ip-calls/preferences');
  }

  async updatePreferences(changes) {
    if (!changes || typeof changes !== 'object'
        || Array.isArray(changes) || Object.keys(changes).length === 0
        || Object.keys(changes).some(key => !['allow_agent_calls', 'do_not_disturb'].includes(key))
        || Object.values(changes).some(value => typeof value !== 'boolean')) {
      throw new Error('invalid_call_preferences');
    }
    return this.request('POST', '/v1/ip-calls/preferences', changes);
  }

  /**
   * Personal operational reporting in UTC, never tenant-wide management data.
   * The signed session is sent in the existing Authorization header, never
   * in URLs; the server enforces actual participant access and 30-day retention.
   */
  async callReport(window = {}) {
    if (!window || typeof window !== 'object' || Array.isArray(window)
        || Object.keys(window).some(key => !['start', 'end'].includes(key))) {
      throw new Error('invalid_call_reporting_window');
    }
    const hasStart = Object.hasOwn(window, 'start');
    const hasEnd = Object.hasOwn(window, 'end');
    if (hasStart !== hasEnd) throw new Error('invalid_call_reporting_window');
    let path = '/v1/ip-calls/report';
    if (hasStart) {
      const { start, end } = window;
      if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)
          || start < 0 || end <= start || end - start > 30 * 86400) {
        throw new Error('invalid_call_reporting_window');
      }
      path += '?start=' + start + '&end=' + end;
    }
    const report = await this.request('GET', path);
    if (report?.schema !== 'porcelainberryme.reporting.calls.v1'
        || report?.scope !== 'authenticated_participant_only'
        || report?.timezone !== 'UTC'
        || !report.metrics || !Array.isArray(report.daily)
        || report.daily.length > 31) {
      throw new Error('invalid_call_reporting_response');
    }
    return report;
  }

  /**
   * A browser may have been asleep while events were pruned. Recover the
   * current live calls with a server-authoritative scoped snapshot.
   * A missing connected call means the server has ended it.
   */
  async syncActiveCalls() {
    const snapshot = await this.request('GET', '/v1/ip-calls/active');
    if (!Array.isArray(snapshot?.calls)) {
      throw new Error('invalid_call_snapshot');
    }
    const known = new Set(snapshot.calls.map(call => call.id));
    if (this.callId && !known.has(this.callId)) {
      await this.disconnect();
    }
    for (const call of snapshot.calls) {
      await this.onCallEvent({ kind: 'ACTIVE_SNAPSHOT', call });
    }
    return snapshot;
  }

  /**
   * A deliberate human click requests a call from the host-configured AI.
   * No model, recipient, tenant or caller can be supplied here; the signed
   * server-side human session is the one and only target.
   */
  async requestSelfCall(idempotencyKey) {
    if (typeof idempotencyKey !== 'string'
        || !/^[A-Za-z0-9_.:-]{8,128}$/.test(idempotencyKey)) {
      throw new Error('stable_self_call_request_id_required');
    }
    const response = await this.request(
      'POST', '/v1/ip-calls/call-me',
      { idempotency_key: idempotencyKey },
    );
    if (response?.schema !== 'porcelainberryme.outbound-job.v1'
        || !/^[a-f0-9]{32}$/.test(response?.approval_id)
        || !['APPROVED', 'QUEUED', 'DISPATCHING', 'FINISHED_UNVERIFIED',
             'UNKNOWN', 'FAILED', 'EXPIRED', 'REVOKED'].includes(response?.state)) {
      throw new Error('invalid_self_call_approval_response');
    }
    return response;
  }

  async selfCallStatus(approvalId) {
    if (typeof approvalId !== 'string' || !/^[a-f0-9]{32}$/.test(approvalId)) {
      throw new Error('invalid_self_call_approval_id');
    }
    const response = await this.request(
      'GET', '/v1/ip-calls/call-me?approval_id=' + approvalId,
    );
    if (response?.schema !== 'porcelainberryme.outbound-job.v1'
        || response.approval_id !== approvalId
        || !['APPROVED', 'QUEUED', 'DISPATCHING', 'FINISHED_UNVERIFIED',
             'UNKNOWN', 'FAILED', 'EXPIRED', 'REVOKED'].includes(response?.state)) {
      throw new Error('invalid_self_call_status_response');
    }
    return response;
  }

  async revokeSelfCall(approvalId) {
    if (typeof approvalId !== 'string' || !/^[a-f0-9]{32}$/.test(approvalId)) {
      throw new Error('invalid_self_call_approval_id');
    }
    const response = await this.request(
      'DELETE', '/v1/ip-calls/call-me', { approval_id: approvalId },
    );
    if (response?.schema !== 'porcelainberryme.outbound-job.v1'
        || response?.approval_id !== approvalId
        || !['REVOKED', 'DISPATCHING', 'FINISHED_UNVERIFIED',
             'UNKNOWN', 'FAILED', 'EXPIRED'].includes(response?.state)) {
      throw new Error('invalid_self_call_revocation_response');
    }
    return response;
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

  /**
   * A staff member explicitly accepts the AI's warm-transfer offer. Only
   * after acceptance may connect(callId) obtain a same-room media token.
   * The originating agent remains connected until its own RTC handler
   * observes this staff microphone and acknowledges the transfer.
   */
  async acceptHandoff(callId) {
    const handoff = await this.request(
      'POST', '/v1/ip-calls/' + encodeURIComponent(callId) + '/handoff/accept'
    );
    if (handoff.state !== 'ACCEPTED' && handoff.state !== 'CONNECTED') {
      throw new Error('handoff_not_accepted');
    }
    return handoff;
  }

  declineHandoff(callId) {
    return this.request(
      'POST', '/v1/ip-calls/' + encodeURIComponent(callId) + '/handoff/decline'
    );
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
      const reportMedia = kind => {
        if (this.room !== room || this.callId !== callId) return;
        // Media events are advisory and never authorize microphone access,
        // token issuance or a new call. The host decides whether to reconnect.
        Promise.resolve().then(() => this.onCallEvent({
          kind, call: { id: callId },
        })).catch(() => {});
      };
      room.on(livekit?.RoomEvent?.Reconnecting || 'reconnecting', () => {
        reportMedia('MEDIA_RECONNECTING');
      });
      room.on(livekit?.RoomEvent?.Reconnected || 'reconnected', () => {
        reportMedia('MEDIA_RECONNECTED');
      });
      room.on(livekit?.RoomEvent?.Disconnected || 'disconnected', () => {
        if (this.room !== room || this.callId !== callId) return;
        // Release the failed media instance. The control-plane call may
        // remain ACTIVE, so the customer must explicitly request a new
        // media token and grant the microphone again.
        this.room = null;
        this.callId = null;
        Promise.resolve().then(() => this.onCallEvent({
          kind: 'MEDIA_DISCONNECTED', call: { id: callId },
        })).catch(() => {});
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

  async setMicrophoneMuted(muted) {
    // Same authenticated and already-accepted LiveKit room. Do not request
    // a microphone or join media as a side effect of toggling a control.
    if (typeof muted !== 'boolean') throw new Error('invalid_microphone_muted');
    const publisher = this.room?.localParticipant;
    if (!publisher || typeof publisher.setMicrophoneEnabled !== 'function') {
      throw new Error('media_not_connected');
    }
    await publisher.setMicrophoneEnabled(!muted);
    return { muted };
  }

  async disconnect() {
    const room = this.room;
    this.room = null;
    this.callId = null;
    if (room) await room.disconnect();
  }

  async pollOnce() {
    // Two parallel callers must not race the cursor or emit duplicates.
    if (this._pollInFlight) return this._pollInFlight;
    this._pollInFlight = (async () => {
      const data = await this.request(
        'GET', '/v1/ip-calls/events?after=' + this.cursor
      );
      if (!Array.isArray(data?.events)) throw new Error('invalid_call_events');
      for (const event of data.events) {
        if (!Number.isSafeInteger(event.cursor) || event.cursor <= 0) {
          throw new Error('invalid_call_cursor');
        }
        if (event.cursor <= this.cursor) continue;
        if (event.call?.id === this.callId
            && ['ENDED', 'DECLINED', 'TIMED_OUT'].includes(event.kind)) {
          await this.disconnect();
        }
        // Ack only after the host application actually handles this event.
        // Otherwise a thrown callback silently loses the incoming call.
        await this.onCallEvent(event);
        this.cursor = event.cursor;
      }
      return data;
    })();
    try {
      return await this._pollInFlight;
    } finally {
      this._pollInFlight = null;
    }
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
        if (!this._didInitialSync) {
          await this.syncActiveCalls();
          this._didInitialSync = true;
        }
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
    this._didInitialSync = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}
