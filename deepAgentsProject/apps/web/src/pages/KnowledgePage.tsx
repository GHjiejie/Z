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
  Search,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { EmptyState, ErrorBanner, LoadingBlock, PageHeader, StatusPill, formatRelative } from '../components/UI'
import { api } from '../lib/api'
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
  const [searchResult, setSearchResult] = useState<KnowledgeSearchResult | null>(null)
  const [searching, setSearching] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

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
        if (result.items[0]) setSelectedId(result.items[0].id)
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
    if (!name.trim()) return
    setBusy(true)
    setError('')
    try {
      const created = await api.createKnowledgeBase({ name: name.trim(), description: description.trim() })
      setName('')
      setDescription('')
      setShowCreate(false)
      setSelectedId(created.id)
      setSelected(created)
      await loadBases(created.id)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function uploadFile(file?: File) {
    if (!file || !selectedId) return
    setBusy(true)
    setError('')
    setJob(null)
    try {
      const preparation = await api.prepareKnowledgeUpload(selectedId, file)
      const uploaded = await api.uploadKnowledgeFile(preparation, file)
      const nextJob = await api.completeKnowledgeUpload(preparation.document_version_id, uploaded.etag)
      setJob(nextJob)
      await loadSelected(selectedId)
    } catch (err) {
      setError((err as Error).message)
    } finally {
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
      setSearchResult(await api.searchKnowledge(selectedId, query.trim()))
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
        <div className="knowledge-boundary"><ShieldCheck size={15} /><div><strong>Project isolated</strong><span>tenant_demo / project_atlas</span></div></div>
      </aside>

      <div className="knowledge-main">
        {!selected ? <section className="panel"><EmptyState icon={Database} title="No knowledge base selected" description="Create a corpus, then upload a source document for indexing." action={<button className="button primary" onClick={() => setShowCreate(true)}><Plus size={15} /> Create knowledge base</button>} /></section> : <>
          <section className="panel knowledge-overview">
            <div className="panel-heading"><div><div className="knowledge-title-line"><h3>{selected.name}</h3><StatusPill status={selected.status} /></div><p>{selected.description || 'No description provided.'}</p></div><div className="revision-chip"><Layers3 size={14} />{selected.revisions?.[0] ? `Revision ${selected.revisions[0].revision_number}` : 'Awaiting first revision'}</div></div>
            <div className="knowledge-stats">
              <div><span>Total documents</span><strong>{selected.documents?.length ?? 0}</strong><small>source objects</small></div>
              <div><span>Ready to retrieve</span><strong>{selected.documents?.filter((item) => item.status === 'READY').length ?? 0}</strong><small>ACL-filtered</small></div>
              <div><span>Immutable revisions</span><strong>{selected.revisions?.length ?? 0}</strong><small>publishable snapshots</small></div>
              <div><span>Storage</span><strong className="storage-value">OSS</strong><small>cn-beijing</small></div>
            </div>
          </section>

          <section className="panel knowledge-documents">
            <div className="panel-heading"><div><h3>Source documents</h3><p>Object metadata, ingestion health, and active revision membership</p></div><button className="button ghost" disabled={busy} onClick={() => fileInput.current?.click()}><CloudUpload size={15} /> Add source</button></div>
            {selected.documents?.length ? <div className="knowledge-table-wrap"><table className="knowledge-table"><thead><tr><th>Document</th><th>Status</th><th>Size</th><th>Indexed</th><th>Source</th></tr></thead><tbody>{selected.documents.map((document) => <tr key={document.id}>
              <td><div className="document-name"><div><FileText size={16} /></div><span><strong>{document.display_name}</strong><small>{document.content_type ?? 'pending metadata'}</small></span></div></td>
              <td><StatusPill status={document.status} /></td>
              <td>{formatBytes(document.size_bytes)}</td>
              <td>{document.indexed_at ? formatRelative(document.indexed_at) : '—'}</td>
              <td><a className={document.current_version_id ? 'download-link' : 'download-link disabled'} href={document.current_version_id ? `/api/v1/knowledge-documents/${document.id}/download` : undefined}><Download size={14} /> OSS</a></td>
            </tr>)}</tbody></table></div> : <EmptyState icon={FileText} title="No source documents yet" description="Upload PDF, DOCX, Markdown, text, HTML, JSON, or CSV. The worker will parse, chunk, embed, and publish a revision." />}
          </section>

          <section className="panel retrieval-lab">
            <div className="panel-heading"><div><h3>Retrieval lab</h3><p>Exercise the same hybrid retriever and citation contract used by knowledge_search</p></div><Sparkles size={18} /></div>
            <form className="retrieval-form" onSubmit={search}><div className="retrieval-query"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ask a question grounded in this knowledge base…" /></div><button className="button primary" disabled={searching || !query.trim()}>{searching ? <LoaderCircle className="spin" size={15} /> : <Search size={15} />} Retrieve</button></form>
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
