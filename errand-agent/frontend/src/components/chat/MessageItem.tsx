import React, { useState } from 'react';
import { ChatMessage } from '../../types';
import {
  User,
  Bot,
  ChevronDown,
  ShieldAlert,
  ShieldCheck,
  Star,
  CheckCircle,
  Clock,
  Cpu,
  Terminal,
  Wrench,
  Sparkles,
  Layers,
} from 'lucide-react';
import { MarkdownContent } from './MarkdownContent';

interface MessageItemProps {
  message: ChatMessage;
  isLatest?: boolean;
  onSelectSlot?: (provider: any, slot: string) => void;
}

const renderReviewText = (review: any): string => {
  if (!review) return '';
  if (typeof review === 'string') return review;
  if (typeof review === 'object') {
    if (review.isolated_review_data) {
      const raw = String(review.isolated_review_data);
      const cleaned = raw.replace(/<[^>]+>/g, '').trim();
      return cleaned || raw;
    }
    if (review.text) return String(review.text);
    if (review.content) return String(review.content);
    if (review.comment) return String(review.comment);
    try {
      return JSON.stringify(review);
    } catch {
      return '[Review Object]';
    }
  }
  return String(review);
};

export const MessageItem: React.FC<MessageItemProps> = ({ message, isLatest = true, onSelectSlot }) => {
  const isUser = message.role === 'user';
  const [showThinking, setShowThinking] = useState<boolean>(false);
  const [showPastProviders, setShowPastProviders] = useState<boolean>(isLatest);
  const reasoning = message.reasoning;

  const hasThoughtOrSteps = Boolean(
    reasoning && (reasoning.thought_summary || (reasoning.steps && reasoning.steps.length > 0))
  );

  return (
    <div className={`flex gap-3 ${isUser ? 'justify-end' : 'justify-start'} group`}>
      {/* Assistant Avatar */}
      {!isUser && (
        <div className="w-8 h-8 rounded-xl bg-gradient-to-tr from-sky-500 via-indigo-600 to-violet-600 flex items-center justify-center text-white shrink-0 shadow-md shadow-sky-900/40 border border-sky-400/20">
          <Bot className="w-4 h-4" />
        </div>
      )}

      {/* Message Body */}
      <div className="max-w-[90%] sm:max-w-[82%] space-y-2">
        {/* Main Content Box */}
        <div
          className={`px-4 py-3 rounded-2xl text-xs sm:text-sm leading-relaxed transition-all ${
            isUser
              ? 'bg-gradient-to-r from-sky-600 to-blue-600 text-white rounded-tr-sm shadow-md shadow-sky-900/20'
              : 'bg-zinc-900/90 text-zinc-200 border border-zinc-800/80 rounded-tl-sm shadow-sm backdrop-blur-md'
          }`}
        >
          {/* Text Content */}
          {isUser ? (
            <div className="whitespace-pre-wrap">{message.content}</div>
          ) : (
            <MarkdownContent content={message.content} />
          )}

          {/* Assistant Metadata Line */}
          {!isUser && reasoning && (
            <div className="flex flex-wrap items-center gap-2 pt-2.5 mt-2 border-t border-zinc-800/60 text-[10px] text-zinc-400 font-mono">
              <span className="flex items-center gap-1 text-sky-400 font-semibold">
                <Cpu className="w-3 h-3" />
                <span>{reasoning.model || 'gemini'}</span>
              </span>
              <span>•</span>
              <span className="flex items-center gap-1 text-zinc-300">
                <Clock className="w-3 h-3 text-zinc-500" />
                <span>{reasoning.latency_ms ?? 0}ms</span>
              </span>
              <span>•</span>
              <span className="text-zinc-400">
                Tokens: {reasoning.tokens?.total ?? 0} / {reasoning.tokens?.cap ?? 1200} ({reasoning.tokens?.utilization_pct ?? 0}%)
              </span>
            </div>
          )}
        </div>

        {/* Collapsible Model Thinking & Verification Section */}
        {!isUser && hasThoughtOrSteps && (
          <div className="space-y-1.5">
            <button
              onClick={() => setShowThinking(!showThinking)}
              className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-[11px] font-mono text-zinc-400 hover:text-sky-300 bg-zinc-900/70 hover:bg-zinc-900 border border-zinc-800/80 hover:border-sky-800/60 transition cursor-pointer shadow-sm select-none"
            >
              <Sparkles className="w-3 h-3 text-sky-400" />
              <span>Thought for {reasoning?.latency_ms ?? 0}ms</span>
              {reasoning?.steps && reasoning.steps.length > 0 && (
                <span className="text-zinc-500">• {reasoning.steps.length} reasoning steps</span>
              )}
              <ChevronDown className={`w-3 h-3 text-zinc-500 transition-transform duration-200 ${showThinking ? 'rotate-180' : ''}`} />
            </button>

            {showThinking && (
              <div className="p-3 rounded-xl bg-zinc-950/80 border border-zinc-800/80 text-xs space-y-2.5 animate-in fade-in duration-200 shadow-inner">
                {/* Chain-of-Thought (CoT) */}
                {reasoning?.thought_summary && (
                  <div className="p-2.5 rounded-lg bg-sky-950/20 border border-sky-800/40 text-sky-200/90 space-y-1">
                    <div className="font-semibold text-sky-300 text-[10px] uppercase tracking-wider flex items-center gap-1.5 font-mono">
                      <Sparkles className="w-3 h-3 text-sky-400" />
                      <span>Model Chain-of-Thought</span>
                    </div>
                    <p className="text-[11px] leading-relaxed text-zinc-300 italic">
                      "{reasoning.thought_summary}"
                    </p>
                  </div>
                )}

                {/* Steps Details */}
                {reasoning?.steps && reasoning.steps.length > 0 && (
                  <div className="space-y-1.5">
                    {reasoning.steps.map((step) => (
                      <div
                        key={step.step_num}
                        className="p-2 rounded-lg bg-zinc-900/70 border border-zinc-800/60 space-y-1 text-xs"
                      >
                        <div className="flex items-center justify-between text-[11px]">
                          <span className="font-semibold text-zinc-200 flex items-center gap-1.5">
                            <span className="w-4 h-4 rounded-full bg-zinc-800 text-sky-400 flex items-center justify-center text-[10px] font-mono">
                              {step.step_num}
                            </span>
                            <span>{step.title}</span>
                          </span>
                          <span className="text-[10px] font-mono uppercase text-zinc-500">
                            {step.type}
                          </span>
                        </div>
                        <p className="text-[11px] text-zinc-400 leading-normal">{step.description}</p>

                        {/* Tool Execution Chips */}
                        {step.details?.tools && Array.isArray(step.details.tools) && (
                          <div className="pt-1 space-y-1">
                            {step.details.tools.map((t: any, idx: number) => (
                              <div
                                key={idx}
                                className="p-1.5 rounded-md bg-zinc-950/80 border border-zinc-800/80 text-[10px] font-mono flex items-center justify-between"
                              >
                                <span className="flex items-center gap-1.5 text-amber-300 font-semibold">
                                  <Wrench className="w-2.5 h-2.5 text-amber-400" />
                                  <span>{t.tool}</span>
                                </span>
                                <div className="flex items-center gap-2 text-zinc-400">
                                  <span>{t.duration_ms}ms</span>
                                  <span
                                    className={`px-1.5 py-0.2 rounded text-[9px] uppercase font-bold ${
                                      t.status === 'ok'
                                        ? 'bg-emerald-950/80 text-emerald-400 border border-emerald-800/40'
                                        : 'bg-rose-950/80 text-rose-400 border border-rose-800/40'
                                    }`}
                                  >
                                    {t.status}
                                  </span>
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Sentinel HITL Proposal Card */}
        {!isUser && message.activeProposal && (
          <div className="p-3.5 rounded-xl bg-amber-950/20 border border-amber-500/50 text-zinc-200 space-y-2 text-xs shadow-lg shadow-amber-950/10 ring-1 ring-amber-500/20">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5 text-amber-300 font-semibold">
                <ShieldCheck className="w-4 h-4 text-amber-400" />
                <span>Sentinel Gatekeeper Proposal</span>
                <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-amber-900/50 text-amber-200 border border-amber-600/50 uppercase font-bold">
                  {message.activeProposal.mutation_type || 'MUTATION'}
                </span>
              </div>
              <span className="text-[10px] font-mono text-zinc-400 bg-zinc-900 px-2 py-0.5 rounded border border-zinc-800" title={message.activeProposal.payload_hash || ''}>
                SHA256: {(message.activeProposal.payload_hash || '--------').slice(0, 10)}...
              </span>
            </div>

            <div className="flex items-center justify-between text-zinc-200 text-xs py-1">
              <div>
                <span className="font-semibold text-white">
                  {message.activeProposal.provider_name || message.activeProposal.provider_id || 'Selected Provider'}
                </span>
                <span className="text-zinc-400 ml-2">@ {message.activeProposal.slot || 'Pending Slot'}</span>
              </div>
              <span className="font-mono font-bold text-emerald-400 text-sm">
                ₹{message.activeProposal.final_price?.toLocaleString() || '0'}
              </span>
            </div>

            {message.activeProposal.price_drift_delta ? (
              <div className="text-[11px] text-rose-300 bg-rose-950/40 p-2 rounded-lg border border-rose-800/50 font-mono">
                ⚠️ Price drift detected: +₹{message.activeProposal.price_drift_delta}. Prior approval invalidated!
              </div>
            ) : (
              <div className="text-[11px] text-zinc-400 flex items-center justify-between border-t border-amber-800/30 pt-1.5">
                <span>Awaiting explicit user affirmation before committing mutation.</span>
                <span className="text-[10px] text-amber-400 font-mono font-semibold">Reply 'Yes, confirm'</span>
              </div>
            )}
          </div>
        )}

        {/* Rich Provider Cards (Collapsible for Past Messages) */}
        {!isUser && reasoning?.rich_providers && reasoning.rich_providers.length > 0 && (
          <div className="space-y-1.5 pt-1">
            {!isLatest && (
              <button
                onClick={() => setShowPastProviders(!showPastProviders)}
                className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-[11px] font-mono text-zinc-400 hover:text-zinc-200 bg-zinc-900/60 border border-zinc-800/60 transition cursor-pointer select-none"
              >
                <Layers className="w-3 h-3 text-sky-400" />
                <span>Provider Candidates Evaluated ({reasoning.rich_providers.length})</span>
                <ChevronDown className={`w-3 h-3 text-zinc-500 transition-transform ${showPastProviders ? 'rotate-180' : ''}`} />
              </button>
            )}

            {showPastProviders && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 pt-1 animate-in fade-in duration-200">
                {reasoning.rich_providers.map((p) => (
                  <div
                    key={p.provider_id}
                    className="p-3 rounded-xl bg-zinc-900/90 border border-zinc-800 hover:border-zinc-700 transition space-y-2 text-xs shadow-sm backdrop-blur-sm"
                  >
                    <div className="flex items-start justify-between">
                      <div>
                        <div className="font-semibold text-zinc-100 flex items-center gap-1">
                          <span>{p.name}</span>
                          <CheckCircle className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                        </div>
                        <div className="text-[11px] text-zinc-400 capitalize">{p.service.replace(/_/g, ' ')}</div>
                      </div>
                      <div className="text-right">
                        <div className="font-mono font-bold text-emerald-400 text-sm">
                          ₹{p.price?.toLocaleString() || '0'}
                        </div>
                        <div className="flex items-center justify-end gap-1 text-[11px] text-amber-400">
                          <Star className="w-3 h-3 fill-current" />
                          <span>{p.rating}</span>
                        </div>
                      </div>
                    </div>

                    {/* Available Slots */}
                    {p.available_slots && p.available_slots.length > 0 && (
                      <div className="space-y-1">
                        <span className="text-[10px] font-medium text-zinc-400 uppercase tracking-wider font-mono">
                          Available Slots:
                        </span>
                        <div className="flex flex-wrap gap-1.5">
                          {p.available_slots.slice(0, 4).map((slot, idx) => (
                            <button
                              key={idx}
                              type="button"
                              onClick={() => onSelectSlot && onSelectSlot(p, slot)}
                              className="px-2 py-0.5 rounded-md bg-zinc-950 hover:bg-sky-950 hover:border-sky-500 hover:text-sky-300 border border-zinc-800 text-[10px] font-mono text-zinc-300 transition cursor-pointer flex items-center gap-1 active:scale-95 shadow-sm"
                              title={`Select ${p.name} for ${slot}`}
                            >
                              <span>{slot}</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Untrusted Review Isolation Shield */}
                    {p.reviews && p.reviews.length > 0 && (
                      <div className="pt-1.5 border-t border-zinc-800/60 space-y-1">
                        <div className="flex items-center justify-between text-[10px] text-zinc-400 font-medium">
                          <div className="flex items-center gap-1">
                            <ShieldAlert className="w-3 h-3 text-sky-400" />
                            <span>Untrusted Provider Review:</span>
                          </div>
                          {typeof p.reviews[0] === 'object' && p.reviews[0]?.prompt_injection_flagged && (
                            <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-rose-950/80 text-rose-300 border border-rose-800 font-semibold">
                              ⚠️ Security Flagged
                            </span>
                          )}
                        </div>
                        <div className="text-[10px] italic text-zinc-400 bg-zinc-950/70 p-2 rounded-lg border border-zinc-800/80 line-clamp-2">
                          {typeof p.reviews[0] === 'object' && p.reviews[0]?.author && (
                            <span className="not-italic font-semibold text-zinc-300 mr-1">
                              {p.reviews[0].author}:
                            </span>
                          )}
                          "{renderReviewText(p.reviews[0])}"
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* User Avatar */}
      {isUser && (
        <div className="w-8 h-8 rounded-xl bg-zinc-800 flex items-center justify-center text-zinc-300 shrink-0 border border-zinc-700 shadow-md">
          <User className="w-4 h-4" />
        </div>
      )}
    </div>
  );
};
