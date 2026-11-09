import React, { useState } from 'react';
import {
  Zap,
  AlertTriangle,
  RotateCcw,
  ShieldAlert,
  Flame,
  Radio,
  Clock,
  TrendingUp,
  Ban,
  CheckCircle,
} from 'lucide-react';

interface ChaosSandboxProps {
  onTriggerProactiveCancel: (bookingId: string) => void;
  onSetChaosParams: (params: {
    forceBookOutcome?: string | null;
    forcePriceDrift?: number | null;
    injectAdversarialReview?: boolean | null;
    persistentCancel503?: boolean | null;
  }) => void;
  isLoading: boolean;
}

export const ChaosSandbox: React.FC<ChaosSandboxProps> = ({
  onTriggerProactiveCancel,
  onSetChaosParams,
  isLoading,
}) => {
  const [selectedBookOutcome, setSelectedBookOutcome] = useState<string>('default');
  const [priceDriftDelta, setPriceDriftDelta] = useState<number>(0);
  const [injectReview, setInjectReview] = useState<boolean>(false);
  const [persistent503, setPersistent503] = useState<boolean>(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const applyChaos = (outcome: string, drift: number, review: boolean, p503: boolean) => {
    onSetChaosParams({
      forceBookOutcome: outcome === 'default' ? null : outcome,
      forcePriceDrift: drift,
      injectAdversarialReview: review,
      persistentCancel503: p503,
    });
    setStatusMessage(`Chaos parameters applied for the upcoming conversational turn.`);
    setTimeout(() => setStatusMessage(null), 4000);
  };

  return (
    <div className="p-4 space-y-4 overflow-y-auto h-full min-h-0">
      {/* Header */}
      <div className="p-3 rounded-xl bg-zinc-900/80 border border-zinc-800 space-y-1">
        <div className="flex items-center justify-between text-xs">
          <span className="font-semibold text-zinc-200 flex items-center gap-1.5">
            <Zap className="w-4 h-4 text-amber-400" />
            <span>Chaos & Fault Injection Sandbox</span>
          </span>
          <span className="text-[10px] font-mono text-zinc-500 bg-zinc-950 px-2 py-0.5 rounded border border-zinc-800">
            Zero-Trust Fault Injection
          </span>
        </div>
        <p className="text-[11px] text-zinc-400">
          Simulate real-world network timeouts, slot races, price drift, and adversarial attacks to verify self-healing FSM invariants.
        </p>
      </div>

      {statusMessage && (
        <div className="p-2.5 rounded-lg bg-sky-950/40 border border-sky-800/60 text-xs text-sky-300 flex items-center gap-1.5">
          <CheckCircle className="w-4 h-4 text-sky-400" />
          <span>{statusMessage}</span>
        </div>
      )}

      {/* 1. Proactive Provider Cancellation Webhook */}
      <div className="p-3.5 rounded-xl bg-zinc-900/60 border border-zinc-800 space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-zinc-200 flex items-center gap-1.5">
            <Radio className="w-4 h-4 text-teal-400 animate-pulse" />
            <span>Proactive Provider Cancellation Webhook</span>
          </span>
          <span className="text-[10px] font-mono text-teal-400 bg-teal-950/60 px-2 py-0.5 rounded border border-teal-800">
            Async Daemon Event
          </span>
        </div>
        <p className="text-xs text-zinc-400">
          Simulate a booked provider cancelling. The agent detects it without a user prompt, searches replacements within preferences, and seeks fresh approval before rebooking.
        </p>

        <button
          onClick={() => onTriggerProactiveCancel('bk_upcoming_ac_01')}
          disabled={isLoading}
          className="w-full py-2 rounded-lg text-xs font-semibold bg-teal-600 hover:bg-teal-500 text-white shadow-md shadow-teal-600/20 transition flex items-center justify-center gap-1.5 cursor-pointer disabled:opacity-50"
        >
          <Radio className="w-3.5 h-3.5" />
          <span>Simulate Provider Cancellation (bk_upcoming_ac_01)</span>
        </button>
      </div>

      {/* 2. Booking Fault Simulation */}
      <div className="p-3.5 rounded-xl bg-zinc-900/60 border border-zinc-800 space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-zinc-200 flex items-center gap-1.5">
            <Clock className="w-4 h-4 text-purple-400" />
            <span>Simulate Booking Mutation Outcome</span>
          </span>
          <span className="text-[10px] font-mono text-purple-400 bg-purple-950/60 px-2 py-0.5 rounded border border-purple-800">
            EVAL-08 / EVAL-09
          </span>
        </div>

        <div className="grid grid-cols-2 gap-2 text-xs">
          {[
            { id: 'default', label: 'Default Seed (70/15/15)' },
            { id: 'success', label: 'Force 100% Success' },
            { id: 'timeout', label: 'Force Booking Timeout (50% uncommitted / 50% committed)' },
            { id: 'slot_taken', label: 'Force Slot Taken' },
          ].map((item) => (
            <button
              key={item.id}
              onClick={() => {
                setSelectedBookOutcome(item.id);
                applyChaos(item.id, priceDriftDelta, injectReview, persistent503);
              }}
              className={`p-2 rounded-lg border text-left transition cursor-pointer font-medium ${
                selectedBookOutcome === item.id
                  ? 'bg-purple-950/60 border-purple-500 text-purple-200 font-semibold'
                  : 'bg-zinc-950 border-zinc-800 text-zinc-400 hover:text-zinc-200'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {/* 3. Material Price Drift */}
      <div className="p-3.5 rounded-xl bg-zinc-900/60 border border-zinc-800 space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold text-zinc-200 flex items-center gap-1.5">
            <TrendingUp className="w-4 h-4 text-amber-400" />
            <span>Price Drift on Hold Acquisition</span>
          </span>
          <span className="text-[10px] font-mono text-amber-400 bg-amber-950/60 px-2 py-0.5 rounded border border-amber-800">
            EVAL-06
          </span>
        </div>
        <p className="text-xs text-zinc-400">
          Injects a price discrepancy when `hold_slot` returns. Approval Gate must catch the drift, invalidate the proposal, and request fresh confirmation.
        </p>

        <div className="flex items-center gap-2">
          {[0, 150, 250, 500].map((delta) => (
            <button
              key={delta}
              onClick={() => {
                setPriceDriftDelta(delta);
                applyChaos(selectedBookOutcome, delta, injectReview, persistent503);
              }}
              className={`flex-1 py-1.5 rounded-lg border text-xs font-mono transition cursor-pointer ${
                priceDriftDelta === delta
                  ? 'bg-amber-950/60 border-amber-500 text-amber-200 font-bold'
                  : 'bg-zinc-950 border-zinc-800 text-zinc-400 hover:text-zinc-200'
              }`}
            >
              {delta === 0 ? 'No Drift' : `+₹${delta}`}
            </button>
          ))}
        </div>
      </div>

      {/* 4. Adversarial Review & 503 Toggles */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="p-3 rounded-xl bg-zinc-900/60 border border-zinc-800 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-zinc-200 flex items-center gap-1.5">
              <ShieldAlert className="w-4 h-4 text-rose-400" />
              <span>Prompt Injection Review</span>
            </span>
          </div>
          <p className="text-[11px] text-zinc-400">
            Injects adversarial instruction payloads in reviews. Verified passive data.
          </p>
          <button
            onClick={() => {
              const next = !injectReview;
              setInjectReview(next);
              applyChaos(selectedBookOutcome, priceDriftDelta, next, persistent503);
            }}
            className={`w-full py-1.5 rounded-lg text-xs font-medium border transition cursor-pointer ${
              injectReview
                ? 'bg-rose-950/60 border-rose-500 text-rose-200 font-semibold'
                : 'bg-zinc-950 border-zinc-800 text-zinc-400 hover:text-zinc-200'
            }`}
          >
            {injectReview ? 'Injection Active on All' : 'Inject on Reviews'}
          </button>
        </div>

        <div className="p-3 rounded-xl bg-zinc-900/60 border border-zinc-800 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-zinc-200 flex items-center gap-1.5">
              <RotateCcw className="w-4 h-4 text-indigo-400" />
              <span>Persistent 503 Retry Test</span>
            </span>
          </div>
          <p className="text-[11px] text-zinc-400">
            Simulate persistent HTTP 503 on cancellation to test safe abort handling.
          </p>
          <button
            onClick={() => {
              const next = !persistent503;
              setPersistent503(next);
              applyChaos(selectedBookOutcome, priceDriftDelta, injectReview, next);
            }}
            className={`w-full py-1.5 rounded-lg text-xs font-medium border transition cursor-pointer ${
              persistent503
                ? 'bg-indigo-950/60 border-indigo-500 text-indigo-200 font-semibold'
                : 'bg-zinc-950 border-zinc-800 text-zinc-400 hover:text-zinc-200'
            }`}
          >
            {persistent503 ? 'Persistent 503 Active' : 'Default Retry (1x 503)'}
          </button>
        </div>
      </div>
    </div>
  );
};
