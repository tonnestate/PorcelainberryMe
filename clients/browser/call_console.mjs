/**
 * First-party, authenticated Internet call console for PorcelainberryMe.
 *
 * Existing IPCallClient, call registry and LiveKit transport are reused.
 * The host MUST supply a verified in-memory identity token. No anonymous
 * sign-in, URL token, localStorage token, new phone vendor or paid service.
 *
 * The controller is independent of HTML and can be embedded in React or a
 * plain first-party PWA. No unsolicited recording, auto-answer or mic access.
 */
const CALL_ID = /^[a-f0-9]{32}$/;
const SUBJECT = /^[A-Za-z0-9_-]{1,48}$/;
const TERMINAL = new Set(['ENDED', 'DECLINED', 'TIMED_OUT']);

function validId(id) {
  if (typeof id !== 'string' || !CALL_ID.test(id)) {
    throw new Error('invalid_call_id');
  }
  return id;
}

export class CallConsoleController {
  constructor({
    client, onStateChange = () => {},
    serviceWorkerContainer = globalThis.navigator?.serviceWorker,
    location = globalThis.location,
    registrationPath = '/push-sw.js',
    pendingDialStorage = null,
  }) {
    if (!client || typeof client.request !== 'function'
        || typeof client.pollOnce !== 'function'
        || typeof onStateChange !== 'function') {
      throw new Error('authenticated_ip_call_client_required');
    }
    this.client = client;
    this.onStateChange = onStateChange;
    this.serviceWorkerContainer = serviceWorkerContainer;
    this.location = location;
    this.registrationPath = registrationPath;
    this.pendingDialStorage = pendingDialStorage;
    this._pendingDial = null;
    this._dialStorageKey = null;
    this._pendingSelfCall = null;
    this._selfCallStorageKey = null;
    this._selfCallApprovalStorageKey = null;
    this.registration = null;
    this.vapidPublicKey = null;
    this.identity = null;
    this.started = false;
    this.destroyed = false;
    this.busy = false;
    this.state = {
      phase: 'idle', callId: null, peer: null, kind: null,
      message: 'Bereit für Internetanrufe.', pushAvailable: false,
      pushEnabled: false, busy: false, microphoneMuted: false,
      selfCallApprovalId: null,
    };
    this._originalEvent = client.onCallEvent;
    this._onPushMessage = event => {
      if (event?.data?.type !== 'PB_INCOMING_CALL_CLICK') return;
      const id = event.data.callId;
      if (typeof id === 'string' && CALL_ID.test(id)) {
        void this.resumeCall(id).catch(error => this._error(error));
      }
    };
  }

  _update(change) {
    if (this.destroyed) return;
    this.state = { ...this.state, ...change };
    this.onStateChange({ ...this.state });
  }

  _error(error) {
    this._update({
      busy: false,
      message: error?.message || 'Unbekannter Verbindungsfehler.',
    });
  }

  async start() {
    if (this.started || this.destroyed) throw new Error('console_already_started');
    // Authorization is delegated to the host's real login/session. Never
    // derive it from the push payload or query string.
    this.identity = await this.client.request('GET', '/v1/ip-calls/whoami');
    if (!this.identity?.tenant || !this.identity?.subject
        || !['human', 'agent'].includes(this.identity.role)) {
      throw new Error('invalid_authenticated_identity');
    }
    this.started = true;
    // The temporary request ID is not an authentication credential. A
    // session-scoped record survives accidental PWA reload after uncertain
    // network responses. Never store identity tokens, calls or transcripts.
    this._dialStorageKey = 'pb-pending-dial-v1:' + this.identity.tenant
      + ':' + this.identity.subject;
    this._selfCallStorageKey = 'pb-pending-self-call-v1:' + this.identity.tenant
      + ':' + this.identity.subject;
    this._selfCallApprovalStorageKey = 'pb-self-call-approval-v1:'
      + this.identity.tenant + ':' + this.identity.subject;
    if (!this.pendingDialStorage) {
      try {
        this.pendingDialStorage = globalThis.sessionStorage || null;
      } catch {
        this.pendingDialStorage = null;
      }
    }
    try {
      const existing = this.pendingDialStorage?.getItem(this._dialStorageKey);
      if (existing) {
        const record = JSON.parse(existing);
        if (SUBJECT.test(record?.recipient)
            && /^[a-f0-9]{32}$/.test(record?.key)) {
          this._pendingDial = {
            recipient: record.recipient, key: record.key,
          };
        } else {
          this.pendingDialStorage.removeItem(this._dialStorageKey);
        }
      }
    } catch {
      // Private browsing may refuse sessionStorage; in-memory fallback
      // still deduplicates calls throughout this controller's lifetime.
    }
    try {
      const pending = this.pendingDialStorage?.getItem(this._selfCallStorageKey);
      if (pending && /^[a-f0-9]{32}$/.test(pending)) {
        this._pendingSelfCall = pending;
      }
      const approval = this.pendingDialStorage?.getItem(
        this._selfCallApprovalStorageKey,
      );
      if (approval && CALL_ID.test(approval)) {
        this.state.selfCallApprovalId = approval;
      }
    } catch {
      // Browser storage is optional; no bearer or login is persisted.
    }
    this.client.onCallEvent = async event => {
      await this.onEvent(event);
      if (typeof this._originalEvent === 'function') {
        await this._originalEvent(event);
      }
    };
    if (this.serviceWorkerContainer?.addEventListener) {
      this.serviceWorkerContainer.addEventListener(
        'message', this._onPushMessage,
      );
    }
    // Calls must remain reachable even when service-worker installation or
    // browser push configuration is slow or unavailable.
    this.client.startPolling(1200);
    // Prepare registration and VAPID key outside a click. Never request
    // notification permission here, even on supported iPhones.
    if (this.identity.role === 'human' && this.serviceWorkerContainer?.register) {
      try {
        const [config, registration] = await Promise.all([
          this.client.pushConfig(),
          this.serviceWorkerContainer.register(this.registrationPath),
        ]);
        this.vapidPublicKey = config?.vapid_public_key || null;
        this.registration = registration;
        if (this.vapidPublicKey) {
          this._update({ pushAvailable: true });
        }
      } catch {
        // Incoming call polling must remain usable when Web Push is absent.
        this._update({ pushAvailable: false });
      }
    }
    const id = new URLSearchParams(this.location?.search || '')
      .get('incoming_call');
    if (id && CALL_ID.test(id)) {
      // A URL is only a candidate identifier; permission and current
      // ringing state MUST be revalidated through the authenticated API.
      await this.resumeCall(id);
    }
    this._update({});
    return { ...this.identity };
  }

  async onEvent(event) {
    if (this.destroyed || !event || typeof event !== 'object') return;
    if (event.kind === 'POLL_ERROR') {
      // Preserve the active call, but report a transient network problem.
      return this._update({
        message: 'Anrufstatus vorübergehend nicht erreichbar.',
      });
    }
    if (event.call?.id === this.state.callId) {
      if (event.kind === 'MEDIA_RECONNECTING' && this.state.phase === 'active') {
        return this._update({
          message: 'Audioverbindung wird automatisch wiederhergestellt.',
        });
      }
      if (event.kind === 'MEDIA_RECONNECTED' && this.state.phase === 'active') {
        return this._update({ message: 'Audioverbindung wiederhergestellt.' });
      }
      if (event.kind === 'MEDIA_DISCONNECTED'
          && ['active', 'connecting'].includes(this.state.phase)) {
        // Never automatically request a fresh microphone permission or
        // reconnect from background network events.
        return this._update({
          phase: 'reconnect', microphoneMuted: false,
          message: 'Audioverbindung unterbrochen. Bitte erneut verbinden.',
        });
      }
    }
    const call = event.call;
    if (!call || typeof call.id !== 'string' || !CALL_ID.test(call.id)) return;
    if (TERMINAL.has(event.kind)) {
      if (this.state.callId === call.id) {
        await this.client.disconnect();
        this._update({
          phase: 'idle', callId: null, peer: null, kind: null, busy: false,
          microphoneMuted: false, selfCallApprovalId: null,
          message: 'Anruf beendet oder nicht mehr verfügbar.',
        });
        this._persistSelfCall();
      }
      return;
    }
    if (event.kind === 'RINGING' && call.direction === 'incoming') {
      return this.resumeCall(call.id);
    }
    if (event.kind === 'HANDOFF_OFFERED') {
      return this.resumeCall(call.id);
    }
    if (event.kind === 'ACTIVE' && call.id === this.state.callId) {
      if (this.state.phase === 'outgoing') {
        this._update({
          phase: 'reconnect',
          message: 'Anruf angenommen. Gesprächsverbindung starten.',
        });
      }
    }
  }

  async resumeCall(callId) {
    validId(callId);
    if (this.busy || this.destroyed) return false;
    if (this.state.callId && this.state.callId !== callId
        && this.state.phase !== 'idle') {
      // Never replace an already pending, ringing or active conversation.
      return false;
    }
    let call;
    try {
      call = await this.client.request(
        'GET', '/v1/ip-calls/' + encodeURIComponent(callId),
      );
    } catch (error) {
      if (error?.status === 404 || error?.status === 403) {
        this._update({ message: 'Diese Einladung ist nicht mehr verfügbar.' });
        return false;
      }
      throw error;
    }
    if (!call || call.id !== callId) return false;
    if (call.state === 'RINGING' && call.direction === 'incoming') {
      if (this.state.phase === 'active') return false;
      this._update({
        phase: 'incoming', callId, peer: call.peer, kind: 'call',
        message: 'Eingehender Internetanruf. Annahme erforderlich.',
      });
      return true;
    }
    const handoff = call.handoff;
    if (call.state === 'ACTIVE'
        && handoff?.target === this.identity?.subject
        && handoff?.state === 'OFFERED') {
      if (this.state.phase === 'active') return false;
      this._update({
        phase: 'incoming', callId, peer: call.peer, kind: 'handoff',
        message: 'Mitarbeiterübernahme angefragt. Annahme erforderlich.',
      });
      return true;
    }
    const staffIdentity = handoff?.target === this.identity?.subject;
    const agentTransferred = (call.direction === 'outgoing'
                              && handoff?.state === 'CONNECTED');
    const participantMayRejoin = staffIdentity
      ? ['ACCEPTED', 'CONNECTED'].includes(handoff?.state)
      : ['incoming', 'outgoing'].includes(call.direction);
    if (call.state === 'ACTIVE' && participantMayRejoin
        && !agentTransferred) {
      // A verified participant may reconnect only after an explicit click.
      // Never request a microphone from push/URL processing.
      this._update({
        phase: 'reconnect', callId, peer: call.peer, kind: null,
        message: 'Gespräch aktiv. Zum Verbinden bitte bestätigen.',
      });
      return true;
    }
    if (this.state.callId === callId) {
      this._update({
        phase: 'idle', callId: null, peer: null, kind: null,
        message: 'Die Einladung ist abgelaufen oder bereits bearbeitet.',
      });
    }
    return false;
  }

  async _perform(callback) {
    if (this.busy || this.destroyed || !this.started) return false;
    this.busy = true;
    this._update({ busy: true });
    try {
      return await callback();
    } catch (error) {
      this._error(error);
      return false;
    } finally {
      this.busy = false;
      this._update({ busy: false });
    }
  }

  async accept() {
    return this._perform(async () => {
      if (this.state.phase !== 'incoming') {
        throw new Error('no_incoming_invitation');
      }
      const { callId, kind } = this.state;
      if (kind === 'handoff') await this.client.acceptHandoff(callId);
      else await this.client.accept(callId);
      return this._connect(callId);
    });
  }

  async decline() {
    return this._perform(async () => {
      if (this.state.phase !== 'incoming') {
        throw new Error('no_incoming_invitation');
      }
      const { callId, kind } = this.state;
      if (kind === 'handoff') await this.client.declineHandoff(callId);
      else await this.client.decline(callId);
      this._update({
        phase: 'idle', callId: null, peer: null, kind: null,
        message: 'Einladung abgelehnt.',
      });
      return true;
    });
  }

  async _connect(id) {
    this._update({ phase: 'connecting', message: 'Audioverbindung wird hergestellt.' });
    try {
      await this.client.connect(id);
      // The room may disconnect while the connection promise completes.
      // Do not overwrite a newer MEDIA_DISCONNECTED state with ACTIVE.
      if (this.state.callId !== id || this.state.phase !== 'connecting') {
        return false;
      }
    } catch (error) {
      // If media initialization fails after accepting, server state remains
      // ACTIVE. Do not imply a hangup: offer explicit manual reconnection.
      this._update({
        phase: 'reconnect',
        message: 'Medienverbindung fehlgeschlagen. Erneut verbinden.',
      });
      return false;
    }
    this._update({
      phase: 'active', kind: null, microphoneMuted: false,
      message: 'Internetgespräch verbunden.',
    });
    return true;
  }

  async reconnect() {
    return this._perform(async () => {
      if (this.state.phase !== 'reconnect' || !this.state.callId) {
        throw new Error('no_active_call_to_reconnect');
      }
      // Confirm that the participant still has an ACTIVE authorized call.
      const call = await this.client.request(
        'GET', '/v1/ip-calls/' + this.state.callId,
      );
      if (call.state !== 'ACTIVE') {
        this._update({
          phase: 'idle', callId: null, kind: null, peer: null,
          microphoneMuted: false, message: 'Anruf inzwischen beendet.',
        });
        return false;
      }
      return this._connect(this.state.callId);
    });
  }

  _persistPendingDial() {
    try {
      if (this._dialStorageKey && this.pendingDialStorage) {
        if (this._pendingDial) {
          this.pendingDialStorage.setItem(
            this._dialStorageKey, JSON.stringify(this._pendingDial),
          );
        } else {
          this.pendingDialStorage.removeItem(this._dialStorageKey);
        }
      }
    } catch {
      // The browser may refuse sessionStorage; the current session still
      // retains the idempotency key in memory.
    }
  }

  _persistSelfCall() {
    try {
      if (!this.pendingDialStorage) return;
      if (this._selfCallStorageKey) {
        if (this._pendingSelfCall) {
          this.pendingDialStorage.setItem(
            this._selfCallStorageKey, this._pendingSelfCall,
          );
        } else {
          this.pendingDialStorage.removeItem(this._selfCallStorageKey);
        }
      }
      if (this._selfCallApprovalStorageKey) {
        if (this.state.selfCallApprovalId) {
          this.pendingDialStorage.setItem(
            this._selfCallApprovalStorageKey, this.state.selfCallApprovalId,
          );
        } else {
          this.pendingDialStorage.removeItem(this._selfCallApprovalStorageKey);
        }
      }
    } catch {
      // Never persist auth tokens, transcripts, media or contact lists.
    }
  }

  async callMe() {
    return this._perform(async () => {
      if (this.identity?.role !== 'human'
          || typeof this.client.requestSelfCall !== 'function') {
        throw new Error('self_call_only_for_signed_humans');
      }
      if (this.state.phase !== 'idle') {
        throw new Error('another_call_in_progress');
      }
      if (this.state.selfCallApprovalId) {
        if (typeof this.client.selfCallStatus !== 'function') {
          throw new Error('self_call_status_unavailable');
        }
        const last = await this.client.selfCallStatus(
          this.state.selfCallApprovalId,
        );
        if (!['FINISHED_UNVERIFIED', 'UNKNOWN', 'FAILED', 'EXPIRED', 'REVOKED']
              .includes(last.state)) {
          throw new Error('self_call_already_requested');
        }
        // A separate, explicit new click can authorize a fresh request
        // only once the earlier durable job is really terminal.
        this._update({ selfCallApprovalId: null });
        this._persistSelfCall();
      }
      if (!this._pendingSelfCall) {
        if (!globalThis.crypto?.randomUUID) {
          throw new Error('secure_random_request_id_required');
        }
        this._pendingSelfCall = globalThis.crypto.randomUUID().replace(/-/g, '');
        this._persistSelfCall();
      }
      let approval;
      try {
        approval = await this.client.requestSelfCall(this._pendingSelfCall);
      } catch (error) {
        if (Number.isInteger(error?.status) && error.status >= 400
            && error.status < 500 && !error.outcomeUncertain) {
          this._pendingSelfCall = null;
          this._persistSelfCall();
        }
        throw error;
      }
      if (['QUEUED', 'APPROVED', 'DISPATCHING'].includes(approval.state)) {
        this._pendingSelfCall = null;
        this._update({
          selfCallApprovalId: approval.approval_id,
          message: 'KI-Anruf beauftragt. Bitte eingehenden Anruf annehmen.',
        });
        this._persistSelfCall();
        return true;
      }
      this._pendingSelfCall = null;
      this._persistSelfCall();
      this._update({
        message: 'Diese KI-Anrufanforderung ist bereits abgeschlossen.',
      });
      return false;
    });
  }

  async revokeCallMe() {
    return this._perform(async () => {
      const approvalId = this.state.selfCallApprovalId;
      if (this.identity?.role !== 'human' || !CALL_ID.test(approvalId)
          || typeof this.client.revokeSelfCall !== 'function') {
        throw new Error('no_self_call_approval_to_revoke');
      }
      const result = await this.client.revokeSelfCall(approvalId);
      if (result.state !== 'REVOKED' || !result.revocation_confirmed) {
        this._update({
          message: 'Anruf wurde möglicherweise bereits gestartet. Bitte bei Eingang ablehnen.',
        });
        return false;
      }
      this._update({
        selfCallApprovalId: null,
        message: 'Noch nicht gestarteter KI-Anruf zurückgenommen.',
      });
      this._persistSelfCall();
      return true;
    });
  }

  async dial(recipient) {
    return this._perform(async () => {
      if (!SUBJECT.test(recipient)) throw new Error('invalid_recipient_identity');
      if (this.state.phase !== 'idle') {
        throw new Error('another_call_in_progress');
      }
      if (this._pendingDial && this._pendingDial.recipient !== recipient) {
        throw new Error('unresolved_prior_dial_same_recipient_retry_required');
      }
      if (!this._pendingDial) {
        if (!globalThis.crypto?.randomUUID) {
          throw new Error('secure_random_request_id_required');
        }
        this._pendingDial = {
          recipient, key: globalThis.crypto.randomUUID().replace(/-/g, ''),
        };
        this._persistPendingDial();
      }
      let call;
      try {
        call = await this.client.dial(
          this._pendingDial.recipient, this._pendingDial.key,
        );
        validId(call?.id);
      } catch (error) {
        // A timeout, transport failure or HTTP 5xx can occur *after*
        // the server accepted the invitation. Never invent a new key.
        if ((Number.isInteger(error?.status)
              && error.status >= 400 && error.status < 500)
            && !error?.outcomeUncertain) {
          this._pendingDial = null;
          this._persistPendingDial();
        }
        throw error;
      }
      if (!['RINGING', 'ACTIVE', 'ENDED', 'DECLINED', 'TIMED_OUT']
            .includes(call.state)) {
        // An invalid body after HTTP 200 may still mean the invitation was
        // persisted. Preserve the same request ID until reconciliation.
        throw new Error('invalid_dial_response_state');
      }
      this._pendingDial = null;
      this._persistPendingDial();
      if (['ENDED', 'DECLINED', 'TIMED_OUT'].includes(call.state)) {
        this._update({
          phase: 'idle', callId: null, peer: null, kind: null,
          message: 'Vorheriger Anruf bereits abgeschlossen.',
        });
        return false;
      }
      this._update({
        phase: call.state === 'ACTIVE' ? 'reconnect' : 'outgoing',
        callId: call.id, peer: recipient, kind: null,
        message: call.state === 'ACTIVE'
          ? 'Anruf angenommen. Gesprächsverbindung starten.'
          : 'Internetverbindung wird angefragt.',
      });
      return true;
    });
  }

  async hangup() {
    return this._perform(async () => {
      const { callId } = this.state;
      if (!callId) throw new Error('no_active_call');
      try {
        await this.client.hangup(callId);
      } finally {
        await this.client.disconnect();
      }
      this._update({
        phase: 'idle', callId: null, kind: null, peer: null,
        microphoneMuted: false, message: 'Anruf beendet.',
      });
      return true;
    });
  }

  async toggleMicrophoneMute() {
    return this._perform(async () => {
      if (this.state.phase !== 'active' || !this.state.callId) {
        throw new Error('microphone_requires_active_media');
      }
      const callId = this.state.callId;
      const muted = !this.state.microphoneMuted;
      await this.client.setMicrophoneMuted(muted);
      // A hangup may have arrived while the device operation was pending.
      if (this.state.phase !== 'active' || this.state.callId !== callId) {
        return false;
      }
      this._update({
        microphoneMuted: muted,
        message: muted ? 'Mikrofon stummgeschaltet.' : 'Mikrofon aktiviert.',
      });
      return true;
    });
  }

  async enablePush() {
    if (!this.started || !this.registration || !this.vapidPublicKey) {
      throw new Error('push_not_configured');
    }
    // CRITICAL: Do not await between the real user click and the browser's
    // pushManager.subscribe() call. The SDK must also preserve activation.
    const pending = this.client.enableCallPush({
      registration: this.registration, vapidPublicKey: this.vapidPublicKey,
    });
    const result = await pending;
    this._update({ pushEnabled: Boolean(result.enabled),
                   message: 'Anrufbenachrichtigungen aktiviert.' });
    return result;
  }

  async disablePush() {
    if (!this.registration) throw new Error('push_not_configured');
    const result = await this.client.disableCallPush({
      registration: this.registration,
    });
    this._update({
      pushEnabled: false, message: 'Anrufbenachrichtigungen deaktiviert.',
    });
    return result;
  }

  async destroy() {
    if (this.destroyed) return;
    this.client.stopPolling();
    if (this.serviceWorkerContainer?.removeEventListener) {
      this.serviceWorkerContainer.removeEventListener(
        'message', this._onPushMessage,
      );
    }
    this.client.onCallEvent = this._originalEvent;
    await this.client.disconnect();
    this.destroyed = true;
  }
}

/**
 * Accessible, unstyled call controls. Host product owns CSS and login.
 * The root is an existing authorized page element; no app-wide wrapper or
 * assumed spacing, colors, border radius, framework, or persistence.
 */
export function mountCallConsole({ root, client, ...options }) {
  if (!root?.ownerDocument?.createElement
      || typeof root.replaceChildren !== 'function') {
    throw new Error('valid_call_console_root_required');
  }
  const doc = root.ownerDocument;
  const element = (tag, label = '') => {
    const e = doc.createElement(tag);
    if (label) e.textContent = label;
    return e;
  };
  const title = element('h2', 'PorcelainberryMe Internetanrufe');
  const status = element('p');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const details = element('p');
  const incoming = element('section');
  incoming.setAttribute('aria-label', 'Eingehender Anruf');
  const accept = element('button', 'Annehmen');
  accept.type = 'button';
  const decline = element('button', 'Ablehnen');
  decline.type = 'button';
  incoming.append(accept, decline);
  const inCall = element('section');
  inCall.setAttribute('aria-label', 'Aktuelles Gespräch');
  const connect = element('button', 'Audio verbinden');
  connect.type = 'button';
  const end = element('button', 'Auflegen');
  end.type = 'button';
  const mute = element('button', 'Stummschalten');
  mute.type = 'button';
  mute.setAttribute('aria-pressed', 'false');
  inCall.append(connect, end, mute);
  const form = element('form');
  const recipientLabel = element('label', 'Internet-ID');
  const recipient = element('input');
  recipient.type = 'text';
  recipient.name = 'recipient';
  recipient.autocomplete = 'off';
  recipient.required = true;
  recipient.maxLength = 48;
  recipient.pattern = '[A-Za-z0-9_-]{1,48}';
  recipientLabel.append(recipient);
  const dial = element('button', 'Anrufen');
  dial.type = 'submit';
  form.append(recipientLabel, dial);
  const notifications = element('section');
  notifications.setAttribute('aria-label', 'Anrufbenachrichtigungen');
  const enable = element('button', 'Benachrichtigungen aktivieren');
  enable.type = 'button';
  const disable = element('button', 'Benachrichtigungen deaktivieren');
  disable.type = 'button';
  notifications.append(enable, disable);
  const selfCallPanel = element('section');
  selfCallPanel.setAttribute('aria-label', 'KI-Rückruf');
  const callMe = element('button', 'KI soll mich anrufen');
  callMe.type = 'button';
  const revokeSelfCall = element('button', 'KI-Anrufanforderung zurücknehmen');
  revokeSelfCall.type = 'button';
  selfCallPanel.append(callMe, revokeSelfCall);
  const audio = element('div');
  audio.setAttribute('aria-label', 'Gesprächsaudio');
  root.replaceChildren(title, status, details, incoming, inCall, form,
                       notifications, selfCallPanel, audio);

  const attached = [];
  const oldAudio = client.onRemoteAudio;
  client.onRemoteAudio = track => {
    if (typeof oldAudio === 'function') oldAudio(track);
    if (typeof track?.attach === 'function') {
      const media = track.attach();
      if (media) {
        media.autoplay = true;
        media.playsInline = true;
        audio.appendChild(media);
        attached.push({ track, media });
        const promise = media.play?.();
        if (promise && typeof promise.catch === 'function') {
          promise.catch(() => {
            status.textContent = 'Audiowiedergabe blockiert. Bitte erneut verbinden.';
          });
        }
      }
    }
  };
  const clearAudio = () => {
    for (const { track, media } of attached.splice(0)) {
      try { track.detach?.(media); } catch { /* room already closed */ }
      media.remove?.();
    }
  };

  const render = state => {
    status.textContent = state.message;
    details.textContent = state.peer ? 'Gesprächspartner: ' + state.peer : '';
    incoming.hidden = state.phase !== 'incoming';
    inCall.hidden = !['active', 'connecting', 'outgoing', 'reconnect']
      .includes(state.phase);
    connect.hidden = state.phase !== 'reconnect';
    mute.hidden = state.phase !== 'active';
    mute.disabled = state.busy;
    mute.textContent = state.microphoneMuted
      ? 'Mikrofon aktivieren' : 'Stummschalten';
    mute.setAttribute('aria-pressed', state.microphoneMuted ? 'true' : 'false');
    form.hidden = state.phase !== 'idle';
    notifications.hidden = !state.pushAvailable;
    selfCallPanel.hidden = controller.identity?.role !== 'human'
      || typeof client.requestSelfCall !== 'function';
    callMe.hidden = Boolean(state.selfCallApprovalId);
    callMe.disabled = state.busy || state.phase !== 'idle';
    revokeSelfCall.hidden = !state.selfCallApprovalId;
    revokeSelfCall.disabled = state.busy || state.phase !== 'idle';
    enable.hidden = state.pushEnabled;
    disable.hidden = !state.pushEnabled;
    accept.disabled = decline.disabled = dial.disabled = state.busy;
    connect.disabled = end.disabled = state.busy;
    enable.disabled = disable.disabled = state.busy;
    if (state.phase === 'idle') clearAudio();
  };
  const controller = new CallConsoleController({
    client, ...options, onStateChange: render,
  });
  const run = action => {
    Promise.resolve().then(action).catch(error => controller._error(error));
  };
  accept.addEventListener('click', () => run(() => controller.accept()));
  decline.addEventListener('click', () => run(() => controller.decline()));
  connect.addEventListener('click', () => run(() => controller.reconnect()));
  end.addEventListener('click', () => run(() => controller.hangup()));
  mute.addEventListener('click', () => run(() => controller.toggleMicrophoneMute()));
  form.addEventListener('submit', event => {
    event.preventDefault();
    run(() => controller.dial(recipient.value.trim()));
  });
  // Call enablePush DIRECTLY in the click handler, with no intermediary
  // await or promise scheduling, to preserve browser user activation.
  enable.addEventListener('click', () => {
    controller.enablePush().catch(error => controller._error(error));
  });
  disable.addEventListener('click', () => run(() => controller.disablePush()));
  callMe.addEventListener('click', () => run(() => controller.callMe()));
  revokeSelfCall.addEventListener('click', () => run(() => controller.revokeCallMe()));
  render(controller.state);
  return {
    controller,
    ready: controller.start(),
    async destroy() {
      await controller.destroy();
      clearAudio();
      client.onRemoteAudio = oldAudio;
      root.replaceChildren();
    },
  };
}
