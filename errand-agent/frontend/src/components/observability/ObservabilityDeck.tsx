import React, { useState } from 'react';
import {
  TraceEvent,
  AgentState,
  MemoryItem,
  MemoryPartitions,
  ContextAudit,
} from '../../types';
import { TraceWaterfall } from './TraceWaterfall';
import { FsmGraph } from './FsmGraph';
import { MemoryCockpit } from './MemoryCockpit';
import { ChaosSandbox } from './ChaosSandbox';
import { McpStudio } from './McpStudio';
import {
  Activity,
  GitBranch,
  Brain,
  Zap,
  Plug,
} from 'lucide-react';

interface ObservabilityDeckProps {
  events: TraceEvent[];
  traceId: string;
  traceparent: string;
  currentState: AgentState;
  memories: MemoryItem[];
  partitions: MemoryPartitions;
  contextAudit: ContextAudit | null;
  onMemoryAction: (action: 'inspect' | 'forget' | 'confirm' | 'update', key: string, val: string) => void;
  onTriggerProactiveCancel: (bookingId: string) => void;
  onSetChaosParams: (params: any) => void;
  isLoading: boolean;
}

export const ObservabilityDeck: React.FC<ObservabilityDeckProps> = ({
  events,
  traceId,
  traceparent,
  currentState,
  memories,
  partitions,
  contextAudit,
  onMemoryAction,
  onTriggerProactiveCancel,
  onSetChaosParams,
  isLoading,
}) => {
  const [activeTab, setActiveTab] = useState<'trace' | 'fsm' | 'memory' | 'chaos' | 'mcp'>('trace');

  const tabs = [
    { id: 'trace', label: `Trace (${events.length})`, icon: <Activity className="w-3.5 h-3.5" /> },
    { id: 'fsm', label: 'FSM Graph', icon: <GitBranch className="w-3.5 h-3.5" /> },
    { id: 'memory', label: `Memory (${memories.length})`, icon: <Brain className="w-3.5 h-3.5" /> },
    { id: 'chaos', label: 'Chaos Sandbox', icon: <Zap className="w-3.5 h-3.5" /> },
    { id: 'mcp', label: 'MCP Tools (8)', icon: <Plug className="w-3.5 h-3.5" /> },
  ];

  return (
    <section className="flex-1 flex flex-col h-full min-h-0 bg-zinc-950/80 border-l border-zinc-800/80">
      {/* Tab Navigation Header */}
      <div className="flex items-center px-2 py-1 border-b border-zinc-800/80 bg-zinc-900/60 overflow-x-auto no-scrollbar shrink-0 gap-1 select-none">
        {tabs.map((t) => {
          const isActive = activeTab === t.id;
          return (
            <button
              key={t.id}
              onClick={() => setActiveTab(t.id as any)}
              className={`px-2.5 py-1.5 text-xs font-semibold rounded-lg transition-all flex items-center gap-1.5 cursor-pointer shrink-0 ${
                isActive
                  ? 'bg-sky-950/60 text-sky-300 border border-sky-800/80 shadow-sm'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-850 border border-transparent'
              }`}
            >
              <span className={isActive ? 'text-sky-400' : 'text-zinc-500'}>{t.icon}</span>
              <span>{t.label}</span>
            </button>
          );
        })}
      </div>

      {/* Tab Content Panes */}
      <div className="flex-1 overflow-hidden min-h-0">
        {activeTab === 'trace' && (
          <TraceWaterfall events={events} traceId={traceId} traceparent={traceparent} />
        )}
        {activeTab === 'fsm' && <FsmGraph currentState={currentState} events={events} />}
        {activeTab === 'memory' && (
          <MemoryCockpit
            memories={memories}
            partitions={partitions}
            contextAudit={contextAudit}
            onMemoryAction={onMemoryAction}
            isLoading={isLoading}
          />
        )}
        {activeTab === 'chaos' && (
          <ChaosSandbox
            onTriggerProactiveCancel={onTriggerProactiveCancel}
            onSetChaosParams={onSetChaosParams}
            isLoading={isLoading}
          />
        )}
        {activeTab === 'mcp' && <McpStudio />}
      </div>
    </section>
  );
};
