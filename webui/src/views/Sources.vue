<script setup lang="ts">
import { computed, ref } from "vue";
import MemoryPicker from "../components/MemoryPicker.vue";
import { api, groupParams, MemoryItem } from "../api/weave";
import { currentGroup } from "../store";
import { reportError } from "../toast";

const item = ref<MemoryItem | null>(null);
const sources = ref<Record<string, any>[]>([]);
const group = computed(() => currentGroup());

async function select(next: MemoryItem | null): Promise<void> {
  item.value = next;
  sources.value = [];
  if (!next || !group.value) return;
  try {
    const payload = await api.history(next.id, groupParams(group.value));
    sources.value = payload.sources ?? [];
  } catch (error) {
    reportError(error);
  }
}
</script>

<template>
  <div>
    <div class="page-heading"><div><h1 class="text-h5">来源证据 <span class="heading-sparkle" aria-hidden="true">⌁</span></h1><p>记忆的来处：每一份记忆都有自己的来源，线索也值得被认真保存。</p></div></div>
    <MemoryPicker @select="select" />
    <v-card class="weave-panel">
      <v-card-text v-if="!item" class="weave-empty"><div class="source-illustration" aria-hidden="true">⌁</div>选择一条群记忆，就能查看它关联的来源证据。</v-card-text>
      <div v-else class="weave-table-wrap"><v-table density="compact">
        <thead>
          <tr><th>来源类型</th><th>操作者</th><th>时间</th><th>evidence_ref</th></tr>
        </thead>
        <tbody>
          <tr v-for="source in sources" :key="source.id ?? source.captured_at">
            <td>{{ source.source_type }}</td>
            <td>{{ source.operator_type }} · {{ source.operator_id }}</td>
            <td>{{ source.captured_at }}</td>
            <td>{{ source.evidence_ref ?? "—" }}</td>
          </tr>
        </tbody>
      </v-table></div>
    </v-card>
  </div>
</template>

<style scoped>
.heading-sparkle, .source-illustration { color: var(--weave-accent); }
.source-illustration { margin-bottom: 8px; font-size: 28px; }
</style>
