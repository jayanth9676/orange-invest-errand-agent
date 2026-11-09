import React, { useState } from 'react';
import { MemoryItem, MemoryPartitions, ContextAudit } from '../../types';
import {
  Brain,
  Shield,
  Trash2,
  CheckCircle,
  Edit3,
  Search,
  Filter,
  AlertOctagon,
  Info,
} from 'lucide-react';

interface MemoryCockpitProps {
  memories: MemoryItem[];
  partitions: MemoryPartitions;
  contextAudit: ContextAudit | null;
  onMemoryAction: (action: 'inspect' | 'forget' | 'confirm' | 'update', key: string, val: string) => void;
  isLoading: boolean;
}

export const MemoryCockpit: React.FC<MemoryCockpitProps> = ({
  memories,
  partitions,
  contextAudit,
  onMemoryAction,
  isLoading,
}) => {
  const [filterType, setFilterType] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState<string>('');

  const safeMemories = (memories || []).filter(Boolean);

  const filteredMemories = safeMemories.filter((m) => {
    // Search query filter
    const keyStr = String(m.key || '').toLowerCase();
    const valStr = String(m.value || '').toLowerCase();
    const qStr = searchQuery.toLowerCase();
    if (searchQuery && !keyStr.includes(qStr) && !valStr.includes(qStr)) {
      return false;
    }
    // Category filter
    if (filterType === 'explicit') return m.source_type === 'EXPLICIT' && !m.forgotten;
    if (filterType === 'inferred') return m.source_type === 'INFERRED' && !m.forgotten;
    if (filterType === 'confirmed') return m.confirmed === 1 && !m.forgotten;
    if (filterType === 'unconfirmed') return m.source_type === 'INFERRED' && m.confirmed === 0 && !m.forgotten;
    if (filterType === 'forgotten') return m.forgotten === 1;
    return true;
  });

  const handleUpdateSubmit = (key: string) => {
    if (!editValue.trim()) return;
    onMemoryAction('update', key, editValue);
    setEditingKey(null);
  };

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full min-h-0">
      {/* Header & Partition Counters */}
      <div className="p-3 rounded-xl bg-zinc-900/80 border border-zinc-800 space-y-2">
        <div className="flex items-center justify-between text-xs">
          <span className="font-semibold text-zinc-200 flex items-center gap-1.5">
            <Brain className="w-4 h-4 text-emerald-400" />
            <span>Persistent Memory & Strict Token Budget</span>
          </span>
          <span className="text-[11px] font-mono text-zinc-400">
            {safeMemories.length} Stored Memories Seeded
          </span>
        </div>

        {/* Partition Pills */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-1">
          <div className="p-2 rounded-lg bg-zinc-950 border border-zinc-800 text-center">
            <div className="text-[10px] text-zinc-500 font-medium">EXPLICIT</div>
            <div className="text-sm font-mono font-bold text-sky-400">
              {partitions?.explicit_count ?? 0}
            </div>
          </div>
          <div className="p-2 rounded-lg bg-zinc-950 border border-zinc-800 text-center">
            <div className="text-[10px] text-zinc-500 font-medium">CONFIRMED INFERRED</div>
            <div className="text-sm font-mono font-bold text-emerald-400">
              {partitions?.confirmed_inferred_count ?? 0}
            </div>
          </div>
          <div className="p-2 rounded-lg bg-zinc-950 border border-zinc-800 text-center">
            <div className="text-[10px] text-zinc-500 font-medium">UNCONFIRMED (BLOCKED)</div>
            <div className="text-sm font-mono font-bold text-amber-400">
              {partitions?.unconfirmed_inferred_count ?? 0}
            </div>
          </div>
          <div className="p-2 rounded-lg bg-zinc-950 border border-zinc-800 text-center">
            <div className="text-[10px] text-zinc-500 font-medium">FORGOTTEN (PURGED)</div>
            <div className="text-sm font-mono font-bold text-rose-400">
              {partitions?.forgotten_count ?? 0}
            </div>
          </div>
        </div>
      </div>

      {/* SQL Pre-Filter Exclusion Diagnostics */}
      {contextAudit && (
        <div className="p-3 rounded-xl bg-zinc-900/60 border border-zinc-800 space-y-1.5 text-xs">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-zinc-300 flex items-center gap-1.5">
              <Shield className="w-3.5 h-3.5 text-sky-400" />
              <span>SQL Pre-Filter Context Gate (Zero-Leakage Invariant)</span>
            </span>
            <span className="font-mono text-[10px] text-zinc-400">
              Sent: {contextAudit.actual_tokens ?? (contextAudit as any).total_tokens ?? 0} / {contextAudit.max_tokens ?? 1200} tokens
            </span>
          </div>
          <p className="text-[11px] text-zinc-400">
            Unconfirmed inferred memories and forgotten facts are strictly filtered out by SQL queries before prompt compilation.
          </p>
        </div>
      )}

      {/* Filters and Search Bar */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1 overflow-x-auto no-scrollbar text-xs">
          {['all', 'explicit', 'inferred', 'confirmed', 'unconfirmed', 'forgotten'].map((f) => (
            <button
              key={f}
              onClick={() => setFilterType(f)}
              className={`px-2.5 py-1 rounded-lg text-[11px] font-medium capitalize transition cursor-pointer ${
                filterType === f
                  ? 'bg-sky-600 text-white font-semibold'
                  : 'bg-zinc-900 text-zinc-400 hover:text-zinc-200 border border-zinc-800'
              }`}
            >
              {f}
            </button>
          ))}
        </div>

        <div className="relative flex-1 min-w-[140px] max-w-[200px]">
          <Search className="w-3 h-3 absolute left-2.5 top-2.5 text-zinc-500" />
          <input
            type="text"
            placeholder="Search key or value..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-7 pr-2.5 py-1 rounded-lg bg-zinc-950 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-sky-500"
          />
        </div>
      </div>

      {/* Memory Items List */}
      <div className="space-y-2">
        {filteredMemories.map((m) => (
          <div
            key={m.memory_id}
            className={`p-3 rounded-xl border text-xs transition space-y-1.5 ${
              m.forgotten
                ? 'bg-zinc-950 border-rose-950/80 opacity-60'
                : m.source_type === 'INFERRED' && !m.confirmed
                ? 'bg-zinc-900/60 border-amber-900/50'
                : 'bg-zinc-900/80 border-zinc-800'
            }`}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <span className="font-mono font-semibold text-zinc-200 text-xs truncate block">
                  {m.key}
                </span>
                <span className="text-[10px] text-zinc-500 font-mono">ID: {m.memory_id}</span>
              </div>

              {/* Status Badges */}
              <div className="flex items-center gap-1 shrink-0 font-mono text-[10px]">
                <span
                  className={`px-1.5 py-0.2 rounded border ${
                    m.source_type === 'EXPLICIT'
                      ? 'bg-sky-950 text-sky-400 border-sky-800'
                      : 'bg-indigo-950 text-indigo-400 border-indigo-800'
                  }`}
                >
                  {m.source_type}
                </span>

                {m.forgotten ? (
                  <span className="px-1.5 py-0.2 rounded bg-rose-950 text-rose-300 border border-rose-800">
                    FORGOTTEN
                  </span>
                ) : m.source_type === 'INFERRED' ? (
                  <span
                    className={`px-1.5 py-0.2 rounded border ${
                      m.confirmed
                        ? 'bg-emerald-950 text-emerald-300 border-emerald-800'
                        : 'bg-amber-950 text-amber-300 border-amber-800'
                    }`}
                  >
                    {m.confirmed ? 'CONFIRMED' : 'UNCONFIRMED'}
                  </span>
                ) : null}
              </div>
            </div>

            {/* Value Display / Edit Form */}
            {editingKey === m.key ? (
              <div className="flex items-center gap-1 pt-1">
                <input
                  type="text"
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  className="flex-1 px-2 py-1 rounded bg-zinc-950 border border-zinc-700 text-xs text-white"
                />
                <button
                  onClick={() => handleUpdateSubmit(m.key)}
                  className="px-2 py-1 rounded bg-emerald-600 text-white text-[11px] font-semibold"
                >
                  Save
                </button>
                <button
                  onClick={() => setEditingKey(null)}
                  className="px-2 py-1 rounded bg-zinc-800 text-zinc-400 text-[11px]"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <p className="text-zinc-300 font-mono text-xs bg-zinc-950/80 p-2 rounded border border-zinc-800/80">
                {m.value}
              </p>
            )}

            {/* Action Buttons */}
            <div className="flex items-center justify-end gap-1.5 pt-1 text-[11px]">
              {m.source_type === 'INFERRED' && !m.confirmed && !m.forgotten && (
                <button
                  onClick={() => onMemoryAction('confirm', m.key, m.value)}
                  disabled={isLoading}
                  className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-emerald-950 text-zinc-300 hover:text-emerald-300 border border-zinc-700 hover:border-emerald-700 transition cursor-pointer flex items-center gap-1"
                >
                  <CheckCircle className="w-3 h-3" />
                  <span>Confirm</span>
                </button>
              )}

              {!m.forgotten && (
                <>
                  <button
                    onClick={() => {
                      setEditingKey(m.key);
                      setEditValue(m.value);
                    }}
                    disabled={isLoading}
                    className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 hover:text-white border border-zinc-700 transition cursor-pointer flex items-center gap-1"
                  >
                    <Edit3 className="w-3 h-3" />
                    <span>Edit</span>
                  </button>

                  <button
                    onClick={() => onMemoryAction('forget', m.key, '')}
                    disabled={isLoading}
                    className="px-2 py-0.5 rounded bg-zinc-800 hover:bg-rose-950 text-zinc-300 hover:text-rose-300 border border-zinc-700 hover:border-rose-800 transition cursor-pointer flex items-center gap-1"
                  >
                    <Trash2 className="w-3 h-3" />
                    <span>Forget</span>
                  </button>
                </>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
