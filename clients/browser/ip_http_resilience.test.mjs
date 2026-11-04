import test from 'node:test';
import assert from 'node:assert/strict';
import { IPCallClient } from './ip_calls_client.mjs';

const create = (opts = {}) => new IPCallClient({
  apiBase: 'https://calls.example.test',
  identityToken: 'private-test-bearer',
  fetchImpl: async () => ({
    status: 200, ok: true, json: async () => ({ ok: true }),
  }),
  ...opts,
});

test('bearer cannot be sent to remote non-TLS origin or URL userinfo', () => {
  for (const apiBase of [
    'http://calls.example.test', 'https://user:password@calls.example.test',
    'https://calls.example.test/?token=secret',
    'https://calls.example.test/#invalid',
    'file:///tmp/calls', '//calls.example.test',
  ]) {
    assert.throws(() => create({ apiBase }), /invalid_call_api_origin/);
  }
  assert.doesNotThrow(() => create({
    apiBase: 'http://127.0.0.1:8874',
  }));
  assert.doesNotThrow(() => create({
    apiBase: 'http://localhost:8874',
  }));
  assert.doesNotThrow(() => create({
    apiBase: 'https://calls.example.test/v1',
  }));
});

test('HTTP 503 with empty body remains explicit retriable server failure', async () => {
  const client = create({
    fetchImpl: async (url, req) => {
      assert.equal(req.redirect, 'error');
      assert.equal(req.cache, 'no-store');
      assert.equal(req.headers.Authorization, 'Bearer private-test-bearer');
      assert.ok(req.signal instanceof AbortSignal);
      return {
        status: 503, ok: false,
        json: async () => { throw new SyntaxError('empty body'); },
      };
    },
  });
  await assert.rejects(client.request('GET', '/v1/ip-calls/events'), err => {
    assert.equal(err.message, 'ip_call_http_error');
    assert.equal(err.status, 503);
    return true;
  });
});

test('timeout cancels HTTP request and marks uncertain mutations', async () => {
  let stopped = 0;
  const hanging = (url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => {
      stopped++;
      const error = new Error('request aborted');
      error.name = 'AbortError';
      reject(error);
    }, { once: true });
  });
  const client = create({ fetchImpl: hanging, requestTimeoutMs: 1000 });
  await assert.rejects(
    client.request('POST', '/v1/ip-calls', { idempotency_key: 'fixed-key' }),
    error => {
      assert.equal(error.message, 'ip_call_request_timeout');
      assert.equal(error.outcomeUncertain, true);
      return true;
    },
  );
  await assert.rejects(
    client.request('GET', '/v1/ip-calls/events'),
    error => {
      assert.equal(error.message, 'ip_call_request_timeout');
      assert.equal(error.outcomeUncertain, false);
      return true;
    },
  );
  assert.equal(stopped, 2);
});

test('invalid local operations and invalid request timeout fail closed', async () => {
  for (const requestTimeoutMs of [0, 999, 30001, NaN, true]) {
    assert.throws(
      () => create({ requestTimeoutMs }),
      /invalid_call_request_timeout/,
    );
  }
  const client = create();
  await assert.rejects(
    client.request('POST', '//evil.example.test'),
    /invalid_call_api_request/,
  );
  await assert.rejects(
    client.request('PATCH', '/v1/ip-calls'),
    /invalid_call_api_request/,
  );
});

test('a malformed successful response is not treated as a valid call', async () => {
  const client = create({
    fetchImpl: async () => ({
      ok: true, status: 200,
      json: async () => { throw new SyntaxError('garbled body'); },
    }),
  });
  await assert.rejects(
    client.request('GET', '/v1/ip-calls/active'),
    /ip_call_invalid_json_response/,
  );
});
