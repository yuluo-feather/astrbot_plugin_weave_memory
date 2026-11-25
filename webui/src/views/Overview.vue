<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api/weave";
import { reportError } from "../toast";

const overview = ref<Record<string, any> | null>(null);
const maintenance = ref<Record<string, any> | null>(null);
const loading = ref(false);

async function load(): Promise<void> {
  loading.value = true;
  try {
    overview.value = await api.overview();
    maintenance.value = await api.maintenanceStatus();
  } catch (error) {
    reportError(error);
  } finally {
    loading.value = false;
  }
}

onMounted(load);

function scopeRow(name: string): { enabled: number; total: number } {
  return overview.value?.scopes?.[name] ?? { enabled: 0, total: 0 };
}

const indexEntries = computed(() => Object.entries(maintenance.value?.index ?? {}));
function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
</script>

<template>
  <div class="overview-page">
    <div class="page-heading">
      <div>
        <h1 class="text-h5">运行概览 <span class="heading-sparkle" aria-hidden="true">✧</span></h1>
        <p>今天也把记忆照顾好啦。这里是织忆的小小状态面板，看看最近有哪些记忆正在被珍藏。</p>
      </div>
      <v-btn class="refresh-button" variant="tonal" :loading="loading" @click="load">刷新概览</v-btn>
    </div>

    <v-row class="mb-1">
      <v-col cols="12" sm="6" md="3">
        <v-card class="weave-stat-card stat-lilac"><v-card-text>
          <span class="weave-stat-icon" aria-hidden="true">▤</span><div class="weave-stat-label">记忆总量</div>
          <div class="weave-stat-value">{{ overview?.memories?.total ?? 0 }}</div>
        </v-card-text></v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card class="weave-stat-card stat-mint"><v-card-text>
          <span class="weave-stat-icon" aria-hidden="true">♡</span><div class="weave-stat-label">个人作用域</div>
          <div class="weave-stat-value">{{ scopeRow("PERSONAL").enabled }}<small> / {{ scopeRow("PERSONAL").total }}</small></div>
        </v-card-text></v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card class="weave-stat-card stat-peach"><v-card-text>
          <span class="weave-stat-icon" aria-hidden="true">⌂</span><div class="weave-stat-label">群作用域</div>
          <div class="weave-stat-value">{{ scopeRow("GROUP").enabled }}<small> / {{ scopeRow("GROUP").total }}</small></div>
        </v-card-text></v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card class="weave-stat-card stat-rose"><v-card-text>
          <span class="weave-stat-icon" aria-hidden="true">✧</span><div class="weave-stat-label">待确认候选</div>
          <div class="weave-stat-value">{{ overview?.candidates?.total ?? 0 }}</div>
        </v-card-text></v-card>
      </v-col>
    </v-row>

    <v-row class="mt-1">
      <v-col cols="12" md="7">
        <v-card class="weave-panel h-100">
          <v-card-title><span class="section-mark" aria-hidden="true">◷</span>最近操作 <v-spacer /><v-chip size="x-small" variant="tonal">近期动态</v-chip></v-card-title>
          <v-card-text v-if="!(overview?.recent_operations ?? []).length" class="weave-empty">
            <div class="empty-illustration" aria-hidden="true">✿</div>还没有新的操作记录，等一段记忆来敲门吧。
          </v-card-text>
          <v-card-text v-else class="timeline-list">
            <div v-for="item in overview?.recent_operations ?? []" :key="`${item.occurred_at}-${item.operation_type}`" class="weave-timeline-item">
              <div class="timeline-title">{{ item.operation_type }} <v-chip size="x-small" variant="tonal" color="primary">{{ item.scope_type }}</v-chip></div>
              <div class="timeline-time">{{ item.occurred_at }}</div>
            </div>
          </v-card-text>
        </v-card>
      </v-col>
      <v-col cols="12" md="5">
        <v-card class="weave-panel h-100">
          <v-card-title><span class="section-mark mint-mark" aria-hidden="true">⌘</span>索引状态</v-card-title>
          <v-card-text v-if="!indexEntries.length" class="weave-empty">暂时没有可展示的索引状态。</v-card-text>
          <v-card-text v-else class="index-status-list">
            <div v-for="([key, value]) in indexEntries" :key="key" class="index-status-row">
              <span>{{ key.replace(/_/g, " ") }}</span><strong>{{ displayValue(value) }}</strong>
            </div>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>
  </div>
</template>

<style scoped>
.heading-sparkle { color: var(--weave-accent); font-size: 17px; }
.section-mark { display: inline-grid; width: 27px; height: 27px; place-items: center; border-radius: 9px; background: var(--weave-accent-soft); color: var(--weave-accent); }
.mint-mark { background: var(--weave-mint-soft); color: var(--weave-mint); }
.weave-stat-card :deep(.v-card-text) { padding: 17px 19px !important; }
.weave-stat-card small { color: var(--weave-muted); font-size: 13px; font-weight: 600; }
.stat-lilac { background: var(--weave-stat-lilac); }
.stat-mint { background: var(--weave-stat-mint); }
.stat-peach { background: var(--weave-stat-peach); }
.stat-rose { background: var(--weave-stat-rose); }
.timeline-list { padding: 14px 22px 4px !important; }
.timeline-title { display: flex; align-items: center; gap: 9px; color: var(--weave-text-strong); font-size: 13px; font-weight: 700; }
.timeline-time { margin-top: 5px; color: var(--weave-muted); font-size: 11px; }
.index-status-list { padding-top: 8px !important; }
.index-status-row { display: flex; justify-content: space-between; gap: 14px; padding: 11px 0; border-bottom: 1px dashed var(--weave-line-soft); font-size: 12px; }
.index-status-row:last-child { border-bottom: 0; }
.index-status-row span { color: var(--weave-muted); text-transform: capitalize; }
.index-status-row strong { color: var(--weave-text-strong); font-weight: 700; text-align: right; overflow-wrap: anywhere; }
.empty-illustration { margin-bottom: 8px; color: var(--weave-accent); font-size: 28px; }
</style>
