'use client';

import {
  Activity, ArrowRight, Check, ChevronRight, CircleDot, CloudLightning, Code2,
  Database, GitBranch, HelpCircle, Layers3, Play, Plus, RefreshCw, Search,
  ShieldCheck, Sparkles, X,
} from 'lucide-react';
import { useEffect, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';

type View = 'investigate' | 'sources' | 'architecture';
type TimelineItem = {
  kind: 'hypothesis' | 'query' | 'evidence' | 'research' | 'complete';
  eyebrow: string;
  title: string;
  detail: string;
  source?: string;
};

const sampleTimeline: TimelineItem[] = [
  { kind: 'hypothesis', eyebrow: 'Hypotheses formed', title: 'Four explanations are plausible', detail: 'Inventory shortage · pricing change · regional demand · supplier disruption' },
  { kind: 'query', eyebrow: 'Investigation 01', title: 'Check whether lost sales track stock availability', detail: 'One query can separate a demand decline from a supply constraint.', source: 'Sales Analytics · TimescaleDB' },
  { kind: 'evidence', eyebrow: 'Evidence', title: 'Inventory fell before revenue', detail: 'Available units dropped 47% across the affected SKUs, beginning six days before sales declined.', source: 'Direct · 0.86 confidence' },
  { kind: 'query', eyebrow: 'Investigation 02', title: 'Resolve the affected products to their suppliers', detail: '11 of 14 affected outdoor SKUs map to the same supplier: S17.', source: 'Product Catalog · PostgreSQL' },
  { kind: 'research', eyebrow: 'Selective external research', title: 'Supplier-side disruption is now plausible', detail: 'Search event records for the supplier region and exact disruption window. External context remains correlated evidence.', source: 'News / event specialist' },
  { kind: 'complete', eyebrow: 'Investigation complete', title: 'Supplier-driven stock shortage', detail: 'The decline is best explained by stock unavailability concentrated in S17 products—not weaker regional demand.', source: 'High confidence · 0.84' },
];

const hypotheses = [
  { name: 'Stock shortage', value: 84, status: 'Supported', tone: 'good' },
  { name: 'Supplier disruption', value: 76, status: 'Supported', tone: 'good' },
  { name: 'Regional demand decline', value: 18, status: 'Weakened', tone: 'muted' },
  { name: 'Pricing change', value: 4, status: 'Rejected', tone: 'bad' },
];

const defaultQuestion = 'Why did outdoor-product revenue fall sharply in northern Italy during July?';

declare global {
  interface Document {
    modelContext?: {
      registerTool: (tool: {
        name: string;
        title: string;
        description: string;
        inputSchema: object;
        annotations: { readOnlyHint: boolean; untrustedContentHint: boolean };
        execute: (input: unknown) => Promise<unknown>;
      }, options?: { signal?: AbortSignal }) => void | Promise<void>;
    };
  }
}

export default function Home() {
  const [view, setView] = useState<View>('investigate');
  const [question, setQuestion] = useState(defaultQuestion);
  const [running, setRunning] = useState(false);
  const [shownSteps, setShownSteps] = useState(sampleTimeline.length);
  const [sourceCount, setSourceCount] = useState(2);

  const startInvestigation = (nextQuestion?: string) => {
    if (nextQuestion) setQuestion(nextQuestion);
    setView('investigate');
    setShownSteps(0);
    setRunning(true);
  };

  useEffect(() => {
    if (!running) return;
    const timer = window.setTimeout(() => setShownSteps((value) => {
      const next = Math.min(value + 1, sampleTimeline.length);
      if (next === sampleTimeline.length) setRunning(false);
      return next;
    }), 650);
    return () => window.clearTimeout(timer);
  }, [running, shownSteps]);

  useEffect(() => {
    const context = document.modelContext;
    if (!context?.registerTool) return;
    const lifecycle = new AbortController();
    void Promise.resolve(context.registerTool({
      name: 'start_business_investigation',
      title: 'Start business investigation',
      description: 'Start the visible root-cause investigation demo with a specific business question.',
      inputSchema: {
        type: 'object',
        properties: { question: { type: 'string', minLength: 10 } },
        required: ['question'],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      async execute(input) {
        const candidate = input as { question?: unknown };
        if (typeof candidate.question !== 'string' || candidate.question.trim().length < 10) {
          throw new Error('question must be a string with at least 10 characters');
        }
        setQuestion(candidate.question.trim());
        setView('investigate');
        setShownSteps(0);
        setRunning(true);
        return { status: 'started', question: candidate.question.trim() };
      },
    }, { signal: lifecycle.signal })).catch(() => undefined);
    return () => lifecycle.abort();
  }, []);

  return (
    <div className="min-h-screen bg-background text-foreground">
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
          {view === 'investigate' && <InvestigationView question={question} setQuestion={setQuestion} running={running} shownSteps={shownSteps} onStart={() => startInvestigation()} />}
          {view === 'sources' && <SourcesView onAdded={() => setSourceCount((value) => value + 1)} />}
          {view === 'architecture' && <ArchitectureView />}
        </main>
      </div>
    </div>
  );
}

function NavItem({ active, icon, label, count, onClick }: { active: boolean; icon: React.ReactNode; label: string; count?: number; onClick: () => void }) {
  return <button className={`nav-item ${active ? 'active' : ''}`} onClick={onClick}>{icon}<span>{label}</span>{count !== undefined && <span className="nav-count">{count}</span>}</button>;
}

function InvestigationView({ question, setQuestion, running, shownSteps, onStart }: { question: string; setQuestion: (value: string) => void; running: boolean; shownSteps: number; onStart: () => void }) {
  const complete = shownSteps === sampleTimeline.length;
  return (
    <div className="investigation-layout">
      <section className="work-column">
        <div className="page-heading">
          <div><span className="section-kicker">ROOT-CAUSE WORKBENCH</span><h1>Ask why. Follow the evidence.</h1></div>
          <Badge variant="outline" className="demo-badge"><Sparkles /> Demo scenario</Badge>
        </div>

        <div className="question-card">
          <label htmlFor="question">Business question</label>
          <Textarea id="question" value={question} onChange={(event) => setQuestion(event.target.value)} rows={3} />
          <div className="question-footer">
            <div className="selected-sources"><span><Database /> Sales Analytics</span><span><Database /> Product Catalog</span></div>
            <Button onClick={onStart} disabled={running || question.trim().length < 10} className="investigate-button">
              {running ? <><RefreshCw className="spin" /> Investigating</> : <><Play /> Investigate</>}
            </Button>
          </div>
        </div>

        <div className="timeline-heading">
          <div><h2>Investigation timeline</h2><span>{running ? 'Graph is choosing the next step' : complete ? '6 decisions · 2 datasources · 1 external call' : 'Ready'}</span></div>
          {running && <span className="live-pill"><span /> Live</span>}
        </div>

        <div className="timeline" aria-live="polite">
          {sampleTimeline.slice(0, shownSteps).map((item, index) => <TimelineCard key={`${item.kind}-${index}`} item={item} last={index === shownSteps - 1} />)}
          {running && shownSteps < sampleTimeline.length && <div className="thinking-row"><span className="thinking-icon"><Activity /></span><div><strong>Choosing the next investigation…</strong><span>Scoring expected information gain across active hypotheses</span></div></div>}
        </div>
      </section>

      <aside className="hypothesis-panel">
        <div className="panel-header"><div><span className="section-kicker">LIVE MODEL</span><h2>Hypotheses</h2></div><CircleDot /></div>
        <p className="panel-copy">Confidence changes only when new evidence supports or contradicts a claim.</p>
        <div className="hypothesis-list">
          {hypotheses.map((hypothesis) => (
            <div className="hypothesis" key={hypothesis.name}>
              <div className="hypothesis-top"><strong>{hypothesis.name}</strong><span className={hypothesis.tone}>{hypothesis.status}</span></div>
              <div className="confidence-track"><span style={{ width: complete ? `${hypothesis.value}%` : '24%' }} className={hypothesis.tone} /></div>
              <span className="confidence-number">{complete ? hypothesis.value : 24}%</span>
            </div>
          ))}
        </div>
        <div className="budget-card">
          <div><span>Investigation budget</span><strong>{complete ? '6 / 8' : `${Math.max(1, shownSteps)} / 8`} iterations</strong></div>
          <div className="budget-bar"><span style={{ width: complete ? '75%' : `${Math.max(12, shownSteps * 12)}%` }} /></div>
          <div className="budget-grid"><span><strong>4</strong> SQL queries</span><span><strong>1</strong> external call</span></div>
        </div>
      </aside>
    </div>
  );
}

function TimelineCard({ item, last }: { item: TimelineItem; last: boolean }) {
  const icons = { hypothesis: <GitBranch />, query: <Database />, evidence: <Check />, research: <CloudLightning />, complete: <Sparkles /> };
  return (
    <article className={`timeline-card ${item.kind} ${last ? 'just-added' : ''}`}>
      <span className="timeline-node">{icons[item.kind]}</span>
      <div className="timeline-body"><span className="timeline-eyebrow">{item.eyebrow}</span><h3>{item.title}</h3><p>{item.detail}</p>{item.source && <span className="source-chip">{item.source}</span>}</div>
      <ChevronRight className="card-chevron" />
    </article>
  );
}

function SourcesView({ onAdded }: { onAdded: () => void }) {
  const [showForm, setShowForm] = useState(false);
  const [notice, setNotice] = useState('');
  const [sources, setSources] = useState([
    { name: 'Sales Analytics', type: 'TimescaleDB', detail: '5 hypertables · schema cached', port: '5434' },
    { name: 'Product Catalog', type: 'PostgreSQL', detail: '6 tables · schema cached', port: '5433' },
  ]);
  const [form, setForm] = useState({ name: '', type: 'postgresql', host: 'localhost', port: '5432', database: '', username: '', password: '' });

  const submit = async (event: { preventDefault: () => void }) => {
    event.preventDefault();
    setNotice('Testing connection…');
    const { name, type, ...credentials } = form;
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'}/api/datasources`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name, type, credentials: { ...credentials, port: Number(credentials.port) } }),
      });
      if (!response.ok) throw new Error('Connection failed');
      const added = await response.json() as { name: string; type: string; table_count: number };
      setSources((current) => [...current, { name: added.name, type: added.type, detail: `${added.table_count} tables · schema cached`, port: form.port }]);
      onAdded(); setShowForm(false); setNotice('Datasource added. Credentials remain in backend memory.');
    } catch { setNotice('Backend unavailable. Start the API locally, then try again.'); }
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
          <div className="form-actions"><span><ShieldCheck /> Never persisted or shared with the LLM</span><Button type="submit">Test & add <ArrowRight /></Button></div>
        </form>
      )}
      {notice && <output className="notice">{notice}</output>}
      <div className="source-list">
        {sources.map((source) => (
          <article className="source-row" key={source.name}>
            <span className="source-icon"><Database /></span>
            <div className="source-info"><div><h2>{source.name}</h2><Badge variant="outline">{source.type}</Badge></div><p>{source.detail}</p></div>
            <div className="connection-meta"><span><span className="pulse-dot" /> Connected</span><small>localhost:{source.port}</small></div>
            <Button variant="outline"><RefreshCw /> Refresh schema</Button>
          </article>
        ))}
      </div>
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
