import React, { useState, useEffect } from 'react';
import { ApprovalProposal } from '../../types';
import {
  ShieldAlert,
  CheckCircle2,
  XCircle,
  Clock,
  KeyRound,
  AlertTriangle,
  ArrowRight,
  TrendingUp,
} from 'lucide-react';

interface ApprovalCockpitProps {
  proposal: ApprovalProposal | null;
  onApprove: (proposalId: string) => void;
  onReject: (proposalId: string) => void;
  isProcessing: boolean;
}

export const ApprovalCockpit: React.FC<ApprovalCockpitProps> = ({
  proposal,
  onApprove,
  onReject,
  isProcessing,
}) => {
  if (!proposal) return null;

  const [timeLeft, setTimeLeft] = useState<number>(proposal.ttl_seconds || 600);

  useEffect(() => {
    // Calculate remaining seconds based on proposal created_at and ttl_seconds
    const now = Date.now() / 1000;
    const createdAt = proposal.created_at || proposal.created_at_epoch || now;
    const ttl = proposal.ttl_seconds || 600;
    const elapsed = Math.max(0, now - createdAt);
    const remaining = Math.max(0, Math.round(ttl - elapsed));
    setTimeLeft(remaining);

    const interval = setInterval(() => {
      setTimeLeft((prev) => {
        if (prev <= 1) {
          clearInterval(interval);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => clearInterval(interval);
  }, [proposal]);

  const isExpired = timeLeft === 0 || proposal.invalidated;
  const ttlProgress = Math.max(0, Math.min(100, (timeLeft / (proposal.ttl_seconds || 600)) * 100));

  return (
    <div className="shrink-0 px-4 py-2 bg-zinc-950/90 border-t border-zinc-800/80 backdrop-blur-md">
      <div
        className={`p-3.5 rounded-xl border transition-all duration-300 shadow-xl ${
          proposal.invalidated
            ? 'bg-rose-950/30 border-rose-500/50 text-rose-200'
            : isExpired
            ? 'bg-zinc-900 border-zinc-700 text-zinc-400'
            : 'bg-amber-950/25 border-amber-500/50 text-zinc-100 ring-1 ring-amber-500/20'
        }`}
      >
        {/* Top line: Header & Badges */}
        <div className="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-zinc-800/80">
          <div className="flex items-center gap-2">
            <span
              className={`w-2.5 h-2.5 rounded-full ${
                proposal.invalidated
                  ? 'bg-rose-500'
                  : isExpired
                  ? 'bg-zinc-500'
                  : 'bg-amber-400 animate-ping-slow'
              }`}
            />
            <span className="text-xs font-bold uppercase tracking-wider text-amber-300 flex items-center gap-1.5">
              <ShieldAlert className="w-3.5 h-3.5 text-amber-400" />
              <span>Zero-Trust Approval Cockpit</span>
            </span>
            <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-zinc-900 border border-zinc-700 text-zinc-300">
              {(proposal.mutation_type || 'BOOK').toUpperCase()}
            </span>
          </div>

          <div className="flex items-center gap-2 text-xs font-mono">
            {/* Hold Expiry Countdown */}
            <div className="flex items-center gap-1 text-[11px] text-amber-300 bg-zinc-900/80 px-2 py-0.5 rounded border border-amber-500/30">
              <Clock className="w-3 h-3 text-amber-400" />
              <span>
                TTL: {Math.floor(timeLeft / 60)}m {timeLeft % 60}s
              </span>
            </div>

            {/* SHA-256 Digest */}
            <div
              className="hidden sm:flex items-center gap-1 text-[10px] text-zinc-400 bg-zinc-950 px-2 py-0.5 rounded border border-zinc-800"
              title={`Cryptographic SHA-256 Proposal Digest: ${proposal.payload_hash || '--------'}`}
            >
              <KeyRound className="w-3 h-3 text-sky-400" />
              <span>SHA256: {(proposal.payload_hash || '--------').slice(0, 12)}...</span>
            </div>
          </div>
        </div>

        {/* TTL Progress Bar */}
        {!proposal.invalidated && (
          <div className="w-full bg-zinc-900 h-1 rounded-full overflow-hidden my-2">
            <div
              className={`h-full transition-all duration-1000 ${
                ttlProgress > 30 ? 'bg-amber-500' : 'bg-rose-500 animate-pulse'
              }`}
              style={{ width: `${ttlProgress}%` }}
            />
          </div>
        )}

        {/* Proposal Details Card */}
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pt-1">
          <div className="space-y-1 flex-1 min-w-0">
            <div className="text-sm font-semibold text-zinc-100 flex items-center gap-2">
              <span>{proposal.provider_name || proposal.provider_id || 'Proposed Errand'}</span>
              <span className="text-xs font-normal text-zinc-400">@ {proposal.slot || 'Pending Slot'}</span>
              <span className="text-emerald-400 font-mono font-bold">
                ₹{proposal.final_price?.toLocaleString() || '0'}
              </span>
            </div>

            {/* Material Drift Alert if present */}
            {proposal.price_drift_delta && proposal.price_drift_delta !== 0 ? (
              <div className="flex items-center gap-1.5 text-xs text-rose-300 bg-rose-950/60 p-1.5 rounded-lg border border-rose-800/80 font-mono">
                <TrendingUp className="w-3.5 h-3.5 text-rose-400 shrink-0" />
                <span>
                  Price Drift Detected: Was ₹{((proposal.final_price || 0) - proposal.price_drift_delta).toLocaleString()} → Now ₹{(proposal.final_price || 0).toLocaleString()} (+₹{proposal.price_drift_delta}). Prior approval invalidated!
                </span>
              </div>
            ) : proposal.invalidated ? (
              <div className="flex items-center gap-1.5 text-xs text-rose-300 bg-rose-950/60 p-1.5 rounded-lg border border-rose-800/80 font-mono">
                <AlertTriangle className="w-3.5 h-3.5 text-rose-400 shrink-0" />
                <span>Invalidated: {proposal.invalidation_reason || 'Material parameters changed'}</span>
              </div>
            ) : (
              <p className="text-xs text-zinc-400">
                Action requires explicit confirmation. Application code ensures no mutation occurs without matching cryptographic digest.
              </p>
            )}
          </div>

          {/* 1-Click Action Buttons */}
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => onApprove(proposal.proposal_id)}
              disabled={isProcessing || isExpired || proposal.invalidated}
              className="px-3.5 py-2 rounded-lg text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-white shadow-md shadow-emerald-600/20 transition flex items-center gap-1.5 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed active:scale-95"
            >
              <CheckCircle2 className="w-3.5 h-3.5" />
              <span>Approve (1-Click)</span>
            </button>

            <button
              onClick={() => onReject(proposal.proposal_id)}
              disabled={isProcessing}
              className="px-3 py-2 rounded-lg text-xs font-medium bg-zinc-800 hover:bg-rose-950 text-zinc-300 hover:text-rose-200 border border-zinc-700 hover:border-rose-800 transition flex items-center gap-1.5 cursor-pointer disabled:opacity-40 active:scale-95"
            >
              <XCircle className="w-3.5 h-3.5" />
              <span>Reject</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
