import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { apiErrorMessage, categoryLabel, DEFAULT_LOCALE, formatDateTime, t } from './locale.js'
import './style.css'

document.documentElement.lang = DEFAULT_LOCALE

const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/+$/, '')
const API = configuredApiBase || (import.meta.env.DEV ? '/api' : 'https://ai-wiki-poc.onrender.com')

async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${API}${path}`, {
      ...options,
      credentials: options.credentials || 'same-origin',
    })
  } catch {
    throw new Error(t('apiUnavailable'))
  }
  if (!response.ok) {
    let detail
    try {
      const body = await response.json()
      detail = body.detail
    } catch { /* Keep the HTTP status message when the response is not JSON. */ }
    throw new Error(apiErrorMessage(detail, response.status))
  }
  if (response.status === 204) return null
  return response.json()
}

function Loading({ label = t('loading') }) {
  return <p className="muted loading">{label}…</p>
}

function safeLink(value) {
  try {
    const url = new URL(value, window.location.href)
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : null
  } catch {
    return null
  }
}

function renderInline(text, prefix) {
  const pattern = /(`[^`]+`|!?\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*|__[^_]+__|~~[^~]+~~|\*[^*]+\*|_[^_]+_)/g
  const pieces = []
  let cursor = 0
  let match
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) pieces.push(text.slice(cursor, match.index))
    const token = match[0]
    if (token.startsWith('`')) {
      pieces.push(<code key={`${prefix}-${match.index}`}>{token.slice(1, -1)}</code>)
    } else if (token.startsWith('![')) {
      const image = token.match(/^!\[([^\]]*)\]\(([^)]+)\)$/)
      pieces.push(image ? <span key={`${prefix}-${match.index}`} className="muted">[{t('image')}: {image[1] || image[2]}]</span> : token)
    } else if (token.startsWith('[')) {
      const link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
      const href = link && safeLink(link[2])
      pieces.push(href
        ? <a key={`${prefix}-${match.index}`} href={href} target="_blank" rel="noreferrer">{link[1]}</a>
        : (link?.[1] || token))
    } else if (token.startsWith('**') || token.startsWith('__')) {
      pieces.push(<strong key={`${prefix}-${match.index}`}>{token.slice(2, -2)}</strong>)
    } else if (token.startsWith('~~')) {
      pieces.push(<del key={`${prefix}-${match.index}`}>{token.slice(2, -2)}</del>)
    } else {
      pieces.push(<em key={`${prefix}-${match.index}`}>{token.slice(1, -1)}</em>)
    }
    cursor = pattern.lastIndex
  }
  if (cursor < text.length) pieces.push(text.slice(cursor))
  return pieces
}

function MarkdownPreview({ content }) {
  const lines = content.replace(/\r\n?/g, '\n').split('\n')
  const blocks = []
  let index = 0
  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) { index += 1; continue }

    if (line.trimStart().startsWith('```') || line.trimStart().startsWith('~~~')) {
      const fence = line.trimStart().slice(0, 3)
      const language = line.trimStart().slice(3).trim()
      const code = []
      index += 1
      while (index < lines.length && !lines[index].trimStart().startsWith(fence)) code.push(lines[index++])
      if (index < lines.length) index += 1
      blocks.push(<pre key={`code-${index}`} data-language={language}><code>{code.join('\n')}</code></pre>)
      continue
    }

    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/)
    if (heading) {
      const Tag = `h${heading[1].length}`
      blocks.push(<Tag key={`heading-${index}`}>{renderInline(heading[2], `heading-${index}`)}</Tag>)
      index += 1
      continue
    }
    if (/^\s{0,3}([-*_]\s*){3,}$/.test(line)) {
      blocks.push(<hr key={`rule-${index}`} />)
      index += 1
      continue
    }
    if (/^\s{0,3}>/.test(line)) {
      const quote = []
      while (index < lines.length && /^\s{0,3}>/.test(lines[index])) quote.push(lines[index++].replace(/^\s{0,3}>\s?/, ''))
      blocks.push(<blockquote key={`quote-${index}`}>{quote.map((item, i) => <p key={i}>{renderInline(item, `quote-${index}-${i}`)}</p>)}</blockquote>)
      continue
    }
    const listMatch = line.match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/)
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1])
      const items = []
      while (index < lines.length) {
        const item = lines[index].match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/)
        if (!item || /^\d/.test(item[1]) !== ordered) break
        const task = item[2].match(/^\[([ xX])\]\s+(.*)$/)
        items.push(task
          ? <li key={index} className="task-item"><input type="checkbox" checked={task[1].toLowerCase() === 'x'} readOnly />{renderInline(task[2], `task-${index}`)}</li>
          : <li key={index}>{renderInline(item[2], `list-${index}`)}</li>)
        index += 1
      }
      const List = ordered ? 'ol' : 'ul'
      blocks.push(<List key={`list-${index}`}>{items}</List>)
      continue
    }

    const paragraph = [line]
    index += 1
    while (index < lines.length && lines[index].trim() && !/^\s{0,3}(#{1,6}\s|>|```|~~~|[-+*]\s+|\d+[.)]\s+)/.test(lines[index])) paragraph.push(lines[index++])
    blocks.push(<p key={`paragraph-${index}`}>{paragraph.map((part, i) => <React.Fragment key={i}>{i > 0 && <br />}{renderInline(part, `paragraph-${index}-${i}`)}</React.Fragment>)}</p>)
  }
  return <article className="markdown-preview">{blocks.length ? blocks : <p className="muted">{t('nothingToPreview')}</p>}</article>
}

function ConfirmationDialog({ title, message, confirmLabel, onCancel, onConfirm, danger = false }) {
  return (
    <div className="dialog-backdrop">
      <section className="confirmation-dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirmation-title" aria-describedby="confirmation-message">
        <p className="eyebrow">{t('confirm')}</p>
        <h2 id="confirmation-title">{title}</h2>
        <p id="confirmation-message" className="muted">{message}</p>
        <div className="dialog-actions"><button type="button" className="button-secondary" onClick={onCancel}>{t('cancel')}</button><button type="button" className={danger ? 'button-danger' : ''} onClick={onConfirm}>{confirmLabel}</button></div>
      </section>
    </div>
  )
}

function LegacyNotePreview({ note }) {
  const blocks = (note.summary || '').split(/\n{2,}/)
  return (
    <article className="markdown-preview">
      <p className="eyebrow">{categoryLabel(note.primary_category)}</p>
      <h2>{note.title}</h2>
      {blocks.map((block, index) => <p key={index}>{block}</p>)}
      {note.key_facts?.length > 0 && <><h3>{t('keyFacts')}</h3><ul>{note.key_facts.map(fact => <li key={fact}>{fact}</li>)}</ul></>}
      {note.processed_at && <p className="muted">{formatDateTime(note.processed_at)}</p>}
      <p className="muted document-path">{note.relative_path}</p>
    </article>
  )
}

function DashboardScreen() {
  const [stats, setStats] = useState(null)
  const [recent, setRecent] = useState([])
  const [selected, setSelected] = useState(null)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState(null)
  const [semanticQuery, setSemanticQuery] = useState('')
  const [semanticResults, setSemanticResults] = useState(null)
  const [chatQuestion, setChatQuestion] = useState('')
  const [chatAnswer, setChatAnswer] = useState('')
  const [chatSources, setChatSources] = useState([])
  const [chatLoading, setChatLoading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const refresh = async () => {
    try {
      const [nextStats, nextRecent] = await Promise.all([request('/stats'), request('/recent')])
      setStats(nextStats)
      setRecent(nextRecent)
    } catch (err) { setError(err.message || t('apiUnavailable')) }
    finally { setLoading(false) }
  }
  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, 5000)
    return () => clearInterval(timer)
  }, [])

  const search = async (event) => {
    event.preventDefault()
    if (!query.trim()) { setResults(null); return }
    try { setError(''); setResults(await request(`/search?q=${encodeURIComponent(query.trim())}`)) }
    catch (err) { setError(err.message || t('searchFailed')) }
  }
  const openNote = async (note) => {
    try { setError(''); setSelected(await request(`/note/${encodeURIComponent(note.source_id || note.id)}`)) }
    catch (err) { setError(err.message || t('loadNoteFailed')) }
  }
  const semanticSearch = async (event) => {
    event.preventDefault()
    if (!semanticQuery.trim()) { setSemanticResults(null); return }
    try { setError(''); setSemanticResults(await request(`/semantic-search?q=${encodeURIComponent(semanticQuery.trim())}`)) }
    catch (err) { setError(err.message || t('semanticSearchFailed')) }
  }
  const chat = async (event) => {
    event.preventDefault()
    if (!chatQuestion.trim()) return
    setChatLoading(true)
    try {
      setError('')
      const data = await request('/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: chatQuestion.trim() }),
      })
      setChatAnswer(data.answer)
      setChatSources(data.sources)
    } catch (err) { setError(err.message || t('chatFailed')) }
    finally { setChatLoading(false) }
  }

  const list = results ?? recent
  return (
    <div className="screen-content">
      <header className="page-heading">
        <div><p className="eyebrow">{t('dashboardEyebrow')}</p><h1>{t('dashboardTitle')}</h1><p className="muted">{t('dashboardDescription')}</p></div>
        <span className="live-indicator"><span />{t('autoRefresh')}</span>
      </header>
      {error && <div className="notice notice-error" role="alert">{error}</div>}
      <section className="stats-grid">
        {['documents', 'ai', 'investment', 'philosophy'].map((key, index) => <div className="panel stat-card" key={key}><span>{index === 0 ? t('indexedDocuments') : categoryLabel(key)}</span><strong>{loading ? '—' : index === 0 ? stats?.documents ?? 0 : stats?.categories?.[key] ?? 0}</strong></div>)}
      </section>
      <section className="dashboard-grid">
        <div className="panel dashboard-sidebar">
          <form onSubmit={search} className="form-row"><input aria-label={t('searchNotes')} value={query} onChange={e => setQuery(e.target.value)} placeholder={t('searchPlaceholder')} /><button type="submit">{t('search')}</button></form>
          <form onSubmit={semanticSearch} className="semantic-form"><p className="eyebrow">{t('semanticSearch')}</p><div className="form-row"><input aria-label={t('semanticSearchAria')} value={semanticQuery} onChange={e => setSemanticQuery(e.target.value)} placeholder={t('askByMeaning')} /><button type="submit">{t('find')}</button></div></form>
          {semanticResults && <div className="result-group"><div className="group-heading"><p className="eyebrow">{t('similarNotes')}</p><button className="text-button" onClick={() => setSemanticResults(null)}>{t('clear')}</button></div>{semanticResults.length === 0 ? <p className="muted">{t('noSemanticMatches')}</p> : semanticResults.map(item => <button className="note-row" key={item.id} onClick={() => openNote(item)}><strong>{item.title}</strong><small>{Math.round(item.score * 100)}% {t('match')} · {item.snippet}</small></button>)}</div>}
          <div className="group-heading"><p className="eyebrow">{results ? t('searchResults') : t('recentDocuments')}</p>{results && <button className="text-button" onClick={() => setResults(null)}>{t('clear')}</button>}</div>
          {loading ? <Loading label={t('loadingDocuments')} /> : list.length === 0 ? <p className="muted">{results ? t('noSearchResults') : t('noDocumentsFound')}</p> : list.map(note => <button className={`note-row ${selected?.source_id === note.source_id ? 'selected' : ''}`} key={note.source_id} onClick={() => openNote(note)}><strong>{note.title}</strong><small>{note.relative_path}{note.processed_at ? ` · ${formatDateTime(note.processed_at)}` : ''}</small></button>)}
        </div>
        <div className="panel dashboard-preview">{selected ? <LegacyNotePreview note={selected} /> : <div className="empty-state"><span>↗</span><h2>{t('selectDocument')}</h2><p>{t('selectDocumentDescription')}</p></div>}</div>
      </section>
      <section className="panel chat-panel"><p className="eyebrow">{t('ragChat')}</p><h2>{t('askKnowledgeBase')}</h2><form onSubmit={chat} className="form-row chat-form"><input aria-label={t('chatQuestion')} value={chatQuestion} onChange={e => setChatQuestion(e.target.value)} placeholder={t('askAboutNotes')} /><button type="submit" disabled={chatLoading}>{chatLoading ? t('thinking') : t('ask')}</button></form>{chatAnswer && <div className="chat-result"><div className="answer-box">{chatAnswer}</div><div><p className="eyebrow">{t('sources')}</p>{chatSources.map(source => <button className="note-row" key={source.id} onClick={() => openNote(source)}><strong>{source.title}</strong><small>{Math.round(source.score * 100)}% {t('match')}</small></button>)}</div></div>}</section>
    </div>
  )
}

function DocumentsScreen({ dirtyRef }) {
  const [authState, setAuthState] = useState('checking')
  const [authConfigured, setAuthConfigured] = useState(true)
  const [password, setPassword] = useState('')
  const [authBusy, setAuthBusy] = useState(false)
  const [authError, setAuthError] = useState('')
  const [documents, setDocuments] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [draft, setDraft] = useState({ title: '', tags: '', content: '' })
  const [baseline, setBaseline] = useState(null)
  const [mode, setMode] = useState('edit')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [confirmation, setConfirmation] = useState(null)
  const draftKey = JSON.stringify(draft)
  const dirty = baseline !== null && draftKey !== baseline
  dirtyRef.current = dirty

  const refreshDocuments = async () => {
    setLoading(true)
    try { setDocuments(await request('/documents', { credentials: 'include' })); setError('') }
    catch (err) { setError(err.message || t('loadDocumentsFailed')) }
    finally { setLoading(false) }
  }
  useEffect(() => {
    request('/auth/status', { credentials: 'include' })
      .then(result => {
        setAuthConfigured(result.configured)
        setAuthState(result.authenticated ? 'authenticated' : 'anonymous')
      })
      .catch(err => { setAuthError(err.message || t('checkSignInFailed')); setAuthState('anonymous') })
  }, [])
  useEffect(() => {
    if (authState === 'authenticated') refreshDocuments()
  }, [authState])

  const signIn = async (event) => {
    event.preventDefault()
    setAuthBusy(true)
    setAuthError('')
    try {
      await request('/login', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Protection': '1' },
        body: JSON.stringify({ password }),
      })
      setPassword('')
      setAuthState('authenticated')
    } catch (err) { setAuthError(err.message || t('signInFailed')) }
    finally { setAuthBusy(false) }
  }
  const signOut = async () => {
    setAuthBusy(true)
    setAuthError('')
    try {
      await request('/logout', {
        method: 'POST', credentials: 'include',
        headers: { 'X-CSRF-Protection': '1' },
      })
      setAuthState('anonymous')
      setDocuments([])
      setActiveId(null)
      setBaseline(null)
    } catch (err) { setAuthError(err.message || t('signOutFailed')) }
    finally { setAuthBusy(false) }
  }

  const startNew = () => {
    setActiveId(null)
    const blank = { title: '', tags: '', content: '' }
    setDraft(blank)
    setBaseline(JSON.stringify(blank))
    setMode('edit')
    setError('')
    setStatus(t('newDocument'))
  }
  const openNew = () => {
    if (dirty) { setConfirmation({ kind: 'new' }); return }
    startNew()
  }
  const loadDocument = async (id) => {
    setLoading(true)
    setError('')
    setStatus('')
    try {
      const document = await request(`/documents/${encodeURIComponent(id)}`, { credentials: 'include' })
      const next = { title: document.title, tags: document.tags.join(', '), content: document.content }
      setDraft(next)
      setBaseline(JSON.stringify(next))
      setActiveId(document.id)
      setMode('edit')
    } catch (err) { setError(err.message || t('loadDocumentFailed')) }
    finally { setLoading(false) }
  }
  const openDocument = (id) => {
    if (id === activeId) return
    if (dirty) { setConfirmation({ kind: 'open', id }); return }
    loadDocument(id)
  }
  const saveDocument = async (event) => {
    event.preventDefault()
    if (!draft.title.trim()) { setError(t('enterTitle')); return }
    setSaving(true)
    setError('')
    setStatus(t('saving'))
    const payload = {
      title: draft.title.trim(), content: draft.content,
      tags: draft.tags.split(',').map(tag => tag.trim()).filter(Boolean),
    }
    try {
      const saved = await request(activeId ? `/documents/${encodeURIComponent(activeId)}` : '/documents', {
        method: activeId ? 'PUT' : 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Protection': '1' },
        body: JSON.stringify(payload),
      })
      const next = { title: saved.title, tags: saved.tags.join(', '), content: saved.content }
      setDraft(next)
      setBaseline(JSON.stringify(next))
      setActiveId(saved.id)
      setDocuments(current => [saved, ...current.filter(item => item.id !== saved.id)]
        .sort((left, right) => right.updated_at.localeCompare(left.updated_at)))
      setStatus(t('savedSuccessfully'))
    } catch (err) {
      setStatus('')
      setError(err.message || t('saveDocumentFailed'))
    } finally { setSaving(false) }
  }
  const removeDocument = () => {
    if (!activeId) return
    setConfirmation({ kind: 'delete' })
  }
  const performDelete = async () => {
    setSaving(true)
    setError('')
    setStatus(t('deleting'))
    try {
      await request(`/documents/${encodeURIComponent(activeId)}`, {
        method: 'DELETE', credentials: 'include', headers: { 'X-CSRF-Protection': '1' },
      })
      setDocuments(current => current.filter(item => item.id !== activeId))
      setActiveId(null)
      const blank = { title: '', tags: '', content: '' }
      setDraft(blank)
      setBaseline(JSON.stringify(blank))
      setStatus(t('documentDeleted'))
    } catch (err) {
      setStatus('')
      setError(err.message || t('deleteDocumentFailed'))
    } finally { setSaving(false) }
  }
  const confirmAction = async () => {
    const pending = confirmation
    setConfirmation(null)
    if (pending?.kind === 'new') startNew()
    if (pending?.kind === 'open') await loadDocument(pending.id)
    if (pending?.kind === 'delete') await performDelete()
  }

  if (authState === 'checking') {
    return <section className="panel auth-panel"><Loading label={t('loadingSignIn')} /></section>
  }
  if (!authConfigured) {
    return <section className="panel auth-panel"><p className="eyebrow">{t('privateDocuments')}</p><h1>{t('signInNotConfigured')}</h1><p className="muted">{t('configureAuth')}</p>{authError && <p className="notice notice-error" role="alert">{authError}</p>}</section>
  }
  if (authState !== 'authenticated') {
    return <section className="panel auth-panel"><p className="eyebrow">{t('privateDocuments')}</p><h1>{t('signInToLibrary')}</h1><form className="auth-form" onSubmit={signIn}><label className="field-label" htmlFor="document-password">{t('password')}</label><input id="document-password" type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required /><button type="submit" disabled={authBusy}>{authBusy ? t('signingIn') : t('signIn')}</button></form>{authError && <p className="notice notice-error" role="alert">{authError}</p>}</section>
  }

  return (
    <>
    <section className="documents-layout">
      <aside className="panel document-list-panel">
        <div className="list-panel-heading"><div><p className="eyebrow">{t('yourLibrary')}</p><h2>{t('documents')}</h2></div><div className="document-list-actions"><button className="button-secondary" onClick={signOut} disabled={authBusy}>{t('logout')}</button><button onClick={openNew}>{t('newDocument')}</button></div></div>
        {loading && documents.length === 0 ? <Loading label={t('loadingDocuments')} /> : documents.length === 0 ? <p className="muted list-empty">{t('noDocumentsYet')}</p> : <div className="document-list">{documents.map(document => <button className={`document-list-item ${document.id === activeId ? 'active' : ''}`} key={document.id} onClick={() => openDocument(document.id)}><strong>{document.title}</strong><small>{document.tags?.length ? document.tags.join(' · ') : t('noTags')}</small></button>)}</div>}
        {error && <p className="notice notice-error" role="alert">{error}</p>}
      </aside>

      <section className="panel editor-panel">
        <div className="editor-heading"><div><p className="eyebrow">{activeId ? t('editDocument') : t('createDocument')}</p><h2>{activeId ? t('editNote') : t('writeNote')}</h2></div>{dirty && <span className="dirty-indicator">{t('unsavedChanges')}</span>}</div>
        <form onSubmit={saveDocument}>
          <label className="field-label" htmlFor="document-title">{t('title')}</label>
          <input id="document-title" className="editor-input" value={draft.title} onChange={event => { setDraft({ ...draft, title: event.target.value }); setStatus('') }} placeholder={t('titlePlaceholder')} required />
          <label className="field-label" htmlFor="document-tags">{t('tags')} <span>({t('commaSeparated')})</span></label>
          <input id="document-tags" className="editor-input" value={draft.tags} onChange={event => { setDraft({ ...draft, tags: event.target.value }); setStatus('') }} placeholder={t('tagsPlaceholder')} />

          <div className="editor-toolbar"><div className="mode-switch" role="tablist" aria-label={t('markdownEditorMode')}><button type="button" role="tab" aria-selected={mode === 'edit'} className={mode === 'edit' ? 'active' : ''} onClick={() => setMode('edit')}>{t('edit')}</button><button type="button" role="tab" aria-selected={mode === 'preview'} className={mode === 'preview' ? 'active' : ''} onClick={() => setMode('preview')}>{t('preview')}</button></div><span className="muted">Markdown</span></div>
          {mode === 'edit' ? <textarea className="markdown-input" aria-label={t('markdownContent')} value={draft.content} onChange={event => { setDraft({ ...draft, content: event.target.value }); setStatus('') }} placeholder={t('startWriting')} spellCheck="false" /> : <div className="preview-surface"><MarkdownPreview content={draft.content} /></div>}

          <div className="editor-actions"><div className="save-feedback" aria-live="polite">{error && <span className="error-text">{error}</span>}{status && <span className={status === t('savedSuccessfully') || status === t('documentDeleted') ? 'success-text' : 'muted'}>{status}</span>}</div><div className="action-buttons">{activeId && <button type="button" className="button-danger" onClick={removeDocument} disabled={saving}>{t('delete')}</button>}<button type="submit" disabled={saving}>{saving ? t('saving') : t('save')}</button></div></div>
        </form>
      </section>
    </section>
    {confirmation && <ConfirmationDialog
      title={confirmation.kind === 'delete' ? t('deleteDocumentTitle') : t('discardUnsavedTitle')}
      message={confirmation.kind === 'delete' ? t('deleteDocumentMessage').replace('{title}', draft.title || '제목 없음') : t('discardUnsavedMessage')}
      confirmLabel={confirmation.kind === 'delete' ? t('deleteDocument') : t('discardChanges')}
      danger={confirmation.kind === 'delete'}
      onCancel={() => setConfirmation(null)}
      onConfirm={confirmAction}
    />}
    </>
  )
}

function App() {
  const [screen, setScreen] = useState('dashboard')
  const [pendingScreen, setPendingScreen] = useState(null)
  const documentDirty = useRef(false)
  const navigate = (next) => {
    if (screen === 'documents' && documentDirty.current) { setPendingScreen(next); return }
    setScreen(next)
  }
  return (
    <main className="app-shell">
      <div className="app-inner">
        <header className="app-header"><a className="brand" href="#top" onClick={event => event.preventDefault()}><span className="brand-mark">W</span><span>AI Wiki</span></a><nav className="primary-nav" aria-label={t('navigation')}><button className={screen === 'dashboard' ? 'active' : ''} onClick={() => navigate('dashboard')}>{t('dashboard')}</button><button className={screen === 'documents' ? 'active' : ''} onClick={() => navigate('documents')}>{t('documents')}</button></nav></header>
        {screen === 'dashboard' ? <DashboardScreen /> : <DocumentsScreen dirtyRef={documentDirty} />}
        {pendingScreen && <ConfirmationDialog title={t('discardUnsavedTitle')} message={t('leaveAndDiscardMessage')} confirmLabel={t('discardChanges')} onCancel={() => setPendingScreen(null)} onConfirm={() => { setScreen(pendingScreen); setPendingScreen(null) }} />}
      </div>
    </main>
  )
}

createRoot(document.getElementById('root')).render(<React.StrictMode><App /></React.StrictMode>)
