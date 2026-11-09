import React, { useState, useEffect } from 'react';
import { api } from '../../services/api';
import {
  Plug,
  Terminal,
  Play,
  Lock,
  CheckCircle,
  AlertCircle,
  ChevronRight,
  Code,
} from 'lucide-react';

export const McpStudio: React.FC = () => {
  const [tools, setTools] = useState<any[]>([]);
  const [selectedTool, setSelectedTool] = useState<string>('search_providers');
  const [argsJson, setArgsJson] = useState<string>('{\n  "service": "plumber",\n  "area": "Koramangala"\n}');
  const [callerRole, setCallerRole] = useState<string>('Admin');
  const [executionResult, setExecutionResult] = useState<any>(null);
  const [isExecuting, setIsExecuting] = useState<boolean>(false);

  useEffect(() => {
    api.getMcpTools()
      .then((data) => {
        if (data.tools) setTools(data.tools);
      })
      .catch((err) => console.error('Error fetching MCP tools:', err));
  }, []);

  const handleToolSelect = (toolName: string) => {
    setSelectedTool(toolName);
    switch (toolName) {
      case 'search_providers':
        setArgsJson('{\n  "service": "plumber",\n  "area": "Koramangala"\n}');
        break;
      case 'get_provider':
        setArgsJson('{\n  "provider_id": "prov_plumb_1"\n}');
        break;
      case 'hold_slot':
        setArgsJson('{\n  "provider_id": "prov_plumb_1",\n  "slot": "2026-11-09 09:00"\n}');
        break;
      case 'book':
        setArgsJson('{\n  "hold_id": "hold_test_123",\n  "idempotency_key": "idem_test_456"\n}');
        break;
      case 'get_booking_status':
        setArgsJson('{\n  "hold_id_or_idempotency_key": "idem_test_456"\n}');
        break;
      case 'cancel':
        setArgsJson('{\n  "booking_id": "bk_upcoming_ac_01",\n  "idempotency_key": "idem_cancel_789"\n}');
        break;
      case 'reschedule':
        setArgsJson('{\n  "booking_id": "bk_upcoming_ac_01",\n  "slot": "2026-11-14 10:00",\n  "idempotency_key": "idem_resched_999"\n}');
        break;
      case 'list_bookings':
        setArgsJson('{\n  "user_id": "user_1"\n}');
        break;
      default:
        setArgsJson('{}');
    }
  };

  const handleExecute = async () => {
    setIsExecuting(true);
    setExecutionResult(null);
    try {
      const parsedArgs = JSON.parse(argsJson);
      const res = await api.callMcpTool(selectedTool, parsedArgs, callerRole);
      setExecutionResult({ success: true, data: res });
    } catch (err: any) {
      setExecutionResult({ success: false, error: err.message });
    } finally {
      setIsExecuting(false);
    }
  };

  const mutatingTools = new Set(['hold_slot', 'book', 'cancel', 'reschedule']);

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full min-h-0">
      {/* Header */}
      <div className="p-3 rounded-xl bg-zinc-900/80 border border-zinc-800 space-y-1">
        <div className="flex items-center justify-between text-xs">
          <span className="font-semibold text-zinc-200 flex items-center gap-1.5">
            <Plug className="w-4 h-4 text-sky-400" />
            <span>Model Context Protocol (MCP) Studio</span>
          </span>
          <span className="text-[10px] font-mono text-sky-400 bg-zinc-950 px-2 py-0.5 rounded border border-zinc-800">
            JSON-RPC 2.0 / MCP 2026
          </span>
        </div>
        <p className="text-[11px] text-zinc-400">
          Standardized tool, prompt, and resource interface conforming to the Anthropic/OpenAI Model Context Protocol.
        </p>
      </div>

      {/* 8 Tool Contracts List */}
      <div className="space-y-1.5">
        <span className="text-xs font-semibold text-zinc-400">Fixed Tool Contracts (8 Available)</span>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
          {[
            'search_providers',
            'get_provider',
            'hold_slot',
            'book',
            'get_booking_status',
            'cancel',
            'reschedule',
            'list_bookings',
          ].map((tName) => {
            const isMutating = mutatingTools.has(tName);
            const isSelected = selectedTool === tName;
            return (
              <button
                key={tName}
                onClick={() => handleToolSelect(tName)}
                className={`p-2 rounded-lg border text-left text-xs font-mono transition cursor-pointer flex flex-col justify-between ${
                  isSelected
                    ? 'bg-sky-950/60 border-sky-500 text-sky-200 font-bold'
                    : 'bg-zinc-950 border-zinc-800 text-zinc-400 hover:text-zinc-200'
                }`}
              >
                <span className="truncate">{tName}</span>
                <span className="text-[9px] uppercase font-sans font-normal pt-1 flex items-center gap-1 text-zinc-500">
                  {isMutating && <Lock className="w-2.5 h-2.5 text-amber-500" />}
                  <span>{isMutating ? 'Mutating' : 'Read-Only'}</span>
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* JSON-RPC Request Workbench */}
      <div className="p-3.5 rounded-xl bg-zinc-900/60 border border-zinc-800 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="font-semibold text-zinc-200 flex items-center gap-1.5">
            <Terminal className="w-4 h-4 text-emerald-400" />
            <span>Invoke Tool: <code className="text-sky-300">{selectedTool}</code></span>
          </span>

          {/* Role Selector */}
          <div className="flex items-center gap-1.5 font-mono text-xs">
            <span className="text-zinc-400 text-[11px]">Role:</span>
            {['Admin', 'Operator', 'Auditor'].map((role) => (
              <button
                key={role}
                onClick={() => setCallerRole(role)}
                className={`px-2 py-0.5 rounded text-[11px] font-semibold border transition cursor-pointer ${
                  callerRole === role
                    ? 'bg-emerald-950 text-emerald-300 border-emerald-700'
                    : 'bg-zinc-950 text-zinc-400 border-zinc-800'
                }`}
              >
                {role}
              </button>
            ))}
          </div>
        </div>

        {/* JSON Arguments Editor */}
        <div className="space-y-1">
          <label className="text-[10px] font-mono uppercase text-zinc-400">Tool Arguments (JSON):</label>
          <textarea
            rows={5}
            value={argsJson}
            onChange={(e) => setArgsJson(e.target.value)}
            className="w-full p-2.5 rounded-lg bg-zinc-950 border border-zinc-800 font-mono text-xs text-zinc-200 focus:outline-none focus:border-sky-500"
          />
        </div>

        {/* Action Button */}
        <button
          onClick={handleExecute}
          disabled={isExecuting}
          className="w-full py-2 rounded-lg text-xs font-semibold bg-sky-600 hover:bg-sky-500 text-white shadow-md shadow-sky-600/20 transition flex items-center justify-center gap-1.5 cursor-pointer disabled:opacity-50"
        >
          <Play className="w-3.5 h-3.5 fill-current" />
          <span>Execute via MCP Gateway</span>
        </button>

        {/* Execution Output */}
        {executionResult && (
          <div
            className={`p-3 rounded-lg border font-mono text-xs space-y-1 ${
              executionResult.success
                ? 'bg-zinc-950 border-emerald-900/60 text-emerald-300'
                : 'bg-rose-950/40 border-rose-900/80 text-rose-300'
            }`}
          >
            <div className="flex items-center gap-1 font-semibold text-[11px]">
              {executionResult.success ? (
                <>
                  <CheckCircle className="w-3.5 h-3.5 text-emerald-400" />
                  <span>200 OK — Tool Result:</span>
                </>
              ) : (
                <>
                  <AlertCircle className="w-3.5 h-3.5 text-rose-400" />
                  <span>Execution Failed / Blocked:</span>
                </>
              )}
            </div>
            <pre className="overflow-x-auto whitespace-pre-wrap max-h-48 text-[11px]">
              {JSON.stringify(executionResult.data || executionResult.error, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
};
