import test from 'node:test';
import assert from 'node:assert/strict';
import { IPCallClient } from './ip_calls_client.mjs';
import { CallConsoleController } from './call_console.mjs';

const APPROVAL = 'e'.repeat(32);
const queued = {
  schema: 'porcelainberryme.outbound-job.v1',
  approval_id: APPROVAL, state: 'QUEUED',
  call_result_verified: false, revocation_confirmed: false,
};
function store() {
  const values = new Map();
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
    list: () => [...values.values()],
  };
}
function fakeHuman(requestSelfCall, revokeSelfCall, role = 'human') {
  return {
    onCallEvent: null,
    async request(method, path) {
      assert.equal(method, 'GET');
      assert.equal(path, '/v1/ip-calls/whoami');
      return { tenant: 'company', subject: 'owner', role };
    },
    requestSelfCall, revokeSelfCall,
    async pollOnce() { return { events: [] }; },
    startPolling() {}, stopPolling() {}, async disconnect() {},
  };
}
async function create(client, storage) {
  const controller = new CallConsoleController({
    client, pendingDialStorage: storage, serviceWorkerContainer: null,
    location: { search: '' },
  });
  await controller.start();
  return controller;
}

test('browser self-call sends only idempotency key and signed bearer', async () => {
  const requests = [];
  const client = new IPCallClient({
    apiBase: 'https://calls.example.test',
    identityToken: 'signed-human-jwt',
    fetchImpl: async (url, options) => {
      requests.push({ url, options });
      return {
        ok: true, status: options.method === 'POST' ? 202 : 200,
        json: async () => options.method === 'POST' ? queued : {
          ...queued, state: 'REVOKED', revocation_confirmed: true,
        },
      };
    },
  });
  assert.equal((await client.requestSelfCall('clicked-call-0001')).state, 'QUEUED');
  assert.equal((await client.revokeSelfCall(APPROVAL)).state, 'REVOKED');
  assert.equal(requests.length, 2);
  assert.equal(requests[0].url, 'https://calls.example.test/v1/ip-calls/call-me');
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    idempotency_key: 'clicked-call-0001',
  });
  assert.equal(requests[0].options.headers.Authorization, 'Bearer signed-human-jwt');
  assert.equal(requests[1].options.method, 'DELETE');
  assert.deepEqual(JSON.parse(requests[1].options.body), {
    approval_id: APPROVAL,
  });
  await assert.rejects(client.requestSelfCall('x'), /stable_self_call_request_id/);
  await assert.rejects(client.revokeSelfCall('../wrong'), /invalid_self_call_approval_id/);
});

test('explicit PWA call-me click survives uncertain server response and reload', async () => {
  const storage = store();
  const ids = [];
  let calls = 0;
  const requestSelfCall = async key => {
    ids.push(key);
    if (calls++ === 0) {
      const uncertain = new Error('upstream connection lost');
      uncertain.outcomeUncertain = true;
      throw uncertain;
    }
    return queued;
  };
  const revokeSelfCall = async id => {
    assert.equal(id, APPROVAL);
    return { ...queued, state: 'REVOKED', revocation_confirmed: true };
  };
  const first = await create(fakeHuman(requestSelfCall, revokeSelfCall), storage);
  assert.equal(await first.callMe(), false);
  assert.equal(storage.list().length, 1);
  assert.doesNotMatch(JSON.stringify(storage.list()), /bearer|jwt|secret/i);
  await first.destroy();

  const resumed = await create(fakeHuman(requestSelfCall, revokeSelfCall), storage);
  assert.equal(await resumed.callMe(), true);
  assert.equal(resumed.state.selfCallApprovalId, APPROVAL);
  assert.equal(ids.length, 2);
  assert.equal(ids[0], ids[1]);
  assert.equal(await resumed.callMe(), false); // no duplicate click
  assert.equal(ids.length, 2);
  assert.equal(await resumed.revokeCallMe(), true);
  assert.equal(resumed.state.selfCallApprovalId, null);
  assert.equal(storage.list().length, 0);
  await resumed.destroy();
});

test('self-call is unavailable to an AI agent and cannot be silently placed', async () => {
  let attempted = false;
  const client = fakeHuman(async () => {
    attempted = true;
    return queued;
  }, async () => queued, 'agent');
  const ui = await create(client, store());
  assert.equal(await ui.callMe(), false);
  assert.equal(attempted, false);
  assert.equal(ui.state.selfCallApprovalId, null);
  await ui.destroy();
});

test('already-dispatching call cannot falsely report successful withdrawal', async () => {
  const client = fakeHuman(async () => queued, async id => ({
    ...queued, state: 'DISPATCHING', revocation_confirmed: false,
  }));
  const ui = await create(client, store());
  assert.equal(await ui.callMe(), true);
  assert.equal(await ui.revokeCallMe(), false);
  assert.equal(ui.state.selfCallApprovalId, APPROVAL);
  assert.match(ui.state.message, /möglicherweise bereits gestartet/);
  await ui.destroy();
});
