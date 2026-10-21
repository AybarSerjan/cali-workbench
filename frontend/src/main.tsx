import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

type Tab = 'chat' | 'wiki' | 'analysis' | 'validation' | 'management' | 'tools' | 'settings'
type Json = Record<string, any>

async function api(path: string, options: RequestInit = {}) {
  const response = await fetch(path, { ...options, headers: options.body instanceof FormData ? options.headers : { 'Content-Type': 'application/json', ...(options.headers || {}) } })
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`)
  return response.json()
}

async function readDatasetMetadata(dataset: Json, save = false) {
  const detected = await api(`/api/datasets/${dataset.id}/tools/metadata`, { method: 'POST', body: '{}' })
  const saved = Object.fromEntries(Object.entries(dataset.metadata || {}).filter(([, value]) => value))
  const metadata = { ...detected, ...saved }
  return save ? api(`/api/datasets/${dataset.id}`, { method: 'PATCH', body: JSON.stringify(metadata) }) : metadata
}

async function loadMdfDatasets(update: (items: Json[]) => void) {
  let items = (await api('/api/datasets')).filter((item: Json) => item.kind === 'mdf')
  update(items)
  for (const item of items.filter((value: Json) => !Object.values(value.metadata || {}).some(Boolean))) {
    try {
      const annotated = await readDatasetMetadata(item, true)
      items = items.map((value: Json) => value.id === annotated.id ? annotated : value)
      update(items)
    } catch { /* Keep the dataset usable when an MDF header cannot be read. */ }
  }
}

async function uploadDatasetFiles(datasetId: string, files: FileList | File[], report: (name: string, ok: boolean, detail: string) => void) {
  for (const file of Array.from(files)) {
    const form = new FormData()
    form.append('file', file)
    try {
      await api(`/api/datasets/${datasetId}/files`, { method: 'POST', body: form })
      report(file.name, true, '上传成功')
    } catch (error) {
      report(file.name, false, String(error))
    }
  }
}

async function uploadEachFileAsDataset(files: FileList | File[], report: (name: string, ok: boolean, detail: string, datasetId?: string) => void) {
  for (const file of Array.from(files)) {
    const form = new FormData()
    form.append('file', file)
    form.append('kind', 'mdf')
    form.append('description', '数据分析')
    try {
      let dataset = await api('/api/datasets/files', { method: 'POST', body: form })
      const duplicate = dataset.duplicate
      let metadataStatus = ''
      try { dataset = await readDatasetMetadata(dataset, true) } catch { metadataStatus = '；MF4 属性读取失败，可稍后编辑标注' }
      report(file.name, true, `${duplicate ? '重复文件，已关联已有数据集' : '已创建数据集'}：${datasetDisplayName(dataset)}${metadataStatus}`, dataset.id)
    } catch (error) {
      report(file.name, false, String(error))
    }
  }
}

function formatBytes(value = 0) {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1 }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`
}

function datasetMeta(item: Json) {
  const summary = item.summary || {}
  return [summary.file_ext?.toUpperCase(), summary.size ? formatBytes(summary.size) : '', summary.sha256_short ? `#${summary.sha256_short}` : ''].filter(Boolean).join(' · ')
}

function datasetDisplayName(item: Json) {
  const metadata = item.metadata || {}, identity = [metadata.experiment, metadata.measurement_date].filter(Boolean)
  const name = identity.length ? [...identity, item.summary?.size ? formatBytes(item.summary.size) : ''].filter(Boolean).join(' · ') : item.summary?.display_name || item.name
  return `${item.serial || item.id.slice(0, 8)} · ${name}`
}

function datasetDetails(item: Json) {
  const metadata = item.metadata || {}
  return [metadata.a2l ? `A2L ${metadata.a2l}` : '', metadata.hex ? `HEX ${metadata.hex}` : '', `ID ${item.id.slice(0, 8)}`].filter(Boolean).join(' · ')
}

function matchesDataset(item: Json, query: string) {
  if (!query.trim()) return true
  const metadata = item.metadata || {}
  return [item.serial, item.name, item.description, item.summary?.source_file, metadata.experiment, metadata.measurement_date, metadata.a2l, metadata.hex]
    .filter(Boolean).join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
}

const Icon = ({ children }: { children: React.ReactNode }) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">{children}</svg>
const TrashIcon = () => <Icon><polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><path d="M10 11v6M14 11v6" /><path d="M9 6V4h6v2" /></Icon>
const icons: Record<Tab, React.ReactNode> = {
  chat: <Icon><path d="M21 11.5a8.5 8.5 0 0 1-12.3 7.6L3 21l1.9-5.7A8.5 8.5 0 1 1 21 11.5z" /></Icon>,
  wiki: <Icon><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" /><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" /></Icon>,
  analysis: <Icon><path d="M3 3v18h18" /><path d="m7 16 4-5 3 3 5-7" /></Icon>,
  validation: <Icon><path d="M4 4h16v16H4z" /><path d="m8 12 3 3 5-6" /></Icon>,
  management: <Icon><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5M3 12c0 1.7 4 3 9 3s9-1.3 9-3" /></Icon>,
  tools: <Icon><path d="M14.7 6.3a4 4 0 0 0-5-5L7.6 3.4l3 3 2.1-2.1a4 4 0 0 0 2 2z" /><path d="m10 6-8 8 4 4 8-8M14 14l7 7" /></Icon>,
  settings: <Icon><circle cx="12" cy="12" r="3" /><path d="M19 15l2 2-4 4-2-2a8 8 0 0 1-3 1v3H6v-3a8 8 0 0 1-3-1l-2 2-4-4 2-2a8 8 0 0 1-1-3h-3V6h3a8 8 0 0 1 1-3l-2-2 4-4 2 2a8 8 0 0 1 3-1v-3h6v3a8 8 0 0 1 3 1l2-2 4 4-2 2a8 8 0 0 1 1 3h3v6h-3a8 8 0 0 1-1 3z" /></Icon>,
}

function CaliMark() {
  return <span className="brand-mark" aria-hidden="true">C</span>
}

const nav: { id: Tab; label: string }[] = [
  { id: 'chat', label: '知识问答' },
  { id: 'wiki', label: '知识库 Wiki' },
  { id: 'analysis', label: '数据分析' },
  { id: 'validation', label: '工况验证' },
  { id: 'management', label: '数据管理' },
  { id: 'tools', label: '常用工具' },
  { id: 'settings', label: '设置' },
]

function App() {
  const [tab, setTab] = useState<Tab>('chat')
  const [health, setHealth] = useState<Json | null>(null)
  useEffect(() => { document.title = 'CALI Workbench' }, [])
  useEffect(() => { api('/api/health').then(setHealth).catch(() => setHealth(null)) }, [])
  return <div className="app"><div className="shell">
    <aside><div className="brand"><CaliMark /><div className="brand-copy"><b>CALI Workbench</b></div></div>
      <nav>{nav.map(item => <button key={item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}><i>{icons[item.id]}</i>{item.label}</button>)}</nav>
      <div className="status"><span className={health ? 'dot online' : 'dot'} /><div><b>{health ? '本地服务在线' : '服务未连接'}</b><small>{health?.llm?.provider || 'offline'}</small></div></div>
    </aside>
    <main><header><div><span className="eyebrow">LOCAL · AUDITABLE · VERSIONED</span><h1>{nav.find(item => item.id === tab)?.label}</h1></div></header>
      {tab === 'chat' && <Chat />}{tab === 'wiki' && <Wiki />}{tab === 'analysis' && <Analysis />}{tab === 'validation' && <Validation />}{tab === 'management' && <Management />}{tab === 'tools' && <Tools />}{tab === 'settings' && <Settings />}
    </main>
  </div></div>
}

const WP_STATUS_LABEL: Record<string, string> = { draft: '草稿', review: '复核中', reviewed: '待批准', approved: '已批准' }
const EVAL_RESULT_LABEL: Record<string, string> = { pass: '通过', fail: '未通过', observation: '观察项', not_evaluable: '不可评估', not_applicable_in_file: '工况未覆盖', not_approved: '未批准', error: '出错' }

function Validation() {
  const [workpackages, setWorkpackages] = useState<Json[]>([]), [datasets, setDatasets] = useState<Json[]>([])
  const [selectedId, setSelectedId] = useState(''), [datasetId, setDatasetId] = useState('')
  const [result, setResult] = useState<Json | null>(null)
  const [overview, setOverview] = useState<Json[]>([]), [batch, setBatch] = useState<Json | null>(null)
  const [notice, setNotice] = useState(''), [busy, setBusy] = useState(false), [definition, setDefinition] = useState<Json | null>(null)
  const [title, setTitle] = useState('')
  const selected = workpackages.find(item => item.id === selectedId), latest = selected?.revisions?.[0]
  const defOf = (wp?: Json) => wp?.revisions?.[0] ? JSON.parse(JSON.stringify(wp.revisions[0].definition)) : null
  const refresh = async (id = selectedId) => {
    const [wps, data, view] = await Promise.all([api('/api/workpackages'), api('/api/datasets'), api('/api/workpackages/overview')])
    setWorkpackages(wps); setDatasets(data); setOverview(view)
    const current = wps.find((item: Json) => item.id === id)
    setDefinition(defOf(current)); setTitle(current?.title || '')
  }
  useEffect(() => { refresh().catch(error => setNotice(String(error))) }, [])
  const choose = (id: string) => { const wp = workpackages.find(item => item.id === id); setSelectedId(id); setResult(null); setNotice(''); setDefinition(defOf(wp)); setTitle(wp?.title || '') }
  const back = () => { setSelectedId(''); setResult(null); setNotice('') }
  const patch = (part: Json) => setDefinition((value: Json) => ({ ...value, ...part }))
  const patchItem = (field: 'signals' | 'criteria', index: number, part: Json | null) => setDefinition((value: Json) => {
    const items = [...(value?.[field] || [])]
    if (part === null) items.splice(index, 1); else items[index] = { ...items[index], ...part }
    return { ...value, [field]: items }
  })
  const addItem = (field: 'signals' | 'criteria', template: Json) => setDefinition((value: Json) => ({ ...value, [field]: [...(value?.[field] || []), template] }))
  async function act(path: string, body: Json = {}) { setBusy(true); setNotice(''); try { await api(path, { method: 'POST', body: JSON.stringify(body) }); await refresh() } catch (error) { setNotice(String(error)) } finally { setBusy(false) } }
  async function removeWorkpackage(id: string) {
    const target = workpackages.find(item => item.id === id)
    if (!target || busy) return
    if (!window.confirm(`删除工作包「${target.wp_code} ${target.title}」？此操作会同时删除所有修订。`)) return
    setBusy(true)
    setNotice('')
    try {
      await api(`/api/workpackages/${id}`, { method: 'DELETE' })
      if (selectedId === id) setSelectedId('')
      await refresh()
    } catch (error) {
      setNotice(String(error))
    } finally {
      setBusy(false)
    }
  }
  async function saveRevision() { if (!selected || !definition) return; setBusy(true); try { await api(`/api/workpackages/${selected.id}/revision`, { method: 'PUT', body: JSON.stringify({ definition, ...(title.trim() ? { title: title.trim() } : {}) }) }); setNotice('已保存为新的不可变草稿修订'); await refresh() } catch (error) { setNotice(String(error)) } finally { setBusy(false) } }
  async function duplicateLast() {
    setBusy(true); setNotice('')
    try {
      const source = [...workpackages].sort((a, b) => String(a.wp_code).localeCompare(String(b.wp_code), undefined, { numeric: true })).pop()
      const next = workpackages.reduce((acc, wp) => Math.max(acc, Number((String(wp.wp_code).match(/\d+/) || ['0'])[0])), 0) + 1
      const code = `WP-${String(next).padStart(2, '0')}`
      const created = await api('/api/workpackages', { method: 'POST', body: JSON.stringify({ wp_code: code, title: code, definition: defOf(source) || { description: '', scenario_gate: '', signals: [], criteria: [] } }) })
      setSelectedId(created.id); setResult(null)
      await refresh(created.id)
    } catch (error) { setNotice(String(error)) } finally { setBusy(false) }
  }
  async function run(kind: 'match' | 'evaluate') {
    if (!selected || !datasetId) return
    setBusy(true); setNotice('')
    try { setResult(await api(`/api/workpackages/${selected.id}/${kind}`, { method: 'POST', body: JSON.stringify({ dataset_id: datasetId }) })); await refresh() }
    catch (error) { setNotice(String(error)) } finally { setBusy(false) }
  }
  async function runAll() {
    if (!datasetId) return
    setBusy(true); setNotice('')
    try { setBatch(await api('/api/workpackages/evaluate-all', { method: 'POST', body: JSON.stringify({ dataset_id: datasetId }) })); await refresh() }
    catch (error) { setNotice(String(error)) } finally { setBusy(false) }
  }
  const mdf = datasets.filter(item => item.kind === 'mdf')
  if (!selected) return <section className="validation-main">
    <div className="panel"><div className="section-title"><h2>运行验证</h2></div>
      {notice && <div className="notice">{notice}</div>}
      <div className="validation-controls"><label>测量数据<select value={datasetId} onChange={event => setDatasetId(event.target.value)}><option value="">选择 MF4 数据集</option>{mdf.map(item => <option key={item.id} value={item.id}>{datasetDisplayName(item)}</option>)}</select></label><div className="validation-run-actions"><button disabled={!datasetId || busy} onClick={runAll}>{busy ? '运行中…' : '评估全部工作包'}</button></div></div>
      {batch && <div className="eval-result"><div className="batch-counts">{Object.entries(batch.counts || {}).map(([key, count]) => <span className={`result-badge ${key.toLowerCase()}`} key={key}>{EVAL_RESULT_LABEL[key.toLowerCase()] || key} × {String(count)}</span>)}</div>{batch.explanation?.text && <p>{batch.explanation.text}</p>}</div>}</div>
    <div className="panel wp-list"><div className="section-title"><h2>完成度总览</h2><div className="actions"><span className="tag">{overview.length}</span><button className="ghost" disabled={busy} onClick={duplicateLast}>{busy ? '创建中…' : '复制上一个新建'}</button></div></div>
      {overview.length === 0 && <p className="rail-empty rail-empty-pad">暂无工作包。</p>}
      {overview.map(item => { const wp = workpackages.find(entry => entry.id === item.workpackage_id); const status = wp?.revisions?.[0]?.status
        return <div className="wp-row" key={item.workpackage_id}><button className="wp-row-main" onClick={() => choose(item.workpackage_id)}><span><b>{item.wp_code}</b><small>{item.title}{item.missing_signals?.length > 0 && ` · 缺 ${item.missing_signals.join(', ')}`}</small></span><span className="wp-row-badges"><i className={`state ${status}`}>{WP_STATUS_LABEL[status] || status}</i><span className={`result-badge ${String(item.overall || '').toLowerCase()}`}>{item.overall ? (EVAL_RESULT_LABEL[String(item.overall).toLowerCase()] || item.overall) : '未评估'}</span></span></button><button className="ghost danger btn-icon wp-row-delete" title="删除工作包" aria-label={`删除工作包 ${item.wp_code}`} disabled={busy} onClick={() => removeWorkpackage(item.workpackage_id)}><TrashIcon /></button></div> })}</div>
  </section>
  return <section className="validation-main"><div className="panel"><div className="section-title"><h2>{selected.wp_code} · {selected.title}</h2><div className="actions"><i className={`state ${latest?.status}`}>{WP_STATUS_LABEL[latest?.status] || latest?.status}</i><button className="ghost" disabled={busy} onClick={back}>返回总览</button><button className="ghost" disabled={busy} onClick={saveRevision}>保存修订</button><button className="ghost" disabled={latest?.status !== 'draft' || busy} onClick={() => act(`/api/workpackages/${selectedId}/review`, { review_notes: ['UI review'] })}>提交复核</button><button disabled={latest?.status !== 'reviewed' || busy} onClick={() => act(`/api/workpackages/${selectedId}/approve`)}>批准</button></div></div>
      {notice && <div className="notice">{notice}</div>}
      {definition ? <div className="wp-cards">
        <div className="wp-card"><h3>描述与工况条件</h3>
          <label>标题<input value={title} onChange={event => setTitle(event.target.value)} placeholder="工作包名称，保存修订时一并更新"/></label>
          <label>工作包描述<textarea rows={3} value={definition.description || ''} onChange={event => patch({ description: event.target.value })} placeholder="这个工作包验证什么"/></label>
          <label>工况前置条件（表达式，留空表示全程适用）<input value={definition.scenario_gate || ''} onChange={event => patch({ scenario_gate: event.target.value })} placeholder="例：nmot_w > 600 and tmot > 60"/></label></div>
        <div className="wp-card"><div className="wp-card-head"><h3>需要的信号</h3><button className="ghost" disabled={busy} onClick={() => addItem('signals', { a2l_name: '', match_status: 'proposed' })}>添加信号</button></div>
          {(definition.signals || []).map((signal: Json, index: number) => <div className="wp-card-row" key={index}>
            <input value={signal.a2l_name || ''} placeholder="MF4 通道名" onChange={event => patchItem('signals', index, { a2l_name: event.target.value })}/>
            <label className="check"><input type="checkbox" checked={signal.match_status === 'confirmed'} onChange={event => patchItem('signals', index, { match_status: event.target.checked ? 'confirmed' : 'proposed' })}/>已确认</label>
            <button className="ghost danger btn-icon" title="删除信号" onClick={() => patchItem('signals', index, null)}><TrashIcon /></button></div>)}
          {!(definition.signals || []).length && <p className="empty">暂无信号；批准前每个信号都必须人工确认。</p>}</div>
        <div className="wp-card"><div className="wp-card-head"><h3>评估判据</h3><button className="ghost" disabled={busy} onClick={() => addItem('criteria', { expression: '', min_duration_s: 0, threshold_source: 'AGENT_PROPOSED' })}>添加判据</button></div>
          {(definition.criteria || []).map((criterion: Json, index: number) => <div className="wp-card-row" key={index}>
            <input value={criterion.expression || ''} placeholder="违规表达式，例：abs(actual - target) > 0.05" onChange={event => patchItem('criteria', index, { expression: event.target.value })}/>
            <label className="check">持续 ≥<input type="number" min={0} step={0.1} value={criterion.min_duration_s ?? 0} onChange={event => patchItem('criteria', index, { min_duration_s: Number(event.target.value) })}/>s</label>
            <label className="check"><input type="checkbox" checked={criterion.threshold_source === 'HUMAN_APPROVED'} onChange={event => patchItem('criteria', index, { threshold_source: event.target.checked ? 'HUMAN_APPROVED' : 'AGENT_PROPOSED' })}/>阈值已人工批准</label>
            <button className="ghost danger btn-icon" title="删除判据" onClick={() => patchItem('criteria', index, null)}><TrashIcon /></button></div>)}
          {!(definition.criteria || []).length && <p className="empty">暂无判据；批准前每条判据的阈值都必须人工批准。</p>}</div>
      </div> : <p className="empty">这个工作包还没有定义内容。</p>}</div>
    <div className="panel eval-launch"><div className="section-title"><h2>运行验证</h2></div><div className="validation-controls"><label>测量数据<select value={datasetId} onChange={event => setDatasetId(event.target.value)}><option value="">选择 MF4 数据集</option>{mdf.map(item => <option key={item.id} value={item.id}>{datasetDisplayName(item)}</option>)}</select></label><div className="validation-run-actions"><button className="ghost" disabled={!datasetId || busy} onClick={() => run('match')}>匹配信号</button><button disabled={!datasetId || busy} onClick={() => run('evaluate')}>{busy ? '运行中…' : '开始评估'}</button></div></div>
      {result && <div className="eval-result"><span className={`result-badge ${String(result.summary?.overall || '').toLowerCase()}`}>{EVAL_RESULT_LABEL[String(result.summary?.overall || '').toLowerCase()] || result.summary?.overall || 'MATCHED'}</span>{result.summary?.missing_signals?.length > 0 && <p>缺失信号：{result.summary.missing_signals.join(', ')}</p>}{result.summary?.explanation?.text && <p>{result.summary.explanation.text}</p>}{result.summary?.criteria?.map((item: Json) => <div className="criterion-block" key={item.id}><div className="criterion"><b>{item.id} · {item.verdict}</b><code>{item.expression}</code><small>{item.windows.length} 个问题窗口</small></div>{item.windows.map((window: Json, index: number) => <div className="window-row" key={`${item.id}-${index}`}><span>{window.start.toFixed(3)} s</span><span>{window.end.toFixed(3)} s</span><b>{window.duration.toFixed(3)} s</b><small>{Object.keys(item.snapshots?.[index]?.signals || {}).length} 个信号快照</small></div>)}</div>)}</div>}</div>
  </section>
}

function chatMessages(item: Json | null) {
  return (item?.messages || []).map((message: Json) => ({ id: message.id, role: message.role, content: message.content, sources: message.sources, meta: message.meta }))
}

function chatSnippet(item: Json) {
  const messages = item.messages || []
  const user = [...messages].reverse().find((message: Json) => message.role === 'user')
  return user?.content || '还没有消息'
}

function Chat() {
  const [conversations, setConversations] = useState<Json[]>([]), [conversation, setConversation] = useState<Json | null>(null)
  const [text, setText] = useState(''), [messages, setMessages] = useState<Json[]>([]), [busy, setBusy] = useState(false), [task, setTask] = useState(false)
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const open = (item: Json) => { setConversation(item); setMessages(chatMessages(item)) }
  const scrollLatest = () => requestAnimationFrame(() => { const box = messagesRef.current; if (box) box.scrollTop = box.scrollHeight })
  useEffect(() => { scrollLatest() }, [conversation?.id, messages.length])
  async function refresh(preferredId = conversation?.id) {
    const list = await api('/api/conversations')
    setConversations(list)
    const next = list.find((item: Json) => item.id === preferredId) || list.find((item: Json) => !item.archived) || list[0]
    if (next) open(next)
    else await newChat()
  }
  useEffect(() => { refresh() }, [])
  async function newChat() {
    const created = await api('/api/conversations', { method: 'POST', body: JSON.stringify({ title: '新对话' }) })
    setConversations(value => [created, ...value]); open(created); setText('')
  }
  async function deliver(question: string) {
    const active = conversation
    if (!question.trim() || !active) return
    setMessages(value => [...value, { role: 'user', content: question }]); setBusy(true)
    try {
      const result = await api(`/api/conversations/${active.id}/messages`, { method: 'POST', body: JSON.stringify({ content: question, grill: task }) })
      setMessages(value => [...value, { id: result.message_id, role: 'assistant', content: result.answer, sources: result.sources, meta: result.meta }])
      await refresh(active.id)
    } catch (error) { setMessages(value => [...value, { role: 'error', content: String(error) }]) } finally { setBusy(false) }
  }
  async function send() { const question = text; if (!question.trim()) return; setText(''); await deliver(question) }
  async function resolveAction(message: Json, verb: 'confirm' | 'cancel') {
    if (!conversation || !message.id) return
    setBusy(true)
    try {
      const result = await api(`/api/conversations/${conversation.id}/messages/${message.id}/${verb}`, { method: 'POST', body: '{}' })
      setMessages(value => value.map(item => item.id === message.id ? { ...item, meta: { ...item.meta, status: verb === 'confirm' ? 'confirmed' : 'cancelled' } } : item)
        .concat(verb === 'confirm' ? [{ id: result.message_id, role: 'assistant', content: result.answer, meta: {} }] : []))
      await refresh(conversation.id)
    } catch (error) { setMessages(value => [...value, { role: 'error', content: String(error) }]) } finally { setBusy(false) }
  }
  async function removeConversation(item: Json) {
    if (!confirm(`删除对话历史「${item.title || '新对话'}」？`)) return
    setBusy(true)
    try {
      await api(`/api/conversations/${item.id}/archive`, { method: 'POST', body: '{}' })
      const list = conversations.filter(value => value.id !== item.id)
      setConversations(list)
      if (conversation?.id === item.id) list[0] ? open(list[0]) : await newChat()
    } catch (error) { setMessages(value => [...value, { role: 'error', content: String(error) }]) } finally { setBusy(false) }
  }
  return <section className="chat-layout">
    <div className="chat-history panel"><div className="history-head"><div><span className="eyebrow">CHAT HISTORY</span><h2>对话历史</h2></div><button onClick={newChat} disabled={busy}>新对话</button></div>
      <div className="history-list">{conversations.map(item => <div key={item.id} className={`history-row ${conversation?.id === item.id ? 'active' : ''}`}><button className="history-item" onClick={() => open(item)}><b>{item.title || '新对话'}</b><span>{chatSnippet(item)}</span><small>{new Date(item.updated_at || item.created_at).toLocaleString()}</small></button><button className="history-delete" title="删除历史" aria-label="删除历史" disabled={busy} onClick={() => removeConversation(item)}><TrashIcon /></button></div>)}{conversations.length === 0 && <p className="rail-empty">暂无历史对话。</p>}</div>
    </div>
    <div className="conversation panel"><div className="conversation-head"><div><span className="eyebrow">KNOWLEDGE CHAT</span><h2>{conversation?.title || '新对话'}</h2></div><span className="tag">{messages.filter(message => message.role === 'user').length} 轮</span></div>
      {messages.length === 0 && <div className="intro"><div className="orb">C</div><h2>先定位证据，再给结论</h2><p>知识问题会精确检索已发布 Wiki；信号异常问题会按需追问数据集和阈值，自动查找窗口，再结合 Wiki 分析。需要写入结果的工具操作仍会在执行前请你确认。</p></div>}
      <div className="messages" ref={messagesRef}>{messages.map((message, index) => <div key={message.id || index} className={`message ${message.role}`}><b>{message.role === 'user' ? '你' : message.role === 'assistant' ? 'CALI' : '错误'}</b><p>{message.content}</p>
        {message.meta?.ask?.options?.length > 0 && <div className="task-options">{message.meta.ask.options.map((option: string) => <button key={option} disabled={busy} onClick={() => deliver(option)}>{option}</button>)}</div>}
        {message.meta?.action && <div className={`task-action ${message.meta.status || 'pending'}`}><div className="task-action-summary">待执行：{message.meta.action.summary || message.meta.action.action}</div>{message.meta.status === 'confirmed' ? <span className="task-action-state">已执行</span> : message.meta.status === 'cancelled' ? <span className="task-action-state">已取消</span> : <div className="task-action-buttons"><button disabled={busy} onClick={() => resolveAction(message, 'confirm')}>确认执行</button><button className="ghost" disabled={busy} onClick={() => resolveAction(message, 'cancel')}>取消</button></div>}</div>}
        {message.sources?.length > 0 && <div className="sources">{message.sources.map((source: Json) => <span key={source.path}>{source.title} · {source.path}</span>)}</div>}</div>)}</div>
      <div className="composer"><label className="grill-toggle" title="调用会生成或写入结果的工具；参数齐全后仍需确认"><input type="checkbox" checked={task} onChange={event => setTask(event.target.checked)}/>工具模式</label><textarea value={text} onChange={event => setText(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send() } }} placeholder={task ? '描述要执行的工具操作，例如「合并 MDF-0001 和 MDF-0002」' : '询问 Wiki，或分析某个数据集中的信号异常'}/><button onClick={send} disabled={busy || !conversation}>{busy ? '运行中' : task ? '执行 →' : '发送 →'}</button></div>
    </div>
  </section>
}

// ---- Structured Karpathy page (mirrors backend page_from_content) ----
type Evidence = string | { type?: string; source?: string; page?: string; section?: string; text?: string }
type Page = { title: string; status: string; summary: string; claims: string[]; evidence: Evidence[]; links: string[] }
type Form = { title: string; status: string; summary: string; claims: string; evidence: string; links: string }
const STATUS_OPTIONS: [string, string][] = [['draft', '草稿'], ['review', '待审'], ['stable', '稳定']]
const STATUS_LABEL: Record<string, string> = Object.fromEntries(STATUS_OPTIONS)
const emptyPage = (): Page => ({ title: '', status: 'draft', summary: '', claims: [], evidence: [], links: [] })

const linesToArray = (value: string) => value.split('\n').map(item => item.trim()).filter(Boolean)
const splitLinks = (value: string) => value.split(',').map(item => item.trim()).filter(Boolean)
function parseEvidenceLine(line: string): Evidence {
  const trimmed = line.trim()
  if (trimmed.startsWith('{') && trimmed.endsWith('}')) { try { return JSON.parse(trimmed) } catch { return trimmed } }
  return trimmed
}
const evidenceLinesToArray = (value: string): Evidence[] => value.split('\n').map(item => item.trim()).filter(Boolean).map(parseEvidenceLine)
const evidenceToLine = (item: Evidence) => typeof item === 'string' ? item : JSON.stringify(item)
const evidenceMeta = (item: Evidence) => typeof item === 'string' ? '' : [item.source, item.page ? `p.${item.page}` : '', item.section].filter(Boolean).join(' · ')
const evidenceBody = (item: Evidence) => typeof item === 'string' ? item : (item.text || '')
const editableLines = (value: string) => value.split('\n')
function replaceLine(value: string, index: number, next: string) { const lines = editableLines(value); lines[index] = next; return lines.join('\n') }
function removeLine(value: string, index: number) { const lines = editableLines(value); lines.splice(index, 1); return lines.join('\n') }

function formFromPage(page: Page): Form {
  return { title: page.title, status: page.status || 'draft', summary: page.summary, claims: page.claims.join('\n'), evidence: page.evidence.map(evidenceToLine).join('\n'), links: page.links.join(', ') }
}
function pageFromForm(form: Form): Page {
  return { title: form.title.trim() || 'Untitled', status: form.status, summary: form.summary.trim(), claims: linesToArray(form.claims), evidence: evidenceLinesToArray(form.evidence), links: splitLinks(form.links) }
}

if ((import.meta as any).env?.DEV) {
  const page: Page = { title: 'T', status: 'review', summary: 's', claims: ['a', 'b'], evidence: [{ source: 'x', text: 'y' }, 'z'], links: ['L1', 'L2'] }
  const round = pageFromForm(formFromPage(page))
  console.assert(round.claims.length === 2 && round.links.length === 2 && evidenceBody(round.evidence[0]) === 'y' && round.evidence[1] === 'z', 'page<->form round-trip broken')
  console.assert(replaceLine('a\nb', 1, 'c') === 'a\nc' && removeLine('a\nb', 0) === 'b', 'inline node editing broken')
}

function Wiki() {
  const [sources, setSources] = useState<Json[]>([]), [articles, setArticles] = useState<Json[]>([]), [selected, setSelected] = useState<Json | null>(null), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false)
  const [form, setForm] = useState<Form>(formFromPage(emptyPage()))
  const [search, setSearch] = useState(''), [scratch, setScratch] = useState(''), [linkForm, setLinkForm] = useState<Json | null>(null)
  const load = (article: Json) => { setSelected(article); setForm(formFromPage(article.revisions[0]?.page || emptyPage())) }
  const refresh = () => Promise.all([api('/api/sources'), api('/api/wiki')]).then(([sourceList, articleList]) => { setSources(sourceList); setArticles(articleList); setSelected(current => { const match = current && articleList.find((a: Json) => a.id === current.id); if (!current || !match) { if (articleList[0]) load(articleList[0]); return articleList[0] || null } return match }) })
  useEffect(() => { refresh() }, [])
  async function upload(file?: File) { if (!file) return; const body = new FormData(); body.append('file', file); await api('/api/sources', { method: 'POST', body }); setNotice('原始资料已按哈希归档'); refresh() }
  async function importBundle(file?: File) {
    if (!file) return
    const body = new FormData(); body.append('file', file)
    try { const result = await api('/api/wiki/import', { method: 'POST', body }); if (result.articles?.[0]) load(result.articles[0]); setNotice(`已导入 ${result.imported} 篇 OKF 草稿，发布前请逐篇复核`); refresh() }
    catch (error) { setNotice(String(error)) }
  }
  async function addLink() {
    if (!linkForm?.url?.trim() || !linkForm?.title?.trim() || !linkForm?.text?.trim()) return
    try { await api('/api/sources/link', { method: 'POST', body: JSON.stringify({ url: linkForm.url.trim(), title: linkForm.title.trim(), text: linkForm.text }) }); setLinkForm(null); setNotice('外部链接已作为知识源归档，可生成草稿'); refresh() }
    catch (error) { setNotice(String(error)) }
  }
  async function removeSource(source: Json) {
    if (busy) return
    if (!window.confirm(`删除知识源「${source.name}」？如果它已被 Wiki 或标定会话引用，系统会阻止删除。`)) return
    setBusy(true)
    setNotice('')
    try {
      await api(`/api/sources/${source.id}`, { method: 'DELETE' })
      await refresh()
    } catch (error) {
      setNotice(String(error))
    } finally {
      setBusy(false)
    }
  }
  async function draft(id: string) { try { const article = await api('/api/wiki/drafts', { method: 'POST', body: JSON.stringify({ source_id: id }) }); load(article); setNotice('已生成结构化草稿，等待逐项审核与发布'); refresh() } catch (error) { setNotice(String(error)) } }
  async function save() {
    if (!selected) return
    try { const article = await api(`/api/wiki/revisions/${selected.revisions[0].id}`, { method: 'PUT', body: JSON.stringify({ content: JSON.stringify(pageFromForm(form)) }) }); load(article); setNotice('已保存为新的不可变修订'); refresh() }
    catch (error) { setNotice(String(error)) }
  }
  async function publish() {
    if (!selected) return
    const revision = selected.revisions[0]
    try { const article = await api(`/api/wiki/revisions/${revision.id}/publish`, { method: 'POST', body: JSON.stringify({ expected_base_sha256: revision.base_sha256 }) }); load(article); setNotice('已发布并更新检索索引'); refresh() }
    catch (error) { setNotice(String(error)) }
  }
  function compileFields() {
    setForm(f => ({ ...f, summary: f.summary.replace(/\s+/g, ' ').trim(), claims: Array.from(new Set(linesToArray(f.claims))).join('\n'), links: Array.from(new Set(splitLinks(f.links).filter(link => link !== f.title.trim()))).join(', ') }))
    setNotice('已就地去重与规整字段，记得保存修订')
  }
  function ingestScratch() {
    const fragments = scratch.split(/\n+/).map(item => item.trim().replace(/^[-*]\s*/, '')).filter(Boolean)
    if (!fragments.length) return
    const urls = fragments.filter(item => /^https?:\/\//i.test(item)), notes = fragments.filter(item => !/^https?:\/\//i.test(item))
    setForm(f => ({ ...f, claims: Array.from(new Set([...linesToArray(f.claims), ...notes])).join('\n'), evidence: [...f.evidence.split('\n').map(x => x.trim()).filter(Boolean), ...urls].join('\n') }))
    setScratch(''); setNotice('已把便签拆入结论/证据，记得保存修订')
  }
  const setField = (key: keyof Form, value: string) => setForm(f => ({ ...f, [key]: value }))
  const setNode = (key: 'claims' | 'evidence', index: number, value: string) => setForm(f => ({ ...f, [key]: replaceLine(f[key], index, value) }))
  const deleteNode = (key: 'claims' | 'evidence', index: number) => setForm(f => ({ ...f, [key]: removeLine(f[key], index) }))
  const addNode = (key: 'claims' | 'evidence') => setForm(f => ({ ...f, [key]: `${f[key]}\n` }))
  const currentPage = pageFromForm(form)
  const links = currentPage.links
  const claimNodes = editableLines(form.claims), evidenceNodes = editableLines(form.evidence)
  // Every article becomes a graph node; the active one reflects live (unsaved) edits.
  type PageNode = { id: string; title: string; status: string; links: string[] }
  const allPages: PageNode[] = articles.map(a => a.id === selected?.id
    ? { id: a.id, title: currentPage.title, status: currentPage.status, links }
    : { id: a.id, title: a.revisions[0]?.page?.title || a.title, status: a.revisions[0]?.page?.status || 'draft', links: (a.revisions[0]?.page?.links || []) as string[] })
  const selectByTitle = (title: string) => { const target = articles.find(a => (a.revisions[0]?.page?.title || a.title) === title); if (target) load(target) }
  const backlinks = allPages.filter(p => p.id !== selected?.id && p.links.includes(currentPage.title))
  const query = search.trim().toLowerCase()
  const visibleArticles = articles.filter(a => { const p = a.revisions[0]?.page; return !query || [p?.title || a.title, p?.summary, ...(p?.claims || []), ...(p?.links || [])].join(' ').toLowerCase().includes(query) })
  const ftype = (source: Json) => { const ext = (source.suffix || '').replace('.', '').toUpperCase(); return ext ? ext.slice(0, 4) : 'URL' }

  const nodes = allPages.slice(0, 12).map((p, index, list) => { const angle = Math.PI * 2 * index / list.length - Math.PI / 2; return { ...p, x: 100 + Math.cos(angle) * 62, y: 80 + Math.sin(angle) * 58 } })
  const nodeByTitle = new Map(nodes.map(n => [n.title, n]))
  const edges = nodes.flatMap(n => n.links.map(link => nodeByTitle.get(link)).filter((t): t is NonNullable<typeof t> => !!t).map(t => ({ a: n, b: t, key: `${n.id}-${t.id}` })))

  return <section className="wiki-workspace">
    <aside className="wiki-rail">
      <label className="rail-search"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m16 16 4 4"/></svg><input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索页面 / 结论 / 证据"/></label>
      <div className="panel wiki-sources"><div className="section-title compact"><div><h2>客观资料</h2></div><label className="button">上传<input type="file" hidden onChange={e => upload(e.target.files?.[0])}/></label></div>
        <div className="rail-tools"><button className="chip-action" onClick={() => setLinkForm(linkForm ? null : { url: '', title: '', text: '' })}>添加链接</button><label className="chip-action">导入 OKF<input type="file" accept=".zip,application/zip" hidden onChange={e => importBundle(e.target.files?.[0])}/></label><a className="chip-action" href="/api/wiki/export">导出 OKF</a></div>
        {linkForm && <div className="link-form"><input value={linkForm.url} placeholder="外部链接 URL（如 NotebookLM 分享链接）" onChange={e => setLinkForm({ ...linkForm, url: e.target.value })}/><input value={linkForm.title} placeholder="标题" maxLength={512} onChange={e => setLinkForm({ ...linkForm, title: e.target.value })}/><textarea value={linkForm.text} placeholder="粘贴正文 / 摘要（将作为可检索证据归档）" onChange={e => setLinkForm({ ...linkForm, text: e.target.value })}/><div className="actions"><button className="ghost" onClick={() => setLinkForm(null)}>取消</button><button onClick={addLink} disabled={!linkForm.url.trim() || !linkForm.title.trim() || !linkForm.text.trim()}>归档为知识源</button></div></div>}
        <div className="source-list">
          {sources.length === 0 && <p className="rail-empty">还没有客观资料。上传文件或添加链接，转成结构化页面。</p>}
          {sources.map(source => <div className="source-item" key={source.id}><span className="ftype">{ftype(source)}</span><div className="source-meta"><b title={source.name}>{source.name}</b><small>{source.source_url ? <a href={source.source_url} target="_blank" rel="noreferrer">链接源</a> : source.suffix} · {source.sha256.slice(0, 12)}</small></div><button className="ghost sm" disabled={busy} onClick={() => draft(source.id)}>生成草稿</button><button className="ghost sm danger btn-icon source-delete" disabled={busy} title="删除知识源" aria-label="删除知识源" onClick={() => removeSource(source)}><TrashIcon /></button></div>)}
        </div>
      </div>
      <div className="panel wiki-articles"><div className="section-title compact"><div><h2>知识页面</h2></div><span className="tag">{articles.length}</span></div>
        <div className="article-list">
          {articles.length === 0 && <p className="rail-empty">从上方知识源生成草稿后，结构化页面会出现在这里。</p>}
          {articles.length > 0 && visibleArticles.length === 0 && <p className="rail-empty">没有匹配的页面。</p>}
          {visibleArticles.map(article => { const p = article.revisions[0]?.page; const status = p?.status || 'draft'; return <button className={`article-item ${selected?.id === article.id ? 'active' : ''}`} key={article.id} onClick={() => load(article)}><span className={`status-pill ${status}`}>{STATUS_LABEL[status] || status}</span><span className="article-title" title={p?.title || article.title}>{p?.title || article.title}</span><small className="article-slug">{article.published_revision_id ? '已发布 · ' : ''}{(p?.claims || []).length} 条结论</small></button> })}
        </div>
      </div>
    </aside>

    <div className="wiki-main">
      {notice && <div className="notice">{notice}</div>}
      {selected ? <>
        <div className="wiki-topbar"><div><h2>{currentPage.title || '未命名页面'}</h2></div><div className="actions"><button className="ghost" onClick={compileFields}>整理</button><button className="ghost" onClick={save}>保存修订</button><button onClick={publish}>发布</button></div></div>
        <div className="wiki-panes">
          <article className="panel document-pane">
            <header className="document-head">
              <div className="title-edit"><span className={`status-pill ${currentPage.status}`}>{STATUS_LABEL[currentPage.status] || currentPage.status}</span><input aria-label="页面标题" value={form.title} onChange={e => setField('title', e.target.value)} placeholder="未命名页面"/></div>
              <select aria-label="页面状态" value={form.status} onChange={e => setField('status', e.target.value)}>{STATUS_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
              <textarea className="summary-edit" aria-label="页面概述" rows={3} value={form.summary} onChange={e => setField('summary', e.target.value)} placeholder="写一段简洁、可复核的页面概述。"/>
            </header>

            <section className="node-section"><div className="node-section-head"><h3>结论</h3><button className="ghost sm" onClick={() => addNode('claims')}>+ 添加结论</button></div>
              <div className="editable-nodes claim-nodes">{claimNodes.map((claim, i) => <div className="editable-node" key={i}><b>C{i + 1}</b><textarea rows={2} spellCheck={false} aria-label={`结论 C${i + 1}`} value={claim} onChange={e => setNode('claims', i, e.target.value)} placeholder="输入一条原子结论"/><button className="node-delete" aria-label={`删除结论 C${i + 1}`} title="删除" onClick={() => deleteNode('claims', i)}>×</button></div>)}</div>
            </section>

            <section className="node-section"><div className="node-section-head"><h3>证据</h3><button className="ghost sm" onClick={() => addNode('evidence')}>+ 添加证据</button></div>
              <div className="editable-nodes evidence-nodes">{evidenceNodes.map((line, i) => { const item = parseEvidenceLine(line); return <div className="editable-node" key={i}><b>E{i + 1}</b><div className="node-content">{evidenceMeta(item) && <span className="evidence-meta">{evidenceMeta(item)}</span>}<textarea rows={3} spellCheck={false} aria-label={`证据 E${i + 1}`} value={line} onChange={e => setNode('evidence', i, e.target.value)} placeholder="输入证据；也可粘贴包含 source、page、section、text 的 JSON"/></div><button className="node-delete" aria-label={`删除证据 E${i + 1}`} title="删除" onClick={() => deleteNode('evidence', i)}>×</button></div> })}</div>
            </section>

            <section className="node-section link-section"><div className="node-section-head"><h3>关联页面</h3></div><input value={form.links} onChange={e => setField('links', e.target.value)} placeholder="页面标题, 另一个页面"/><div className="link-row">{links.map(link => <button className={`pill ${nodeByTitle.has(link) ? '' : 'dangling'}`} key={link} onClick={() => selectByTitle(link)}>{link}</button>)}</div></section>
          </article>

          <aside className="wiki-side">
            <section className="panel"><div className="panel-heading"><h3>关系图</h3><span className="tag">{nodes.length} 节点</span></div>
              <div className="graph">{nodes.length ? <svg viewBox="0 0 200 160" preserveAspectRatio="xMidYMid meet">
                {edges.map(e => <line key={e.key} x1={e.a.x} y1={e.a.y} x2={e.b.x} y2={e.b.y} className="edge"/>)}
                {nodes.map(n => <g key={n.id} className={`node ${n.id === selected?.id ? 'active' : ''}`} onClick={() => selectByTitle(n.title)}><circle cx={n.x} cy={n.y} r={11}/><text x={n.x} y={n.y} dy="0.32em" textAnchor="middle">{(n.title || '?').slice(0, 2).toUpperCase()}</text></g>)}
              </svg> : <p className="rail-empty">暂无页面关系。</p>}</div>
            </section>
            <section className="panel"><div className="panel-heading"><h3>反向链接</h3><span className="tag">{backlinks.length}</span></div>
              <div className="pill-list">{backlinks.length ? backlinks.map(p => <button className="pill" key={p.id} onClick={() => selectByTitle(p.title)}>{p.title}</button>) : <span className="empty">没有反向链接。</span>}</div>
            </section>
            <section className="panel"><div className="panel-heading"><h3>便签 Scratch</h3><button className="ghost sm" onClick={ingestScratch}>拆入</button></div>
              <textarea className="scratch" value={scratch} onChange={e => setScratch(e.target.value)} placeholder="粘贴笔记、URL、摘录或观察，每行一条。URL 归为证据，其余归为结论。"/>
            </section>
          </aside>
        </div>
      </> : <div className="panel editor-empty"><h2>选择一个知识页面</h2><p>从左侧选择一个页面开始编辑，或先为一份客观资料「生成草稿」。页面由概述、结论、证据、关联组成；保存即写入一条不可变修订，发布后进入默认检索。</p></div>}
    </div>
  </section>
}

const SIGNAL_OPS = ['<', '<=', '>', '>=', '==', '!=']
type SignalRule = { signal: string; op: string; threshold: string; minDuration: string }
const defaultRule = (signal: string): SignalRule => ({ signal, op: '<', threshold: '0.9', minDuration: '0' })
function SignalAnalysis() {
  const [datasets, setDatasets] = useState<Json[]>([]), [selectedIds, setSelectedIds] = useState<string[]>([])
  const [channels, setChannels] = useState<Json[]>([]), [loadingChannels, setLoadingChannels] = useState(false)
  const [signalInput, setSignalInput] = useState(''), [rules, setRules] = useState<SignalRule[]>([])
  const [groups, setGroups] = useState<Json[]>([]), [error, setError] = useState(''), [busy, setBusy] = useState(false), [datasetSearch, setDatasetSearch] = useState('')
  useEffect(() => { loadMdfDatasets(setDatasets) }, [])
  const key = selectedIds.join(',')
  useEffect(() => {
    if (!selectedIds.length) { setChannels([]); return }
    setLoadingChannels(true); setError('')
    Promise.all(selectedIds.map(id => api(`/api/datasets/${id}/tools/channels`, { method: 'POST', body: '{}' }).then((data: Json) => data.channels || []).catch(() => [])))
      .then(lists => { const map = new Map<string, Json>(); lists.flat().forEach((channel: Json) => { if (!map.has(channel.name)) map.set(channel.name, channel) }); setChannels([...map.values()]) })
      .finally(() => setLoadingChannels(false))
  }, [key])
  function toggleDataset(id: string) { setSelectedIds(ids => ids.includes(id) ? ids.filter(value => value !== id) : [...ids, id]) }
  function addSignal(name = signalInput) {
    const signal = name.trim()
    if (signal && !rules.some(rule => rule.signal === signal)) setRules(list => [...list, defaultRule(signal)])
    setSignalInput('')
  }
  function removeSignal(name: string) { setRules(list => list.filter(rule => rule.signal !== name)) }
  function updateRule(name: string, patch: Partial<SignalRule>) { setRules(list => list.map(rule => rule.signal === name ? { ...rule, ...patch } : rule)) }
  const targetCount = rules.length * selectedIds.length
  const selectedDatasets = datasets.filter(item => selectedIds.includes(item.id))
  const visibleDatasets = datasets.filter(item => matchesDataset(item, datasetSearch))
  const datasetLabel = datasetDisplayName
  const recommendedChannels = channels.filter((channel: Json) => !rules.some(rule => rule.signal === channel.name)).slice(0, 12)
  async function run() {
    if (!targetCount) return
    setBusy(true); setError(''); setGroups([])
    const collected: Json[] = []
    for (const id of selectedIds) {
      const dataset = datasets.find(item => item.id === id)
      for (const rule of rules) {
        try {
          const result = await api(`/api/datasets/${id}/analysis/windows`, { method: 'POST', body: JSON.stringify({ signal: rule.signal, op: rule.op, threshold: Number(rule.threshold), min_duration: Number(rule.minDuration) }) })
          collected.push({ dataset: dataset ? datasetLabel(dataset) : id, ...rule, ...result })
        } catch (err) { collected.push({ dataset: dataset ? datasetLabel(dataset) : id, ...rule, error: String(err) }) }
      }
    }
    setGroups(collected); setBusy(false)
  }
  return <div className="signal-workbench">
    <div className="panel signal-hero"><div><span className="eyebrow">SIGNAL ANALYSIS</span><h2>按信号配置特征窗口</h2><p>先选数据集，再为每个信号添加独立的条件、阈值和持续时间；同一批分析会按「数据集 × 信号规则」逐组执行。</p></div><div className="signal-metrics"><span><b>{selectedIds.length}</b><small>数据集</small></span><span><b>{rules.length}</b><small>信号规则</small></span><span><b>{targetCount}</b><small>分析组</small></span></div></div>
    <div className="analysis-grid signal-layout">
      <div className="signal-left">
        <div className="panel signal-config"><div className="section-title compact"><div><span className="eyebrow">STEP 1</span><h3>选择数据集</h3></div>{loadingChannels && <span className="tag">读取通道中…</span>}</div>
          {datasets.length > 0 && <input className="dataset-search" value={datasetSearch} onChange={event => setDatasetSearch(event.target.value)} placeholder="搜索文件名、Experiment、日期、A2L、HEX…"/>}
          <div className="ds-picker">{datasets.length === 0 && <p className="muted">无 MDF 数据集，请先在「数据上传」上传。</p>}{datasets.length > 0 && visibleDatasets.length === 0 && <p className="muted">没有匹配的数据集。</p>}{visibleDatasets.map(item => <label className="check dataset-check" key={item.id} title={item.summary?.source_file || item.name}><input type="checkbox" checked={selectedIds.includes(item.id)} onChange={() => toggleDataset(item.id)}/><span><b>{datasetLabel(item)}</b><small>{datasetDetails(item)}</small>{item.description && <small>{item.description}</small>}</span></label>)}</div>
          {selectedDatasets.length > 0 && <div className="selected-summary">已选择：{selectedDatasets.map(datasetLabel).join('、')}</div>}
        </div>
        <div className="panel signal-config"><div className="section-title compact"><div><span className="eyebrow">STEP 2</span><h3>添加信号</h3></div><span className="tag">{channels.length || 0} 通道</span></div>
          <label><span>输入或选择信号</span><div className="signal-add"><input list="cali-channels" value={signalInput} onChange={event => setSignalInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addSignal() } }} placeholder={channels.length ? `输入或选择，共 ${channels.length} 个通道` : '输入信号名'}/><button className="ghost" onClick={() => addSignal()} disabled={!signalInput.trim()}>添加信号</button></div></label>
          <datalist id="cali-channels">{channels.map((channel: Json) => <option key={channel.name} value={channel.name}>{channel.unit ? `${channel.name} (${channel.unit})` : channel.name}</option>)}</datalist>
          <div className="channel-suggestions">{recommendedChannels.map((channel: Json) => <button className="chip-button" key={channel.name} onClick={() => addSignal(channel.name)}>{channel.name}{channel.unit ? <small>{channel.unit}</small> : null}</button>)}</div>
          {!selectedIds.length && <p className="muted hint">选择数据集后会自动读取可选通道。</p>}
        </div>
      </div>
      <div className="panel signal-config signal-rule-panel"><div className="section-title compact"><div><span className="eyebrow">STEP 3</span><h3>每个信号的特征规则</h3></div><button onClick={run} disabled={busy || !targetCount}>{busy ? '分析中…' : `查找窗口${targetCount ? `（${targetCount} 组）` : ''}`}</button></div>
        <div className="signal-rules">
          {rules.length === 0 && <div className="empty-state mini"><h3>还没有信号规则</h3><p className="muted">添加一个信号后，这里会出现独立的条件、阈值和最小持续时间配置。</p></div>}
          {rules.map(rule => <article className="signal-rule" key={rule.signal}>
            <header><div><span className="eyebrow">SIGNAL</span><b>{rule.signal}</b></div><button className="ghost btn-icon" onClick={() => removeSignal(rule.signal)} title="移除信号规则" aria-label={`移除信号 ${rule.signal}`}><TrashIcon /></button></header>
            <div className="config-row"><label><span>条件</span><select value={rule.op} onChange={event => updateRule(rule.signal, { op: event.target.value })}>{SIGNAL_OPS.map(item => <option key={item} value={item}>{item}</option>)}</select></label><label><span>阈值</span><input type="number" step="any" value={rule.threshold} onChange={event => updateRule(rule.signal, { threshold: event.target.value })}/></label><label><span>最小持续 (s)</span><input type="number" step="any" min="0" value={rule.minDuration} onChange={event => updateRule(rule.signal, { minDuration: event.target.value })}/></label></div>
          </article>)}
        </div>
        {error && <div className="notice danger">{error}</div>}
        <p className="muted hint">每张卡片就是一个信号自己的特征选择；不会再和其他信号共用阈值。</p>
      </div>
      <div className="panel window-results"><div className="section-title"><div><span className="eyebrow">WINDOWS</span><h3>{groups.length ? `${groups.length} 组结果` : '窗口结果'}</h3></div></div>
        {!groups.length && !busy && <p className="muted">勾选数据集、添加信号并配置各自条件后点击「查找窗口」。</p>}
        {groups.map((group: Json, groupIndex: number) => <div className="result-group" key={groupIndex}>
          <header className="rg-head"><b>{group.dataset} · {group.signal} {group.op} {group.threshold}{group.unit ? ` ${group.unit}` : ''}</b><span className="tag">{group.error ? '错误' : `${group.window_count} 窗口`}</span></header>
          {group.error ? <div className="notice danger">{group.error}</div> : group.window_count === 0 ? <p className="muted">未发现满足条件的窗口。</p> : <>{group.windows.slice(0, 100).map((window: Json, index: number) => <article className="window-row" key={index}><code>#{index + 1}</code><div><b>{window.start.toFixed(3)}s → {window.end.toFixed(3)}s</b><small>时长 {window.duration.toFixed(3)}s · {window.samples} 点 · min {window.min.toFixed(3)} / mean {window.mean.toFixed(3)} / max {window.max.toFixed(3)}</small><small className="wr-file" title={window.file}>{window.file}</small></div></article>)}{group.window_count > 100 && <p className="muted">仅显示前 100 个，共 {group.window_count} 个窗口。</p>}</>}
        </div>)}
      </div>
    </div>
  </div>
}

function Obfcm() {
  const [datasets, setDatasets] = useState<Json[]>([]), [selectedId, setSelectedId] = useState('')
  const [result, setResult] = useState<Json | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false), [search, setSearch] = useState('')
  useEffect(() => { loadMdfDatasets(setDatasets) }, [])
  const visible = datasets.filter(item => matchesDataset(item, search))
  const fmt = (value?: number, digits = 4) => value === undefined || value === null ? '—' : value.toFixed(digits)
  async function run() {
    if (!selectedId) return
    setBusy(true); setError(''); setResult(null)
    try { setResult(await api(`/api/datasets/${selectedId}/analysis/obfcm`, { method: 'POST', body: '{}' })) }
    catch (err) { setError(String(err)) } finally { setBusy(false) }
  }
  const k = result?.k_factor, acc = result?.obfcm_accuracy
  return <div className="signal-workbench">
    <div className="panel signal-hero"><div><span className="eyebrow">OBFCM</span><h2>车载油耗监测分析</h2><p>从所选数据集读取喷油、负载与排放通道，按碳平衡法计算参考油耗，得到 OBFCM 精度与燃油校正因子 K（按转速 × 负载分区）。</p></div><div className="signal-metrics"><span><b>{result?.samples ?? 0}</b><small>采样点</small></span><span><b>{fmt(k?.mean, 4)}</b><small>平均 K</small></span><span><b>{acc ? `${(acc.mean * 100).toFixed(2)}%` : '—'}</b><small>OBFCM 精度</small></span></div></div>
    <div className="analysis-grid signal-layout">
      <div className="signal-left">
        <div className="panel signal-config"><div className="section-title compact"><div><span className="eyebrow">STEP 1</span><h3>选择数据集</h3></div><button onClick={run} disabled={busy || !selectedId}>{busy ? '分析中…' : '计算 OBFCM'}</button></div>
          {datasets.length > 0 && <input className="dataset-search" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索文件名、Experiment、日期、A2L、HEX…"/>}
          <div className="ds-picker">{datasets.length === 0 && <p className="muted">无 MDF 数据集，请先在「数据上传」上传。</p>}{datasets.length > 0 && visible.length === 0 && <p className="muted">没有匹配的数据集。</p>}{visible.map(item => <label className="check dataset-check" key={item.id} title={item.summary?.source_file || item.name}><input type="radio" name="obfcm-ds" checked={selectedId === item.id} onChange={() => setSelectedId(item.id)}/><span><b>{datasetDisplayName(item)}</b><small>{datasetDetails(item)}</small></span></label>)}</div>
          {error && <div className="notice danger">{error}</div>}
        </div>
      </div>
      <div className="panel window-results"><div className="section-title"><div><span className="eyebrow">RESULT</span><h3>OBFCM 结果与 K 因子</h3></div></div>
        {!result && !busy && <p className="muted">选择一个数据集后点击「计算 OBFCM」。</p>}
        {result && <>
          {!result.reference_fuel_available && <div className="notice">未找到排放通道（{result.channel_map.thc_s} 等），仅计算 K 因子，无法评估 OBFCM 精度。</div>}
          <div className="result-group"><header className="rg-head"><b>汇总指标</b><span className="tag">{result.samples} 点</span></header>
            <article className="window-row"><code>K</code><div><b>平均 K = {fmt(k?.mean, 6)} ± {fmt(k?.std, 6)}</b><small>范围 {fmt(k?.min, 6)} ~ {fmt(k?.max, 6)} · 有效 {k?.count ?? 0} 点</small></div></article>
            {acc && <article className="window-row"><code>ACC</code><div><b>OBFCM 平均精度 = {(acc.mean * 100).toFixed(2)}%</b><small>平均绝对精度 {(result.obfcm_abs_accuracy_mean * 100).toFixed(2)}% · 有效 {acc.count} 点</small></div></article>}
          </div>
          <div className="result-group"><header className="rg-head"><b>K 因子分区表（行=负载，列=转速）</b><span className="tag">{result.rpm_intervals.length - 1}×{result.load_intervals.length - 1}</span></header>
            <table className="k-matrix"><thead><tr><th>负载 \ 转速</th>{result.rpm_intervals.slice(0, -1).map((rpm: number, i: number) => <th key={i}>{rpm}–{result.rpm_intervals[i + 1]}</th>)}</tr></thead>
              <tbody>{result.k_matrix.map((row: Json[], li: number) => <tr key={li}><th>{result.load_intervals[li]}–{result.load_intervals[li + 1]}</th>{row.map((cell: Json, ri: number) => <td key={ri} title={cell ? `${cell.count} 点` : '无数据'}>{cell ? cell.k_avg.toFixed(4) : '—'}</td>)}</tr>)}</tbody></table>
          </div>
        </>}
      </div>
    </div>
  </div>
}

const analysisFeatures = ['数据上传', '信号分析', '排放分析', '瞬态分析', 'OBFCM']
function Analysis() {
  const [feature, setFeature] = useState('数据上传'), [datasets, setDatasets] = useState<Json[]>([]), [results, setResults] = useState<Json[]>([]), [busy, setBusy] = useState(false), [recentDatasetId, setRecentDatasetId] = useState(''), [editingId, setEditingId] = useState(''), [draft, setDraft] = useState<Json>({}), [metadataNotice, setMetadataNotice] = useState('')
  const refresh = () => loadMdfDatasets(setDatasets)
  useEffect(() => { refresh() }, [])
  async function uploadNew(files?: FileList | null) {
    if (!files?.length) return
    setResults([]); setBusy(true); setRecentDatasetId('')
    await uploadEachFileAsDataset(files, (file, ok, detail, datasetId) => {
      if (datasetId) setRecentDatasetId(datasetId)
      setResults(value => [...value, { file, ok, detail }])
    })
    setBusy(false); refresh()
  }
  async function removeDataset(id: string) {
    if (!window.confirm('确定移除这个数据集吗？此操作会从列表和本地归档中删除对应文件。')) return
    await api(`/api/datasets/${id}`, { method: 'DELETE' })
    setRecentDatasetId(value => value === id ? '' : value)
    refresh()
  }
  async function editDataset(item: Json) {
    setEditingId(item.id)
    setDraft({ description: item.description || '', ...item.metadata })
    setMetadataNotice('正在通过内置工具读取 MF4 属性…')
    try {
      setDraft({ description: item.description || '', ...await readDatasetMetadata(item) })
      setMetadataNotice('MF4 属性已读取并填充，可继续修改。')
    } catch (error) { setMetadataNotice(`MF4 属性读取失败：${String(error)}`) }
  }
  async function saveDataset(id: string) {
    await api(`/api/datasets/${id}`, { method: 'PATCH', body: JSON.stringify(draft) })
    setEditingId(''); refresh()
  }
  const totalFiles = datasets.reduce((count, item) => count + item.files.length, 0)
  const successfulUploads = results.filter(item => item.ok).length
  return <section><div className="subnav">{analysisFeatures.map(item => <button key={item} className={feature === item ? 'active' : ''} onClick={() => setFeature(item)}>{item}</button>)}</div>
    {feature === '信号分析' ? <SignalAnalysis /> : feature === 'OBFCM' ? <Obfcm /> : feature !== '数据上传' ? <div className="panel placeholder"><span className="eyebrow">RESERVED</span><h2>{feature}</h2><p>功能入口已预留，待分析规则和输出定义明确后接入。</p><button disabled>待接入</button></div> : <div className="upload-page">
      <div className="upload-overview">
        <label className={`dropzone hero ${busy ? 'uploading' : ''}`}><span className="eyebrow">NEW DATASET</span><span className="dz-title">{busy ? '正在上传并归档…' : '选择 MF4 / MDF / DAT 上传'}</span><span className="dz-sub">一个文件就是一个数据集；一次多选会分别建立多个数据集，重复文件会复用已有数据集。</span><input type="file" accept=".mf4,.mdf,.dat" multiple hidden disabled={busy} onChange={event => uploadNew(event.target.files)}/></label>
        <div className="panel upload-stats"><span className="eyebrow">SUMMARY</span><div><b>{datasets.length}</b><small>数据集</small></div><div><b>{totalFiles}</b><small>已归档文件</small></div><div><b>{busy ? '进行中' : results.length ? `${successfulUploads}/${results.length}` : '就绪'}</b><small>最近上传</small></div></div>
      </div>
      {results.length > 0 && <section className="panel upload-section"><div className="section-title compact"><div><span className="eyebrow">RECENT UPLOADS</span><h2>最近上传结果</h2></div><span className="tag">{successfulUploads}/{results.length} 成功</span></div><div className="upload-log">{results.slice(0, 8).map(item => <div className={`upload-result ${item.ok ? 'ok' : 'fail'}`} key={item.file}><b title={item.file}>{item.file}</b><span>{item.detail}</span></div>)}{results.length > 8 && <p className="muted">仅显示最近 8 条，共 {results.length} 条。</p>}</div></section>}
      <section className="dataset-section"><div className="section-title"><div><span className="eyebrow">DATASETS</span><h2>历史数据集</h2></div>{datasets.length > 0 && <span className="muted">上传完成后可在「信号分析」中选择数据集。</span>}</div>
        {datasets.length === 0 ? <div className="panel empty-state"><h3>还没有数据集</h3><p className="muted">上传 MF4 / MDF / DAT 文件后，会在这里创建可用于信号分析的数据集。</p></div> : <div className="panel dataset-table"><div className="dataset-row dataset-header"><span>文件 / 数据集</span><span>日期</span><span>A2L</span><span>HEX</span><span>测量界面</span><span>操作</span></div>{datasets.map(item => <React.Fragment key={item.id}><div className={`dataset-row ${recentDatasetId === item.id ? 'recent' : ''}`}><div><b className="file-name" title={item.summary?.source_file}>{item.summary?.source_file || item.name}</b><small>{item.serial} · {item.name} · {datasetMeta(item)}</small></div><span>{item.metadata?.measurement_date || '—'}</span><span title={item.metadata?.a2l}>{item.metadata?.a2l || '—'}</span><span title={item.metadata?.hex}>{item.metadata?.hex || '—'}</span><span title={item.metadata?.experiment}>{item.metadata?.experiment || '—'}</span><div className="row-actions"><button className="ghost" onClick={() => editDataset(item)}>编辑标注</button><button className="ghost" onClick={() => setFeature('信号分析')}>分析</button><button className="ghost danger btn-icon" disabled={busy} title="移除数据集" aria-label="移除数据集" onClick={() => removeDataset(item.id)}><TrashIcon /></button></div></div>{editingId === item.id && <div className="metadata-editor"><div className="metadata-notice">{metadataNotice}</div><label><span>日期</span><input type="date" value={draft.measurement_date || ''} onChange={event => setDraft({ ...draft, measurement_date: event.target.value })}/></label><label><span>A2L</span><input value={draft.a2l || ''} onChange={event => setDraft({ ...draft, a2l: event.target.value })}/></label><label><span>HEX</span><input value={draft.hex || ''} onChange={event => setDraft({ ...draft, hex: event.target.value })}/></label><label><span>测量界面（experiment）</span><input value={draft.experiment || ''} onChange={event => setDraft({ ...draft, experiment: event.target.value })}/></label><label className="wide"><span>备注</span><textarea value={draft.description || ''} onChange={event => setDraft({ ...draft, description: event.target.value })}/></label><div className="editor-actions"><button className="ghost" onClick={() => setEditingId('')}>取消</button><button onClick={() => saveDataset(item.id)}>保存</button></div></div>}</React.Fragment>)}</div>}
      </section>
    </div>}
  </section>
}

function ChangeCells({ detail }: { detail?: Json }) {
  if (!detail || !detail.chg?.length) return null
  return <details className="cell-diff"><summary>{detail.nChg} 个单元 ↑{detail.nUp} ↓{detail.nDn}</summary>
    <div className="cell-diff-list">{detail.chg.map((cell: Json, index: number) => <code key={index}>{cell.r !== undefined ? `[${cell.r},${cell.c}]` : `[${cell.i}]`} {cell.o} → {cell.n}</code>)}</div>
  </details>
}

function CalibrationTraceability() {
  const [datasets, setDatasets] = useState<Json[]>([]), [datasetId, setDatasetId] = useState(''), [report, setReport] = useState<Json | null>(null)
  const [selectedId, setSelectedId] = useState(''), [view, setView] = useState<'timeline' | 'baseline' | 'matrix'>('timeline'), [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false), [notice, setNotice] = useState('')
  useEffect(() => { api('/api/datasets').then((items: Json[]) => { const values = items.filter(item => item.kind === 'calibration'); setDatasets(values); setDatasetId(values[0]?.id || '') }).catch(error => setNotice(String(error))) }, [])
  useEffect(() => { if (datasetId) loadReport(datasetId); else setReport(null) }, [datasetId])
  async function loadReport(id = datasetId) {
    if (!id) return
    setBusy(true); setNotice('')
    try {
      const value = await api(`/api/datasets/${id}/evolution`)
      setReport(value)
      const first = value.comparisons.find((item: Json) => item.mode === 'adjacent')
      setSelectedId(first?.id || '')
    } catch (error) { setNotice(String(error)); setReport(null) } finally { setBusy(false) }
  }
  const comparisons = report?.comparisons || [], adjacent = comparisons.filter((item: Json) => item.mode === 'adjacent')
  const baseline = comparisons.filter((item: Json) => item.mode === 'baseline')
  const baselineRows = [...adjacent.filter((item: Json) => item.from_version === 1), ...baseline]
  const selected = comparisons.find((item: Json) => item.id === selectedId)
  const filteredChanges = (selected?.changes || []).filter((item: Json) => !query.trim() || `${item.parameter_name} ${item.module} ${item.a2l_description}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
  const maxBaseline = Math.max(1, ...baselineRows.map((item: Json) => item.total))
  const typeCount = (item: Json, type: string) => item?.counts?.[type] || 0
  return <section className="evolution-report">
    <div className="panel evolution-head"><div><span className="eyebrow">CALIBRATION EVOLUTION</span><h2>标定版本演进报告</h2><p className="muted">基于数据集 Commit 自动比较；结构参考 EngineBench Evolution Report。</p></div><div className="report-controls"><label><span>标定数据集</span><select value={datasetId} onChange={event => setDatasetId(event.target.value)}><option value="">请选择</option>{datasets.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><button className="ghost" disabled={busy || !datasetId} onClick={() => loadReport()}>{busy ? '计算中…' : '重新计算'}</button></div></div>
    {notice && <div className="notice danger">{notice}</div>}
    {!datasetId && <div className="panel empty-state"><h3>还没有标定数据集</h3><p className="muted">先在“A2L / HEX 数据集”中上传文件并建立版本。</p></div>}
    {report && <>
      <div className="report-kpis"><div className="panel report-kpi"><b>{report.latest_label_count.toLocaleString()}</b><span>当前 A2L Labels</span></div><div className="panel report-kpi"><b>{report.versions.length}</b><span>HEX 版本</span></div><div className="panel report-kpi"><b>{comparisons.length}</b><span>比较组合</span></div><div className={`panel report-kpi ${report.a2l_updates.length ? 'warn' : ''}`}><b>{report.a2l_updates.length}</b><span>A2L 更新</span></div><div className="panel report-kpi"><b>{report.max_baseline_diff}</b><span>最大基线差异</span></div></div>
      {report.anomalies.length > 0 && <div className="report-alert"><b>{report.anomalies.length} 组文件无法比较</b>{report.anomalies.map((item: Json) => <span key={item.id}>{item.comparison} · {item.detail}</span>)}</div>}
      {report.a2l_updates.length > 0 && <div className="panel a2l-update-summary"><h3>A2L 更新摘要</h3><div className="report-table-wrap"><table><thead><tr><th>版本迁移</th><th>旧 A2L</th><th>新 A2L</th><th>+新增</th><th>-移除</th><th>共同参数变化</th></tr></thead><tbody>{report.a2l_updates.map((item: Json) => <tr key={`${item.from_version}-${item.to_version}`}><td><b>V{item.from_version} → V{item.to_version}</b></td><td><code>{item.old_name}</code></td><td><code>{item.new_name}</code></td><td className="positive">+{item.added}</td><td className="negative">-{item.removed}</td><td>{item.common_changes}</td></tr>)}</tbody></table></div></div>}
      <div className="report-tabs"><button className={view === 'timeline' ? 'active' : ''} onClick={() => setView('timeline')}>版本演进</button><button className={view === 'baseline' ? 'active' : ''} onClick={() => setView('baseline')}>相对基线漂移</button><button className={view === 'matrix' ? 'active' : ''} onClick={() => setView('matrix')}>完整比较矩阵</button></div>
      {report.versions.length < 2 ? <div className="panel empty-state"><h3>至少需要两个版本</h3><p className="muted">回到数据集添加下一份 HEX，并建立新版本。</p></div> : view === 'timeline' ? <div className="panel timeline-panel"><div className="section-title compact"><div><span className="eyebrow">EVOLUTION TIMELINE</span><h3>版本演进</h3></div><span className="muted">点击差异数字查看明细</span></div><div className="evolution-track">{report.versions.map((version: Json, index: number) => { const transition = adjacent.find((item: Json) => item.from_version === version.index); return <React.Fragment key={version.id}><div className="version-node"><b>V{version.index}</b><span title={version.hex.name}>{version.hex.name}</span><small title={version.a2l.name}>{version.a2l.name}</small></div>{index < report.versions.length - 1 && <button className={`transition-edge ${transition?.a2l_updated ? 'a2l-updated' : ''} ${selectedId === transition?.id ? 'active' : ''}`} disabled={!transition} onClick={() => transition && setSelectedId(transition.id)}><i>{transition?.total ?? '!'}</i><span>{transition?.a2l_updated ? 'A2L ↑' : '相同 A2L'}</span></button>}</React.Fragment>})}</div></div>
      : view === 'baseline' ? <div className="panel baseline-panel"><div className="section-title compact"><div><span className="eyebrow">BASELINE DRIFT</span><h3>相对 V1 的累计差异</h3></div></div><div className="baseline-chart">{baselineRows.map((item: Json) => <button className="baseline-bar" key={item.id} onClick={() => { setSelectedId(item.id); setView('timeline') }}><b>{item.total}</b><div className="bar-stack-report"><i className={`map level-${Math.min(10, typeCount(item, 'MAP'))}`}/><i className={`curve level-${Math.min(10, typeCount(item, 'CURVE'))}`}/><i className={`value level-${Math.min(10, typeCount(item, 'VALUE'))}`}/><i className={`valblk level-${Math.min(10, typeCount(item, 'VAL_BLK'))}`}/></div><span>V{item.to_version}</span>{item.a2l_updated && <small>A2L ↑</small>}</button>)}</div><div className="chart-key"><span className="map">MAP</span><span className="curve">CURVE</span><span className="value">VALUE</span><span className="valblk">VAL_BLK</span></div></div>
      : <div className="panel matrix-panel"><div className="section-title compact"><div><span className="eyebrow">COMPARISON MATRIX</span><h3>完整比较矩阵</h3></div></div><div className="report-table-wrap"><table><thead><tr><th>比较</th><th>A2L</th><th>MAP</th><th>CURVE</th><th>VALUE</th><th>VAL_BLK</th><th>总计</th><th></th></tr></thead><tbody>{comparisons.map((item: Json) => <tr className={item.a2l_updated ? 'a2l-row' : ''} key={item.id}><td><b>V{item.from_version} → V{item.to_version}</b><small>{item.mode === 'baseline' ? '基线' : '相邻'}</small></td><td><code>{item.a2l_updated ? `${item.a2l_old} → ${item.a2l_new}` : item.a2l_new}</code></td><td>{typeCount(item, 'MAP')}</td><td>{typeCount(item, 'CURVE')}</td><td>{typeCount(item, 'VALUE')}</td><td>{typeCount(item, 'VAL_BLK')}</td><td><b>{item.total}</b></td><td><button className="link-button" onClick={() => { setSelectedId(item.id); setView('timeline') }}>查看</button></td></tr>)}</tbody></table></div></div>}
      {selected && view === 'timeline' && <div className="panel comparison-detail"><div className="comparison-title"><div><span className="eyebrow">V{selected.from_version} → V{selected.to_version}</span><h3>{selected.from_hex} → {selected.to_hex}</h3></div><span className={`tag ${selected.a2l_updated ? 'calibration' : ''}`}>{selected.a2l_updated ? 'A2L 已更新' : '相同 A2L'}</span></div><div className="detail-kpis"><span><b>{selected.total}</b>总变化</span><span><b>{typeCount(selected, 'MAP')}</b>MAP</span><span><b>{typeCount(selected, 'CURVE')}</b>CURVE</span><span><b>{typeCount(selected, 'VALUE')}</b>VALUE</span><span><b>{typeCount(selected, 'VAL_BLK')}</b>VAL_BLK</span></div><input className="change-search" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索参数、模块或 A2L 描述…"/>{filteredChanges.length === 0 ? <div className="empty-compact"><b>没有匹配的参数变化</b></div> : <div className="report-table-wrap change-detail-table"><table><thead><tr><th>参数</th><th>类型</th><th>模块</th><th>变化明细</th></tr></thead><tbody>{filteredChanges.map((item: Json) => <tr key={`${item.parameter_name}-${item.module}`}><td><b>{item.parameter_name}</b><small>{item.a2l_description}</small></td><td><span className={`param-type ${item.char_type.toLowerCase()}`}>{item.char_type}</span></td><td>{item.module}</td><td>{item.char_type === 'VALUE' || item.char_type === 'ASCII' ? <code>{item.old_value} → {item.new_value} {item.unit}</code> : <><span>{item.new_value}</span><ChangeCells detail={item.detail}/></>}</td></tr>)}</tbody></table></div>}</div>}
    </>}
  </section>
}

function Management() {
  const [view, setView] = useState<'library' | 'trace'>('library')
  return <section><div className="subnav"><button className={view === 'library' ? 'active' : ''} onClick={() => setView('library')}>A2L / HEX 数据集</button><button className={view === 'trace' ? 'active' : ''} onClick={() => setView('trace')}>参数演进报告</button></div>{view === 'library' ? <CommitLibrary onCompare={() => setView('trace')}/> : <CalibrationTraceability />}</section>
}

function CommitLibrary({ onCompare }: { onCompare: () => void }) {
  const [datasets, setDatasets] = useState<Json[]>([]), [selectedId, setSelectedId] = useState(''), [name, setName] = useState(''), [commits, setCommits] = useState<Json[]>([])
  const [notice, setNotice] = useState(''), [busy, setBusy] = useState(false), [initialFiles, setInitialFiles] = useState<File[]>([])
  const [matches, setMatches] = useState<Record<string, Json>>({}), [manualA2l, setManualA2l] = useState<Record<string, string>>({})
  const [datasetQuery, setDatasetQuery] = useState(''), [hexQuery, setHexQuery] = useState(''), [pairFilter, setPairFilter] = useState<'pending' | 'all' | 'committed'>('pending')
  const selected = datasets.find(item => item.id === selectedId)
  const a2lFiles = selected?.files.filter((file: Json) => file.suffix === '.a2l') || [], hexFiles = selected?.files.filter((file: Json) => file.suffix === '.hex') || []
  const fileKey = selected?.files.map((file: Json) => file.id).join(',') || ''
  async function refresh(preferredId = selectedId) {
    const items = await api('/api/datasets'), calibration = items.filter((item: Json) => item.kind === 'calibration')
    setDatasets(calibration); setSelectedId(calibration.some((item: Json) => item.id === preferredId) ? preferredId : calibration[0]?.id || '')
    return calibration.find((item: Json) => item.id === preferredId) || calibration[0]
  }
  async function removeDataset(id: string) {
    if (busy) return
    const target = datasets.find(item => item.id === id)
    if (!target) return
    if (!window.confirm(`删除数据集「${target.name}」？这会删除其所有标定版本。`)) return
    setBusy(true)
    setNotice('')
    try {
      await api(`/api/datasets/${id}`, { method: 'DELETE' })
      const next = await refresh(id)
      setCommits([])
      setMatches({})
      if (next) await loadWorkspace(next.id, next.files || [])
    } catch (error) {
      setNotice(String(error))
    } finally {
      setBusy(false)
    }
  }
  async function loadWorkspace(id: string, files: Json[]) {
    try {
      const history = await api(`/api/datasets/${id}/commits`)
      setCommits(history)
      const results = await Promise.all(files.filter(file => file.suffix === '.hex').map(async file => [file.id, await api(`/api/datasets/${id}/a2l-match/${file.id}`)]))
      setMatches(Object.fromEntries(results))
    } catch (error) { setNotice(String(error)) }
  }
  useEffect(() => { refresh() }, [])
  useEffect(() => { if (selectedId) loadWorkspace(selectedId, selected?.files || []); else { setCommits([]); setMatches({}) } }, [selectedId, fileKey])
  async function autoPair(dataset: Json, history: Json[]) {
    const committed = new Set(history.map(item => item.hex.id)), nextMatches: Record<string, Json> = {}
    let parentId = history[0]?.id || null, created = 0
    const pending: string[] = []
    for (const hex of dataset.files.filter((file: Json) => file.suffix === '.hex').sort((a: Json, b: Json) => a.name.localeCompare(b.name))) {
      if (committed.has(hex.id)) continue
      const match = await api(`/api/datasets/${dataset.id}/a2l-match/${hex.id}`)
      nextMatches[hex.id] = match
      if (match.status !== 'matched') { pending.push(hex.name); continue }
      const version = await api(`/api/datasets/${dataset.id}/commits`, { method: 'POST', body: JSON.stringify({ parent_id: parentId, a2l_file_id: match.a2l.id, hex_file_id: hex.id }) })
      history = [version, ...history]; parentId = version.id; committed.add(hex.id); created += 1
    }
    setCommits(history); setMatches(value => ({ ...value, ...nextMatches }))
    return { created, pending }
  }
  async function createDataset() {
    const a2l = initialFiles.filter(file => file.name.toLowerCase().endsWith('.a2l'))
    const hex = initialFiles.filter(file => file.name.toLowerCase().endsWith('.hex'))
    if (!name.trim() || !hex.length) return
    setBusy(true); setNotice('')
    let created: Json | null = null
    try {
      const dataset = await api('/api/datasets', { method: 'POST', body: JSON.stringify({ name: name.trim(), kind: 'calibration', description: 'A2L / HEX 标定数据集' }) })
      created = dataset
      let uploaded = dataset
      for (const file of [...a2l, ...hex]) {
        const form = new FormData(); form.append('file', file)
        uploaded = await api(`/api/datasets/${dataset.id}/files`, { method: 'POST', body: form })
        created = uploaded
      }
      const result = await autoPair(uploaded, [])
      setName(''); setInitialFiles([]); setSelectedId(dataset.id); await refresh(dataset.id)
      setNotice(result.pending.length ? `已建立 ${result.created} 个版本；${result.pending.length} 个 HEX 未找到唯一 A2L，请补充或手动选择。` : `已自动配对并建立 ${result.created} 个版本。`)
    } catch (error) {
      setNotice(`${created ? '数据集已创建，但自动配对未完成：' : ''}${String(error)}`); await refresh(created?.id)
    } finally { setBusy(false) }
  }
  async function uploadAndPair(files?: FileList | null) {
    if (!files?.length || !selectedId) return
    setBusy(true); setNotice('')
    let updated = selected
    try {
      for (const file of Array.from(files)) { const form = new FormData(); form.append('file', file); updated = await api(`/api/datasets/${selectedId}/files`, { method: 'POST', body: form }) }
      if (!updated) return
      const result = await autoPair(updated, commits)
      await refresh(selectedId)
      setNotice(result.pending.length ? `已建立 ${result.created} 个版本；${result.pending.length} 个 HEX 仍需 A2L。` : `上传完成，自动建立 ${result.created} 个新版本。`)
    } catch (error) { setNotice(String(error)) } finally { setBusy(false) }
  }
  async function createManualVersion(hexId: string) {
    const a2lId = manualA2l[hexId] || matches[hexId]?.a2l?.id
    if (!selectedId || !a2lId) return
    setBusy(true); setNotice('')
    try {
      await api(`/api/datasets/${selectedId}/commits`, { method: 'POST', body: JSON.stringify({ parent_id: commits[0]?.id || null, a2l_file_id: a2lId, hex_file_id: hexId }) })
      setNotice('版本已建立。'); setCommits(await api(`/api/datasets/${selectedId}/commits`))
    } catch (error) { setNotice(String(error)) } finally { setBusy(false) }
  }
  const committedByHex = new Map(commits.map(item => [item.hex.id, item])), initialA2lCount = initialFiles.filter(file => file.name.toLowerCase().endsWith('.a2l')).length, initialHexCount = initialFiles.filter(file => file.name.toLowerCase().endsWith('.hex')).length
  function hexStatus(hex: Json) {
    if (committedByHex.has(hex.id)) return 'committed'
    return matches[hex.id]?.status || 'checking'
  }
  const visibleDatasets = datasets.filter(item => {
    const keyword = datasetQuery.trim().toLocaleLowerCase()
    if (!keyword) return true
    return [item.serial, item.name, item.description, ...(item.files || []).map((file: Json) => file.name)]
      .filter(Boolean).join(' ').toLocaleLowerCase().includes(keyword)
  })
  const pendingHexCount = hexFiles.filter((file: Json) => hexStatus(file) !== 'committed').length
  const sortedHexFiles = [...hexFiles].sort((left, right) => {
    const l = hexStatus(left), r = hexStatus(right)
    if (l === 'committed' && r !== 'committed') return 1
    if (l !== 'committed' && r === 'committed') return -1
    return left.name.localeCompare(right.name)
  })
  const visibleHexFiles = sortedHexFiles.filter(hex => {
    const status = hexStatus(hex)
    if (pairFilter === 'pending' && status === 'committed') return false
    if (pairFilter === 'committed' && status !== 'committed') return false
    if (!hexQuery.trim()) return true
    return hex.name.toLocaleLowerCase().includes(hexQuery.trim().toLocaleLowerCase())
  })
  return <section className="calibration-library">
    <div className="panel library-intro"><div><span className="eyebrow">CALIBRATION DATA</span><h2>A2L / HEX 自动配对</h2><p className="muted">批量上传 A2L 与 HEX；系统按参数地址覆盖自动匹配，只有唯一匹配才建立可追溯版本。</p></div><div className="library-metrics"><span><b>{datasets.length}</b> 数据集</span><span><b>{a2lFiles.length}</b> A2L</span><span><b>{commits.length}</b> 已配对版本</span></div></div>
    <div className="library-layout">
      <div className="panel repository-list"><div className="section-title compact"><div><span className="eyebrow">DATASETS</span><h3>数据集</h3></div></div>
        <div className="quick-create"><b>新建并批量导入</b><p>至少选择一份 HEX；没有合适 A2L 时，数据会保留并提示补充。</p><input value={name} maxLength={80} onChange={event => setName(event.target.value)} placeholder="例如：VCU 项目标定"/><label className="file-picker"><span>{initialFiles.length ? `已选择 ${initialFiles.length} 个文件` : '选择多个 A2L / HEX'}</span><small>支持多选 .a2l 与 .hex</small><input type="file" accept=".a2l,.hex" multiple hidden disabled={busy} onChange={event => setInitialFiles(Array.from(event.target.files || []))}/></label>
          {initialFiles.length > 0 && <div className={`file-check ${initialHexCount > 0 ? 'valid' : 'invalid'}`}>A2L {initialA2lCount} · HEX {initialHexCount}</div>}
          <button onClick={createDataset} disabled={busy || !name.trim() || initialHexCount < 1}>{busy ? '正在分析配对…' : '导入并自动配对'}</button></div>
        <input className="dataset-search" value={datasetQuery} onChange={event => setDatasetQuery(event.target.value)} placeholder="搜索数据集名、序列号或文件名"/>
        <div className="repository-items">{visibleDatasets.map(item => <div key={item.id} className={`repository-item ${selectedId === item.id ? 'active' : ''}`}><button className="repository-item-main" onClick={() => setSelectedId(item.id)}><span><b>{item.name}</b><small>{item.serial} · {item.files.filter((file: Json) => file.suffix === '.a2l').length} A2L · {item.files.filter((file: Json) => file.suffix === '.hex').length} HEX</small></span><i>{item.files.filter((file: Json) => file.suffix === '.hex').length}</i></button><button className="ghost sm danger btn-icon repository-item-delete" disabled={busy} title="删除数据集" aria-label="删除数据集" onClick={() => removeDataset(item.id)}><TrashIcon /></button></div>)}{datasets.length === 0 && <p className="muted">还没有标定数据集。</p>}{datasets.length > 0 && visibleDatasets.length === 0 && <p className="muted">没有匹配的数据集。</p>}</div>
      </div>
      <div className="library-content">
        {!selected ? <div className="panel empty-state"><h3>先建立标定数据集</h3><p className="muted">一次选择多份 A2L 与 HEX，系统会自动配对。</p></div> : <>
          <div className="panel pairing-head"><div><span className="eyebrow">{selected.serial}</span><h2>{selected.name}</h2><p className="muted">{a2lFiles.length} A2L · {hexFiles.length} HEX · {commits.length} 已配对版本</p></div><div className="actions"><button className="ghost" disabled={commits.length < 2} onClick={onCompare}>查看版本对比</button><label className="button">{busy ? '正在识别…' : '添加 A2L / HEX'}<input type="file" accept=".a2l,.hex" multiple hidden disabled={busy} onChange={event => uploadAndPair(event.target.files)}/></label></div></div>
          <div className="panel pairing-panel"><div className="pairing-focus"><div><span className="eyebrow">FOCUS</span><h3>待处理配对</h3><p className="muted">先处理未配对 HEX，再补齐版本链。</p></div><div className={`focus-kpi ${pendingHexCount ? 'warn' : 'ok'}`}><b>{pendingHexCount}</b><small>待处理 HEX</small></div></div><div className="section-title compact"><div><span className="eyebrow">AUTO MATCH</span><h3>HEX 与 A2L 配对</h3></div><span className="tag calibration">{commits.length}/{hexFiles.length} 已建版</span></div>
            <div className="pairing-toolbar"><div className="pair-filters"><button className={pairFilter === 'pending' ? 'active' : ''} onClick={() => setPairFilter('pending')}>待处理</button><button className={pairFilter === 'all' ? 'active' : ''} onClick={() => setPairFilter('all')}>全部</button><button className={pairFilter === 'committed' ? 'active' : ''} onClick={() => setPairFilter('committed')}>已配对</button></div><input value={hexQuery} onChange={event => setHexQuery(event.target.value)} placeholder="搜索 HEX 文件名"/></div>
            {hexFiles.length === 0 && <div className="empty-compact"><b>还没有 HEX</b><span>点击上方“添加 A2L / HEX”开始。</span></div>}
            <div className="pairing-list">{visibleHexFiles.map((hex: Json) => { const version = committedByHex.get(hex.id), match = matches[hex.id], status = version ? 'committed' : match?.status || 'checking'; const selectedA2l = manualA2l[hex.id] || match?.a2l?.id || ''; return <div className={`pairing-row ${status}`} key={hex.id}><div className="pair-file"><span className="file-kind hex">HEX</span><div><b title={hex.name}>{hex.name}</b><small>#{hex.sha256.slice(0, 8)} · {formatBytes(hex.size)}</small></div></div><span className="pair-arrow">→</span><div className="pair-target">{version ? <><b>{version.a2l.name}</b><small>已建立版本</small></> : status === 'checking' ? <span className="muted">正在检查…</span> : <><select aria-label={`为 ${hex.name} 选择 A2L`} title={`为 ${hex.name} 选择 A2L`} value={selectedA2l} onChange={event => setManualA2l(value => ({ ...value, [hex.id]: event.target.value }))}><option value="">{status === 'missing' ? '未找到匹配 A2L' : status === 'ambiguous' ? '多个候选，请选择' : '选择 A2L'}</option>{a2lFiles.map((file: Json) => <option key={file.id} value={file.id}>{file.name}</option>)}</select>{match?.a2l && <small>地址覆盖 {match.a2l.matched}/{match.a2l.total}（{(match.a2l.coverage * 100).toFixed(0)}%）</small>}</>}</div><div className="pair-action">{version ? <span className="match-state ok">已配对</span> : status === 'missing' && !a2lFiles.length ? <label className="button ghost sm">上传 A2L<input type="file" accept=".a2l" multiple hidden disabled={busy} onChange={event => uploadAndPair(event.target.files)}/></label> : <button className="ghost sm" disabled={busy || !selectedA2l} onClick={() => createManualVersion(hex.id)}>建立版本</button>}</div></div> })}</div>
            {hexFiles.length > 0 && visibleHexFiles.length === 0 && <div className="empty-compact"><b>没有匹配的 HEX</b><span>调整筛选条件或搜索关键词。</span></div>}
          </div>
          <div className="panel commit-history"><div className="section-title compact"><div><span className="eyebrow">VERSION HISTORY</span><h3>版本历史</h3></div><span className="tag calibration">{commits.length} 个版本</span></div>{commits.length === 0 && <div className="empty-compact"><b>暂无可比较版本</b><span>上传匹配的 A2L 后会自动建版。</span></div>}{commits.map((item, index) => <article className="commit" key={item.id}><span className="version-index">v{commits.length - index}</span><div><b>{item.hex.name}</b><small>{item.a2l.name}</small><small>HEX +{item.diff.hex.lines.added} / -{item.diff.hex.lines.removed}</small></div><time>{new Date(item.created_at).toLocaleString()}</time></article>)}</div>
        </>}
      </div>
    </div>{notice && <div className="notice library-notice">{notice}</div>}
  </section>
}

function MergeMf4() {
  const [fileA, setFileA] = useState<File | null>(null), [fileB, setFileB] = useState<File | null>(null), [name, setName] = useState('')
  const [busy, setBusy] = useState(false), [saving, setSaving] = useState(false), [notice, setNotice] = useState(''), [error, setError] = useState('')
  const [result, setResult] = useState<Json | null>(null)
  async function merge() {
    if (!fileA || !fileB) return
    setBusy(true); setError(''); setNotice(''); setResult(null)
    try {
      const form = new FormData()
      form.append('file_a', fileA); form.append('file_b', fileB); form.append('name', name.trim())
      const merged = await api('/api/tools/merge_mf4', { method: 'POST', body: form })
      setResult(merged)
      setNotice(`合并完成：${merged.name}（${formatBytes(merged.size)}）`)
    } catch (err) { setError(String(err)) } finally { setBusy(false) }
  }
  async function saveAsDataset() {
    if (!result) return
    setSaving(true); setError('')
    try {
      const ds = await api(`/api/tools/merge_mf4/${result.token}/save`, { method: 'POST' })
      setNotice(`已另存为数据集「${ds.summary?.display_name || ds.name}」，可在「数据分析 / 数据管理」中使用。`)
    } catch (err) { setError(String(err)) } finally { setSaving(false) }
  }
  const picker = (label: string, file: File | null, set: (f: File | null) => void) => <label className="file-pick">
    <span>{label}</span>
    <span className="file-pick-box">{file ? file.name : '点击选择 MF4 / MDF / DAT'}</span>
    <input type="file" accept=".mf4,.mdf,.dat" hidden onChange={event => { set(event.target.files?.[0] || null); setResult(null) }}/>
  </label>
  function pickFolder(files: FileList | null) {
    const candidates = Array.from(files || []).filter(file => /\.(?:mf4|mdf|dat)$/i.test(file.name))
      .sort((left, right) => left.name.localeCompare(right.name))
    setResult(null); setError('')
    if (candidates.length !== 2) {
      setFileA(null); setFileB(null)
      setError(`文件夹中需要恰好 2 个 MF4 / MDF / DAT 文件，当前找到 ${candidates.length} 个。`)
      return
    }
    setFileA(candidates[0]); setFileB(candidates[1])
    setNotice(`已从文件夹选择：${candidates[0].name} + ${candidates[1].name}`)
  }
  return <article className="panel merge-tool"><span className="tag">TOOL</span><h2>merge_mf4</h2>
    <p>选择一个含两个测量文件的文件夹，或分别上传两个 MF4 / MDF / DAT 文件。系统按车速互相关完成时间对齐后 stack；合并后可下载或另存为数据集。</p>
    <label className="file-pick"><span>从文件夹自动选择两个文件</span><span className="file-pick-box">点击选择文件夹</span><input type="file" accept=".mf4,.mdf,.dat" multiple hidden {...({ webkitdirectory: '', directory: '' } as any)} onChange={event => pickFolder(event.target.files)}/></label>
    {picker('文件 A（台架 / dyno）', fileA, setFileA)}
    {picker('文件 B（ECU）', fileB, setFileB)}
    <label><span>输出文件名（可选）</span><input value={name} maxLength={80} onChange={event => setName(event.target.value)} placeholder="留空则自动命名"/></label>
    <div className="actions">
      <button onClick={merge} disabled={busy || !fileA || !fileB}>{busy ? '合并中…（大文件较慢）' : '合并'}</button>
      {result && <a className="button ghost" href={`/api/tools/merge_mf4/${result.token}/download`}>下载合并结果</a>}
      {result && <button className="ghost" onClick={saveAsDataset} disabled={saving}>{saving ? '保存中…' : '另存为数据集'}</button>}
    </div>
    {notice && <div className="notice">{notice}</div>}{error && <div className="notice danger">{error}</div>}
  </article>
}

function ExcelToMf4() {
  const [file, setFile] = useState<File | null>(null), [name, setName] = useState(''), [sampleRate, setSampleRate] = useState('1'), [unitsRow, setUnitsRow] = useState('auto')
  const [busy, setBusy] = useState(false), [notice, setNotice] = useState(''), [error, setError] = useState('')
  const [result, setResult] = useState<Json | null>(null), [saved, setSaved] = useState<Json | null>(null)
  async function convert() {
    if (!file) return
    setBusy(true); setError(''); setNotice(''); setResult(null); setSaved(null)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('sample_rate', sampleRate || '1')
      form.append('units_row', unitsRow)
      if (name.trim()) form.append('name', name.trim())
      const converted = await api('/api/tools/excel_to_mf4', { method: 'POST', body: form })
      setResult(converted)
      setNotice(`已生成「${converted.name}」：${converted.channels?.length || 0} 个通道、${converted.rows || 0} 行（时间来源：${converted.time_source}）`)
    } catch (err) { setError(String(err)) } finally { setBusy(false) }
  }
  async function save() {
    if (!result?.token) return
    setBusy(true); setError(''); setNotice('')
    try {
      const dataset = await api(`/api/tools/excel_to_mf4/${result.token}/save`, { method: 'POST' })
      setSaved(dataset)
      setNotice(`已保存为数据集「${dataset.summary?.display_name || dataset.name}」`)
    } catch (err) { setError(String(err)) } finally { setBusy(false) }
  }
  return <article className="panel merge-tool"><span className="tag">TOOL</span><h2>excel_to_mf4</h2><p>把 Excel(.xlsx) 转换为 MF4：第 1 行通道名、可选第 2 行单位，自动识别时间列（无则按采样率生成时间轴），非数值列丢弃。</p>
    <label className="dropzone"><span className="eyebrow">UPLOAD XLSX</span><span className="dz-title">{file ? file.name : '选择 / 拖入 .xlsx'}</span><span className="dz-sub">单个工作簿的活动表；列为信号，行为采样点。</span><input type="file" accept=".xlsx" hidden onChange={event => { setFile(event.target.files?.[0] || null); setResult(null); setSaved(null) }}/></label>
    <label><span>采样率 Hz（无时间列时生成时间轴）</span><input type="number" min="0" step="any" value={sampleRate} onChange={event => setSampleRate(event.target.value)} placeholder="1"/></label>
    <label><span>单位行（第 2 行）</span><select value={unitsRow} onChange={event => setUnitsRow(event.target.value)}><option value="auto">自动识别</option><option value="yes">第 2 行是单位</option><option value="no">没有单位行</option></select></label>
    <label><span>输出文件名（可选）</span><input value={name} maxLength={80} onChange={event => setName(event.target.value)} placeholder="留空则按源文件命名"/></label>
    <div className="actions">
      <button onClick={convert} disabled={busy || !file}>{busy ? '处理中…' : '转换'}</button>
      {result?.token && <a className="button ghost" href={`/api/tools/excel_to_mf4/${result.token}/download`}>下载 MF4</a>}
      {result?.token && <button className="ghost" onClick={save} disabled={busy || !!saved}>{saved ? '已保存' : '保存为数据集'}</button>}
    </div>
    {result?.skipped?.length ? <div className="notice">已跳过非数值列：{result.skipped.join('、')}</div> : null}
    {notice && <div className="notice">{notice}</div>}{error && <div className="notice danger">{error}</div>}
  </article>
}

function Tools() {
  return <section className="tools-page">
    <MergeMf4 />
    <ExcelToMf4 />
    <div className="tool-grid">
      <article className="panel tool-card"><span className="tag">TOOL</span><h2>emission_summary</h2><p>生成排放数据摘要</p><button disabled>待接入</button></article>
    </div>
  </section>
}

function Runs() {
  const [runs, setRuns] = useState<Json[]>([])
  useEffect(() => { const load = () => api('/api/runs').then(setRuns); load(); const id = setInterval(load, 3000); return () => clearInterval(id) }, [])
  return <div className="panel runs-panel"><div className="section-title"><div><span className="eyebrow">CHECKPOINTS & AUDIT</span><h2>Agent 运行记录</h2></div><span className="live">● LIVE</span></div><div className="run-table"><div className="run head"><span>运行</span><span>类型</span><span>当前节点</span><span>状态</span><span>时间</span></div>{runs.map(run => <div className="run" key={run.id}><code>{run.id.slice(0, 10)}</code><span>{run.kind}</span><span>{run.current_node}</span><span className={`state ${run.status}`}>{run.status}</span><small>{new Date(run.updated_at).toLocaleString()}</small></div>)}</div></div>
}

function Settings() {
  const [form, setForm] = useState<Json>({ provider: 'disabled', base_url: 'http://127.0.0.1:11434', chat_model: 'qwen3:8b', reasoning_model: 'qwen3:8b', embedding_model: '', api_key: '', trust_env: false, proxy_url: '' }), [note, setNote] = useState('')
  useEffect(() => { api('/api/settings/model').then(value => setForm((current: Json) => ({ ...current, ...value, api_key: '', proxy_url: '' }))) }, [])
  const field = (key: string, label: string, type = 'text') => <label><span>{label}</span><input type={type} value={form[key] || ''} onChange={event => setForm({ ...form, [key]: event.target.value })}/></label>
  function selectProvider(provider: string) {
    setForm(provider === 'zhipu' ? { ...form, provider, base_url: 'https://open.bigmodel.cn/api/paas/v4', chat_model: 'glm-4.5-air', reasoning_model: 'glm-4.5-air' } : { ...form, provider })
  }
  async function save() { const value = await api('/api/settings/model', { method: 'PUT', body: JSON.stringify(form) }); setNote(`已保存：${value.provider}`) }
  async function test() { try { const value = await api('/api/settings/model/test', { method: 'POST' }); setNote(value.detail) } catch (error) { setNote(String(error)) } }
  return <section className="settings-page"><div className="settings-grid"><div className="panel form"><span className="eyebrow">MODEL PROFILE</span><h2>模型连接</h2><label><span>提供方</span><select value={form.provider} onChange={event => selectProvider(event.target.value)}><option value="disabled">禁用（确定性模式）</option><option value="ollama">Ollama</option><option value="zhipu">智谱 GLM API</option><option value="openai_compatible">OpenAI-compatible</option></select></label>{field('base_url', 'Base URL（API 模式需包含版本段）')}{field('chat_model', '聊天模型')}{field('reasoning_model', '推理模型')}{field('embedding_model', 'Embedding 模型（可选）')}{field('api_key', form.has_api_key ? 'API Key（留空复用已配置密钥）' : 'API Key（不会返回前端）', 'password')}{field('proxy_url', form.has_proxy_url ? 'Proxy URL（留空保留已保存值）' : 'Proxy URL（可选）')}<label><span>使用系统代理 HTTP(S)_PROXY</span><input type="checkbox" checked={!!form.trust_env} onChange={event => setForm({ ...form, trust_env: event.target.checked })}/></label><div className="actions"><button className="ghost" onClick={test}>测试连接</button><button onClick={save}>保存</button></div>{note && <div className="notice">{note}</div>}</div><div className="panel principles"><span className="eyebrow">GUARDRAILS</span><h3>系统边界</h3><ul><li>仅监听本机地址</li><li>原始文件不可变</li><li>Agent 不自动发布 Wiki</li><li>API key 不进入运行产物</li><li>缺少证据时拒绝猜测</li></ul></div></div><Runs /></section>
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)
