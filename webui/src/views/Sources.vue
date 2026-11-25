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
    <div class="text-h5 mb-3">来源证据</div>
    <MemoryPicker @select="select" />
    <v-card>
      <v-card-text v-if="!item" class="text-medium-emphasis">选择一条群记忆查看来源。</v-card-text>
      <v-table v-else density="compact">
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
      </v-table>
    </v-card>
  </div>
</template>
