import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

const API = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

async function request(path) {
  const response = await fetch(`${API}${path}`)
  if (!response.ok) throw new Error(`${response.status}: ${response.statusText}`)
  return response.json()
}

function Loading({ label = 'Loading' }) {
  return <div className="text-sm text-slate-400 animate-pulse">{label}...</div>
}

function MarkdownPreview({ note }) {
  const blocks = note.summary.split(/\n{2,}/)
  return (
    <article className="prose prose-invert max-w-none">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div><p className="eyebrow">{note.primary_category}</p><h2>{note.title}</h2></div>
        <span className="rounded-full border border-cyan-400/30 px-3 py-1 text-xs text-cyan-300">Markdown preview</span>
      </div>
      {blocks.map((block, index) => <p key={index}>{block}</p>)}
      {note.key_facts?.length > 0 && <><h3>Key facts</h3><ul>{note.key_facts.map(fact => <li key={fact}>{fact}</li>)}</ul></>}
      <div className="mt-8 border-t border-white/10 pt-4 text-sm text-slate-500">{note.relative_path}</div>
    </article>
  )
}

function App() {
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
      setError('')
      const [nextStats, nextRecent] = await Promise.all([request('/stats'), request('/recent')])
      setStats(nextStats); setRecent(nextRecent)
      if (selected) {
        try { setSelected(await request(`/note/${encodeURIComponent(selected.source_id)}`)) } catch { setSelected(null) }
      }
    } catch (err) { setError(err.message || 'Unable to connect to the API') }
    finally { setLoading(false) }
  }

  useEffect(() => { refresh(); const timer = setInterval(refresh, 5000); return () => clearInterval(timer) }, [])

  const search = async (event) => {
    event?.preventDefault()
    if (!query.trim()) return setResults(null)
    try { setError(''); setResults(await request(`/search?q=${encodeURIComponent(query.trim())}`)) }
    catch (err) { setError(err.message || 'Search failed') }
  }

  const openNote = async (note) => {
    try { setError(''); setSelected(await request(`/note/${encodeURIComponent(note.source_id || note.id)}`)) }
    catch (err) { setError(err.message || 'Could not load note') }
  }

  const semanticSearch = async (event) => {
    event?.preventDefault()
    if (!semanticQuery.trim()) return setSemanticResults(null)
    try { setError(''); setSemanticResults(await request(`/semantic-search?q=${encodeURIComponent(semanticQuery.trim())}`)) }
    catch (err) { setError(err.message || 'Semantic search failed') }
  }

  const chat = async (event) => {
    event.preventDefault()
    if (!chatQuestion.trim()) return
    setChatLoading(true)
    try {
      setError('')
      const response = await fetch(`${API}/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: chatQuestion.trim() }) })
      if (!response.ok) throw new Error(`${response.status}: ${response.statusText}`)
      const data = await response.json()
      setChatAnswer(data.answer); setChatSources(data.sources)
    } catch (err) { setError(err.message || 'Chat failed') }
    finally { setChatLoading(false) }
  }

  const list = results ?? recent
  return <main className="min-h-screen px-5 py-8 md:px-10 lg:px-16">
    <header className="mx-auto mb-8 max-w-7xl flex flex-col gap-5 md:flex-row md:items-end md:justify-between">
      <div><p className="eyebrow">AI WIKI / LIVE INDEX</p><h1>Knowledge dashboard</h1><p className="mt-2 text-slate-400">A quiet place to find what your vault knows.</p></div>
      <div className="flex items-center gap-3 text-xs text-slate-500"><span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_14px_#34d399]" /> Auto-refreshing every 5 seconds</div>
    </header>
    <section className="mx-auto max-w-7xl grid gap-4 sm:grid-cols-2 lg:grid-cols-4 mb-8">
      {['documents', 'ai', 'investment', 'philosophy'].map((key, index) => <div className="panel stat-card" key={key}><span>{index === 0 ? 'Indexed documents' : key}</span><strong>{loading ? '—' : index === 0 ? stats?.documents ?? 0 : stats?.categories?.[key] ?? 0}</strong></div>)}
    </section>
    {error && <div className="mx-auto mb-6 max-w-7xl rounded-xl border border-rose-400/30 bg-rose-400/10 px-4 py-3 text-sm text-rose-200">{error}</div>}
    <section className="mx-auto max-w-7xl grid gap-6 lg:grid-cols-[minmax(280px,0.8fr)_minmax(0,1.5fr)]">
      <div className="panel min-h-[520px] p-5">
        <form onSubmit={search} className="mb-6 flex gap-2"><input aria-label="Search notes" value={query} onChange={e => setQuery(e.target.value)} placeholder="Search title, summary, tags..." /><button type="submit">Search</button></form>
        <form onSubmit={semanticSearch} className="mb-7 rounded-xl border border-cyan-400/20 bg-cyan-400/5 p-3"><p className="eyebrow mb-2">SEMANTIC SEARCH</p><div className="flex gap-2"><input aria-label="Semantic search" value={semanticQuery} onChange={e => setSemanticQuery(e.target.value)} placeholder="Ask by meaning..." /><button type="submit">Find</button></div></form>
        {semanticResults && <div className="mb-6"><div className="mb-3 flex items-center justify-between"><p className="eyebrow">SIMILAR NOTES</p><button className="text-xs text-cyan-300" onClick={() => setSemanticResults(null)}>Clear</button></div>{semanticResults.length === 0 ? <p className="text-sm text-slate-500">No semantic matches.</p> : <div className="space-y-2">{semanticResults.map(result => <button className="note-row" key={result.id} onClick={() => openNote(result)}><span className="block truncate text-left font-medium text-slate-200">{result.title}</span><span className="mt-1 block truncate text-left text-xs text-slate-500">{Math.round(result.score * 100)}% match · {result.snippet}</span></button>)}</div>}</div>}
        <div className="mb-4 flex items-center justify-between"><p className="eyebrow">{results ? 'SEARCH RESULTS' : 'RECENT DOCUMENTS'}</p>{results && <button className="text-xs text-cyan-300" onClick={() => setResults(null)}>Clear</button>}</div>
        {loading ? <Loading label="Loading documents" /> : list.length === 0 ? <p className="text-sm text-slate-500">No documents found.</p> : <div className="space-y-2">{list.map(note => <button className={`note-row ${selected?.source_id === note.source_id ? 'selected' : ''}`} key={note.source_id} onClick={() => openNote(note)}><span className="block truncate text-left font-medium text-slate-200">{note.title}</span><span className="mt-1 block truncate text-left text-xs text-slate-500">{note.relative_path}</span></button>)}</div>}
      </div>
      <div className="panel min-h-[520px] p-6 md:p-8">{selected ? <MarkdownPreview note={selected} /> : <div className="flex h-full min-h-[440px] items-center justify-center text-center"><div><div className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-2xl bg-cyan-400/10 text-2xl text-cyan-300">↗</div><h2 className="text-xl font-semibold text-slate-200">Select a document</h2><p className="mt-2 max-w-xs text-sm text-slate-500">Choose a note from the index to preview its contents.</p></div></div>}</div>
    </section>
    <section className="panel mx-auto mt-6 max-w-7xl p-6 md:p-8"><p className="eyebrow mb-2">RAG CHAT</p><h2 className="text-2xl font-semibold text-slate-100">Ask your knowledge base</h2><form onSubmit={chat} className="mt-5 flex flex-col gap-2 sm:flex-row"><input aria-label="Chat question" value={chatQuestion} onChange={e => setChatQuestion(e.target.value)} placeholder="Ask a question about your notes..." /><button type="submit" disabled={chatLoading}>{chatLoading ? 'Thinking...' : 'Ask'}</button></form>{chatAnswer && <div className="mt-7 grid gap-6 lg:grid-cols-[1fr_280px]"><div className="rounded-xl border border-white/10 bg-slate-950/30 p-5 text-base leading-8 text-slate-200 whitespace-pre-wrap">{chatAnswer}</div><div><p className="eyebrow mb-3">SOURCES</p><div className="space-y-2">{chatSources.map(source => <button className="note-row" key={source.id} onClick={() => openNote(source)}><span className="block truncate text-left text-sm text-slate-200">{source.title}</span><span className="block text-left text-xs text-slate-500">{Math.round(source.score * 100)}% match</span></button>)}</div></div></div>}</section>
  </main>
}

createRoot(document.getElementById('root')).render(<React.StrictMode><App /></React.StrictMode>)
