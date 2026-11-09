import React from 'react';
import { AgentState, TraceEvent } from '../../types';
import { GitBranch, Activity, CheckCircle, AlertTriangle } from 'lucide-react';

interface FsmGraphProps {
  currentState: AgentState;
  events: TraceEvent[];
}

export const FsmGraph: React.FC<FsmGraphProps> = ({ currentState, events }) => {
  const transitions = (events || []).filter((e) => e && e.event_type === 'state_transition');

  const nodes = [
    { id: 'IDLE', label: 'IDLE', x: 70, y: 50, color: '#38bdf8' },
    { id: 'COLLECTING_SLOTS', label: 'COLLECTING_SLOTS', x: 250, y: 50, color: '#0ea5e9' },
    { id: 'SEARCHING_PROVIDERS', label: 'SEARCHING_PROVIDERS', x: 250, y: 130, color: '#6366f1' },
    { id: 'HOLDING_SLOT', label: 'HOLDING_SLOT', x: 250, y: 210, color: '#eab308' },
    { id: 'AWAITING_APPROVAL', label: 'AWAITING_APPROVAL', x: 250, y: 290, color: '#f59e0b' },
    { id: 'EXECUTING_MUTATION', label: 'EXECUTING_MUTATION', x: 250, y: 370, color: '#ec4899' },
    { id: 'RECONCILING_TIMEOUT', label: 'RECONCILING_TIMEOUT', x: 80, y: 370, color: '#a855f7' },
    { id: 'PROACTIVE_RECOVERY', label: 'PROACTIVE_RECOVERY', x: 80, y: 210, color: '#14b8a6' },
    { id: 'COMPLETED', label: 'COMPLETED', x: 250, y: 450, color: '#10b981' },
    { id: 'SAFE_ABORT', label: 'SAFE_ABORT', x: 420, y: 250, color: '#ef4444' },
  ];

  const edges = [
    { from: 'IDLE', to: 'COLLECTING_SLOTS' },
    { from: 'COLLECTING_SLOTS', to: 'SEARCHING_PROVIDERS' },
    { from: 'SEARCHING_PROVIDERS', to: 'HOLDING_SLOT' },
    { from: 'HOLDING_SLOT', to: 'AWAITING_APPROVAL' },
    { from: 'AWAITING_APPROVAL', to: 'EXECUTING_MUTATION' },
    { from: 'EXECUTING_MUTATION', to: 'COMPLETED' },
    { from: 'EXECUTING_MUTATION', to: 'RECONCILING_TIMEOUT' },
    { from: 'RECONCILING_TIMEOUT', to: 'COMPLETED' },
    { from: 'PROACTIVE_RECOVERY', to: 'HOLDING_SLOT' },
    { from: 'AWAITING_APPROVAL', to: 'SAFE_ABORT' },
    { from: 'COLLECTING_SLOTS', to: 'SAFE_ABORT' },
    { from: 'SEARCHING_PROVIDERS', to: 'SAFE_ABORT' },
  ];

  const nodeMap = new Map(nodes.map((n) => [n.id, n]));

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full min-h-0">
      {/* Graph Header */}
      <div className="p-3 rounded-xl bg-zinc-900/80 border border-zinc-800 flex items-center justify-between">
        <span className="text-xs font-semibold text-zinc-200 flex items-center gap-1.5">
          <GitBranch className="w-4 h-4 text-purple-400" />
          <span>Deterministic 10-State FSM Workflow DAG</span>
        </span>
        <span className="text-[11px] font-mono font-bold text-sky-400 bg-zinc-950 px-2 py-0.5 rounded border border-zinc-800">
          Active: {currentState}
        </span>
      </div>

      {/* SVG State Diagram Container */}
      <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800 overflow-x-auto flex justify-center">
        <svg viewBox="0 0 500 500" className="w-full max-w-[480px] h-auto select-none">
          <defs>
            <marker
              id="arrow"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#52525b" />
            </marker>
            <marker
              id="arrow-active"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#38bdf8" />
            </marker>
          </defs>

          {/* Render Connections */}
          {edges.map((e, idx) => {
            const f = nodeMap.get(e.from);
            const t = nodeMap.get(e.to);
            if (!f || !t) return null;
            const isEdgeActive = currentState === e.to;
            return (
              <line
                key={idx}
                x1={f.x}
                y1={f.y}
                x2={t.x}
                y2={t.y}
                stroke={isEdgeActive ? '#38bdf8' : '#3f3f46'}
                strokeWidth={isEdgeActive ? 2 : 1}
                strokeDasharray={isEdgeActive ? '5,5' : 'none'}
                markerEnd={isEdgeActive ? 'url(#arrow-active)' : 'url(#arrow)'}
                className={isEdgeActive ? 'edge-flowing' : ''}
              />
            );
          })}

          {/* Render Nodes */}
          {nodes.map((n) => {
            const isActive = currentState === n.id;
            return (
              <g key={n.id} className="cursor-pointer">
                {isActive && (
                  <circle
                    cx={n.x}
                    cy={n.y}
                    r={26}
                    fill="none"
                    stroke={n.color}
                    strokeWidth={2}
                    className="animate-ping opacity-75"
                  />
                )}
                <circle
                  cx={n.x}
                  cy={n.y}
                  r={18}
                  fill={isActive ? n.color : '#18181b'}
                  stroke={isActive ? '#ffffff' : n.color}
                  strokeWidth={isActive ? 3 : 1.5}
                />
                <text
                  x={n.x}
                  y={n.y + 30}
                  textAnchor="middle"
                  fill={isActive ? '#ffffff' : '#a1a1aa'}
                  fontSize="9px"
                  fontFamily="JetBrains Mono, monospace"
                  fontWeight={isActive ? 'bold' : 'normal'}
                >
                  {n.label}
                </text>
              </g>
            );
          })}
        </svg>
      </div>

      {/* Transition History Table */}
      <div className="space-y-2">
        <div className="text-xs font-semibold text-zinc-300">
          State Transition Event Log ({transitions.length} recorded)
        </div>

        {transitions.length === 0 ? (
          <div className="p-4 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
            Initial state IDLE. No state transitions recorded yet.
          </div>
        ) : (
          <div className="space-y-1.5 max-h-56 overflow-y-auto">
            {transitions.slice(-10).map((t, idx) => (
              <div
                key={idx}
                className="p-2 rounded-lg bg-zinc-900/60 border border-zinc-800 text-xs flex items-center justify-between gap-2"
              >
                <div className="flex items-center gap-1.5 font-mono">
                  <span className="text-zinc-400">{t.from_state}</span>
                  <span className="text-sky-400">→</span>
                  <span className="text-zinc-100 font-semibold">{t.to_state}</span>
                </div>
                <span className="text-[10px] text-zinc-500 truncate max-w-[200px]" title={t.reason}>
                  {t.reason || 'Transition'}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
