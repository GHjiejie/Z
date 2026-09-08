import assert from 'node:assert/strict'
import test from 'node:test'
import { KnowledgeRetryStore, knowledgeUploadBody, uploadHeaders } from '../src/lib/knowledgeRetry.ts'

test('browser upload validates signed length without setting forbidden headers', () => {
  assert.deepEqual(uploadHeaders({ 'Content-Length': '7', 'Content-Type': 'text/plain' }, 7), { 'Content-Type': 'text/plain' })
  assert.deepEqual(uploadHeaders({ 'content-length': '7' }, 7), {})
  assert.throws(() => uploadHeaders({ 'Content-Length': '8' }, 7), /size/)
})

function storage() {
  const rows = new Map()
  return { rows, getItem: (key) => rows.get(key) ?? null,
    setItem: (key, value) => rows.set(key, value), removeItem: (key) => rows.delete(key) }
}

const scope = ['create-base', 'tenant', 'project', 'environment', 'author']
const body = { name: 'Sensitive title', description: 'Private description' }

test('failed response, reload and concurrent submissions retain one request key', async () => {
  const persisted = storage()
  const first = await new KnowledgeRetryStore(persisted).begin(scope, body)
  const results = await Promise.all(Array.from({ length: 8 }, () => new KnowledgeRetryStore(persisted).begin(scope, body)))
  assert.ok(results.every((result) => result.key === first.key))
  assert.equal(persisted.rows.size, 1)
  const serialized = JSON.stringify([...persisted.rows])
  assert.ok(!serialized.includes(body.name) && !serialized.includes(body.description))
})

test('only confirmed completion permits a new identical intent', async () => {
  const persisted = storage()
  const store = new KnowledgeRetryStore(persisted)
  const first = await store.begin(scope, body)
  first.finish()
  const next = await store.begin(scope, body)
  assert.notEqual(next.key, first.key)
  first.finish() // A delayed old callback must not erase the next action.
  assert.equal((await store.begin(scope, body)).key, next.key)
})

test('actor, tenant, project, environment, operation and body are isolated', async () => {
  const store = new KnowledgeRetryStore(storage())
  const first = await store.begin(scope, body)
  for (let index = 0; index < scope.length; index++) {
    const other = [...scope]
    other[index] = 'different'
    assert.notEqual((await store.begin(other, body)).key, first.key)
  }
  assert.notEqual((await store.begin(scope, { ...body, description: 'Changed' })).key, first.key)
})

test('file bytes distinguish retries even with identical name, size and timestamp', async () => {
  const store = new KnowledgeRetryStore(storage())
  const firstFile = new File(['abc'], 'source.txt', { type: 'text/plain', lastModified: 0 })
  const sameFile = new File(['abc'], 'source.txt', { type: 'text/plain', lastModified: 0 })
  const otherFile = new File(['xyz'], 'source.txt', { type: 'text/plain', lastModified: 0 })
  const first = await store.begin(scope, await knowledgeUploadBody(firstFile))
  assert.equal((await store.begin(scope, await knowledgeUploadBody(sameFile))).key, first.key)
  assert.notEqual((await store.begin(scope, await knowledgeUploadBody(otherFile))).key, first.key)
  assert.equal((await knowledgeUploadBody(new File(['x'], 'raw.txt'))).content_type, 'application/octet-stream')
})

test('unavailable or corrupted storage fails closed before submission', async () => {
  for (const method of ['getItem', 'setItem']) {
    const persisted = storage()
    persisted[method] = () => { throw new Error('Storage unavailable') }
    await assert.rejects(new KnowledgeRetryStore(persisted).begin(scope, body), /Storage unavailable/)
  }
  const persisted = storage()
  await new KnowledgeRetryStore(persisted).begin(scope, body)
  persisted.rows.set([...persisted.rows.keys()][0], 'invalid\nkey')
  await assert.rejects(new KnowledgeRetryStore(persisted).begin(scope, body), /retry state is invalid/)
})
