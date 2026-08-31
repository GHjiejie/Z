import assert from 'node:assert/strict'
import test from 'node:test'
import { requestIdentity, errorWithRequestIdentity } from '../src/lib/requestIdentity.ts'

test('safe server request ID is retained for support without replacing the error', () => {
  const id = 'a123'.repeat(8)
  assert.equal(requestIdentity(id), id)
  assert.equal(errorWithRequestIdentity('Request failed', id), `Request failed (Request ID: ${id})`)
})

test('missing or malformed IDs never reach the visible error', () => {
  for (const value of [null, undefined, '', 'customer@example.com', 'a'.repeat(33), 'a'.repeat(32) + '\nsecret']) {
    assert.equal(requestIdentity(value), undefined)
    assert.equal(errorWithRequestIdentity('Request failed', value), 'Request failed')
  }
})
