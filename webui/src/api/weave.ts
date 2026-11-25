import { apiGet, apiPost } from "./bridge";

export interface MemoryItem {
  id: string;
  memory_type: string;
  status: string;
  source_type: string;
  content: string;
  current_revision: number;
  created_at: string;
  updated_at: string;
  [key: string]: unknown;
}

export interface GroupItem {
  scope_id: string;
  platform: string;
  tenant_id: string;
  group_id: string;
  enabled: boolean;
  active_count: number;
}

export interface Capabilities {
  webui: number;
  write: boolean;
  admin: boolean;
  personal: boolean;
  bound: boolean;
  csrf_required: boolean;
  nonce_required: string[];
}

export interface SettingsItem {
  key: string;
  type: string;
  default: unknown;
  important: boolean;
  writable: boolean;
  hot_apply: boolean;
}

let csrfToken = "";

export async function ensureCsrf(force = false): Promise<string> {
  if (!csrfToken || force) {
    const payload = (await apiGet("v1/ui/csrf")) as { csrf_token: string };
    csrfToken = payload.csrf_token;
  }
  return csrfToken;
}

async function write(endpoint: string, body: Record<string, unknown> = {}): Promise<unknown> {
  try {
    return await apiPost(endpoint, { ...body, csrf_token: await ensureCsrf() });
  } catch (error) {
    // A stale token after a plugin restart is the common cause; retry once.
    if (String(error).includes("csrf")) {
      return await apiPost(endpoint, {
        ...body, csrf_token: await ensureCsrf(true),
      });
    }
    throw error;
  }
}

export async function requestNonce(action: string, target: string): Promise<string> {
  const payload = (await write("v1/ui/nonce", { action, target })) as { nonce: string };
  return payload.nonce;
}

export const api = {
  capabilities: () => apiGet("v1/webui/capabilities") as Promise<Capabilities>,
  overview: () => apiGet("v1/overview") as Promise<Record<string, any>>,
  maintenanceStatus: () => apiGet("v1/maintenance/status") as Promise<Record<string, unknown>>,
  groups: () => apiGet("v1/groups") as Promise<{ items: GroupItem[] }>,
  memories: (params: Record<string, unknown>) =>
    apiGet("v1/memories", params) as Promise<{ items: MemoryItem[]; total: number; next_cursor: string | null }>,
  archived: (params: Record<string, unknown>) =>
    apiGet("v1/archived-memories", params) as Promise<{ items: MemoryItem[]; total: number }>,
  memory: (id: string, params: Record<string, unknown>) =>
    apiGet(`v1/memories/${encodeURIComponent(id)}`, params) as Promise<MemoryItem>,
  history: (id: string, params: Record<string, unknown>) =>
    apiGet(`v1/memories/${encodeURIComponent(id)}/history`, params) as Promise<Record<string, any>>,
  operations: (id: string, params: Record<string, unknown>) =>
    apiGet(`v1/memories/${encodeURIComponent(id)}/operations`, params) as Promise<{ items: Record<string, unknown>[] }>,
  debug: (body: Record<string, unknown>) =>
    apiPost("v1/retrieval/debug", body) as Promise<Record<string, any>>,
  settings: () => apiGet("v1/settings") as Promise<Record<string, unknown>>,
  settingsSchema: () => apiGet("v1/settings/schema") as Promise<{ items: SettingsItem[] }>,
  backups: () => apiGet("v1/maintenance/backups") as Promise<{ items: Record<string, unknown>[] }>,
  groupPersonalStatus: (params: Record<string, unknown>) =>
    apiGet("v1/group-personal/status", params) as Promise<Record<string, unknown>>,
  bindingStatus: () => apiGet("v1/webui/binding") as Promise<Record<string, unknown>>,
  personalMemories: (params: Record<string, unknown>) =>
    apiGet("v1/personal/memories", params) as Promise<{ items: MemoryItem[]; total: number }>,
  personalCandidates: () =>
    apiGet("v1/personal/candidates") as Promise<{ items: MemoryItem[]; total: number }>,
  updateSettings: (values: Record<string, unknown>, nonce?: string) =>
    write("v1/settings", { values, ...(nonce ? { nonce } : {}) }),
  createMemory: (body: Record<string, unknown>) => write("v1/memories", body),
  updateMemory: (id: string, body: Record<string, unknown>) =>
    write(`v1/memories/${encodeURIComponent(id)}/update`, body),
  archiveMemory: (id: string, body: Record<string, unknown>) =>
    write(`v1/memories/${encodeURIComponent(id)}/archive`, body),
  restoreMemory: (id: string, body: Record<string, unknown>, nonce: string) =>
    write(`v1/memories/${encodeURIComponent(id)}/restore`, { ...body, nonce }),
  purgeMemory: (id: string, body: Record<string, unknown>, nonce: string) =>
    write(`v1/memories/${encodeURIComponent(id)}/purge`, {
      ...body, nonce, confirm: `PURGE ${id}`,
    }),
  bind: (code: string) => write("v1/webui/binding", { code }),
  unbind: () => write("v1/webui/binding/unbind"),
  personalUpdate: (id: string, body: Record<string, unknown>) =>
    write(`v1/personal/memories/${encodeURIComponent(id)}/update`, body),
  personalArchive: (id: string) => write(`v1/personal/memories/${encodeURIComponent(id)}/archive`),
  personalRestore: (id: string) => write(`v1/personal/memories/${encodeURIComponent(id)}/restore`),
  personalConfirm: (id: string, revision: number) =>
    write(`v1/personal/candidates/${encodeURIComponent(id)}/confirm`, {
      expected_revision: revision,
    }),
  personalReject: (id: string, revision: number, reason = "") =>
    write(`v1/personal/candidates/${encodeURIComponent(id)}/reject`, {
      expected_revision: revision, reason,
    }),
  createBackup: () => write("v1/maintenance/backup", { confirm: true }),
  reindex: () => write("v1/maintenance/reindex", { confirm: true }),
  restoreBackup: (body: Record<string, unknown>, nonce?: string) =>
    write("v1/maintenance/restore", { ...body, ...(nonce ? { nonce } : {}) }),
};

export function groupParams(group: GroupItem): Record<string, string> {
  return {
    platform: group.platform,
    tenant_id: group.tenant_id,
    group_id: group.group_id,
  };
}
