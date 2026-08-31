import {
  BookOpenText,
  CheckCircle2,
  ChevronRight,
  Clock3,
  CloudUpload,
  Database,
  Download,
  FileText,
  Layers3,
  LoaderCircle,
  Plus,
  RotateCcw,
  Search,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { EmptyState, ErrorBanner, LoadingBlock, PageHeader, StatusPill, formatRelative } from '../components/UI'
import { api } from '../lib/api'
import { KnowledgeRetryStore, knowledgeUploadBody } from '../lib/knowledgeRetry'
import { usePlatform } from '../context/PlatformContext'
import type {
  KnowledgeBase,
  KnowledgeIngestionJob,
  KnowledgeSearchResult,
} from '../types'

function formatBytes(value?: number | null) {
  if (!value) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function locatorLabel(locator: Record<string, unknown>) {
  const entries = Object.entries(locator).filter(([, value]) => value !== null && value !== undefined)
  return entries.length ? entries.map(([key, value]) => `${key} ${String(value)}`).join(' · ') : 'Document body'
}

export function KnowledgePage() {
  const [bases, setBases] = useState<KnowledgeBase[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [selected, setSelected] = useState<KnowledgeBase | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [job, setJob] = useState<KnowledgeIngestionJob | null>(null)
  const [query, setQuery] = useState('')
  const [topK, setTopK] = useState(8)
  const [searchResult, setSearchResult] = useState<KnowledgeSearchResult | null>(null)
  const [searching, setSearching] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const writing = useRef(false)
  const [searchParams, setSearchParams] = useSearchParams()
  const { context } = usePlatform()

  async function beginRequest(operation: string, body: unknown) {
    if (!context) throw new Error('Wait for your account context to finish loading.')
    return new KnowledgeRetryStore(window.sessionStorage).begin([
      operation, context.tenant.id, context.project.id, context.environment.id, context.user.id,
    ], body)
  }

  const loadBases = useCallback(async (preferredId?: string) => {
    const result = await api.knowledgeBases()
    setBases(result.items)
    setSelectedId((current) => preferredId || current || result.items[0]?.id || '')
    return result.items
  }, [])

  const loadSelected = useCallback(async (id: string) => {
    if (!id) {
      setSelected(null)
      return
    }
    setSelected(await api.knowledgeBase(id))
  }, [])

  useEffect(() => {
    api.knowledgeBases()
      .then((result) => {
        setBases(result.items)
        const requestedId = searchParams.get('base')
        const initial = result.items.find((base) => base.id === requestedId) ?? result.items[0]
        if (initial) setSelectedId(initial.id)
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoaded(true))
  }, [])

  useEffect(() => {
    if (!selectedId) {
      setSelected(null)
      return
    }
    setSearchResult(null)
    const next = new URLSearchParams(searchParams)
    if (next.get('base') !== selectedId) { next.set('base', selectedId); setSearchParams(next, { replace: true }) }
    loadSelected(selectedId).catch((err) => setError(err.message))
  }, [selectedId, loadSelected])

  useEffect(() => {
    if (!job || !['QUEUED', 'RUNNING'].includes(job.status)) return
    const timer = window.setInterval(() => {
      api.knowledgeJob(job.id).then((next) => {
        setJob(next)
        if (!['QUEUED', 'RUNNING'].includes(next.status)) {
          window.clearInterval(timer)
          Promise.all([loadSelected(next.knowledge_base_id), loadBases(next.knowledge_base_id)]).catch((err) => setError(err.message))
        }
      }).catch((err) => {
        window.clearInterval(timer)
        setError(err.message)
      })
    }, 900)
    return () => window.clearInterval(timer)
  }, [job, loadBases, loadSelected])

  async function createBase(event: FormEvent) {
    event.preventDefault()
    if (!name.trim() || writing.current) return
    writing.current = true
    setBusy(true)
    setError('')
    try {
      const body = { name: name.trim(), description: description.trim() }
      const intent = await beginRequest('create-base', body)
      const created = await api.createKnowledgeBase(body, intent.key)
      intent.finish()
      setName('')
      setDescription('')
      setShowCreate(false)
      setSelectedId(created.id)
      setSelected(created)
      await loadBases(created.id)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      writing.current = false
      setBusy(false)
    }
  }

  async function uploadFile(file?: File) {
    if (!file || !selectedId || writing.current) return
    writing.current = true
    setBusy(true)
    setError('')
    setJob(null)
    try {
      const body = await knowledgeUploadBody(file)
      const intent = await beginRequest(`prepare-upload:${selectedId}`, body)
      const preparation = await api.prepareKnowledgeUpload(selectedId, body, intent.key)
      if (preparation.status === 'EXPIRED') {
        intent.finish()
        throw new Error('Upload intent expired. Choose the file again to start a new upload.')
      }
      const uploaded = await api.uploadKnowledgeFile(preparation, file)
      const nextJob = await api.completeKnowledgeUpload(preparation.document_version_id, uploaded.etag)
      intent.finish()
      setJob(nextJob)
      await loadSelected(selectedId)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      writing.current = false
      setBusy(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  async function search(event: FormEvent) {
    event.preventDefault()
    if (!selectedId || !query.trim()) return
    setSearching(true)
    setError('')
    try {
      setSearchResult(await api.searchKnowledge(selectedId, query.trim(), topK))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSearching(false)
    }
  }

  if (!loaded) return <LoadingBlock label="Loading knowledge control plane…" />

  return <div className="page-stack knowledge-page">
    <PageHeader
      eyebrow="RAG CONTROL PLANE"
      title="Ground agents in governed knowledge"
      description="Original files live in object storage; immutable revisions, chunks, retrieval policy, and citations stay under platform control."
      actions={<>
        <button className="button secondary" onClick={() => setShowCreate(true)}><Plus size={16} /> New knowledge base</button>
        <button className="button primary" disabled={!selectedId || busy} onClick={() => fileInput.current?.click()}>
          {busy ? <LoaderCircle className="spin" size={16} /> : <CloudUpload size={16} />} Upload document
        </button>
        <input ref={fileInput} className="hidden-file-input" type="file" accept=".pdf,.docx,.md,.txt,.html,.htm,.json,.csv" onChange={(event) => uploadFile(event.target.files?.[0])} />
      </>}
    />
    {error && <ErrorBanner message={error} />}
    {job && <div className={`ingestion-banner ${job.status.toLowerCase()}`}>
      <div className="ingestion-icon">{job.status === 'SUCCEEDED' ? <CheckCircle2 size={18} /> : job.status === 'FAILED' ? <Clock3 size={18} /> : <LoaderCircle className="spin" size={18} />}</div>
      <div><strong>Document ingestion · {job.stage.replaceAll('_', ' ')}</strong><span>{job.status === 'FAILED' ? job.error_message : `${job.chunk_count ?? 0} chunks · attempt ${job.attempts}`}</span></div>
      <StatusPill status={job.status} />
      {job.status === 'FAILED' && <button className="button secondary" disabled={busy} onClick={async () => { setBusy(true); try { setJob(await api.retryKnowledgeJob(job.id)) } catch (nextError) { setError((nextError as Error).message) } finally { setBusy(false) } }}><RotateCcw size={14} /> Retry</button>}
    </div>}

    <div className="knowledge-layout">
      <aside className="panel knowledge-sidebar">
        <div className="panel-heading"><div><h3>Knowledge bases</h3><p>{bases.length} project-scoped corpora</p></div><Database size={18} /></div>
        <div className="knowledge-base-list">
          {bases.map((base) => <button key={base.id} className={`knowledge-base-item ${base.id === selectedId ? 'selected' : ''}`} onClick={() => setSelectedId(base.id)}>
            <div className="knowledge-base-icon"><BookOpenText size={17} /></div>
            <div><strong>{base.name}</strong><span>{base.ready_document_count ?? 0} / {base.document_count ?? 0} documents ready</span></div>
            <ChevronRight size={15} />
          </button>)}
          {!bases.length && <div className="knowledge-sidebar-empty"><Database size={22} /><span>Create a knowledge base to begin.</span></div>}
        </div>
        <div className="knowledge-boundary"><ShieldCheck size={15} /><div><strong>Project isolated</strong><span>{context ? `${context.tenant.id} / ${context.project.id}` : 'Context unavailable'}</span></div></div>
      </aside>

      <div className="knowledge-main">
        {!selected ? <section className="panel"><EmptyState icon={Database} title="No knowledge base selected" description="Create a corpus, then upload a source document for indexing." action={<button className="button primary" onClick={() => setShowCreate(true)}><Plus size={15} /> Create knowledge base</button>} /></section> : <>
          <section className="panel knowledge-overview">
            <div className="panel-heading"><div><div className="knowledge-title-line"><h3>{selected.name}</h3><StatusPill status={selected.status} /></div><p>{selected.description || 'No description provided.'}</p></div><div className="revision-chip"><Layers3 size={14} />{selected.revisions?.[0] ? `Revision ${selected.revisions[0].revision_number}` : 'Awaiting first revision'}</div></div>
            <div className="knowledge-stats">
              <div><span>Total documents</span><strong>{selected.documents?.length ?? 0}</strong><small>source objects</small></div>
              <div><span>Ready to retrieve</span><strong>{selected.documents?.filter((item) => item.status === 'READY').length ?? 0}</strong><small>ACL-filtered</small></div>
              <div><span>Immutable revisions</span><strong>{selected.revisions?.length ?? 0}</strong><small>publishable snapshots</small></div>
              <div><span>Storage</span><strong className="storage-value">{selected.documents?.[0]?.canonical_uri?.split(':')[0]?.toUpperCase() ?? '—'}</strong><small>{selected.documents?.length ? 'Authoritative source objects' : 'No source uploaded'}</small></div>
            </div>
          </section>

          <section className="panel knowledge-documents">
            <div className="panel-heading"><div><h3>Source documents</h3><p>Object metadata, ingestion health, and active revision membership</p></div><button className="button ghost" disabled={busy} onClick={() => fileInput.current?.click()}><CloudUpload size={15} /> Add source</button></div>
            {selected.documents?.length ? <div className="knowledge-table-wrap"><table className="knowledge-table"><thead><tr><th>Document</th><th>Status</th><th>Size</th><th>Indexed</th><th>Source</th></tr></thead><tbody>{selected.documents.map((document) => <tr key={document.id}>
              <td><div className="document-name"><div><FileText size={16} /></div><span><strong>{document.display_name}</strong><small>{document.content_type ?? 'pending metadata'}</small></span></div></td>
              <td><StatusPill status={document.status} /></td>
              <td>{formatBytes(document.size_bytes)}</td>
              <td>{document.indexed_at ? formatRelative(document.indexed_at) : '—'}</td>
              <td><a className={document.current_version_id ? 'download-link' : 'download-link disabled'} href={document.current_version_id ? `/api/v1/knowledge-documents/${document.id}/download` : undefined}><Download size={14} /> Download</a></td>
            </tr>)}</tbody></table></div> : <EmptyState icon={FileText} title="No source documents yet" description="Upload PDF, DOCX, Markdown, text, HTML, JSON, or CSV. The worker will parse, chunk, embed, and publish a revision." />}
          </section>

          <section className="panel retrieval-lab">
            <div className="panel-heading"><div><h3>Retrieval lab</h3><p>Exercise the same hybrid retriever and citation contract used by knowledge_search</p></div><Sparkles size={18} /></div>
            <form className="retrieval-form" onSubmit={search}><div className="retrieval-query"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ask a question grounded in this knowledge base…" /></div><label className="top-k-field"><span>Top K</span><input type="number" min={1} max={20} value={topK} onChange={(event) => setTopK(Math.max(1, Math.min(20, Number(event.target.value))))} /></label><button className="button primary" disabled={searching || !query.trim()}>{searching ? <LoaderCircle className="spin" size={15} /> : <Search size={15} />} Retrieve</button></form>
            {searchResult && <div className="retrieval-results">
              <div className="retrieval-summary"><span><strong>{searchResult.hits.length}</strong> evidence chunks</span><span>{searchResult.latency_ms} ms</span><StatusPill status={searchResult.status} /></div>
              {searchResult.hits.map((hit) => <article className="retrieval-hit" key={hit.chunk_id}><div className="citation-badge">{hit.citation_id}</div><div><div className="retrieval-hit-head"><strong>{hit.source.title}</strong><span>{locatorLabel(hit.source.locator)} · score {hit.score.toFixed(3)}</span></div><p>{hit.text}</p><code>{hit.source.canonical_uri}</code></div></article>)}
              {!searchResult.hits.length && <div className="retrieval-empty">No admissible evidence was found in the active revision.</div>}
            </div>}
          </section>
        </>}
      </div>
    </div>

    {showCreate && <div className="modal-backdrop" onMouseDown={() => setShowCreate(false)}><div className="modal" onMouseDown={(event) => event.stopPropagation()}>
      <div className="modal-heading"><div><span className="page-eyebrow">NEW CORPUS</span><h3>Create knowledge base</h3><p>Documents added here will produce immutable retrieval revisions.</p></div><Database size={20} /></div>
      <form onSubmit={createBase}><div className="form-stack"><label>Name<input autoFocus value={name} onChange={(event) => setName(event.target.value)} placeholder="Engineering handbook" minLength={2} maxLength={120} /></label><label>Description<textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={3} placeholder="Policies, architecture decisions, and operating guides" /></label></div><div className="modal-actions"><button type="button" className="button secondary" onClick={() => setShowCreate(false)}>Cancel</button><button className="button primary" disabled={busy || name.trim().length < 2}>{busy && <LoaderCircle className="spin" size={14} />} Create</button></div></form>
    </div></div>}
  </div>
}
