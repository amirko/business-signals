'use client';

import {
  Activity, ArrowRight, ChevronDown, ChevronRight, CircleDot, Code2, Database, GitBranch,
  HelpCircle, Layers3, Play, Plus, RefreshCw, Search, ShieldCheck, Trash2, X,
} from 'lucide-react';
import { Component, type ErrorInfo, type FormEvent, type ReactNode, useEffect, useRef, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Input } from '@/components/ui/input';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Textarea } from '@/components/ui/textarea';

type View = 'investigate' | 'sources' | 'architecture';
type HypothesisStep = {
  id: string;
  type: string;
  message: string;
  source?: string;
};
type Hypothesis = { id: string; name: string; description: string; confidence: number; status: string };
type Evidence = { id: string; description: string; relationship: string; confidence: number; hypothesis_ids: string[] };
type FinalAnalysis = { likely_root_cause: string | null; confidence: number; summary: string; caveats: string[] };
type InvestigationEvent = { id: string; type: string; message: string; data: Record<string, unknown> };
type Datasource = { id: string; name: string; type: string; connected: boolean; table_count: number; schema_cached: boolean; discovered_at: string | null };
type Column = { name: string; data_type: string; nullable: boolean; primary_key: boolean };
type SchemaTable = { schema_name: string; name: string; columns: Column[]; foreign_keys: Record<string, string>[]; indexes: { name: string; definition: string }[]; approximate_rows: number | null; is_hypertable: boolean; time_column: string | null };
type SchemaMetadata = { datasource_id: string; structural_metadata: SchemaTable[]; fingerprint: string; discovered_at: string };
const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

const defaultQuestion = 'Why did unit sales of Shell Jacket 001, Trail Backpack 006, Day Pack 011, and Rain Cover 016 in northern Italian stores fall after July 14, 2024?';

class ClientErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError(): { hasError: boolean } {
    return { hasError: true };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error('Business Signals client error', error, errorInfo);
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return <main className="client-error"><h1>Something went wrong</h1><p>The app could not complete that action. Refresh and try again.</p><button onClick={() => window.location.reload()}>Refresh app</button></main>;
    }
    return this.props.children;
  }
}

export default function Home() {
  const [view, setView] = useState<View>('investigate');
  const [question, setQuestion] = useState(defaultQuestion);
  const [datasources, setDatasources] = useState<Datasource[]>([]);
  const [sourceCount, setSourceCount] = useState(0);

  useEffect(() => {
    fetch(`${apiUrl}/api/datasources`)
      .then((response) => response.ok ? response.json() as Promise<Datasource[]> : Promise.reject())
      .then((sources) => { setDatasources(sources); setSourceCount(sources.length); })
      .catch(() => undefined);
  }, []);

  return (
    <ClientErrorBoundary><div className="min-h-screen bg-background text-foreground">
      <header className="app-header">
        <button className="brand" onClick={() => setView('investigate')} aria-label="Business Signals home">
          <span className="brand-mark"><GitBranch /></span>
          <span>Business Signals</span>
          <Badge variant="outline" className="version-badge">v0.1</Badge>
        </button>
        <div className="header-meta">
          <span className="pulse-dot" /> Engine ready
          <a href="https://github.com/amirko/business-signals" target="_blank" rel="noreferrer" className="github-link"><Code2 /> Source</a>
        </div>
      </header>

      <div className="app-shell">
        <aside className="sidebar">
          <nav aria-label="Primary navigation">
            <NavItem active={view === 'investigate'} icon={<Search />} label="Investigate" onClick={() => setView('investigate')} />
            <NavItem active={view === 'sources'} icon={<Database />} label="Data sources" count={sourceCount} onClick={() => setView('sources')} />
            <NavItem active={view === 'architecture'} icon={<Layers3 />} label="How it works" onClick={() => setView('architecture')} />
          </nav>
          <div className="sidebar-note">
            <ShieldCheck />
            <div><strong>Read only by design</strong><span>Credentials stay in backend memory and never enter model context.</span></div>
          </div>
        </aside>

        <main className="main-content">
          {view === 'investigate' && <InvestigationView question={question} setQuestion={setQuestion} datasources={datasources} />}
          {view === 'sources' && <SourcesView onSourceCount={setSourceCount} onSourcesChanged={setDatasources} />}
          {view === 'architecture' && <ArchitectureView />}
        </main>
      </div>
    </div></ClientErrorBoundary>
  );
}

function NavItem({ active, icon, label, count, onClick }: { active: boolean; icon: React.ReactNode; label: string; count?: number; onClick: () => void }) {
  return <button className={`nav-item ${active ? 'active' : ''}`} onClick={onClick}>{icon}<span>{label}</span>{count !== undefined && <span className="nav-count">{count}</span>}</button>;
}

function InvestigationView({ question, setQuestion, datasources }: { question: string; setQuestion: (value: string) => void; datasources: Datasource[] }) {
  const [selectedDatasourceIds, setSelectedDatasourceIds] = useState<string[]>([]);
  const [hypotheses, setHypotheses] = useState<Hypothesis[]>([]);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [hypothesisSteps, setHypothesisSteps] = useState<Record<string, HypothesisStep[]>>({});
  const [expandedHypotheses, setExpandedHypotheses] = useState<Record<string, boolean>>({});
  const [finalAnalysis, setFinalAnalysis] = useState<FinalAnalysis | null>(null);
  const [clarificationQuestion, setClarificationQuestion] = useState<string | null>(null);
  const [clarificationResponse, setClarificationResponse] = useState('');
  const [error, setError] = useState('');
  const [running, setRunning] = useState(false);
  const stream = useRef<EventSource | null>(null);

  useEffect(() => {
    setSelectedDatasourceIds((current) => current.filter((id) => datasources.some((source) => source.id === id)));
  }, [datasources]);
  useEffect(() => () => stream.current?.close(), []);

  const addHypothesisStep = (hypothesisId: string, event: InvestigationEvent) => {
    const datasourceId = typeof event.data.datasource_id === 'string' ? event.data.datasource_id : undefined;
    const source = datasourceId ? datasources.find((item) => item.id === datasourceId)?.name : undefined;
    setHypothesisSteps((current) => {
      const existing = current[hypothesisId] ?? [];
      if (existing.some((item) => item.id === event.id)) return current;
      return { ...current, [hypothesisId]: [...existing, { id: event.id, type: event.type, message: event.message, source }] };
    });
  };

  const handleEvent = (event: InvestigationEvent) => {
    if ((event.type === 'HypothesisCreated' || event.type === 'HypothesisUpdated') && event.data.hypothesis) {
      const hypothesis = event.data.hypothesis as Hypothesis;
      setHypotheses((current) => current.some((item) => item.id === hypothesis.id)
        ? current.map((item) => item.id === hypothesis.id ? hypothesis : item)
        : [...current, hypothesis]);
      if (event.type === 'HypothesisUpdated') addHypothesisStep(hypothesis.id, event);
    }
    const hypothesisId = typeof event.data.hypothesis_id === 'string' ? event.data.hypothesis_id : undefined;
    if (hypothesisId && ['DatasourceSelected', 'QueryStarted', 'QueryRejected', 'QueryCompleted'].includes(event.type)) addHypothesisStep(hypothesisId, event);
    if (event.type === 'EvidenceFound' && event.data.evidence) {
      const found = event.data.evidence as Evidence;
      setEvidence((current) => [...current.filter((item) => item.id !== found.id), found]);
    }
    if (event.type === 'HumanInputRequested') {
      setClarificationQuestion(event.message); setClarificationResponse(''); setRunning(false);
    }
    if (event.type === 'HumanInputReceived') {
      setClarificationQuestion(null); setRunning(true);
    }
    if (event.type === 'InvestigationCompleted') {
      setFinalAnalysis(event.data.final_analysis as FinalAnalysis);
      setRunning(false); stream.current?.close(); stream.current = null;
    }
    if (event.type === 'InvestigationFailed') {
      setError(event.message);
      setRunning(false); stream.current?.close(); stream.current = null;
    }
  };

  const startInvestigation = async () => {
    if (selectedDatasourceIds.length === 0) { setError('Choose at least one connected datasource first.'); return; }
    setError(''); setHypotheses([]); setEvidence([]); setHypothesisSteps({}); setExpandedHypotheses({}); setFinalAnalysis(null); setClarificationQuestion(null); setClarificationResponse(''); setRunning(true); stream.current?.close();
    try {
      const response = await fetch(`${apiUrl}/api/investigations`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ question, datasource_ids: selectedDatasourceIds }) });
      if (!response.ok) {
        throw new Error(response.status === 404
          ? 'A selected datasource is no longer available. Refresh the page and select it again.'
          : 'The investigation could not be started. Check the datasource connection and backend logs.');
      }
      const created = await response.json() as { investigation_id: string };
      const eventSource = new EventSource(`${apiUrl}/api/investigations/${created.investigation_id}/events`);
      stream.current = eventSource;
      const processEvent = (message: Event) => {
        try {
          handleEvent(JSON.parse((message as MessageEvent).data) as InvestigationEvent);
        } catch (cause) {
          console.error('Could not process investigation event', cause, message);
          setError('An investigation update could not be displayed. Check the browser console and backend logs.');
          setRunning(false); eventSource.close(); stream.current = null;
        }
      };
      ['InvestigationStarted', 'HypothesisCreated', 'DatasourceSelected', 'QueryStarted', 'QueryRejected', 'QueryCompleted', 'EvidenceFound', 'HypothesisUpdated', 'HumanInputRequested', 'HumanInputReceived', 'InvestigationCompleted', 'InvestigationFailed'].forEach((type) => eventSource.addEventListener(type, processEvent));
      eventSource.onerror = () => { if (stream.current === eventSource) { setError('The live investigation stream disconnected.'); setRunning(false); eventSource.close(); stream.current = null; } };
    } catch (cause) { console.error('Could not start investigation', cause); setError('The investigation could not be started. Check the backend logs and try again.'); setRunning(false); }
  };

  const submitClarification = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!stream.current || !clarificationResponse.trim()) return;
    const investigationId = stream.current.url.split('/').at(-2);
    if (!investigationId) return;
    try {
      const response = await fetch(`${apiUrl}/api/investigations/${investigationId}/responses`, {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ response: clarificationResponse.trim() }),
      });
      if (!response.ok) throw new Error(`Clarification request failed with ${response.status}`);
      setClarificationQuestion(null); setClarificationResponse(''); setRunning(true);
    } catch (cause) {
      console.error('Could not submit clarification', cause);
      setError('The clarification could not be submitted. Check the backend logs and try again.');
    }
  };

  const toggleDatasource = (id: string) => setSelectedDatasourceIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  return (
    <div className="investigation-layout">
      <section className="work-column">
        <div className="page-heading">
          <div><span className="section-kicker">ROOT-CAUSE WORKBENCH</span><h1>Ask why. Follow the evidence.</h1></div>
          <Badge variant="outline" className="demo-badge"><Activity /> Live investigation</Badge>
        </div>

        <div className="question-card">
          <label htmlFor="question">Business question</label>
          <Textarea id="question" value={question} onChange={(event) => setQuestion(event.target.value)} rows={3} />
          <div className="question-footer">
            <div className="selected-sources datasource-picker">{datasources.map((source) => <label key={source.id}><input type="checkbox" checked={selectedDatasourceIds.includes(source.id)} onChange={() => toggleDatasource(source.id)} /><Database /> {source.name}</label>)}{datasources.length === 0 && <span>Connect a datasource to investigate</span>}</div>
            <Button onClick={startInvestigation} disabled={running || question.trim().length < 10 || datasources.length === 0} className="investigate-button">
              {running ? <><RefreshCw className="spin" /> Investigating</> : <><Play /> Investigate</>}
            </Button>
          </div>
        </div>

        <div className="timeline-heading">
          <div><h2>Hypothesis investigation tree</h2><span>{running ? 'The graph is evaluating live evidence' : finalAnalysis ? 'Investigation complete' : 'Ready'}</span></div>
          {running && <span className="live-pill"><span /> Live</span>}
        </div>

        <div className="hypothesis-tree-list" aria-live="polite">
          {hypotheses.map((hypothesis, hypothesisIndex) => {
            const relatedEvidence = evidence.filter((item) => item.hypothesis_ids.includes(hypothesis.id));
            const steps = hypothesisSteps[hypothesis.id] ?? [];
            const hypothesisLabel = `H${hypothesisIndex + 1}`;
            const tone = hypothesis.status === 'rejected' ? 'bad' : hypothesis.status === 'supported' || hypothesis.status === 'confirmed' ? 'good' : 'muted';
            return <details className="hypothesis-tree" key={hypothesis.id} open={expandedHypotheses[hypothesis.id] ?? true} onToggle={(event) => { const isOpen = event.currentTarget.open; setExpandedHypotheses((current) => ({ ...current, [hypothesis.id]: isOpen })); }}>
              <summary>
                <code>{hypothesisLabel}</code><strong>{hypothesis.name}</strong><span className={tone}>{hypothesis.status}</span><span className="tree-confidence">{Math.round(hypothesis.confidence * 100)}%</span><ChevronDown />
              </summary>
              <div className="tree-children">
                <p className="hypothesis-description">{hypothesis.description}</p>
                {steps.map((step) => <article className="hypothesis-step" key={step.id}><span>{step.type.replaceAll(/([A-Z])/g, ' $1').trim()}</span><p>{step.message}</p>{step.source && <small>{step.source}</small>}</article>)}
                {relatedEvidence.map((item) => {
                  const evidenceLabel = `E${evidence.findIndex((candidate) => candidate.id === item.id) + 1}`;
                  return <article className="hypothesis-evidence" key={item.id}><code>{evidenceLabel}</code><div><span>{item.relationship} evidence · {Math.round(item.confidence * 100)}%</span><p>{item.description}</p></div></article>;
                })}
                {steps.length === 0 && relatedEvidence.length === 0 && <p className="tree-empty">Awaiting the first investigation step.</p>}
              </div>
            </details>;
          })}
          {!running && hypotheses.length === 0 && <div className="empty-timeline">Start an investigation to build a hypothesis tree.</div>}
          {running && <div className="thinking-row"><span className="thinking-icon"><Activity /></span><div><strong>Waiting for the next graph decision…</strong><span>The engine is selecting the most informative internal step.</span></div></div>}
        </div>
        {clarificationQuestion && <form className="clarification-panel" onSubmit={submitClarification}><span className="section-kicker">CLARIFICATION NEEDED</span><h2>Before continuing</h2><p>{clarificationQuestion}</p><Textarea value={clarificationResponse} onChange={(event) => setClarificationResponse(event.target.value)} placeholder="Provide the business context the investigation needs…" rows={3} /><div><span>The investigation is paused until you respond.</span><Button type="submit" disabled={!clarificationResponse.trim()}>Resume investigation <ArrowRight /></Button></div></form>}
        {error && <output className="notice investigation-error">{error}</output>}
        {finalAnalysis && <article className="final-result"><span className="section-kicker">FINAL ANALYSIS</span><h2>{finalAnalysis.likely_root_cause ?? 'Insufficient evidence'}</h2><p>{finalAnalysis.summary}</p><span className="source-chip">{Math.round(finalAnalysis.confidence * 100)}% confidence</span>{finalAnalysis.caveats.map((caveat) => <p className="final-caveat" key={caveat}>{caveat}</p>)}</article>}
      </section>

      <aside className="hypothesis-panel">
        <div className="panel-header"><div><span className="section-kicker">LIVE MODEL</span><h2>Hypotheses</h2></div><CircleDot /></div>
        <p className="panel-copy">Confidence changes only when new evidence supports or contradicts a claim.</p>
        <p className="panel-copy">Each node in the tree shows its status, evidence, and completed investigative steps.</p>
        <div className="budget-card">
          <div><span>Investigation state</span><strong>{running ? 'Running' : finalAnalysis ? 'Complete' : 'Ready'}</strong></div>
          <div className="budget-bar"><span style={{ width: running ? '55%' : finalAnalysis ? '100%' : '0%' }} /></div>
          <div className="budget-grid"><span><strong>{Object.values(hypothesisSteps).flat().filter((item) => item.type.startsWith('Query')).length}</strong> query events</span><span><strong>{evidence.length}</strong> evidence items</span></div>
        </div>
      </aside>
    </div>
  );
}

function SourcesView({ onSourceCount, onSourcesChanged }: { onSourceCount: (count: number) => void; onSourcesChanged: (sources: Datasource[]) => void }) {
  const [showForm, setShowForm] = useState(false);
  const [notice, setNotice] = useState('');
  const [sources, setSources] = useState<Datasource[]>([]);
  const [metadata, setMetadata] = useState<Record<string, SchemaMetadata>>({});
  const [consoleLines, setConsoleLines] = useState<Record<string, string[]>>({});
  const [schemaDetailsOpen, setSchemaDetailsOpen] = useState<Record<string, boolean>>({});
  const [loadingSchemaId, setLoadingSchemaId] = useState<string | null>(null);
  const [selectedTable, setSelectedTable] = useState<SchemaTable | null>(null);
  const [form, setForm] = useState({ name: '', type: 'postgresql', host: 'localhost', port: '5432', database: '', username: '', password: '' });

  useEffect(() => {
    fetch(`${apiUrl}/api/datasources`)
      .then((response) => response.ok ? response.json() as Promise<Datasource[]> : Promise.reject())
      .then(async (result) => {
        setSources(result); onSourceCount(result.length); onSourcesChanged(result);
        const cached = await Promise.all(result.filter((source) => source.schema_cached).map(async (source) => {
          const response = await fetch(`${apiUrl}/api/datasources/${source.id}/metadata`);
          return response.ok ? [source.id, await response.json() as SchemaMetadata] as const : null;
        }));
        setMetadata(Object.fromEntries(cached.filter((item): item is readonly [string, SchemaMetadata] => item !== null)));
      })
      .catch(() => setNotice('Backend unavailable. Start the API locally to connect a datasource.'));
  }, []);

  const submit = async (event: { preventDefault: () => void }) => {
    event.preventDefault();
    setNotice('Testing connection…');
    const { name, type, ...credentials } = form;
    try {
      const response = await fetch(`${apiUrl}/api/datasources`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name, type, credentials: { ...credentials, port: Number(credentials.port) } }),
      });
      if (!response.ok) throw new Error('Connection failed');
      const added = await response.json() as Datasource;
      setSources((current) => {
        const updated = [...current, added];
        onSourceCount(updated.length);
        onSourcesChanged(updated);
        return updated;
      });
      setShowForm(false); setNotice('Datasource connected. Select Discover schema when you are ready.');
    } catch { setNotice('Backend unavailable. Start the API locally, then try again.'); }
  };

  const discoverSchema = async (source: Datasource) => {
    const started = new Date().toLocaleTimeString();
    setLoadingSchemaId(source.id);
    setSchemaDetailsOpen((current) => ({ ...current, [source.id]: true }));
    setConsoleLines((current) => ({ ...current, [source.id]: [
      `$ ${started}  inspect ${source.type} connection`,
      '  connected · read-only session enabled',
      '  reading tables, columns, keys, indexes, and time-series metadata…',
    ] }));
    try {
      const response = await fetch(`${apiUrl}/api/datasources/${source.id}/refresh`, { method: 'POST' });
      if (!response.ok) throw new Error('Schema discovery failed');
      const result = await response.json() as SchemaMetadata;
      setMetadata((current) => ({ ...current, [source.id]: result }));
      setSources((current) => current.map((item) => item.id === source.id ? { ...item, table_count: result.structural_metadata.length, schema_cached: true, discovered_at: result.discovered_at } : item));
      setConsoleLines((current) => ({ ...current, [source.id]: [
        ...current[source.id],
        `  found ${result.structural_metadata.length} tables`,
        ...result.structural_metadata.map((table) => `  ✓ ${table.schema_name}.${table.name} · ${table.columns.length} columns${table.is_hypertable ? ` · hypertable (${table.time_column})` : ''}`),
        `  fingerprint ${result.fingerprint.slice(0, 12)}…`,
        '  discovery complete',
      ] }));
    } catch {
      setConsoleLines((current) => ({ ...current, [source.id]: [...current[source.id], '  × discovery failed — verify the backend and connection details'] }));
    } finally { setLoadingSchemaId(null); }
  };

  const deleteDatasource = async (source: Datasource) => {
    if (!window.confirm(`Delete ${source.name}? Its saved connection and cached schema will be removed.`)) return;
    try {
      const response = await fetch(`${apiUrl}/api/datasources/${source.id}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(`Delete request failed with ${response.status}`);
      setSources((current) => {
        const updated = current.filter((item) => item.id !== source.id);
        onSourceCount(updated.length); onSourcesChanged(updated);
        return updated;
      });
      setMetadata((current) => { const { [source.id]: _removed, ...remaining } = current; return remaining; });
      setConsoleLines((current) => { const { [source.id]: _removed, ...remaining } = current; return remaining; });
      setSelectedTable(null);
      setNotice(`${source.name} was deleted.`);
    } catch (cause) {
      console.error('Could not delete datasource', cause);
      setNotice('The datasource could not be deleted. Check the backend logs and try again.');
    }
  };

  return (
    <section className="standard-page">
      <div className="page-heading">
        <div><span className="section-kicker">CONNECTIONS</span><h1>Data sources</h1><p>Independent connections stay separate. The graph carries evidence between them.</p></div>
        <Button onClick={() => setShowForm((value) => !value)}><Plus /> Add datasource</Button>
      </div>
      {showForm && (
        <form className="source-form" onSubmit={submit}>
          <div className="form-heading"><div><h2>New datasource</h2><p>Use a dedicated read-only database role.</p></div><button type="button" onClick={() => setShowForm(false)} aria-label="Close"><X /></button></div>
          <div className="form-grid">
            <div className="form-field"><label htmlFor="source-name">Name</label><Input id="source-name" required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Product Catalog" /></div>
            <div className="form-field"><label htmlFor="source-type">Type</label><select id="source-type" value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}><option value="postgresql">PostgreSQL</option><option value="timescaledb">TimescaleDB</option></select></div>
            <div className="form-field"><label htmlFor="source-host">Host</label><Input id="source-host" required value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} /></div>
            <div className="form-field"><label htmlFor="source-port">Port</label><Input id="source-port" required type="number" value={form.port} onChange={(e) => setForm({ ...form, port: e.target.value })} /></div>
            <div className="form-field"><label htmlFor="source-database">Database</label><Input id="source-database" required value={form.database} onChange={(e) => setForm({ ...form, database: e.target.value })} /></div>
            <div className="form-field"><label htmlFor="source-username">Username</label><Input id="source-username" required value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></div>
            <div className="form-field full-field"><label htmlFor="source-password">Password</label><Input id="source-password" required type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></div>
          </div>
          <div className="form-actions"><span><ShieldCheck /> Saved only in this local app store; never shared with the LLM</span><Button type="submit">Test & add <ArrowRight /></Button></div>
        </form>
      )}
      {notice && <output className="notice">{notice}</output>}
      <div className="source-list">
        {sources.length === 0 && !notice && <div className="empty-sources"><Database /><strong>No datasources connected</strong><span> Add a connection, then explicitly discover its schema.</span></div>}
        {sources.map((source) => (
          <article className="source-row" key={source.id}>
            <div className="source-summary">
              <span className="source-icon"><Database /></span>
              <div className="source-info"><div><h2>{source.name}</h2><Badge variant="outline">{source.type}</Badge></div><p>{source.schema_cached ? `${source.table_count} tables discovered` : 'Schema not loaded'}</p></div>
              <div className="connection-meta"><span><span className="pulse-dot" /> {source.connected ? 'Connected' : 'Saved'}</span><small>{source.schema_cached ? 'Schema available' : 'Awaiting discovery'}</small></div>
              <div className="source-actions"><Button variant="outline" onClick={() => discoverSchema(source)} disabled={loadingSchemaId === source.id}><RefreshCw className={loadingSchemaId === source.id ? 'spin' : ''} />{source.schema_cached ? 'Refresh schema' : 'Discover schema'}</Button><Button variant="destructive" size="icon" onClick={() => deleteDatasource(source)} aria-label={`Delete ${source.name}`}><Trash2 /></Button></div>
            </div>
            {(consoleLines[source.id] || metadata[source.id]) && <Collapsible open={schemaDetailsOpen[source.id] ?? true} onOpenChange={(open) => setSchemaDetailsOpen((current) => ({ ...current, [source.id]: open }))}>
              <CollapsibleTrigger className="schema-details-trigger"><span><Database /> Schema discovery details</span><span>{metadata[source.id] ? `${metadata[source.id].structural_metadata.length} tables` : 'Running…'}<ChevronDown /></span></CollapsibleTrigger>
              <CollapsibleContent className="schema-details-content">
                {consoleLines[source.id] && <div className="schema-console" role="log" aria-live="polite">{consoleLines[source.id].map((line, index) => <div key={`${line}-${index}`}>{line}</div>)}</div>}
                {metadata[source.id] && <div className="schema-table-list"><span>Detected schema — select a table for details</span>{metadata[source.id].structural_metadata.map((table) => <button key={`${table.schema_name}.${table.name}`} onClick={() => setSelectedTable(table)}><Database /><code>{table.schema_name}.{table.name}</code><small>{table.columns.length} columns{table.approximate_rows !== null ? ` · ~${table.approximate_rows.toLocaleString()} rows` : ''}</small><ChevronRight /></button>)}</div>}
              </CollapsibleContent>
            </Collapsible>}
          </article>
        ))}
      </div>
      <Sheet open={selectedTable !== null} onOpenChange={(open) => !open && setSelectedTable(null)}>
        <SheetContent className="schema-detail-sheet">
          {selectedTable && <><SheetHeader><SheetTitle>{selectedTable.schema_name}.{selectedTable.name}</SheetTitle><SheetDescription>{selectedTable.is_hypertable ? `Timescale hypertable · time column: ${selectedTable.time_column}` : 'PostgreSQL table'}{selectedTable.approximate_rows !== null ? ` · approximately ${selectedTable.approximate_rows.toLocaleString()} rows` : ''}</SheetDescription></SheetHeader><div className="schema-detail-body"><h3>Columns</h3><div className="column-list">{selectedTable.columns.map((column) => <div key={column.name}><code>{column.name}</code><span>{column.data_type}</span><small>{column.primary_key ? 'Primary key' : column.nullable ? 'Nullable' : 'Required'}</small></div>)}</div>{selectedTable.foreign_keys.length > 0 && <><h3>Relationships</h3><div className="relationship-list">{selectedTable.foreign_keys.map((key, index) => <code key={index}>{key.column_name} → {key.foreign_schema}.{key.foreign_table}.{key.foreign_column}</code>)}</div></>}{selectedTable.indexes.length > 0 && <><h3>Indexes</h3><div className="relationship-list">{selectedTable.indexes.map((index) => <code key={index.name}>{index.name}</code>)}</div></>}</div></>}
        </SheetContent>
      </Sheet>
    </section>
  );
}

function ArchitectureView() {
  const stages = [
    ['01', 'Understand', 'Resolve metric, scope, dates, and ambiguity from cached structural metadata.'],
    ['02', 'Hypothesize', 'Keep multiple testable explanations alive instead of anchoring early.'],
    ['03', 'Investigate', 'Choose the highest-value datasource and query for the current uncertainty.'],
    ['04', 'Evaluate', 'Calculate results deterministically, then update support and contradiction.'],
    ['05', 'Route', 'Loop, ask a human, call one relevant external specialist, or stop.'],
    ['06', 'Synthesize', 'Report the evidence, rejected alternatives, confidence, and caveats.'],
  ];
  return (
    <section className="standard-page architecture-page">
      <div className="page-heading"><div><span className="section-kicker">LANGGRAPH ENGINE</span><h1>A decision loop, not a pipeline.</h1><p>Every step is selected from current uncertainty, available evidence, and the remaining budget.</p></div></div>
      <div className="loop-map">
        {stages.map(([number, title, detail], index) => <article key={title}><span>{number}</span><div><h2>{title}</h2><p>{detail}</p></div>{index < stages.length - 1 && <ArrowRight />}</article>)}
        <div className="loop-back"><GitBranch /> Evidence changes what the graph inspects next</div>
      </div>
      <div className="principles-grid">
        <article><ShieldCheck /><h3>Guarded execution</h3><p>AST validation, read-only sessions, timeouts, and hard row limits protect every database.</p></article>
        <article><Activity /><h3>Deterministic math</h3><p>Code calculates changes, anomalies, distributions, and correlations. The model chooses what to test.</p></article>
        <article><HelpCircle /><h3>Human when it matters</h3><p>Ambiguous metrics and uncertain cross-source relationships pause through LangGraph interrupts.</p></article>
      </div>
    </section>
  );
}
