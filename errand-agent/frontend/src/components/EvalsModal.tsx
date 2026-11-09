import React from 'react';
import { CheckCircle2, XCircle, AlertCircle, X, Download } from 'lucide-react';

interface EvalsModalProps {
  isOpen: boolean;
  onClose: () => void;
  results: {
    total: number;
    passed: number;
    failed: number;
    results: Array<{
      scenario_id: string;
      title: string;
      expected: string;
      actual: string;
      status: 'PASS' | 'FAIL';
      notes?: string;
    }>;
  } | null;
  isLoading: boolean;
}

export const EvalsModal: React.FC<EvalsModalProps> = ({
  isOpen,
  onClose,
  results,
  isLoading,
}) => {
  if (!isOpen) return null;

  const downloadReport = () => {
    if (!results) return;
    let md = `# Errand Agent Enterprise — Automated Evaluation Report\n\n`;
    md += `**Total Scenarios:** ${results.total} | **Passed:** ${results.passed} | **Failed:** ${results.failed}\n\n`;
    md += `| ID | Scenario | Expected | Actual | Status | Notes |\n`;
    md += `|---|---|---|---|---|---|\n`;
    results.results.forEach((r) => {
      md += `| ${r.scenario_id} | ${r.title} | ${r.expected} | ${r.actual} | ${r.status} | ${r.notes || ''} |\n`;
    });
    const blob = new Blob([md], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `EVAL_REPORT_${new Date().toISOString().slice(0, 10)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-zinc-900 border border-zinc-800 rounded-2xl max-w-4xl w-full max-h-[85vh] flex flex-col overflow-hidden shadow-2xl">
        {/* Header */}
        <div className="p-4 border-b border-zinc-800 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="w-5 h-5 text-emerald-400" />
            <span className="font-bold text-base text-zinc-100">
              Automated Evaluation Suite Results
            </span>
          </div>
          <div className="flex items-center gap-2">
            {results && (
              <button
                onClick={downloadReport}
                className="px-2.5 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs flex items-center gap-1"
              >
                <Download className="w-3.5 h-3.5" />
                <span>Export Report</span>
              </button>
            )}
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-zinc-800 transition"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="p-5 overflow-y-auto flex-1 space-y-4">
          {isLoading ? (
            <div className="py-12 text-center space-y-3">
              <div className="w-8 h-8 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin mx-auto" />
              <p className="text-sm text-zinc-300 font-medium">
                Running 33 simulated user scenarios and verifying state invariants...
              </p>
            </div>
          ) : results ? (
            <>
              {/* Summary Cards */}
              <div className="grid grid-cols-3 gap-3">
                <div className="p-3 rounded-xl bg-zinc-950 border border-zinc-800 text-center">
                  <div className="text-xs text-zinc-400 font-medium">Total Evaluated</div>
                  <div className="text-xl font-bold font-mono text-zinc-100">{results.total}</div>
                </div>
                <div className="p-3 rounded-xl bg-zinc-950 border border-emerald-900/60 text-center">
                  <div className="text-xs text-emerald-400 font-medium">Passed</div>
                  <div className="text-xl font-bold font-mono text-emerald-400">{results.passed}</div>
                </div>
                <div className="p-3 rounded-xl bg-zinc-950 border border-zinc-800 text-center">
                  <div className="text-xs text-zinc-400 font-medium">Failed</div>
                  <div className="text-xl font-bold font-mono text-rose-400">{results.failed}</div>
                </div>
              </div>

              {/* Scenarios Table */}
              <div className="border border-zinc-800 rounded-xl overflow-hidden">
                <table className="w-full text-left text-xs border-collapse">
                  <thead className="bg-zinc-950 border-b border-zinc-800 font-mono text-zinc-400">
                    <tr>
                      <th className="p-2.5">ID</th>
                      <th className="p-2.5">Scenario Title</th>
                      <th className="p-2.5">Expected Invariant</th>
                      <th className="p-2.5">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800/80">
                    {results.results.map((r) => (
                      <tr key={r.scenario_id} className="hover:bg-zinc-950/40">
                        <td className="p-2.5 font-mono text-sky-400 font-semibold">{r.scenario_id}</td>
                        <td className="p-2.5 font-medium text-zinc-200">{r.title}</td>
                        <td className="p-2.5 text-zinc-400">{r.expected}</td>
                        <td className="p-2.5 font-mono">
                          <span
                            className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                              r.status === 'PASS'
                                ? 'bg-emerald-950 text-emerald-300 border border-emerald-800'
                                : 'bg-rose-950 text-rose-300 border border-rose-800'
                            }`}
                          >
                            {r.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <p className="text-center text-xs text-zinc-500 py-8">No results available.</p>
          )}
        </div>
      </div>
    </div>
  );
};
