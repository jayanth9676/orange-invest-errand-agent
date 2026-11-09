import React, { useState, useMemo } from 'react';
import { TraceEvent } from '../../types';
import {
  GitCommit,
  Clock,
  Cpu,
  Wrench,
  ShieldCheck,
  AlertCircle,
  Copy,
  Check,
  Filter,
} from 'lucide-react';

interface TraceWaterfallProps {
  events: TraceEvent[];
  traceId: string;
  traceparent: string;
}

export const TraceWaterfall: React.FC<TraceWaterfallProps> = ({
  events,
  traceId,
  traceparent,
}) => {
  const [copied, setCopied] = useState<boolean>(false);
  const [selectedSpan, setSelectedSpan] = useState<TraceEvent | null>(null);
  const [filterType, setFilterType] = useState<'all' | 'tools' | 'fsm' | 'errors'>('all');

  const handleCopy = () => {
    navigator.clipboard.writeText(traceparent);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // Ensure events array is defined and filtered for non-null items
  const safeEvents = useMemo(() => (events || []).filter(Boolean), [events]);

  // Calculate token usage from model call events
  const modelCalls = safeEvents.filter((e) => e && e.event_type === 'model_call');
  const totalPromptTokens = modelCalls.reduce((acc, e) => acc + (e?.prompt_tokens || 0), 0);
  const totalCompletionTokens = modelCalls.reduce((acc, e) => acc + (e?.completion_tokens || 0), 0);
  const totalTokens = totalPromptTokens + totalCompletionTokens;
  const tokenCap = 1200;
  const tokenPercent = Math.min(100, Math.round((totalTokens / tokenCap) * 100));

  // Max duration for relative flamegraph bar
  const maxDuration = useMemo(() => {
    const durations = safeEvents.map((e) => Number(e.duration_ms ?? e.latency_ms ?? 1));
    return Math.max(...durations, 10);
  }, [safeEvents]);

  const isErrorEvent = (e: TraceEvent) =>
    e.status === 'error' ||
    e.event_type === 'error' ||
    e.status === 'booking_timeout' ||
    (Boolean(e.status) && e.status !== 'ok' && e.status !== 'success');

  // Filtered events
  const filteredEvents = useMemo(() => {
    if (filterType === 'tools') {
      return safeEvents.filter((e) => e.event_type === 'tool_call' || Boolean(e.tool_name));
    }
    if (filterType === 'fsm') {
      return safeEvents.filter((e) => e.event_type === 'state_transition');
    }
    if (filterType === 'errors') {
      return safeEvents.filter(isErrorEvent);
    }
    return safeEvents;
  }, [safeEvents, filterType]);

  const toolCount = safeEvents.filter((e) => e.event_type === 'tool_call' || Boolean(e.tool_name)).length;
  const fsmCount = safeEvents.filter((e) => e.event_type === 'state_transition').length;
  const errorCount = safeEvents.filter(isErrorEvent).length;

  const getEventIcon = (type: string, status?: string) => {
    if (status === 'error') return <AlertCircle className="w-3.5 h-3.5 text-rose-400 shrink-0" />;
    switch (type) {
      case 'model_call':
        return <Cpu className="w-3.5 h-3.5 text-sky-400 shrink-0" />;
      case 'tool_call':
        return <Wrench className="w-3.5 h-3.5 text-amber-400 shrink-0" />;
      case 'state_transition':
        return <GitCommit className="w-3.5 h-3.5 text-purple-400 shrink-0" />;
      case 'guardrail_event':
        return <ShieldCheck className="w-3.5 h-3.5 text-emerald-400 shrink-0" />;
      default:
        return <GitCommit className="w-3.5 h-3.5 text-zinc-400 shrink-0" />;
    }
  };

  return (
    <div className="p-4 space-y-3.5 overflow-y-auto h-full min-h-0">
      {/* W3C Traceparent Header Card */}
      <div className="p-3 rounded-xl bg-zinc-900/80 border border-zinc-800 space-y-2 shadow-sm">
        <div className="flex items-center justify-between text-xs">
          <span className="font-semibold text-zinc-200 flex items-center gap-1.5 font-mono">
            <GitCommit className="w-4 h-4 text-sky-400" />
            <span>OpenTelemetry W3C Distributed Trace</span>
          </span>
          <span className="font-mono text-[11px] text-sky-300 bg-zinc-950 px-2 py-0.5 rounded border border-zinc-800">
            {traceId ? traceId.slice(0, 16) + '...' : '--'}
          </span>
        </div>

        <div className="flex items-center justify-between gap-2 bg-zinc-950 p-2 rounded-lg border border-zinc-800 text-[11px] font-mono text-zinc-400">
          <span className="truncate select-all">{traceparent || 'traceparent: 00-pending-01'}</span>
          <button
            onClick={handleCopy}
            className="p-1 rounded hover:bg-zinc-800 text-zinc-400 hover:text-zinc-200 transition shrink-0 cursor-pointer"
            title="Copy traceparent"
          >
            {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
          </button>
        </div>
      </div>

      {/* Token Budget Gauge */}
      <div className="p-3 rounded-xl bg-zinc-900/60 border border-zinc-800 space-y-2 shadow-sm">
        <div className="flex items-center justify-between text-xs">
          <span className="font-medium text-zinc-300 flex items-center gap-1.5 font-mono">
            <Cpu className="w-3.5 h-3.5 text-indigo-400" />
            <span>Context Token Budget (tiktoken cl100k_base)</span>
          </span>
          <span className="font-mono text-zinc-200 font-bold">
            {totalTokens} / {tokenCap} ({tokenPercent}%)
          </span>
        </div>

        {/* Progress Bar */}
        <div className="w-full bg-zinc-950 h-2 rounded-full overflow-hidden border border-zinc-800 flex">
          <div
            className="bg-indigo-500 h-full transition-all duration-500"
            style={{ width: `${(totalPromptTokens / tokenCap) * 100}%` }}
            title={`Prompt Tokens: ${totalPromptTokens}`}
          />
          <div
            className="bg-sky-400 h-full transition-all duration-500"
            style={{ width: `${(totalCompletionTokens / tokenCap) * 100}%` }}
            title={`Completion Tokens: ${totalCompletionTokens}`}
          />
        </div>

        <div className="flex items-center justify-between text-[10px] text-zinc-400 font-mono">
          <span>Prompt: {totalPromptTokens}</span>
          <span>Completion: {totalCompletionTokens}</span>
          <span>Cap: {tokenCap} tokens</span>
        </div>
      </div>

      {/* Execution Span Flamegraph */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs font-semibold text-zinc-300">
          <span className="font-mono">Execution Flamegraph ({safeEvents.length} events)</span>

          {/* Quick Filter Bar */}
          <div className="flex items-center gap-1 text-[10px] font-mono">
            <button
              onClick={() => setFilterType('all')}
              className={`px-2 py-0.5 rounded-md transition cursor-pointer ${
                filterType === 'all'
                  ? 'bg-sky-950 text-sky-300 border border-sky-800'
                  : 'bg-zinc-900 text-zinc-400 hover:text-zinc-200 border border-zinc-800'
              }`}
            >
              All ({safeEvents.length})
            </button>
            <button
              onClick={() => setFilterType('tools')}
              className={`px-2 py-0.5 rounded-md transition cursor-pointer ${
                filterType === 'tools'
                  ? 'bg-amber-950 text-amber-300 border border-amber-800'
                  : 'bg-zinc-900 text-zinc-400 hover:text-zinc-200 border border-zinc-800'
              }`}
            >
              Tools ({toolCount})
            </button>
            <button
              onClick={() => setFilterType('fsm')}
              className={`px-2 py-0.5 rounded-md transition cursor-pointer ${
                filterType === 'fsm'
                  ? 'bg-purple-950 text-purple-300 border border-purple-800'
                  : 'bg-zinc-900 text-zinc-400 hover:text-zinc-200 border border-zinc-800'
              }`}
            >
              FSM ({fsmCount})
            </button>
            {errorCount > 0 && (
              <button
                onClick={() => setFilterType('errors')}
                className={`px-2 py-0.5 rounded-md transition cursor-pointer ${
                  filterType === 'errors'
                    ? 'bg-rose-950 text-rose-300 border border-rose-800'
                    : 'bg-zinc-900 text-rose-400 hover:text-rose-300 border border-zinc-800'
                }`}
              >
                Errors ({errorCount})
              </button>
            )}
          </div>
        </div>

        {filteredEvents.length === 0 ? (
          <div className="p-6 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
            No trace spans matching filter '{filterType}'.
          </div>
        ) : (
          <div className="space-y-1.5">
            {filteredEvents.slice(-30).map((ev, idx) => {
              const duration = Number(ev.duration_ms ?? ev.latency_ms ?? 0);
              const barWidth = Math.max(4, Math.min(100, (duration / maxDuration) * 100));
              const isError = isErrorEvent(ev);

              return (
                <div
                  key={idx}
                  onClick={() => setSelectedSpan(ev)}
                  className={`relative overflow-hidden p-2.5 rounded-lg border text-xs cursor-pointer transition flex items-center justify-between gap-2 shadow-sm ${
                    isError
                      ? 'bg-rose-950/20 border-rose-900/60 hover:border-rose-600'
                      : 'bg-zinc-900/80 border-zinc-800/80 hover:border-zinc-700 hover:bg-zinc-850'
                  }`}
                >
                  {/* Visual Duration Flame Bar in Background */}
                  <div
                    className={`absolute inset-y-0 left-0 opacity-15 pointer-events-none transition-all duration-300 ${
                      isError
                        ? 'bg-rose-500'
                        : ev.event_type === 'tool_call'
                        ? 'bg-amber-400'
                        : ev.event_type === 'state_transition'
                        ? 'bg-purple-500'
                        : 'bg-sky-400'
                    }`}
                    style={{ width: `${barWidth}%` }}
                  />

                  {/* Left: Icon & Name */}
                  <div className="relative flex items-center gap-2 min-w-0">
                    {getEventIcon(ev.event_type, ev.status)}
                    <div className="min-w-0 truncate font-mono">
                      <span className={`font-semibold ${isError ? 'text-rose-300' : 'text-zinc-200'}`}>
                        {ev.tool_name || ev.model || ev.event_type}
                      </span>
                      {ev.from_state && ev.to_state && (
                        <span className="text-[11px] text-zinc-400 ml-1.5">
                          {ev.from_state} → {ev.to_state}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Right: Duration & Status Badge */}
                  <div className="relative flex items-center gap-2 shrink-0 font-mono text-[11px]">
                    {duration > 0 && (
                      <span className="text-zinc-400 flex items-center gap-1">
                        <Clock className="w-2.5 h-2.5 text-zinc-500" />
                        <span>{Math.round(duration)}ms</span>
                      </span>
                    )}
                    <span
                      className={`px-1.5 py-0.2 rounded text-[10px] uppercase font-bold ${
                        isError
                          ? 'bg-rose-950 text-rose-300 border border-rose-800'
                          : 'bg-zinc-950 text-emerald-400 border border-emerald-900/40'
                      }`}
                    >
                      {ev.status || 'ok'}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Selected Span Inspect Modal */}
      {selectedSpan && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-xl w-full max-h-[80vh] flex flex-col overflow-hidden shadow-2xl">
            <div className="p-4 border-b border-zinc-800 flex items-center justify-between">
              <span className="font-semibold text-sm text-zinc-100 flex items-center gap-2 font-mono">
                {getEventIcon(selectedSpan.event_type, selectedSpan.status)}
                <span>Span Inspector: {selectedSpan.tool_name || selectedSpan.event_type}</span>
              </span>
              <button
                onClick={() => setSelectedSpan(null)}
                className="text-zinc-400 hover:text-white text-xs px-2.5 py-1 rounded-lg bg-zinc-800 hover:bg-zinc-700 transition cursor-pointer"
              >
                Close
              </button>
            </div>
            <div className="p-4 overflow-y-auto flex-1 font-mono text-xs text-zinc-300 bg-zinc-950">
              <pre className="whitespace-pre-wrap">{JSON.stringify(selectedSpan, null, 2)}</pre>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
