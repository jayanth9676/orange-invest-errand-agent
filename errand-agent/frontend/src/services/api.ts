import { StateSnapshot, ApprovalProposal } from '../types';

export interface ChatOptions {
  force_book_outcome?: string | null;
  force_price_drift_delta?: number | null;
  inject_adversarial_review?: boolean | null;
  persistent_cancel_503?: boolean | null;
}

export const api = {
  async getState(tenantId = 'tenant_default', sessionId = 'live_demo_session'): Promise<StateSnapshot> {
    const res = await fetch('/api/state', {
      headers: {
        'X-Tenant-ID': tenantId,
        'X-Session-ID': sessionId,
      },
    });
    if (!res.ok) throw new Error(`Failed to fetch state: ${res.statusText}`);
    return res.json();
  },

  async sendChat(
    message: string,
    options: ChatOptions = {},
    tenantId = 'tenant_default',
    sessionId = 'live_demo_session'
  ): Promise<StateSnapshot> {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Tenant-ID': tenantId,
        'X-Session-ID': sessionId,
      },
      body: JSON.stringify({
        message,
        ...options,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || err.title || 'Error processing chat message');
    }
    return res.json();
  },

  async executeApprovalAction(
    proposalId: string,
    action: 'approve' | 'reject',
    reason?: string,
    tenantId = 'tenant_default',
    sessionId = 'live_demo_session'
  ): Promise<StateSnapshot> {
    const res = await fetch('/api/approval/action', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Tenant-ID': tenantId,
        'X-Session-ID': sessionId,
      },
      body: JSON.stringify({
        proposal_id: proposalId,
        action,
        reason,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || err.title || `Approval action ${action} failed`);
    }
    return res.json();
  },

  async memoryAction(
    action: 'inspect' | 'forget' | 'confirm' | 'update',
    key = '',
    value = '',
    tenantId = 'tenant_default',
    sessionId = 'live_demo_session'
  ): Promise<StateSnapshot> {
    const res = await fetch('/api/memory', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Tenant-ID': tenantId,
        'X-Session-ID': sessionId,
      },
      body: JSON.stringify({ action, key, value }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `Memory operation failed: ${res.statusText}`);
    }
    return res.json();
  },

  async triggerProactiveCancel(
    bookingId = 'bk_upcoming_ac_01',
    tenantId = 'tenant_default',
    sessionId = 'live_demo_session'
  ): Promise<StateSnapshot> {
    const res = await fetch('/api/proactive-cancel', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Tenant-ID': tenantId,
        'X-Session-ID': sessionId,
      },
      body: JSON.stringify({ booking_id: bookingId }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Proactive cancellation trigger failed');
    }
    return res.json();
  },

  async resetSession(tenantId = 'tenant_default', sessionId = 'live_demo_session'): Promise<any> {
    const res = await fetch('/api/reset', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Tenant-ID': tenantId,
        'X-Session-ID': sessionId,
      },
    });
    if (!res.ok) throw new Error('Reset failed');
    return res.json();
  },

  async runEvals(): Promise<{ total: number; passed: number; failed: number; results: any[] }> {
    const res = await fetch('/api/run-evals', { method: 'POST' });
    if (!res.ok) throw new Error('Failed to run eval suite');
    return res.json();
  },

  async getMcpTools(): Promise<{ tools: any[] }> {
    const res = await fetch('/mcp/tools');
    if (!res.ok) throw new Error('Failed to fetch MCP tools');
    return res.json();
  },

  async callMcpTool(name: string, args: Record<string, any> = {}, role = 'Admin'): Promise<any> {
    const res = await fetch('/mcp/call', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-User-Role': role,
      },
      body: JSON.stringify({ name, arguments: args, role }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || err.title || 'MCP tool execution failed');
    }
    return res.json();
  },

  async callMcpRpc(method: string, params: Record<string, any> = {}, id = 1): Promise<any> {
    const res = await fetch('/mcp/rpc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id, method, params }),
    });
    return res.json();
  },

  async getHealth(): Promise<any> {
    const res = await fetch('/healthz');
    return res.json();
  },
};
