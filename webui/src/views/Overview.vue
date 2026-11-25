<script setup lang="ts">
import { onMounted, ref } from "vue";
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
</script>

<template>
  <div>
    <div class="d-flex align-center justify-space-between mb-4">
      <div>
        <div class="text-h5">运行概览</div>
        <div class="text-medium-emphasis">记忆规模、作用域与索引健康状态。</div>
      </div>
      <v-btn variant="tonal" :loading="loading" @click="load">刷新</v-btn>
    </div>

    <v-row dense>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="tonal">
          <v-card-text>
            <div class="text-caption text-medium-emphasis">记忆总量</div>
            <div class="text-h4">{{ overview?.memories?.total ?? 0 }}</div>
          </v-card-text>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="tonal">
          <v-card-text>
            <div class="text-caption text-medium-emphasis">个人作用域</div>
            <div class="text-h4">{{ scopeRow("PERSONAL").enabled }} / {{ scopeRow("PERSONAL").total }}</div>
          </v-card-text>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="tonal">
          <v-card-text>
            <div class="text-caption text-medium-emphasis">群作用域</div>
            <div class="text-h4">{{ scopeRow("GROUP").enabled }} / {{ scopeRow("GROUP").total }}</div>
          </v-card-text>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="tonal">
          <v-card-text>
            <div class="text-caption text-medium-emphasis">待确认候选</div>
            <div class="text-h4">{{ overview?.candidates?.total ?? 0 }}</div>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-row class="mt-2">
      <v-col cols="12" md="6">
        <v-card>
          <v-card-title>最近操作</v-card-title>
          <v-list density="compact">
            <v-list-item
              v-for="item in overview?.recent_operations ?? []"
              :key="`${item.occurred_at}-${item.operation_type}`"
              :title="`${item.operation_type} · ${item.scope_type}`"
              :subtitle="item.occurred_at"
            />
            <v-list-item v-if="!(overview?.recent_operations ?? []).length" title="暂无操作记录" />
          </v-list>
        </v-card>
      </v-col>
      <v-col cols="12" md="6">
        <v-card>
          <v-card-title>索引状态</v-card-title>
          <v-card-text>
            <pre class="code">{{ JSON.stringify(maintenance?.index ?? {}, null, 2) }}</pre>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>
  </div>
</template>

<style scoped>
.code {
  white-space: pre-wrap;
  font-size: 12px;
  max-height: 280px;
  overflow: auto;
}
</style>
