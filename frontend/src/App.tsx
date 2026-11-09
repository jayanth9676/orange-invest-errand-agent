import React, { useState, useEffect, useRef } from 'react';
import {
  AgentState,
  StateSnapshot,
  ChatMessage,
  ApprovalProposal,
  IntentSlots,
  TraceEvent,
} from './types';
import { api, ChatOptions } from './services/api';
import { Header } from './components/Header';
import { ChatPane } from './components/chat/ChatPane';
import { ObservabilityDeck } from './components/observability/ObservabilityDeck';
import { EvalsModal } from './components/EvalsModal';
import { ErrorBoundary } from './components/common/ErrorBoundary';

export const App: React.FC = () => {
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');
  const [tenantId] = useState<string>('tenant_default');
  const [sessionId] = useState<string>('live_demo_session');
  const [sseConnected, setSseConnected] = useState<boolean>(false);

  const [stateSnapshot, setStateSnapshot] = useState<StateSnapshot | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'msg_welcome',
      role: 'assistant',
      content:
        'Hello! I am your Enterprise Errand Agent. I help you arrange home-service errands (plumbing, electrical, AC servicing) with deterministic zero-trust approval gates, strict token budgeting, and resilient timeout reconciliation.\n\nTry sending an errand request or click one of the preset prompts below!',
      timestamp: new Date(),
    },
  ]);

  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [liveThought, setLiveThought] = useState<string>('');
  const [liveTool, setLiveTool] = useState<string>('');
  const [chaosParams, setChaosParams] = useState<ChatOptions>({});

  // Evals modal state
  const [evalModalOpen, setEvalModalOpen] = useState<boolean>(false);
  const [isEvaluating, setIsEvaluating] = useState<boolean>(false);
  const [evalResults, setEvalResults] = useState<any>(null);

  // Mobile layout switch
  const [mobileTab, setMobileTab] = useState<'chat' | 'deck'>('chat');

  // Toggle Dark/Light Theme
  const handleToggleTheme = () => {
    const nextTheme = theme === 'dark' ? 'light' : 'dark';
    setTheme(nextTheme);
    if (nextTheme === 'dark') {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  };

  // Initial state fetch
  const refreshState = async () => {
    try {
      const data = await api.getState(tenantId, sessionId);
      setStateSnapshot(data);
    } catch (err) {
      console.error('Error fetching state snapshot:', err);
    }
  };

  useEffect(() => {
    refreshState();
  }, [tenantId, sessionId]);

  // Server-Sent Events (SSE) Stream
  useEffect(() => {
    let eventSource: EventSource | null = null;
    try {
      eventSource = new EventSource(`/events?tenant_id=${tenantId}&session_id=${sessionId}`);
      eventSource.onopen = () => setSseConnected(true);
      eventSource.onmessage = (event) => {
        try {
          const parsed = JSON.parse(event.data);
          if (!parsed) return;

          // Track live thoughts for real-time CoT display
          if (parsed.event_type === 'agent_thought' && parsed.thought) {
            setLiveThought(parsed.thought);
          } else if (parsed.thought && typeof parsed.thought === 'string') {
            setLiveThought(parsed.thought);
          }

          // Track live tool executions for real-time chips
          if (parsed.event_type === 'tool_call' && parsed.tool_name) {
            setLiveTool(parsed.tool_name);
          }

          const nextState: AgentState | undefined =
            parsed.event_type === 'state_transition' && parsed.to_state
              ? (parsed.to_state as AgentState)
              : parsed.fsm_state
              ? (parsed.fsm_state as AgentState)
              : undefined;

          const nextProposal: ApprovalProposal | null | undefined =
            parsed.active_proposal !== undefined
              ? (parsed.active_proposal as ApprovalProposal | null)
              : undefined;

          setStateSnapshot((prev) => {
            if (!prev) {
              if (parsed.slots && parsed.fsm_state) {
                return parsed as StateSnapshot;
              }
              return prev;
            }

            let updatedEvents = prev.trace_events || [];
            if (parsed.event_type && parsed.event_type !== 'state_sync') {
              const exists = updatedEvents.some(
                (e) =>
                  (e && e.span_id && e.span_id === parsed.span_id) ||
                  (e &&
                    e.trace_id === parsed.trace_id &&
                    e.timestamp === parsed.timestamp &&
                    e.event_type === parsed.event_type)
              );
              if (!exists) {
                updatedEvents = [...updatedEvents, parsed as TraceEvent];
              }
            }

            return {
              ...prev,
              ...(parsed.slots ? parsed : {}),
              fsm_state: nextState || prev.fsm_state,
              active_proposal: nextProposal !== undefined ? nextProposal : prev.active_proposal,
              trace_events: updatedEvents,
            };
          });
        } catch (e) {
          // heartbeat comment or non-json message
        }
      };
      eventSource.onerror = () => {
        setSseConnected(false);
      };
    } catch (err) {
      setSseConnected(false);
    }

    return () => {
      eventSource?.close();
    };
  }, [tenantId, sessionId]);

  // Send Message
  const handleSendMessage = async (msgText: string) => {
    const userMsg: ChatMessage = {
      id: `msg_user_${Date.now()}`,
      role: 'user',
      content: msgText,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setIsLoading(true);
    setLiveThought('');
    setLiveTool('');
    // Clear active proposal while new prompt is executing so cockpit does not linger prematurely
    setStateSnapshot((prev) => (prev ? { ...prev, active_proposal: null } : null));

    try {
      const res = await api.sendChat(msgText, chaosParams, tenantId, sessionId);
      setStateSnapshot(res);

      const assistantMsg: ChatMessage = {
        id: `msg_asst_${Date.now()}`,
        role: 'assistant',
        content: res.reply || 'Request processed.',
        timestamp: new Date(),
        reasoning: res.agent_reasoning,
        activeProposal: res.active_proposal,
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err: any) {
      const errorMsg: ChatMessage = {
        id: `msg_err_${Date.now()}`,
        role: 'assistant',
        content: `Error: ${err.message || 'Operation failed'}`,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsLoading(false);
      setLiveThought('');
      setLiveTool('');
      // Reset one-turn chaos params
      setChaosParams({});
    }
  };

  // 1-Click Approve Proposal
  const handleApproveProposal = async (proposalId: string) => {
    setIsLoading(true);
    try {
      const res = await api.executeApprovalAction(proposalId, 'approve', undefined, tenantId, sessionId);
      setStateSnapshot(res);
      const confirmMsg: ChatMessage = {
        id: `msg_approve_${Date.now()}`,
        role: 'assistant',
        content: res.reply || 'Proposal approved. Booking executed with write-ahead idempotency key.',
        timestamp: new Date(),
        reasoning: res.agent_reasoning,
      };
      setMessages((prev) => [...prev, confirmMsg]);
    } catch (err: any) {
      const errorMsg: ChatMessage = {
        id: `msg_err_${Date.now()}`,
        role: 'assistant',
        content: `Approval execution blocked: ${err.message}`,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsLoading(false);
    }
  };

  // 1-Click Reject Proposal
  const handleRejectProposal = async (proposalId: string) => {
    setIsLoading(true);
    try {
      const res = await api.executeApprovalAction(proposalId, 'reject', 'User rejected terms', tenantId, sessionId);
      setStateSnapshot(res);
      const rejectMsg: ChatMessage = {
        id: `msg_reject_${Date.now()}`,
        role: 'assistant',
        content: res.reply || 'Proposal was rejected. No mutation was performed.',
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, rejectMsg]);
    } catch (err: any) {
      alert(`Reject failed: ${err.message}`);
    } finally {
      setIsLoading(false);
    }
  };

  // Select Slot on Provider Card
  const handleSelectSlot = (provider: any, slot: string) => {
    handleSendMessage(`Hold and book ${provider.name} (${provider.provider_id}) on ${slot}`);
  };

  // Memory Actions
  const handleMemoryAction = async (action: 'inspect' | 'forget' | 'confirm' | 'update', key: string, val: string) => {
    setIsLoading(true);
    try {
      const res = await api.memoryAction(action, key, val, tenantId, sessionId);
      setStateSnapshot(res);
      const memReply: ChatMessage = {
        id: `msg_mem_${Date.now()}`,
        role: 'assistant',
        content: res.reply || `Memory ${action} completed for key '${key}'.`,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, memReply]);
    } catch (err: any) {
      alert(`Memory action failed: ${err.message}`);
    } finally {
      setIsLoading(false);
    }
  };

  // Proactive Cancellation Trigger
  const handleTriggerProactiveCancel = async (bookingId: string) => {
    setIsLoading(true);
    try {
      const res = await api.triggerProactiveCancel(bookingId, tenantId, sessionId);
      setStateSnapshot(res);
      const proactiveMsg: ChatMessage = {
        id: `msg_proactive_${Date.now()}`,
        role: 'assistant',
        content: res.reply || `Proactive cancellation event handled for booking ${bookingId}. Replacement proposed.`,
        timestamp: new Date(),
        reasoning: res.agent_reasoning,
        activeProposal: res.active_proposal,
      };
      setMessages((prev) => [...prev, proactiveMsg]);
    } catch (err: any) {
      alert(`Proactive cancellation failed: ${err.message}`);
    } finally {
      setIsLoading(false);
    }
  };

  // Reset Session
  const handleResetSession = async () => {
    if (!confirm('Reinitialize the session and re-seed the SQLite database?')) return;
    try {
      await api.resetSession(tenantId, sessionId);
      setMessages([
        {
          id: `msg_welcome_${Date.now()}`,
          role: 'assistant',
          content: 'Session state machine cleanly reset and re-seeded with 56 memories.',
          timestamp: new Date(),
        },
      ]);
      await refreshState();
    } catch (err: any) {
      alert(`Reset failed: ${err.message}`);
    }
  };

  // Run Evals
  const handleRunEvals = async () => {
    setIsEvaluating(true);
    setEvalModalOpen(true);
    setEvalResults(null);
    try {
      const res = await api.runEvals();
      setEvalResults(res);
    } catch (err: any) {
      alert(`Eval run failed: ${err.message}`);
    } finally {
      setIsEvaluating(false);
    }
  };

  // Export Trace
  const handleExportTrace = () => {
    window.open('/api/export-trace', '_blank');
  };

  // Guided Demo Tour
  const handleGuidedDemo = async () => {
    await handleSendMessage('Find a plumber for my kitchen tap this week, mornings only, under ₹1,500');
  };

  const currentFsmState = stateSnapshot?.fsm_state || 'IDLE';
  const slots = stateSnapshot?.slots || {
    service: null,
    area: null,
    date_window: null,
    time_window: null,
    max_budget: null,
    requested_slot: null,
    target_booking_id: null,
    preferred_provider_id: null,
    preferences: {},
  };
  const missingFields = stateSnapshot?.missing_fields || [];
  const turnCounter = stateSnapshot?.turn_counter || 0;
  const activeProposal = stateSnapshot?.active_proposal || null;
  const traceEvents = stateSnapshot?.trace_events || [];
  const traceId = stateSnapshot?.trace_id || '';
  const otelTraceparent = stateSnapshot?.otel_traceparent || '';
  const allMemories = stateSnapshot?.all_memories || [];
  const partitions = stateSnapshot?.memory_partitions || {
    explicit_count: 0,
    confirmed_inferred_count: 0,
    unconfirmed_inferred_count: 0,
    forgotten_count: 0,
    total_count: 0,
  };
  const contextAudit = stateSnapshot?.latest_context_audit || null;

  return (
    <div className="h-screen w-screen flex flex-col overflow-hidden bg-zinc-950 text-zinc-100">
      {/* Top Application Header */}
      <Header
        fsmState={currentFsmState}
        sseConnected={sseConnected}
        theme={theme}
        onToggleTheme={handleToggleTheme}
        onGuidedDemo={handleGuidedDemo}
        onRunEvals={handleRunEvals}
        onExportTrace={handleExportTrace}
        onResetSession={handleResetSession}
        isEvaluating={isEvaluating}
        tenantId={tenantId}
        sessionId={sessionId}
      />

      {/* Mobile Tab Selector (< 1024px) */}
      <div className="lg:hidden flex items-center border-b border-zinc-800 bg-zinc-900 px-3 py-1.5 gap-2 shrink-0">
        <button
          onClick={() => setMobileTab('chat')}
          className={`flex-1 py-1.5 text-xs font-semibold rounded-lg transition ${
            mobileTab === 'chat' ? 'bg-sky-600 text-white' : 'bg-zinc-800 text-zinc-400'
          }`}
        >
          💬 Agent Conversation
        </button>
        <button
          onClick={() => setMobileTab('deck')}
          className={`flex-1 py-1.5 text-xs font-semibold rounded-lg transition ${
            mobileTab === 'deck' ? 'bg-sky-600 text-white' : 'bg-zinc-800 text-zinc-400'
          }`}
        >
          📊 Observability Deck
        </button>
      </div>

      {/* Main Split-Screen Workspace */}
      <main className="flex-1 flex overflow-hidden min-h-0 bg-zinc-950">
        {/* Left Pane: Chat & Operations */}
        <div
          className={`w-full lg:w-[50%] xl:w-[52%] flex flex-col h-full min-h-0 ${
            mobileTab === 'deck' ? 'hidden lg:flex' : 'flex'
          }`}
        >
          <ErrorBoundary fallbackTitle="Chat Pane Error Recovery">
            <ChatPane
              messages={messages}
              slots={slots}
              missingFields={missingFields}
              turnCounter={turnCounter}
              activeProposal={isLoading ? null : activeProposal}
              onSendMessage={handleSendMessage}
              onApproveProposal={handleApproveProposal}
              onRejectProposal={handleRejectProposal}
              onSelectSlot={handleSelectSlot}
              isLoading={isLoading}
              liveThought={liveThought}
              liveTool={liveTool}
            />
          </ErrorBoundary>
        </div>

        {/* Right Pane: Observability & Control Deck */}
        <div
          className={`w-full lg:w-[50%] xl:w-[48%] flex flex-col h-full min-h-0 ${
            mobileTab === 'chat' ? 'hidden lg:flex' : 'flex'
          }`}
        >
          <ErrorBoundary fallbackTitle="Observability Deck Error Recovery">
            <ObservabilityDeck
              events={traceEvents}
              traceId={traceId}
              traceparent={otelTraceparent}
              currentState={currentFsmState}
              memories={allMemories}
              partitions={partitions}
              contextAudit={contextAudit}
              onMemoryAction={handleMemoryAction}
              onTriggerProactiveCancel={handleTriggerProactiveCancel}
              onSetChaosParams={(p) => setChaosParams((prev) => ({ ...prev, ...p }))}
              isLoading={isLoading}
            />
          </ErrorBoundary>
        </div>
      </main>

      {/* Evaluation Scenarios Results Modal */}
      <EvalsModal
        isOpen={evalModalOpen}
        onClose={() => setEvalModalOpen(false)}
        results={evalResults}
        isLoading={isEvaluating}
      />
    </div>
  );
};
export default App;
