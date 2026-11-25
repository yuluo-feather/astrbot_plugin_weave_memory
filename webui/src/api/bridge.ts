export interface PluginContext {
  pluginName?: string;
  displayName?: string;
  pageName?: string;
  pageTitle?: string;
  locale?: string;
  isDark?: boolean;
}

interface AstrBotBridge {
  ready(): Promise<void>;
  getContext(): PluginContext | null;
  onContext(handler: (context: PluginContext) => void): () => void;
  apiGet(endpoint: string, params?: Record<string, unknown>): Promise<unknown>;
  apiPost(endpoint: string, body?: unknown): Promise<unknown>;
}

declare global {
  interface Window {
    AstrBotPluginView?: AstrBotBridge;
  }
}

const bridge = window.AstrBotPluginView;

export function hasBridge(): boolean {
  return Boolean(bridge);
}

export async function waitReady(): Promise<void> {
  if (bridge) {
    await bridge.ready();
  }
}

export function isDark(): boolean {
  return Boolean(bridge?.getContext()?.isDark);
}

export function onContextChange(handler: (context: PluginContext) => void): void {
  bridge?.onContext(handler);
}

function requireBridge(): AstrBotBridge {
  if (!bridge) {
    throw new Error("请从 AstrBot 插件详情页打开织忆控制台");
  }
  return bridge;
}

export async function apiGet(
  endpoint: string,
  params?: Record<string, unknown>,
): Promise<unknown> {
  return requireBridge().apiGet(endpoint, params);
}

export async function apiPost(endpoint: string, body?: unknown): Promise<unknown> {
  return requireBridge().apiPost(endpoint, body);
}
