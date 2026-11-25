<script setup lang="ts">
import { computed, ref } from "vue";
import { api, groupParams } from "../api/weave";
import { currentGroup, state } from "../store";
import { reportError } from "../toast";

const query = ref("");
const limit = ref(8);
const result = ref<Record<string, any> | null>(null);
const loading = ref(false);
const group = computed(() => currentGroup());

async function run(): Promise<void> {
  if (!group.value || !query.value.trim()) return;
  loading.value = true;
  try {
    result.value = await api.debug({
      ...groupParams(group.value), query: query.value.trim(), limit: limit.value,
    });
  } catch (error) {
    reportError(error);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div>
    <div class="page-heading">
      <div><h1 class="text-h5">检索调试 <span class="heading-sparkle" aria-hidden="true">⌕</span></h1><p>检索实验室：只读模拟检索，不更新访问时间，不写诊断记录，也不返回个人记忆。</p></div>
    </div>
    <v-card class="mb-4 weave-panel">
      <v-card-text class="d-flex flex-wrap ga-3 align-center">
        <v-select
          v-model="state.groupIndex"
          :items="state.groups"
          item-title="group_id"
          label="群作用域"
          density="compact"
          hide-details
          style="max-width: 240px"
        />
        <v-text-field
          v-model="query"
          label="查询"
          density="compact"
          hide-details
          style="max-width: 420px"
          @keydown.enter="run"
        />
        <v-select
          v-model="limit"
          :items="[5, 8, 12, 20]"
          label="候选上限"
          density="compact"
          hide-details
          style="max-width: 130px"
        />
        <v-btn color="primary" :loading="loading" @click="run">运行</v-btn>
      </v-card-text>
    </v-card>

    <v-card v-if="result" class="weave-panel">
      <v-card-title class="d-flex justify-space-between">
        <span>结果</span>
        <span class="text-caption text-medium-emphasis">{{ result.estimated_tokens }} tokens</span>
      </v-card-title>
      <v-card-text>
        <div class="injected-summary mb-3">
          <span class="summary-label"><span aria-hidden="true">✦</span> 最终注入</span>
          <span>{{ (result.injected_ids ?? []).join(", ") || "无" }}</span>
        </div>
        <div class="weave-table-wrap"><v-table density="compact">
          <thead>
            <tr><th>排名</th><th>ID</th><th>BM25</th><th>过滤原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="candidate in result.candidates ?? []" :key="candidate.id">
              <td>{{ candidate.rank }}</td>
              <td>{{ candidate.id }}</td>
              <td>{{ candidate.bm25_score ?? "—" }}</td>
              <td>{{ candidate.filter_reason }}</td>
            </tr>
          </tbody>
        </v-table></div>
      </v-card-text>
    </v-card>
  </div>
</template>

<style scoped>
.heading-sparkle { color: var(--weave-accent); }
.injected-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 12px 14px; border: 1px solid var(--weave-line); border-radius: 13px; background: var(--weave-soft-panel); color: var(--weave-text); font-size: 12px; overflow-wrap: anywhere; }
.summary-label { color: var(--weave-accent); font-weight: 800; }
</style>
