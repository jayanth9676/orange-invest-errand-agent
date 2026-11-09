import React from 'react';
import {
  Play,
  CheckCircle2,
  Download,
  RotateCcw,
  Sun,
  Moon,
  Activity,
} from 'lucide-react';
import { AgentState } from '../types';

interface HeaderProps {
  fsmState: AgentState;
  sseConnected: boolean;
  theme: 'dark' | 'light';
  onToggleTheme: () => void;
  onGuidedDemo: () => void;
  onRunEvals: () => void;
  onExportTrace: () => void;
  onResetSession: () => void;
  isEvaluating: boolean;
  tenantId: string;
  sessionId: string;
}

export const Header: React.FC<HeaderProps> = ({
  fsmState,
  sseConnected,
  theme,
  onToggleTheme,
  onGuidedDemo,
  onRunEvals,
  onExportTrace,
  onResetSession,
  isEvaluating,
}) => {
  const getFsmColor = (state: AgentState) => {
    switch (state) {
      case 'IDLE':
        return 'bg-zinc-850 text-zinc-300 border-zinc-700';
      case 'COLLECTING_SLOTS':
        return 'bg-sky-950/80 text-sky-300 border-sky-800';
      case 'SEARCHING_PROVIDERS':
        return 'bg-indigo-950/80 text-indigo-300 border-indigo-800';
      case 'HOLDING_SLOT':
        return 'bg-amber-950/80 text-amber-300 border-amber-800';
      case 'AWAITING_APPROVAL':
        return 'bg-amber-500/20 text-amber-300 border-amber-500 ring-1 ring-amber-500/30 animate-pulse';
      case 'EXECUTING_MUTATION':
        return 'bg-purple-950/80 text-purple-300 border-purple-800 animate-pulse';
      case 'RECONCILING_TIMEOUT':
        return 'bg-violet-950/80 text-violet-300 border-violet-800 animate-pulse';
      case 'PROACTIVE_RECOVERY':
        return 'bg-teal-950/80 text-teal-300 border-teal-800';
      case 'COMPLETED':
        return 'bg-emerald-950/80 text-emerald-300 border-emerald-800';
      case 'SAFE_ABORT':
        return 'bg-rose-950/80 text-rose-300 border-rose-800';
      default:
        return 'bg-zinc-800 text-zinc-300 border-zinc-700';
    }
  };

  return (
    <header className="h-14 border-b border-zinc-800/80 bg-zinc-950/95 backdrop-blur-xl px-4 flex items-center justify-between sticky top-0 z-40 shrink-0 select-none">
      {/* Left: Brand & Status indicators */}
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-xl bg-gradient-to-tr from-sky-500 via-indigo-600 to-violet-600 flex items-center justify-center font-bold text-white text-sm shadow-md shadow-indigo-500/20 border border-white/10">
          EA
        </div>

        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold tracking-tight text-white flex items-center gap-1.5">
            Errand Agent
            <span className="text-[10px] font-mono font-medium text-sky-400 px-2 py-0.5 rounded-full bg-sky-950/80 border border-sky-800/60">
              v2.0 Enterprise
            </span>
          </span>
          <span className="text-xs text-zinc-500 hidden xl:inline">
            Autonomous Multi-Tenant Agentic Operations
          </span>
        </div>

        <div className="h-4 w-px bg-zinc-800 mx-1 hidden sm:block" />

        {/* Dynamic FSM State Indicator */}
        <div className="flex items-center gap-1.5">
          <div
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-xs font-mono font-medium transition-all duration-300 shadow-sm ${getFsmColor(
              fsmState
            )}`}
          >
            <span className="w-2 h-2 rounded-full bg-current animate-ping-slow" />
            <span className="text-[10px] uppercase tracking-wider opacity-70">FSM:</span>
            <span className="font-semibold">{fsmState}</span>
          </div>
        </div>

        {/* Live SSE Badge */}
        <div
          className={`hidden md:flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[11px] font-mono transition-colors shadow-sm ${
            sseConnected
              ? 'bg-emerald-950/40 text-emerald-400 border-emerald-800/50'
              : 'bg-zinc-900 text-zinc-500 border-zinc-800'
          }`}
        >
          <Activity className={`w-3 h-3 ${sseConnected ? 'text-emerald-400 animate-pulse' : 'text-zinc-600'}`} />
          <span>{sseConnected ? 'SSE LIVE' : 'SSE RECONNECTING'}</span>
        </div>
      </div>

      {/* Right: Enterprise Actions Toolbar */}
      <div className="flex items-center gap-2">
        {/* Guided Demo Button */}
        <button
          onClick={onGuidedDemo}
          className="h-8 px-3 rounded-lg text-xs font-semibold bg-amber-950/30 hover:bg-amber-900/50 text-amber-300 border border-amber-500/40 hover:border-amber-400 transition shadow-sm flex items-center gap-1.5 cursor-pointer active:scale-95"
          title="Run guided step-by-step end-to-end booking tour"
        >
          <Play className="w-3 h-3 fill-current text-amber-400" />
          <span className="hidden sm:inline">Guided Demo</span>
        </button>

        {/* Run Evals Button */}
        <button
          onClick={onRunEvals}
          disabled={isEvaluating}
          className="h-8 px-3 rounded-lg text-xs font-semibold bg-emerald-950/30 hover:bg-emerald-900/50 text-emerald-300 border border-emerald-500/40 hover:border-emerald-400 transition shadow-sm flex items-center gap-1.5 cursor-pointer disabled:opacity-50 active:scale-95"
          title="Run all 33 automated test scenarios and view invariant assertions"
        >
          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
          <span className="hidden sm:inline">
            {isEvaluating ? 'Running 33 Evals...' : 'Run 33 Evals'}
          </span>
        </button>

        {/* Export Trace */}
        <button
          onClick={onExportTrace}
          className="h-8 px-2.5 sm:px-3 rounded-lg text-xs font-medium bg-zinc-900 hover:bg-zinc-850 text-zinc-300 hover:text-white border border-zinc-800 hover:border-zinc-700 transition flex items-center gap-1.5 cursor-pointer active:scale-95"
          title="Export OpenTelemetry W3C Distributed Trace JSONL"
        >
          <Download className="w-3.5 h-3.5 text-zinc-400" />
          <span className="hidden md:inline">Export Trace</span>
        </button>

        {/* Reset Session */}
        <button
          onClick={onResetSession}
          className="h-8 px-2.5 sm:px-3 rounded-lg text-xs font-medium bg-zinc-900 hover:bg-zinc-850 text-zinc-300 hover:text-white border border-zinc-800 hover:border-zinc-700 transition flex items-center gap-1.5 cursor-pointer active:scale-95"
          title="Reinitialize state machine, session, and seed database"
        >
          <RotateCcw className="w-3.5 h-3.5 text-zinc-400" />
          <span className="hidden md:inline">Reset</span>
        </button>

        <div className="h-4 w-px bg-zinc-800 mx-1 hidden sm:block" />

        {/* Theme Toggle */}
        <button
          onClick={onToggleTheme}
          className="h-8 w-8 rounded-lg flex items-center justify-center text-zinc-400 hover:text-zinc-100 hover:bg-zinc-900 border border-transparent hover:border-zinc-800 transition cursor-pointer"
          title={theme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode'}
        >
          {theme === 'dark' ? <Sun className="w-4 h-4 text-amber-400" /> : <Moon className="w-4 h-4 text-sky-400" />}
        </button>
      </div>
    </header>
  );
};
