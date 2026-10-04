'use client';

import {
  Activity, Archive, ArrowLeft, ArrowRight, ChevronDown, ChevronRight, CircleDot, Code2, Database, GitBranch,
  HelpCircle, Layers3, Play, Plus, RefreshCw, Search, ShieldCheck, Trash2, X,
} from 'lucide-react';
import { Component, type ErrorInfo, type FormEvent, type ReactNode, useCallback, useEffect, useRef, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Input } from '@/components/ui/input';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Textarea } from '@/components/ui/textarea';

type View = 'investigate' | 'sources' | 'saved' | 'architecture';
type HypothesisStep = {
  id: string;
  type: string;
  message: string;
  source?: string;
};
type HypothesisActivity = { id: string; kind: 'step' | 'evidence' | 'clarification'; itemId: string };
type Hypothesis = { id: string; name: string; description: string; confidence: number; status: string };
type EvidenceDataPoint = { field: string; value: string | number | boolean | null };
type Evidence = { id: string; description: string; relationship: string; confidence: number; hypothesis_ids: string[]; data?: EvidenceDataPoint[] };
type FinalAnalysis = { likely_root_cause: string | null; confidence: number; summary: string; caveats: string[]; evidence?: Evidence[]; follow_up_question?: string | null };
type InvestigationEvent = { id: string; type: string; message: string; data: Record<string, unknown> };
type Clarification = { question: string; response?: string; hypothesisId?: string };
type ConversationTurn = { question: string; answer: FinalAnalysis; created_at: string };
type ConversationFeedback = { question: string; response: string; hypothesis_id: string | null; received_at: string };
type SavedInvestigation = {
  investigation_id: string;
  question: string;
  original_question: string | null;
  current_conversation_question: string | null;
  datasources: { id: string }[];
  status: string;
  request_type: 'investigation' | 'direct_answer';
  started_at: string;
  hypotheses: Hypothesis[];
  evidence: Evidence[];
  human_feedback: ConversationFeedback[];
  pending_human_question: string | null;
  final_analysis: FinalAnalysis | null;
  conversation_turns: { question: string; answer: FinalAnalysis; created_at: string }[];
};
type Datasource = { id: string; name: string; type: string; connected: boolean; table_count: number; schema_cached: boolean; discovered_at: string | null };
type Column = { name: string; data_type: string; nullable: boolean; primary_key: boolean };
type SchemaTable = { schema_name: string; name: string; columns: Column[]; foreign_keys: Record<string, string>[]; indexes: { name: string; definition: string }[]; approximate_rows: number | null; is_hypertable: boolean; time_column: string | null };
type SchemaMetadata = { datasource_id: string; structural_metadata: SchemaTable[]; fingerprint: string; discovered_at: string };
type CrossDatasourceRelation = {
  id: string;
  source_datasource: string;
  source_field: string;
  target_datasource: string;
  target_field: string;
  label: string;
  cardinality: 'many_to_one' | 'one_to_one';
  confidence: number;
  sampled_values: number;
  matched_values: number;
  origin: 'human' | 'value_overlap';
  confirmed: boolean;
};
type RelationshipForm = {
  id?: string;
  source_datasource: string;
  source_field: string;
  target_datasource: string;
  target_field: string;
  label: string;
  cardinality: 'many_to_one' | 'one_to_one';
};
const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
const activeInvestigationStorageKey = 'business-signals.active-investigation-id';
const investigationStepLabels: Record<string, string> = {
  DatasourceSelected: 'Next check',
  QueryStarted: 'Checking the data',
  QueryRejected: 'Adjusting the check',
  QueryFailed: 'Check unavailable',
  QueryCompleted: 'Check complete',
  ExternalResearchSelected: 'Considering external context',
  ExternalResearchStarted: 'Checking external context',
  ExternalResearchCompleted: 'External finding',
  ExternalResearchUnavailable: 'External check unavailable',
  HypothesisUpdated: 'What we learned',
};

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

function ReportText({ children }: { children: string }) {
  return <p className="report-text">{children}</p>;
}

function ReportData({ evidence }: { evidence?: Evidence[] }) {
  const reportEvidence = evidence?.filter((item) => item.data && item.data.length > 0) ?? [];
  if (reportEvidence.length === 0) return null;
  return <section className="report-data" aria-label="Report details">
    {reportEvidence.map((item) => <div className="report-data-table" key={item.id}>
      {reportEvidence.length > 1 && <p>{item.description}</p>}
      <table>
        <thead><tr><th scope="col">Result</th><th scope="col">Value</th></tr></thead>
        <tbody>{item.data?.map((point, index) => <tr key={`${point.field}-${index}`}>
          <th scope="row">{point.field}</th>
          <td>{point.value === null ? 'Not available' : String(point.value)}</td>
        </tr>)}</tbody>
      </table>
    </div>)}
  </section>;
}

function ConversationTranscript({ turns, feedback, pendingQuestion, omitLatestAnswer, isInvestigating }: {
  turns: ConversationTurn[];
  feedback: ConversationFeedback[];
  pendingQuestion: string | null;
  omitLatestAnswer: boolean;
  isInvestigating: boolean;
}) {
  const orderedTurns = [...turns].sort((left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime());
  const orderedFeedback = [...feedback].sort((left, right) => new Date(left.received_at).getTime() - new Date(right.received_at).getTime());
  const hasRecordedQuestion = pendingQuestion
    ? orderedTurns.some((turn) => turn.question.trim() === pendingQuestion.trim())
    : false;
  if (orderedTurns.length === 0 && orderedFeedback.length === 0 && (!pendingQuestion || hasRecordedQuestion)) return null;
  let feedbackIndex = 0;
  return <section className="conversation-transcript" aria-label="Conversation history">
    <span className="section-kicker">CONVERSATION SO FAR</span>
    {orderedTurns.flatMap((turn, index) => {
      const items: ReactNode[] = [
        <article className="conversation-turn conversation-question" key={`question-${turn.created_at}-${index}`}><small>QUESTION</small><p>{turn.question}</p></article>,
      ];
      const answerTime = new Date(turn.created_at).getTime();
      while (feedbackIndex < orderedFeedback.length && new Date(orderedFeedback[feedbackIndex].received_at).getTime() <= answerTime) {
        const item = orderedFeedback[feedbackIndex];
        items.push(<article className="conversation-turn conversation-feedback" key={`feedback-${item.received_at}-${feedbackIndex}`}><small>FOLLOW-UP REQUEST</small><p>{item.question}</p><small>YOUR ANSWER</small><p className="saved-answer">{item.response}</p></article>);
        feedbackIndex += 1;
      }
      if (!(omitLatestAnswer && index === orderedTurns.length - 1)) {
        items.push(<article className="conversation-turn conversation-answer" key={`answer-${turn.created_at}-${index}`}><small>ANSWER</small><h2>{turn.answer.likely_root_cause ?? 'Direct answer'}</h2><ReportText>{turn.answer.summary}</ReportText><ReportData evidence={turn.answer.evidence} />{turn.answer.caveats.map((caveat) => <p className="saved-caveat" key={caveat}>{caveat}</p>)}</article>);
      }
      return items;
    })}
    {orderedFeedback.slice(feedbackIndex).map((item, index) => <article className="conversation-turn conversation-feedback" key={`feedback-later-${item.received_at}-${index}`}><small>FOLLOW-UP REQUEST</small><p>{item.question}</p><small>YOUR ANSWER</small><p className="saved-answer">{item.response}</p></article>)}
    {pendingQuestion && !hasRecordedQuestion && <article className="conversation-turn conversation-question conversation-question-pending"><small>QUESTION</small><p>{pendingQuestion}</p>{isInvestigating && <span className="hypothesis-working"><RefreshCw className="spin" /> Investigating</span>}</article>}
  </section>;
}

export default function Home() {
  const [view, setView] = useState<View>('investigate');
  const [question, setQuestion] = useState(defaultQuestion);
  const [datasources, setDatasources] = useState<Datasource[]>([]);
  const [sourceCount, setSourceCount] = useState(0);
  const [savedRunCount, setSavedRunCount] = useState(0);
  const [savedListNavigation, setSavedListNavigation] = useState(0);
  const [resumedInvestigation, setResumedInvestigation] = useState<SavedInvestigation | null>(null);
  const [workbenchKey, setWorkbenchKey] = useState(0);

  const openFreshWorkbench = () => {
    window.sessionStorage.removeItem(activeInvestigationStorageKey);
    setResumedInvestigation(null);
    setQuestion('');
    setWorkbenchKey((current) => current + 1);
    setView('investigate');
  };

  const resumeConversation = useCallback((investigation: SavedInvestigation) => {
    setResumedInvestigation(investigation);
    setQuestion(investigation.original_question ?? investigation.question);
    setView('investigate');
  }, []);

  useEffect(() => {
    fetch(`${apiUrl}/api/datasources`)
      .then((response) => response.ok ? response.json() as Promise<Datasource[]> : Promise.reject())
      .then((sources) => { setDatasources(sources); setSourceCount(sources.length); })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const loadSavedRunCount = () => {
      fetch(`${apiUrl}/api/investigations`)
        .then((response) => response.ok ? response.json() as Promise<SavedInvestigation[]> : Promise.reject())
        .then((saved) => setSavedRunCount(saved.length))
        .catch(() => undefined);
    };
    loadSavedRunCount();
    const refreshTimer = window.setInterval(loadSavedRunCount, 5000);
    return () => window.clearInterval(refreshTimer);
  }, []);

  return (
    <ClientErrorBoundary><div className="min-h-screen bg-background text-foreground">
      <header className="app-header">
        <button className="brand" onClick={openFreshWorkbench} aria-label="Business Signals home">
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
            <NavItem active={view === 'investigate'} icon={<Search />} label="Investigate" onClick={openFreshWorkbench} />
            <NavItem active={view === 'sources'} icon={<Database />} label="Data sources" count={sourceCount} onClick={() => setView('sources')} />
            <NavItem active={view === 'saved'} icon={<Archive />} label="Saved items" count={savedRunCount} onClick={() => { setView('saved'); setSavedListNavigation((current) => current + 1); }} />
            <NavItem active={view === 'architecture'} icon={<Layers3 />} label="How it works" onClick={() => setView('architecture')} />
          </nav>
          <div className="sidebar-note">
            <ShieldCheck />
            <div><strong>Read only by design</strong><span>Credentials stay in backend memory and never enter model context.</span></div>
          </div>
        </aside>

        <main className="main-content">
          {view === 'investigate' && <InvestigationView key={workbenchKey} question={question} setQuestion={setQuestion} datasources={datasources} resumedInvestigation={resumedInvestigation} onStartNew={() => setResumedInvestigation(null)} onRecoverActive={resumeConversation} />}
          {view === 'sources' && <SourcesView onSourceCount={setSourceCount} onSourcesChanged={setDatasources} />}
          {view === 'saved' && <SavedInvestigationsView onSavedRunCount={setSavedRunCount} navigationKey={savedListNavigation} onResume={resumeConversation} />}
          {view === 'architecture' && <ArchitectureView />}
        </main>
      </div>
    </div></ClientErrorBoundary>
  );
}

function NavItem({ active, icon, label, count, onClick }: { active: boolean; icon: React.ReactNode; label: string; count?: number; onClick: () => void }) {
  return <button className={`nav-item ${active ? 'active' : ''}`} onClick={onClick}>{icon}<span>{label}</span>{count !== undefined && <span className="nav-count">{count}</span>}</button>;
}

function InvestigationView({ question, setQuestion, datasources, resumedInvestigation, onStartNew, onRecoverActive }: {
  question: string;
  setQuestion: (value: string) => void;
  datasources: Datasource[];
  resumedInvestigation: SavedInvestigation | null;
  onStartNew: () => void;
  onRecoverActive: (investigation: SavedInvestigation) => void;
}) {
  const [selectedDatasourceIds, setSelectedDatasourceIds] = useState<string[]>([]);
  const [hypotheses, setHypotheses] = useState<Hypothesis[]>([]);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [hypothesisSteps, setHypothesisSteps] = useState<Record<string, HypothesisStep[]>>({});
  const [hypothesisActivity, setHypothesisActivity] = useState<Record<string, HypothesisActivity[]>>({});
  const [expandedHypotheses, setExpandedHypotheses] = useState<Record<string, boolean>>({});
  const [activeHypothesisId, setActiveHypothesisId] = useState<string | null>(null);
  const [finalAnalysis, setFinalAnalysis] = useState<FinalAnalysis | null>(null);
  const [conversationTurns, setConversationTurns] = useState<ConversationTurn[]>([]);
  const [conversationFeedback, setConversationFeedback] = useState<ConversationFeedback[]>([]);
  const [pendingConversationQuestion, setPendingConversationQuestion] = useState<string | null>(null);
  const [investigationId, setInvestigationId] = useState<string | null>(null);
  const [followUpResponse, setFollowUpResponse] = useState('');
  const [clarificationQuestion, setClarificationQuestion] = useState<string | null>(null);
  const [clarificationResponse, setClarificationResponse] = useState('');
  const [switchingClarification, setSwitchingClarification] = useState(false);
  const [replacementQuestion, setReplacementQuestion] = useState('');
  const [clarifications, setClarifications] = useState<Clarification[]>([]);
  const [error, setError] = useState('');
  const [running, setRunning] = useState(false);
  const stream = useRef<EventSource | null>(null);
  const reconciliationTimer = useRef<number | null>(null);
  const eventCursor = useRef(0);
  const answeredClarificationQuestions = useRef(new Set<string>());
  // Each user action starts a newer view of the same conversation. Async work from
  // the prior view must never restore an old waiting-for-answer snapshot over it.
  const investigationEpoch = useRef(0);
  const terminalEpoch = useRef<number | null>(null);

  useEffect(() => {
    setSelectedDatasourceIds((current) => current.filter((id) => datasources.some((source) => source.id === id)));
  }, [datasources]);
  useEffect(() => () => {
    stream.current?.close();
    if (reconciliationTimer.current !== null) window.clearTimeout(reconciliationTimer.current);
  }, []);

  const addHypothesisStep = (hypothesisId: string, event: InvestigationEvent) => {
    const datasourceId = typeof event.data.datasource_id === 'string' ? event.data.datasource_id : undefined;
    const source = datasourceId ? datasources.find((item) => item.id === datasourceId)?.name : undefined;
    setHypothesisSteps((current) => {
      const existing = current[hypothesisId] ?? [];
      if (existing.some((item) => item.id === event.id)) return current;
      return { ...current, [hypothesisId]: [...existing, { id: event.id, type: event.type, message: event.message, source }] };
    });
    addHypothesisActivity(hypothesisId, { id: `step:${event.id}`, kind: 'step', itemId: event.id });
  };

  const addHypothesisActivity = (hypothesisId: string, activity: HypothesisActivity) => {
    setHypothesisActivity((current) => {
      const existing = current[hypothesisId] ?? [];
      return existing.some((item) => item.id === activity.id)
        ? current
        : { ...current, [hypothesisId]: [...existing, activity] };
    });
  };

  const handleEvent = (event: InvestigationEvent) => {
    if ((event.type === 'HypothesisCreated' || event.type === 'HypothesisUpdated') && event.data.hypothesis) {
      const hypothesis = event.data.hypothesis as Hypothesis;
      setHypotheses((current) => current.some((item) => item.id === hypothesis.id)
        ? current.map((item) => item.id === hypothesis.id ? hypothesis : item)
        : [...current, hypothesis]);
      if (event.type === 'HypothesisUpdated') addHypothesisStep(hypothesis.id, event);
      if (event.type === 'HypothesisUpdated') setActiveHypothesisId((current) => current === hypothesis.id ? null : current);
    }
    const hypothesisId = typeof event.data.hypothesis_id === 'string' ? event.data.hypothesis_id : undefined;
    if (hypothesisId && ['DatasourceSelected', 'QueryStarted', 'QueryRejected', 'QueryCompleted', 'QueryFailed', 'ExternalResearchSelected', 'ExternalResearchStarted', 'ExternalResearchCompleted', 'ExternalResearchUnavailable'].includes(event.type)) {
      addHypothesisStep(hypothesisId, event);
      if (event.type === 'DatasourceSelected' || event.type === 'QueryStarted' || event.type === 'QueryRejected' || event.type === 'ExternalResearchSelected' || event.type === 'ExternalResearchStarted') setActiveHypothesisId(hypothesisId);
    }
    if (event.type === 'EvidenceFound' && event.data.evidence) {
      const found = event.data.evidence as Evidence;
      setEvidence((current) => [...current.filter((item) => item.id !== found.id), found]);
      found.hypothesis_ids.forEach((hypothesisId) => addHypothesisActivity(
        hypothesisId, { id: `evidence:${found.id}`, kind: 'evidence', itemId: found.id },
      ));
    }
    if (event.type === 'HumanInputRequested') {
      // A reconnect can replay the pause that has just been answered. It is not a new
      // question and must never put the workbench back into its earlier paused state.
      if (answeredClarificationQuestions.current.has(event.message)) return;
      const hypothesisId = typeof event.data.hypothesis_id === 'string' ? event.data.hypothesis_id : undefined;
      setClarificationQuestion(event.message); setClarificationResponse(''); setRunning(false);
      setSwitchingClarification(false); setReplacementQuestion('');
      setClarifications((current) => current.some((item) => item.question === event.message)
        ? current
        : [...current, { question: event.message, hypothesisId }]);
      if (hypothesisId) addHypothesisActivity(
        hypothesisId, { id: `clarification:${event.message}`, kind: 'clarification', itemId: event.message },
      );
    }
    if (event.type === 'HumanInputReceived') {
      setRunning(true);
    }
    if (event.type === 'InvestigationCompleted') {
      terminalEpoch.current = investigationEpoch.current;
      const completedAnalysis = event.data.final_analysis as FinalAnalysis;
      setFinalAnalysis(completedAnalysis);
      setPendingConversationQuestion(null);
      setFollowUpResponse(completedAnalysis.follow_up_question ?? '');
      if (Array.isArray(event.data.conversation_turns)) setConversationTurns(event.data.conversation_turns as ConversationTurn[]);
      setRunning(false); setActiveHypothesisId(null); stream.current?.close(); stream.current = null;
      if (reconciliationTimer.current !== null) window.clearTimeout(reconciliationTimer.current);
      reconciliationTimer.current = null;
      window.sessionStorage.removeItem(activeInvestigationStorageKey);
    }
    if (event.type === 'InvestigationFailed') {
      terminalEpoch.current = investigationEpoch.current;
      setError(event.message);
      setRunning(false); setActiveHypothesisId(null); stream.current?.close(); stream.current = null;
      if (reconciliationTimer.current !== null) window.clearTimeout(reconciliationTimer.current);
      reconciliationTimer.current = null;
      window.sessionStorage.removeItem(activeInvestigationStorageKey);
    }
  };

  const reconcileFinishedInvestigation = async (id: string, epoch = investigationEpoch.current): Promise<boolean> => {
    try {
      const response = await fetch(`${apiUrl}/api/investigations/${id}`, { cache: 'no-store' });
      if (!response.ok) return false;
      const latest = await response.json() as SavedInvestigation;
      // A clarification/follow-up was submitted after this fetch began. Its response is
      // authoritative, so discard this stale snapshot without treating the stream as failed.
      if (epoch !== investigationEpoch.current) return true;
      // A retained terminal event is more recent than an in-flight poll. Never
      // let an older running snapshot erase the completed result from the UI.
      if (terminalEpoch.current === epoch) return true;
      // A newly-created investigation is briefly queued before its background graph task
      // marks it running. Neither state is a completed reconciliation target: treating
      // `queued` as terminal cleared the submitted question and closed the live stream.
      setHypotheses(latest.hypotheses);
      setEvidence(latest.evidence);
      setConversationTurns(latest.conversation_turns);
      setConversationFeedback(latest.human_feedback);
      answeredClarificationQuestions.current = new Set(latest.human_feedback.map((item) => item.question));
      setClarifications([
        ...latest.human_feedback.map((item) => ({
          question: item.question, response: item.response, hypothesisId: item.hypothesis_id ?? undefined,
        })),
        ...(latest.status === 'waiting_for_human' && latest.pending_human_question ? [{ question: latest.pending_human_question }] : []),
      ]);
      setClarificationQuestion(latest.status === 'waiting_for_human' ? latest.pending_human_question : null);
      setClarificationResponse('');
      // A clarification pauses before a conversation turn is completed, so the submitted
      // question is not yet in conversation_turns. Keep it visible above the clarification.
      setPendingConversationQuestion(
        latest.status === 'waiting_for_human' || latest.status === 'running' || latest.status === 'queued'
          ? latest.current_conversation_question ?? latest.original_question ?? latest.question
          : null,
      );
      setFinalAnalysis(latest.final_analysis);
      setFollowUpResponse(latest.final_analysis?.follow_up_question ?? '');
      if (latest.status === 'queued' || latest.status === 'running') {
        // Creation is asynchronous: a newly accepted investigation is queued
        // briefly before the graph marks it running.  Both are live states.
        setRunning(true);
        return false;
      }
      terminalEpoch.current = epoch;
      setRunning(false);
      if (reconciliationTimer.current !== null) window.clearTimeout(reconciliationTimer.current);
      reconciliationTimer.current = null;
      if (latest.status !== 'waiting_for_human') window.sessionStorage.removeItem(activeInvestigationStorageKey);
      if (latest.status === 'failed') setError('The investigation could not be completed. Check the backend logs and try again.');
      stream.current?.close(); stream.current = null;
      return true;
    } catch (cause) {
      console.error('Could not reconcile investigation state', cause);
      return false;
    }
  };

  const openInvestigationStream = (id: string, liveOnly = false) => {
    stream.current?.close();
    const streamEpoch = investigationEpoch.current;
    const eventSource = new EventSource(`${apiUrl}/api/investigations/${id}/events?after=${eventCursor.current}${liveOnly ? '&live_only=true' : ''}`);
    stream.current = eventSource;
    const processEvent = (message: Event) => {
      if (stream.current !== eventSource || streamEpoch !== investigationEpoch.current) return;
      try {
        const cursor = Number((message as MessageEvent).lastEventId);
        if (Number.isSafeInteger(cursor) && cursor > eventCursor.current) eventCursor.current = cursor;
        handleEvent(JSON.parse((message as MessageEvent).data) as InvestigationEvent);
      } catch (cause) {
        console.error('Could not process investigation event', cause, message);
        // One malformed or interrupted SSE message must not strand this tab.
        // The persisted investigation snapshot remains authoritative and will
        // fill in every missed update while EventSource reconnects.
        setRunning(true);
        void reconcileUntilTerminal(id, streamEpoch);
      }
    };
    ['InvestigationStarted', 'SchemaDiscovered', 'HypothesisCreated', 'DatasourceSelected', 'QueryStarted', 'QueryRejected', 'QueryCompleted', 'QueryFailed', 'ExternalResearchSelected', 'ExternalResearchStarted', 'ExternalResearchCompleted', 'ExternalResearchUnavailable', 'EvidenceFound', 'HypothesisUpdated', 'HumanInputRequested', 'HumanInputReceived', 'InvestigationCompleted', 'InvestigationFailed'].forEach((type) => eventSource.addEventListener(type, processEvent));
    eventSource.onerror = () => {
      if (stream.current === eventSource && streamEpoch === investigationEpoch.current) {
        void reconcileFinishedInvestigation(id, streamEpoch).then((reconciled) => {
          if (!reconciled && stream.current === eventSource) setRunning(true);
        });
      }
    };
  };

  const reconcileUntilTerminal = (id: string, epoch = investigationEpoch.current) => {
    if (reconciliationTimer.current !== null) window.clearTimeout(reconciliationTimer.current);
    const poll = async () => {
      if (epoch !== investigationEpoch.current) return;
      const reconciled = await reconcileFinishedInvestigation(id, epoch);
      if (!reconciled && epoch === investigationEpoch.current) {
        reconciliationTimer.current = window.setTimeout(() => { void poll(); }, 1000);
      }
    };
    void poll();
  };

  useEffect(() => {
    // SSE makes progress feel immediate, but it is not the source of truth.
    // Keep reconciling an active turn even if an interrupted stream has
    // temporarily cleared `running`; otherwise the original tab can remain
    // stale while a newly opened tab correctly reads the persisted state.
    if (!investigationId || clarificationQuestion || finalAnalysis || error) return;
    const epoch = investigationEpoch.current;
    void reconcileFinishedInvestigation(investigationId, epoch);
    const timer = window.setInterval(() => void reconcileFinishedInvestigation(investigationId, epoch), 2500);
    return () => window.clearInterval(timer);
  }, [clarificationQuestion, error, finalAnalysis, investigationId]);

  useEffect(() => {
    if (!resumedInvestigation) return;
    investigationEpoch.current += 1;
    terminalEpoch.current = null;
    answeredClarificationQuestions.current = new Set(
      resumedInvestigation.human_feedback.map((item) => item.question),
    );
    if (resumedInvestigation.status === 'running' || resumedInvestigation.status === 'waiting_for_human') {
      window.sessionStorage.setItem(activeInvestigationStorageKey, resumedInvestigation.investigation_id);
    }
    const resumedDatasourceIds = resumedInvestigation.datasources
      .map((source) => source.id)
      .filter((id) => datasources.some((source) => source.id === id));
    setSelectedDatasourceIds(resumedDatasourceIds);
    setHypotheses(resumedInvestigation.hypotheses);
    setEvidence(resumedInvestigation.evidence);
    setHypothesisSteps({});
    setHypothesisActivity({});
    setExpandedHypotheses({});
    setActiveHypothesisId(null);
    setFinalAnalysis(resumedInvestigation.final_analysis);
    setConversationTurns(
      resumedInvestigation.conversation_turns.length > 0
        ? resumedInvestigation.conversation_turns
        : resumedInvestigation.final_analysis
        ? [{
            question: resumedInvestigation.original_question ?? resumedInvestigation.question,
            answer: resumedInvestigation.final_analysis,
            created_at: resumedInvestigation.started_at,
          }]
        : [],
    );
    setConversationFeedback(resumedInvestigation.human_feedback);
    setPendingConversationQuestion(resumedInvestigation.current_conversation_question);
    setInvestigationId(resumedInvestigation.investigation_id);
    setFollowUpResponse(resumedInvestigation.final_analysis?.follow_up_question ?? '');
    setClarifications([
      ...resumedInvestigation.human_feedback.map((item) => ({
        question: item.question, response: item.response, hypothesisId: item.hypothesis_id ?? undefined,
      })),
      ...(
        resumedInvestigation.status === 'waiting_for_human' && resumedInvestigation.pending_human_question
          ? [{ question: resumedInvestigation.pending_human_question }]
          : []
      ),
    ]);
    setClarificationQuestion(
      resumedInvestigation.status === 'waiting_for_human' ? resumedInvestigation.pending_human_question : null,
    );
    setClarificationResponse(''); setSwitchingClarification(false); setReplacementQuestion('');
    setError('');
    setRunning(resumedInvestigation.status === 'running');
    eventCursor.current = 0;
    if (resumedInvestigation.status === 'running') {
      openInvestigationStream(resumedInvestigation.investigation_id);
      reconcileUntilTerminal(resumedInvestigation.investigation_id);
    }
  }, [resumedInvestigation?.investigation_id, datasources]);

  useEffect(() => {
    if (resumedInvestigation || investigationId) return;
    const activeId = window.sessionStorage.getItem(activeInvestigationStorageKey);
    if (!activeId) return;
    let cancelled = false;
    void fetch(`${apiUrl}/api/investigations/${activeId}`)
      .then((response) => response.ok ? response.json() as Promise<SavedInvestigation> : Promise.reject())
      .then((investigation) => {
        if (cancelled) return;
        if (investigation.status === 'running' || investigation.status === 'waiting_for_human') {
          onRecoverActive(investigation);
        } else {
          window.sessionStorage.removeItem(activeInvestigationStorageKey);
        }
      })
      .catch(() => window.sessionStorage.removeItem(activeInvestigationStorageKey));
    return () => { cancelled = true; };
  }, [investigationId, onRecoverActive, resumedInvestigation]);

  const startInvestigation = async () => {
    if (selectedDatasourceIds.length === 0) { setError('Choose at least one connected datasource first.'); return; }
    onStartNew();
    investigationEpoch.current += 1;
    terminalEpoch.current = null;
    answeredClarificationQuestions.current = new Set();
    setError(''); setHypotheses([]); setEvidence([]); setHypothesisSteps({}); setHypothesisActivity({}); setExpandedHypotheses({}); setActiveHypothesisId(null); setFinalAnalysis(null); setConversationTurns([]); setConversationFeedback([]); setPendingConversationQuestion(question.trim()); setInvestigationId(null); setFollowUpResponse(''); setClarificationQuestion(null); setClarificationResponse(''); setSwitchingClarification(false); setReplacementQuestion(''); setClarifications([]); setRunning(true); eventCursor.current = 0; stream.current?.close();
    try {
      const response = await fetch(`${apiUrl}/api/investigations`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ question, datasource_ids: selectedDatasourceIds }) });
      if (!response.ok) {
        throw new Error(response.status === 404
          ? 'A selected datasource is no longer available. Refresh the page and select it again.'
          : 'The investigation could not be started. Check the datasource connection and backend logs.');
      }
      const created = await response.json() as { investigation_id: string };
      window.sessionStorage.setItem(activeInvestigationStorageKey, created.investigation_id);
      setInvestigationId(created.investigation_id);
      openInvestigationStream(created.investigation_id);
      reconcileUntilTerminal(created.investigation_id);
    } catch (cause) { console.error('Could not start investigation', cause); setError('The investigation could not be started. Check the backend logs and try again.'); setRunning(false); }
  };

  const submitClarification = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!clarificationResponse.trim()) return;
    if (!investigationId) {
      setError('The conversation reference is unavailable. Open the saved conversation and submit the clarification there.');
      return;
    }
    investigationEpoch.current += 1;
    terminalEpoch.current = null;
    const answeredQuestion = clarificationQuestion;
    // Move straight into the live waiting state. The answer has been submitted,
    // so leaving an editable clarification form on screen invites duplicate input.
    setError('');
    setClarificationQuestion(null);
    setRunning(true);
    try {
      const response = await fetch(`${apiUrl}/api/investigations/${investigationId}/responses`, {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ response: clarificationResponse.trim() }),
      });
      if (!response.ok) throw new Error(`Clarification request failed with ${response.status}`);
      // The response is the source of truth for the accepted answer. Applying it immediately
      // keeps the answer on screen even if the EventSource reconnects while the graph resumes.
      const updated = await response.json() as SavedInvestigation;
      setHypotheses(updated.hypotheses);
      setEvidence(updated.evidence);
      setConversationTurns(updated.conversation_turns);
    setConversationFeedback(updated.human_feedback);
      answeredClarificationQuestions.current = new Set(updated.human_feedback.map((item) => item.question));
      setClarifications(updated.human_feedback.map((item) => ({
        question: item.question, response: item.response, hypothesisId: item.hypothesis_id ?? undefined,
      })));
      setPendingConversationQuestion(updated.current_conversation_question ?? updated.original_question ?? updated.question);
      setFinalAnalysis(updated.final_analysis);
      setClarificationQuestion(updated.status === 'waiting_for_human' ? updated.pending_human_question : null);
      setClarificationResponse(''); setSwitchingClarification(false); setReplacementQuestion('');
      const isActive = updated.status === 'queued' || updated.status === 'running';
      setRunning(isActive);
      // The POST response is the authoritative snapshot. Start the replacement stream
      // from now so it cannot replay the just-answered pause; reconciliation covers the
      // small gap before this EventSource is connected.
      if (isActive) {
        // Replay events after the last cursor rather than using a live-only
        // subscription. A fast graph can finish before the SSE connection is
        // established; retained events and polling make that completion visible.
        openInvestigationStream(investigationId);
        reconcileUntilTerminal(investigationId);
      }
    } catch (cause) {
      console.error('Could not submit clarification', cause);
      setClarificationQuestion(answeredQuestion);
      setRunning(false);
      setError('The clarification could not be submitted. Check the backend logs and try again.');
    }
  };

  const skipClarification = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!investigationId || !replacementQuestion.trim()) return;
    investigationEpoch.current += 1;
    terminalEpoch.current = null;
    try {
      const response = await fetch(`${apiUrl}/api/investigations/${investigationId}/skip-clarification`, {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ response: replacementQuestion.trim() }),
      });
      if (!response.ok) throw new Error(`Skip clarification request failed with ${response.status}`);
      const updated = await response.json() as SavedInvestigation;
      setConversationTurns(updated.conversation_turns);
      setConversationFeedback(updated.human_feedback);
      answeredClarificationQuestions.current = new Set(updated.human_feedback.map((item) => item.question));
      setClarifications(updated.human_feedback.map((item) => ({
        question: item.question, response: item.response, hypothesisId: item.hypothesis_id ?? undefined,
      })));
      setPendingConversationQuestion(replacementQuestion.trim()); setFinalAnalysis(null); setClarificationQuestion(null); setClarificationResponse(''); setSwitchingClarification(false); setReplacementQuestion(''); setError(''); setRunning(true);
      openInvestigationStream(investigationId);
      reconcileUntilTerminal(investigationId);
    } catch (cause) {
      console.error('Could not skip clarification', cause);
      setError('The new question could not be started. Check the backend logs and try again.');
    }
  };

  const submitFollowUp = async (response: string) => {
    if (!investigationId || !response.trim()) return;
    investigationEpoch.current += 1;
    terminalEpoch.current = null;
    const submittedResponse = response.trim() === finalAnalysis?.follow_up_question?.trim() ? 'Yes' : response.trim();
      const followUpQuestion = finalAnalysis?.follow_up_question ?? 'Would you like to know more?';
      const requestedDetail = submittedResponse.toLowerCase() === 'yes' || submittedResponse.toLowerCase() === 'yes please' || submittedResponse.toLowerCase() === 'sure' || submittedResponse.toLowerCase() === 'please'
        ? followUpQuestion
        : submittedResponse;
    try {
      // A saved conversation can have changed state in another browser tab or while its
      // event stream was reconnecting. Never send a follow-up into a pending clarification.
      const latestRequest = await fetch(`${apiUrl}/api/investigations/${investigationId}`);
      if (latestRequest.ok) {
        const latest = await latestRequest.json() as SavedInvestigation;
        if (latest.status === 'waiting_for_human' && latest.pending_human_question) {
          setFinalAnalysis(latest.final_analysis);
          setConversationTurns(latest.conversation_turns);
          setConversationFeedback(latest.human_feedback);
          setClarifications([
            ...latest.human_feedback.map((item) => ({
              question: item.question, response: item.response, hypothesisId: item.hypothesis_id ?? undefined,
            })),
            { question: latest.pending_human_question },
          ]);
          setClarificationQuestion(latest.pending_human_question);
          setClarificationResponse('');
          setFollowUpResponse('');
          setError('');
          return;
        }
      }
      const request = await fetch(`${apiUrl}/api/investigations/${investigationId}/follow-up`, {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ response: submittedResponse }),
      });
      if (!request.ok) throw new Error(`Follow-up request failed with ${request.status}`);
      const updated = await request.json() as { status: string; final_analysis: FinalAnalysis | null };
      setConversationFeedback((current) => [...current, {
        question: followUpQuestion, response: submittedResponse, hypothesis_id: null, received_at: new Date().toISOString(),
      }]);
      setFollowUpResponse('');
      if (updated.status === 'completed') {
        setFinalAnalysis(updated.final_analysis);
        return;
      }
      setError(''); setHypotheses([]); setEvidence([]); setHypothesisSteps({}); setHypothesisActivity({}); setExpandedHypotheses({}); setActiveHypothesisId(null); setFinalAnalysis(null); setPendingConversationQuestion(requestedDetail); setClarificationQuestion(null); setClarificationResponse(''); setClarifications([]); setRunning(true);
      openInvestigationStream(investigationId);
      reconcileUntilTerminal(investigationId);
    } catch (cause) {
      console.error('Could not request more detail', cause);
      setError('More detail could not be requested. Check the backend logs and try again.');
    }
  };

  const renderClarification = (item: Clarification, index: number) => {
    const awaitingAnswer = clarificationQuestion === item.question && !item.response;
    const questionNumber = clarifications.indexOf(item) + 1;
    return <article className="clarification-at-stage" key={`${item.question}-${index}`}>
      <span>Q{questionNumber}</span>
      <div>
        <small>{awaitingAnswer ? 'CLARIFICATION NEEDED' : 'CLARIFICATION'}</small>
        <p>{item.question}</p>
        {awaitingAnswer
          ? switchingClarification
          ? <form onSubmit={skipClarification}><Textarea value={replacementQuestion} onChange={(event) => setReplacementQuestion(event.target.value)} placeholder="Ask a different question…" rows={3} /><div><span>This skips the clarification and keeps the conversation context.</span><Button type="submit" disabled={!replacementQuestion.trim()}>Ask question <ArrowRight /></Button><Button type="button" variant="outline" onClick={() => { setSwitchingClarification(false); setReplacementQuestion(''); }}>Cancel</Button></div></form>
          : <form onSubmit={submitClarification}><Textarea value={clarificationResponse} onChange={(event) => setClarificationResponse(event.target.value)} placeholder="Provide the business context the investigation needs…" rows={3} /><div><span>The investigation is paused until you respond.</span><Button type="submit" disabled={!clarificationResponse.trim()}>Resume investigation <ArrowRight /></Button><Button type="button" variant="outline" onClick={() => setSwitchingClarification(true)}>Ask something else</Button></div></form>
          : item.response ? <><small>YOUR ANSWER</small><p className="clarification-answer">{item.response}</p></> : <small className="clarification-pending">AWAITING YOUR ANSWER</small>}
      </div>
    </article>;
  };

  const toggleDatasource = (id: string) => setSelectedDatasourceIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  const isResumedConversation = resumedInvestigation?.investigation_id === investigationId;
  // While a clarification is pending there is no completed conversation turn yet. The editor
  // still holds the submitted question, so use it as a safe display fallback during event and
  // polling reconciliation.
  const questionAwaitingClarification = pendingConversationQuestion
    ?? (clarificationQuestion ? question.trim() || null : null);
  return (
    <div className="investigation-layout">
      <section className="work-column">
        <div className="page-heading">
          <div><span className="section-kicker">{isResumedConversation ? 'RESUMED CONVERSATION' : 'ROOT-CAUSE WORKBENCH'}</span><h1>{isResumedConversation ? 'Continue from the evidence.' : 'Ask why. Follow the evidence.'}</h1>{investigationId && <p className="resumed-conversation-id">Conversation ID <code>{investigationId}</code></p>}</div>
          <Badge variant="outline" className="demo-badge"><Activity /> {isResumedConversation ? 'Conversation resumed' : 'Live investigation'}</Badge>
        </div>

        <ConversationTranscript turns={conversationTurns} feedback={conversationFeedback} pendingQuestion={questionAwaitingClarification} omitLatestAnswer={finalAnalysis !== null} isInvestigating={running} />

        <div className="timeline-heading">
          <div><h2>Hypothesis investigation tree</h2><span>{running ? 'The graph is evaluating live evidence' : finalAnalysis ? 'Investigation complete' : 'Ready'}</span></div>
          {running && <span className="live-pill"><span /> Live</span>}
        </div>

        <div className="hypothesis-tree-list" aria-live="polite">
          {hypotheses.map((hypothesis, hypothesisIndex) => {
            const relatedEvidence = evidence.filter((item) => item.hypothesis_ids.includes(hypothesis.id));
            const steps = hypothesisSteps[hypothesis.id] ?? [];
            const activities = hypothesisActivity[hypothesis.id] ?? [];
            const hypothesisLabel = `H${hypothesisIndex + 1}`;
            const tone = hypothesis.status === 'rejected' ? 'bad' : hypothesis.status === 'supported' || hypothesis.status === 'confirmed' ? 'good' : 'muted';
            return <details className="hypothesis-tree" key={hypothesis.id} open={expandedHypotheses[hypothesis.id] ?? true} onToggle={(event) => { const isOpen = event.currentTarget.open; setExpandedHypotheses((current) => ({ ...current, [hypothesis.id]: isOpen })); }}>
              <summary>
                <code>{hypothesisLabel}</code><strong>{hypothesis.name}</strong>{activeHypothesisId === hypothesis.id && <span className="hypothesis-working"><RefreshCw className="spin" /> Checking</span>}<span className={tone}>{hypothesis.status}</span><span className="tree-confidence">{Math.round(hypothesis.confidence * 100)}%</span><ChevronDown />
              </summary>
              <div className="tree-children">
                <p className="hypothesis-description">{hypothesis.description}</p>
                {activeHypothesisId === hypothesis.id && <div className="hypothesis-working-detail"><RefreshCw className="spin" /><span>Checking the next piece of evidence…</span></div>}
                {activities.map((activity) => {
                  if (activity.kind === 'step') {
                    const step = steps.find((item) => item.id === activity.itemId);
                    const isCurrentStep = activeHypothesisId === hypothesis.id && steps.at(-1)?.id === step?.id;
                    return step ? <article className="hypothesis-step" key={activity.id}><span>{isCurrentStep && <RefreshCw className="spin" />} {investigationStepLabels[step.type] ?? step.type.replaceAll(/([A-Z])/g, ' $1').trim()}</span><p>{step.message}</p>{step.source && <small>{step.source}</small>}</article> : null;
                  }
                  if (activity.kind === 'clarification') {
                    const clarification = clarifications.find((item) => item.question === activity.itemId);
                    return clarification ? renderClarification(clarification, clarifications.indexOf(clarification)) : null;
                  }
                  const item = relatedEvidence.find((candidate) => candidate.id === activity.itemId);
                  if (!item) return null;
                  const evidenceLabel = `E${evidence.findIndex((candidate) => candidate.id === item.id) + 1}`;
                  return <article className="hypothesis-evidence" key={activity.id}><code>{evidenceLabel}</code><div><span>{item.relationship} evidence · {Math.round(item.confidence * 100)}%</span><p>{item.description}</p></div></article>;
                })}
                {steps.length === 0 && relatedEvidence.length === 0 && <p className="tree-empty">Awaiting the first investigation step.</p>}
              </div>
            </details>;
          })}
          {!running && hypotheses.length === 0 && <div className="empty-timeline">Start an investigation to build a hypothesis tree.</div>}
          {running && <div className="thinking-row"><span className="thinking-icon"><RefreshCw className="spin" /></span><div><strong>Investigation in progress…</strong><span>The next update will appear here automatically.</span></div></div>}
        </div>
        {error && <output className="notice investigation-error">{error}</output>}
        {finalAnalysis && <article className="final-result"><span className="section-kicker">FINAL ANALYSIS</span><h2>{finalAnalysis.likely_root_cause ?? 'Insufficient evidence'}</h2><ReportText>{finalAnalysis.summary}</ReportText><ReportData evidence={finalAnalysis.evidence} /><span className="source-chip">{Math.round(finalAnalysis.confidence * 100)}% confidence</span>{finalAnalysis.caveats.map((caveat) => <p className="final-caveat" key={caveat}>{caveat}</p>)}{!clarificationQuestion && <section className="follow-up-prompt"><p>Would you like to know more?</p><form onSubmit={(event) => { event.preventDefault(); void submitFollowUp(followUpResponse); }}><Textarea value={followUpResponse} onChange={(event) => setFollowUpResponse(event.target.value)} placeholder="Ask a follow-up question…" rows={3} /><div><Button type="submit" disabled={!followUpResponse.trim()}>Submit <ArrowRight /></Button><Button type="button" variant="outline" onClick={() => void submitFollowUp('No thanks')}>Stop</Button></div></form></section>}</article>}
        {clarificationQuestion && <section className="clarification-stage active-clarification"><span className="section-kicker">CLARIFICATION</span>{clarifications.filter((item) => item.question === clarificationQuestion).map(renderClarification)}</section>}
        {!investigationId && <div className="question-card">
          <label htmlFor="question">Business question</label>
          <Textarea id="question" value={question} onChange={(event) => setQuestion(event.target.value)} rows={3} />
          <div className="question-footer">
            <div className="selected-sources datasource-picker">{datasources.map((source) => <label key={source.id}><input type="checkbox" checked={selectedDatasourceIds.includes(source.id)} onChange={() => toggleDatasource(source.id)} /><Database /> {source.name}</label>)}{datasources.length === 0 && <span>Connect a datasource to investigate</span>}</div>
            <Button onClick={startInvestigation} disabled={running || question.trim().length < 10 || datasources.length === 0} className="investigate-button">
              {running ? <><RefreshCw className="spin" /> Investigating</> : <><Play /> Investigate</>}
            </Button>
          </div>
        </div>}
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
  const [relationships, setRelationships] = useState<CrossDatasourceRelation[]>([]);
  const [relationshipSuggestions, setRelationshipSuggestions] = useState<CrossDatasourceRelation[]>([]);
  const [relationshipForm, setRelationshipForm] = useState<RelationshipForm | null>(null);
  const [relationshipPreview, setRelationshipPreview] = useState<CrossDatasourceRelation | null>(null);
  const [loadingRelationships, setLoadingRelationships] = useState(false);
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
        const relationshipResponse = await fetch(`${apiUrl}/api/relationships`);
        if (relationshipResponse.ok) setRelationships(await relationshipResponse.json() as CrossDatasourceRelation[]);
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
      setRelationships((current) => current.filter((relation) => relation.source_datasource !== source.id && relation.target_datasource !== source.id));
      setNotice(`${source.name} was deleted.`);
    } catch (cause) {
      console.error('Could not delete datasource', cause);
      setNotice('The datasource could not be deleted. Check the backend logs and try again.');
    }
  };

  const sourceName = (sourceId: string) => sources.find((source) => source.id === sourceId)?.name ?? 'Unknown connection';
  const relationshipFields = (sourceId: string) => (
    metadata[sourceId]?.structural_metadata.flatMap((table) => table.columns.map((column) => ({
      value: `${table.schema_name}.${table.name}.${column.name}`,
      label: `${table.schema_name}.${table.name} · ${column.name}`,
      primaryKey: column.primary_key,
    }))) ?? []
  );
  const beginRelationship = (relation?: CrossDatasourceRelation) => {
    setRelationshipPreview(null);
    setRelationshipForm(relation ? {
      id: relation.confirmed ? relation.id : undefined,
      source_datasource: relation.source_datasource,
      source_field: relation.source_field,
      target_datasource: relation.target_datasource,
      target_field: relation.target_field,
      label: relation.label,
      cardinality: relation.cardinality,
    } : {
      source_datasource: sources[0]?.id ?? '', source_field: '', target_datasource: sources[1]?.id ?? '', target_field: '',
      label: '', cardinality: 'many_to_one',
    });
  };
  const validateRelationship = async () => {
    if (!relationshipForm) return;
    setLoadingRelationships(true);
    try {
      const response = await fetch(`${apiUrl}/api/relationships/validate`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ...relationshipForm, id: undefined }),
      });
      if (!response.ok) throw new Error(await response.text());
      setRelationshipPreview(await response.json() as CrossDatasourceRelation);
    } catch (cause) {
      console.error('Could not validate relationship', cause);
      setNotice('The relationship could not be validated. Confirm that both schemas are loaded and check the backend log.');
    } finally { setLoadingRelationships(false); }
  };
  const saveRelationship = async (candidate?: CrossDatasourceRelation) => {
    const payload = candidate ? {
      source_datasource: candidate.source_datasource, source_field: candidate.source_field,
      target_datasource: candidate.target_datasource, target_field: candidate.target_field,
      label: candidate.label, cardinality: candidate.cardinality,
    } : relationshipForm;
    if (!payload) return;
    setLoadingRelationships(true);
    try {
      const response = await fetch(
        `${apiUrl}/api/relationships${'id' in payload && payload.id ? `/${payload.id}` : ''}`,
        {
          method: 'id' in payload && payload.id ? 'PUT' : 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ ...payload, id: undefined }),
        },
      );
      if (!response.ok) throw new Error(await response.text());
      const saved = await response.json() as CrossDatasourceRelation;
      setRelationships((current) => [...current.filter((relation) => relation.id !== saved.id), saved]);
      setRelationshipSuggestions((current) => current.filter((relation) => !(relation.source_datasource === saved.source_datasource && relation.source_field === saved.source_field && relation.target_datasource === saved.target_datasource && relation.target_field === saved.target_field)));
      setRelationshipForm(null); setRelationshipPreview(null); setNotice('Relationship approved and saved. New investigations will use it.');
    } catch (cause) {
      console.error('Could not save relationship', cause);
      setNotice('The relationship could not be saved. Check the backend log and try again.');
    } finally { setLoadingRelationships(false); }
  };
  const reviewRelationshipSuggestions = async () => {
    setLoadingRelationships(true);
    try {
      const response = await fetch(`${apiUrl}/api/relationships/suggestions`);
      if (!response.ok) throw new Error(await response.text());
      setRelationshipSuggestions(await response.json() as CrossDatasourceRelation[]);
      const refreshedMetadata = await Promise.all(sources.map(async (source) => {
        const metadataResponse = await fetch(`${apiUrl}/api/datasources/${source.id}/metadata`);
        return metadataResponse.ok ? [source.id, await metadataResponse.json() as SchemaMetadata] as const : null;
      }));
      setMetadata(Object.fromEntries(refreshedMetadata.filter((item): item is readonly [string, SchemaMetadata] => item !== null)));
      const sourcesResponse = await fetch(`${apiUrl}/api/datasources`);
      if (sourcesResponse.ok) {
        const refreshedSources = await sourcesResponse.json() as Datasource[];
        setSources(refreshedSources); onSourceCount(refreshedSources.length); onSourcesChanged(refreshedSources);
      }
      setNotice('Relationship suggestions are ready for review. Nothing is used until you approve it.');
    } catch (cause) {
      console.error('Could not review relationship suggestions', cause);
      setNotice('Suggestions could not be checked. Discover both schemas first, then try again.');
    } finally { setLoadingRelationships(false); }
  };
  const deleteRelationship = async (relation: CrossDatasourceRelation) => {
    if (!window.confirm(`Remove “${relation.label}”? New investigations will no longer use it.`)) return;
    try {
      const response = await fetch(`${apiUrl}/api/relationships/${relation.id}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(await response.text());
      setRelationships((current) => current.filter((item) => item.id !== relation.id));
      setNotice('Relationship removed.');
    } catch (cause) {
      console.error('Could not delete relationship', cause);
      setNotice('The relationship could not be removed. Check the backend log and try again.');
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
      <section className="data-model-section">
        <div className="data-model-heading"><div><span className="section-kicker">TRUSTED DATA MODEL</span><h2>Relationships between connections</h2><p>Only an administrator-approved relationship can connect data across databases. Suggestions are checked from sampled values, never from matching field names.</p></div><div className="source-actions"><Button variant="outline" onClick={() => void reviewRelationshipSuggestions()} disabled={loadingRelationships || sources.length < 2}><RefreshCw className={loadingRelationships ? 'spin' : ''} /> Find suggestions</Button><Button onClick={() => beginRelationship()} disabled={sources.length < 2}><Plus /> Add relationship</Button></div></div>
        {relationships.length === 0 && <div className="empty-relationships">No cross-database relationships have been approved yet.</div>}
        <div className="relationship-list-cards">{relationships.map((relation) => <article className="relationship-card" key={relation.id}><GitBranch /><div><strong>{relation.label}</strong><span>{sourceName(relation.source_datasource)} → {sourceName(relation.target_datasource)} · {relation.cardinality === 'many_to_one' ? 'many records belong to one record' : 'one record matches one record'}</span><small>Validated with {relation.matched_values} of {relation.sampled_values} sampled values matching</small></div><div className="source-actions"><Button size="sm" variant="outline" onClick={() => beginRelationship(relation)}>Edit</Button><Button size="icon" variant="destructive" onClick={() => void deleteRelationship(relation)} aria-label={`Remove ${relation.label}`}><Trash2 /></Button></div></article>)}</div>
        {relationshipSuggestions.length > 0 && <div className="relationship-suggestions"><h3>Suggestions to review</h3>{relationshipSuggestions.map((relation) => <article className="relationship-card suggestion" key={`${relation.source_datasource}-${relation.source_field}-${relation.target_datasource}-${relation.target_field}`}><HelpCircle /><div><strong>{relation.label}</strong><span>{sourceName(relation.source_datasource)} → {sourceName(relation.target_datasource)} · {Math.round(relation.confidence * 100)}% of sampled values match</span><small>Suggested from key structure and sampled values. It is not active.</small></div><div className="source-actions"><Button size="sm" onClick={() => void saveRelationship(relation)} disabled={loadingRelationships}>Approve</Button><Button size="sm" variant="outline" onClick={() => beginRelationship(relation)}>Edit first</Button></div></article>)}</div>}
        {relationshipForm && <div className="relationship-editor"><div className="form-heading"><div><h3>{relationshipForm.id ? 'Edit relationship' : 'Add relationship'}</h3><p>Describe the business connection. Technical fields are shown only here for the administrator.</p></div><button type="button" onClick={() => { setRelationshipForm(null); setRelationshipPreview(null); }} aria-label="Close"><X /></button></div><div className="form-grid"><div className="form-field"><label>Relationship name</label><Input value={relationshipForm.label} onChange={(event) => { setRelationshipPreview(null); setRelationshipForm({ ...relationshipForm, label: event.target.value }); }} placeholder="Sales records belong to products" /></div><div className="form-field"><label>Relationship shape</label><select value={relationshipForm.cardinality} onChange={(event) => setRelationshipForm({ ...relationshipForm, cardinality: event.target.value as RelationshipForm['cardinality'] })}><option value="many_to_one">Many records belong to one record</option><option value="one_to_one">One record matches one record</option></select></div><div className="form-field"><label>From connection</label><select value={relationshipForm.source_datasource} onChange={(event) => { const sourceId = event.target.value; const targetId = relationshipForm.target_datasource === sourceId ? sources.find((source) => source.id !== sourceId)?.id ?? '' : relationshipForm.target_datasource; setRelationshipPreview(null); setRelationshipForm({ ...relationshipForm, source_datasource: sourceId, source_field: '', target_datasource: targetId, target_field: '' }); }}>{sources.map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}</select></div><div className="form-field"><label>From field <small>(advanced)</small></label><select value={relationshipForm.source_field} onChange={(event) => { setRelationshipPreview(null); setRelationshipForm({ ...relationshipForm, source_field: event.target.value }); }}><option value="">Choose a field</option>{relationshipFields(relationshipForm.source_datasource).map((field) => <option key={field.value} value={field.value}>{field.label}</option>)}</select></div><div className="form-field"><label>To connection</label><select value={relationshipForm.target_datasource} onChange={(event) => { const targetId = event.target.value; const sourceId = relationshipForm.source_datasource === targetId ? sources.find((source) => source.id !== targetId)?.id ?? '' : relationshipForm.source_datasource; setRelationshipPreview(null); setRelationshipForm({ ...relationshipForm, source_datasource: sourceId, source_field: '', target_datasource: targetId, target_field: '' }); }}>{sources.filter((source) => source.id !== relationshipForm.source_datasource).map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}</select></div><div className="form-field"><label>To primary key <small>(advanced)</small></label><select value={relationshipForm.target_field} onChange={(event) => { setRelationshipPreview(null); setRelationshipForm({ ...relationshipForm, target_field: event.target.value }); }}><option value="">Choose a primary key</option>{relationshipFields(relationshipForm.target_datasource).filter((field) => field.primaryKey).map((field) => <option key={field.value} value={field.value}>{field.label}</option>)}</select></div></div>{relationshipPreview && <output className="relationship-validation">Validation passed: {relationshipPreview.matched_values} of {relationshipPreview.sampled_values} sampled values match ({Math.round(relationshipPreview.confidence * 100)}%).</output>}<div className="form-actions"><span><ShieldCheck /> Read-only validation checks value coverage before approval</span><div className="source-actions"><Button type="button" variant="outline" onClick={() => void validateRelationship()} disabled={loadingRelationships || !relationshipForm.label || !relationshipForm.source_field || !relationshipForm.target_field}>Validate</Button><Button type="button" onClick={() => void saveRelationship()} disabled={loadingRelationships || !relationshipForm.label || !relationshipForm.source_field || !relationshipForm.target_field}>Approve & save</Button></div></div></div>}
      </section>
      <Sheet open={selectedTable !== null} onOpenChange={(open) => !open && setSelectedTable(null)}>
        <SheetContent className="schema-detail-sheet">
          {selectedTable && <><SheetHeader><SheetTitle>{selectedTable.schema_name}.{selectedTable.name}</SheetTitle><SheetDescription>{selectedTable.is_hypertable ? `Timescale hypertable · time column: ${selectedTable.time_column}` : 'PostgreSQL table'}{selectedTable.approximate_rows !== null ? ` · approximately ${selectedTable.approximate_rows.toLocaleString()} rows` : ''}</SheetDescription></SheetHeader><div className="schema-detail-body"><h3>Columns</h3><div className="column-list">{selectedTable.columns.map((column) => <div key={column.name}><code>{column.name}</code><span>{column.data_type}</span><small>{column.primary_key ? 'Primary key' : column.nullable ? 'Nullable' : 'Required'}</small></div>)}</div>{selectedTable.foreign_keys.length > 0 && <><h3>Relationships</h3><div className="relationship-list">{selectedTable.foreign_keys.map((key, index) => <code key={index}>{key.column_name} → {key.foreign_schema}.{key.foreign_table}.{key.foreign_column}</code>)}</div></>}{selectedTable.indexes.length > 0 && <><h3>Indexes</h3><div className="relationship-list">{selectedTable.indexes.map((index) => <code key={index.name}>{index.name}</code>)}</div></>}</div></>}
        </SheetContent>
      </Sheet>
    </section>
  );
}

function SavedInvestigationsView({ onSavedRunCount, navigationKey, onResume }: {
  onSavedRunCount: (count: number) => void;
  navigationKey: number;
  onResume: (investigation: SavedInvestigation) => void;
}) {
  const [investigations, setInvestigations] = useState<SavedInvestigation[]>([]);
  const [selected, setSelected] = useState<SavedInvestigation | null>(null);
  const [notice, setNotice] = useState('');

  const loadInvestigations = async () => {
    try {
      const response = await fetch(`${apiUrl}/api/investigations`);
      if (!response.ok) throw new Error(`List request failed with ${response.status}`);
      const saved = await response.json() as SavedInvestigation[];
      setInvestigations(saved);
      onSavedRunCount(saved.length);
      setSelected((current) => current ? saved.find((item) => item.investigation_id === current.investigation_id) ?? null : null);
    } catch (cause) {
      console.error('Could not load saved investigations', cause);
      setNotice('Saved investigations could not be loaded. Check the backend logs and try again.');
    }
  };

  useEffect(() => {
    void loadInvestigations();
    const refreshTimer = window.setInterval(() => { void loadInvestigations(); }, 5000);
    return () => window.clearInterval(refreshTimer);
  }, []);

  useEffect(() => {
    setSelected(null);
    setNotice('');
  }, [navigationKey]);

  const deleteOne = async (investigation: SavedInvestigation) => {
    if (!window.confirm(`Delete this saved investigation?\n\n${investigation.question}`)) return;
    try {
      const response = await fetch(`${apiUrl}/api/investigations/${investigation.investigation_id}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(`Delete request failed with ${response.status}`);
      setInvestigations((current) => current.filter((item) => item.investigation_id !== investigation.investigation_id));
      onSavedRunCount(Math.max(0, investigations.length - 1));
      setSelected((current) => current?.investigation_id === investigation.investigation_id ? null : current);
      setNotice('Saved investigation deleted.');
    } catch (cause) {
      console.error('Could not delete saved investigation', cause);
      setNotice('The saved investigation could not be deleted. A running investigation must finish first.');
    }
  };

  const deleteAll = async () => {
    if (!window.confirm('Delete every saved investigation? This cannot be undone.')) return;
    try {
      const response = await fetch(`${apiUrl}/api/investigations`, { method: 'DELETE' });
      if (!response.ok) throw new Error(`Delete-all request failed with ${response.status}`);
      setInvestigations([]); onSavedRunCount(0); setSelected(null); setNotice('All saved investigations were deleted.');
    } catch (cause) {
      console.error('Could not delete saved investigations', cause);
      setNotice('Saved investigations could not be deleted. A running investigation must finish first.');
    }
  };

  const originalConversationQuestion = (investigation: SavedInvestigation) => {
    if (investigation.original_question) return investigation.original_question;
    return investigation.question.split('\n\nEarlier answer context: ').at(-1) || investigation.question;
  };
  const conversationTitle = selected ? originalConversationQuestion(selected) : '';
  const isResumable = (investigation: SavedInvestigation) => (
    investigation.status === 'waiting_for_human'
    || ['completed', 'insufficient_evidence', 'failed'].includes(investigation.status)
  );
  const canResume = selected ? isResumable(selected) : false;
  const conversationTimeline = selected ? (() => {
    const timeline: Array<
      | { kind: 'question'; index: number; turn: ConversationTurn }
      | { kind: 'answer'; index: number; turn: ConversationTurn }
      | { kind: 'feedback'; index: number; feedback: SavedInvestigation['human_feedback'][number] }
      | { kind: 'current_question'; question: string }
      | { kind: 'pending'; question: string }
    > = [];
    const feedback = [...selected.human_feedback]
      .map((item, index) => ({ item, index, at: new Date(item.received_at).getTime() }))
      .sort((left, right) => left.at - right.at);
    let feedbackIndex = 0;
    [...selected.conversation_turns]
      .sort((left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime())
      .forEach((turn, index) => {
        timeline.push({ kind: 'question', index, turn });
        const answerTime = new Date(turn.created_at).getTime();
        while (feedbackIndex < feedback.length && feedback[feedbackIndex].at <= answerTime) {
          const item = feedback[feedbackIndex];
          timeline.push({ kind: 'feedback', index: item.index, feedback: item.item });
          feedbackIndex += 1;
        }
        timeline.push({ kind: 'answer', index, turn });
      });
    while (feedbackIndex < feedback.length) {
      const item = feedback[feedbackIndex];
      timeline.push({ kind: 'feedback', index: item.index, feedback: item.item });
      feedbackIndex += 1;
    }
    const currentQuestion = selected.current_conversation_question ?? selected.original_question ?? selected.question;
    const currentQuestionAlreadyRecorded = selected.conversation_turns.some(
      (turn) => turn.question.trim() === currentQuestion.trim(),
    );
    if (selected.status === 'waiting_for_human' && !currentQuestionAlreadyRecorded) {
      timeline.push({ kind: 'current_question', question: currentQuestion });
    }
    if (selected.pending_human_question) timeline.push({ kind: 'pending', question: selected.pending_human_question });
    return timeline;
  })() : [];

  if (selected) return <section className="standard-page saved-conversation-screen">
    <div className="conversation-topbar"><Button variant="outline" onClick={() => setSelected(null)}><ArrowLeft /> Back to saved items</Button></div>
    <article className="saved-conversation">
      <div className="saved-detail-header"><div><span className="section-kicker">SAVED CONVERSATION</span><h1>{conversationTitle}</h1><p className="opened-conversation-id">Conversation ID <code>{selected.investigation_id}</code></p></div><div className="saved-conversation-actions">{canResume && <Button variant="outline" onClick={() => onResume(selected)}><Play /> Resume conversation</Button>}<Button variant="destructive" size="sm" onClick={() => void deleteOne(selected)}><Trash2 /> Delete</Button></div></div>
      {notice && <output className="notice">{notice}</output>}
      {selected.status === 'running' && <div className="thinking-row"><span className="thinking-icon"><RefreshCw className="spin" /></span><div><strong>Conversation is continuing…</strong><span>The next answer will appear here automatically.</span></div></div>}
      {selected.conversation_turns.length > 0 && <section><h3>Conversation</h3>{conversationTimeline.map((item) => item.kind === 'question'
        ? <article className="conversation-turn conversation-question" key={`question-${item.turn.created_at}-${item.index}`}><small>QUESTION</small><p>{item.turn.question}</p></article>
        : item.kind === 'current_question'
        ? <article className="conversation-turn conversation-question conversation-question-pending" key="current-question"><small>QUESTION</small><p>{item.question}</p></article>
        : item.kind === 'answer'
        ? <article className="conversation-turn conversation-answer" key={`answer-${item.turn.created_at}-${item.index}`}><small>ANSWER</small><h2>{item.turn.answer.likely_root_cause ?? 'Direct answer'}</h2><ReportText>{item.turn.answer.summary}</ReportText><ReportData evidence={item.turn.answer.evidence} />{item.turn.answer.caveats.map((caveat) => <p className="saved-caveat" key={caveat}>{caveat}</p>)}</article>
        : item.kind === 'pending'
        ? <article className="saved-detail-item conversation-feedback conversation-pending" key="pending-question"><code>Q{selected.human_feedback.length + 1}</code><div><small>AWAITING YOUR ANSWER</small><p>{item.question}</p><Button size="sm" onClick={() => onResume(selected)}><Play /> Continue in workbench</Button></div></article>
        : <article className="saved-detail-item conversation-feedback" key={`feedback-${item.feedback.received_at}-${item.index}`}><code>Q{item.index + 1}</code><div><small>FOLLOW-UP REQUEST</small><p>{item.feedback.question}</p><small>YOUR ANSWER</small><p className="saved-answer">{item.feedback.response}</p></div></article>
      )}</section>}
      {selected.conversation_turns.length === 0 && <><section><h3>Conversation</h3><article className="conversation-turn conversation-question conversation-question-pending"><small>QUESTION</small><p>{selected.current_conversation_question ?? selected.original_question ?? selected.question}</p></article>{selected.pending_human_question && <article className="saved-detail-item conversation-feedback conversation-pending"><code>Q{selected.human_feedback.length + 1}</code><div><small>AWAITING YOUR ANSWER</small><p>{selected.pending_human_question}</p><Button size="sm" onClick={() => onResume(selected)}><Play /> Continue in workbench</Button></div></article>}</section>{selected.human_feedback.length > 0 && <section><h3>Clarifications</h3>{selected.human_feedback.map((item, index) => <article className="saved-detail-item" key={`${item.question}-${index}`}><code>Q{index + 1}</code><div><p>{item.question}</p><small>YOUR ANSWER</small><p className="saved-answer">{item.response}</p></div></article>)}</section>}</>}
      <section><h3>Hypotheses</h3>{selected.hypotheses.map((hypothesis, index) => <article className="saved-detail-item" key={hypothesis.id}><code>H{index + 1}</code><div><strong>{hypothesis.name}</strong><span>{hypothesis.status} · {Math.round(hypothesis.confidence * 100)}%</span><p>{hypothesis.description}</p></div></article>)}</section>
      <section><h3>Evidence</h3>{selected.evidence.map((item, index) => <article className="saved-detail-item" key={item.id}><code>E{index + 1}</code><div><span>{item.relationship} · {Math.round(item.confidence * 100)}%</span><p>{item.description}</p></div></article>)}{selected.evidence.length === 0 && <p className="saved-empty">No separate evidence was saved for this conversation.</p>}</section>
      {selected.conversation_turns.length === 0 && selected.final_analysis && <section className="saved-conclusion"><h3>Conclusion</h3><h4>{selected.final_analysis.likely_root_cause ?? 'Insufficient evidence'}</h4><ReportText>{selected.final_analysis.summary}</ReportText><ReportData evidence={selected.final_analysis.evidence} /><span>{Math.round(selected.final_analysis.confidence * 100)}% confidence</span>{selected.final_analysis.caveats.map((caveat) => <p className="saved-caveat" key={caveat}>{caveat}</p>)}</section>}
    </article>
  </section>;

  return <section className="standard-page saved-investigations-page">
    <div className="page-heading">
      <div><span className="section-kicker">LOCAL ARCHIVE</span><h1>Saved investigations</h1><p>Questions, clarifications, hypotheses, evidence, and conclusions are saved locally.</p></div>
      <div className="archive-actions"><Button variant="outline" onClick={() => void loadInvestigations()}><RefreshCw /> Refresh</Button><Button variant="destructive" onClick={() => void deleteAll()} disabled={investigations.length === 0}><Trash2 /> Delete all</Button></div>
    </div>
    {notice && <output className="notice">{notice}</output>}
    {investigations.length === 0 && <div className="empty-sources"><Archive /><strong>No saved investigations</strong><span>Completed and paused work will appear here.</span></div>}
    <div className="saved-run-list">
      {investigations.map((investigation) => <article key={investigation.investigation_id} className="saved-run">
        <div><h2>{originalConversationQuestion(investigation)}</h2><span className="saved-run-status">{investigation.status.replaceAll('_', ' ')}</span><small>{new Date(investigation.started_at).toLocaleString()}</small><code className="saved-list-uid">UID: {investigation.investigation_id}</code><p>{investigation.hypotheses.length} hypotheses · {investigation.evidence.length} evidence items · {investigation.human_feedback.length} follow-ups</p></div>
        <div className="saved-run-actions"><Button variant="outline" onClick={() => setSelected(investigation)}>View</Button>{isResumable(investigation) && <Button variant="outline" onClick={() => onResume(investigation)}><Play /> Resume</Button>}<Button variant="destructive" size="icon" onClick={() => void deleteOne(investigation)} aria-label={`Delete investigation: ${investigation.question}`}><Trash2 /></Button></div>
      </article>)}
    </div>
  </section>;
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
