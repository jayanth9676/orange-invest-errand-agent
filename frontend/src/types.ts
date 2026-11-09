export type AgentState =
  | 'IDLE'
  | 'COLLECTING_SLOTS'
  | 'SEARCHING_PROVIDERS'
  | 'HOLDING_SLOT'
  | 'AWAITING_APPROVAL'
  | 'EXECUTING_MUTATION'
  | 'RECONCILING_TIMEOUT'
  | 'PROACTIVE_RECOVERY'
  | 'COMPLETED'
  | 'SAFE_ABORT';

export interface IntentSlots {
  service: string | null;
  area: string | null;
  date_window: string | null;
  time_window: string | null;
  max_budget: number | null;
  requested_slot: string | null;
  target_booking_id: string | null;
  preferred_provider_id: string | null;
  preferences: Record<string, string>;
  intent_type?: string | null;
}

export interface ApprovalProposal {
  proposal_id: string;
  mutation_type: string;
  provider_id: string;
  provider_name?: string;
  booking_id?: string | null;
  slot: string;
  final_price: number;
  original_price?: number;
  proposed_at_turn: number;
  approved_at_turn?: number | null;
  created_at: number;
  created_at_epoch?: number;
  ttl_seconds: number;
  payload_hash: string;
  invalidated: boolean;
  invalidation_reason?: string | null;
  price_drift_delta?: number;
}

export interface TraceEvent {
  trace_id: string;
  span_id?: string;
  parent_span_id?: string | null;
  event_type: 'state_transition' | 'model_call' | 'tool_call' | 'guardrail_event' | string;
  status?: 'ok' | 'error' | string;
  latency_ms?: number;
  duration_ms?: number;
  model?: string;
  prompt_tokens?: number;
  completion_tokens?: number;
  tool_name?: string;
  arguments?: Record<string, any>;
  sanitized_result?: any;
  from_state?: string;
  to_state?: string;
  reason?: string;
  guardrail?: string;
  action?: string;
  details?: any;
  timestamp?: number;
}

export interface MemoryItem {
  memory_id: string;
  user_id: string;
  key: string;
  value: string;
  source_type: 'EXPLICIT' | 'INFERRED';
  confirmed: number;
  forgotten: number;
  updated_at: number;
}

export interface MemoryPartitions {
  explicit_count: number;
  confirmed_inferred_count: number;
  unconfirmed_inferred_count: number;
  forgotten_count: number;
  total_count: number;
}

export interface Booking {
  booking_id: string;
  user_id: string;
  provider_id: string;
  provider_name: string;
  service: string;
  slot: string;
  price: number;
  status: 'CONFIRMED' | 'CANCELLED' | 'RESCHEDULED' | string;
  created_at: number;
}

export interface Hold {
  hold_id: string;
  user_id: string;
  provider_id: string;
  slot: string;
  quoted_price: number;
  status: 'ACTIVE' | 'USED' | 'EXPIRED' | string;
  created_at: number;
  expires_at: number;
}

export interface ContextAudit {
  session_id: string;
  max_tokens: number;
  actual_tokens: number;
  included_memory_ids: string[];
  excluded_memory_ids: string[];
  exclusion_diagnostics?: Record<string, string>;
  excluded_memories?: Array<{ key: string; memory_id: string; reason: string }>;
  created_at: number;
}

export interface ReasoningStep {
  step_num: number;
  type: 'intent_extraction' | 'guardrail_verification' | 'tool_execution' | 'state_transition' | string;
  title: string;
  description: string;
  details?: any;
}

export interface ReasoningPayload {
  thought_summary: string;
  model: string;
  latency_ms: number;
  tokens: {
    prompt: number;
    completion: number;
    total: number;
    cap: number;
    utilization_pct: number;
  };
  steps: ReasoningStep[];
  tools_executed: string[];
  rich_providers?: Array<{
    provider_id: string;
    name: string;
    service: string;
    rating: number;
    price: number;
    available_slots: string[];
    reviews?: Array<string | { author?: string; isolated_review_data?: string; prompt_injection_flagged?: boolean; text?: string; [key: string]: any }>;
    verified?: boolean;
  }>;
  fsm_state: AgentState;
  active_proposal?: ApprovalProposal | null;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: Date;
  reasoning?: ReasoningPayload;
  activeProposal?: ApprovalProposal | null;
  status?: 'sending' | 'delivered' | 'error';
}

export interface StateSnapshot {
  tenant_id: string;
  session_id: string;
  fsm_state: AgentState;
  turn_counter: number;
  slots: IntentSlots;
  missing_fields: string[];
  active_proposal: ApprovalProposal | null;
  trace_events: TraceEvent[];
  trace_id: string;
  otel_traceparent: string;
  bookings: Booking[];
  holds: Hold[];
  journal: any[];
  core_memories: MemoryItem[];
  all_memories: MemoryItem[];
  memory_partitions: MemoryPartitions;
  exclusion_diagnostics: Record<string, string>;
  total_memories_seeded: number;
  latest_context_audit: ContextAudit | null;
  mcp_tools_count: number;
  limits: {
    max_model_turns_per_run: number;
    max_tool_calls_per_run: number;
    max_wall_clock_seconds: number;
    max_context_tokens: number;
    max_memory_tokens: number;
  };
  reply?: string;
  agent_reasoning?: ReasoningPayload;
}
