import test from 'node:test';
import assert from 'node:assert/strict';
import { IPCallClient, callReportDailyCsv } from './ip_calls_client.mjs';

const metrics = {
  calls: 2, incoming: 1, outgoing: 1, accepted_confirmed: 1,
  declined: 1, timed_out: 0, transferred_confirmed: 0,
  control_session_seconds: 67,
};
const sample = {
  schema: 'porcelainberryme.reporting.calls.v1',
  scope: 'authenticated_participant_only', timezone: 'UTC',
  metrics,
  daily: [{
    day: '2026-11-04', ...metrics,
  }],
};

test('authenticated participant-only reporting uses existing bearer header', async () => {
  const requests = [];
  const client = new IPCallClient({
    apiBase: 'http://127.0.0.1:8874',
    identityToken: 'scoped-test-token',
    fetchImpl: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, json: async () => sample };
    },
  });
  assert.deepEqual(await client.callReport(), sample);
  assert.deepEqual(await client.callReport({
    start: 1800000000, end: 1800001000,
  }), sample);
  assert.equal(requests[0].url, 'http://127.0.0.1:8874/v1/ip-calls/report');
  assert.match(requests[1].url, /start=1800000000&end=1800001000/);
  for (const request of requests) {
    assert.equal(request.options.headers.Authorization, 'Bearer scoped-test-token');
    assert.equal(request.options.cache, 'no-store');
    assert.doesNotMatch(request.url, /scoped-test-token/);
  }
});

test('client rejects account or date injection before HTTP request', async () => {
  let calls = 0;
  const client = new IPCallClient({
    apiBase: 'http://localhost:8874', identityToken: 'token',
    fetchImpl: async () => { calls++; return { ok: true, json: async () => sample }; },
  });
  for (const window of [
    null, { tenant: 'other' }, { start: 1 }, { end: 2 },
    { start: -1, end: 4 }, { start: 7, end: 7 },
    { start: 1.5, end: 3 }, { start: 1, end: 999999999 },
  ]) {
    await assert.rejects(client.callReport(window), /invalid_call_reporting_window/);
  }
  assert.equal(calls, 0);
});

test('CSV contains only daily numeric aggregates and actual newlines', () => {
  const csv = callReportDailyCsv(sample);
  assert.equal(csv.split('\n').length, 3);
  assert.match(csv, /^day;calls;incoming;outgoing;/);
  assert.match(csv, /2026-11-04;2;1;1;1;1;0;0;67\n$/);
  assert.doesNotMatch(csv, /bearer|token|peer|contact|transcript/i);
  assert.throws(() => callReportDailyCsv({
    ...sample, daily: [{ ...sample.daily[0], calls: '=HYPERLINK(1)' }],
  }), /invalid_call_reporting_response/);
  assert.throws(() => callReportDailyCsv({
    ...sample, daily: [{ ...sample.daily[0], day: '=2+2' }],
  }), /invalid_call_reporting_response/);
});
