<script setup lang="ts">
import { computed, ref } from "vue";
import MemoryPicker from "../components/MemoryPicker.vue";
import { api, groupParams, MemoryItem } from "../api/weave";
import { currentGroup } from "../store";
import { reportError } from "../toast";

const item = ref<MemoryItem | null>(null);
const revisions = ref<Record<string, any>[]>([]);
const group = computed(() => currentGroup());

async function select(next: MemoryItem | null): Promise<void> {
  item.value = next;
  revisions.value = [];
  if (!next || !group.value) return;
  try {
    const payload = await api.history(next.id, groupParams(group.value));
    revisions.value = payload.revisions ?? [];
  } catch (error) {
    reportError(error);
  }
}
</script>

<template>
  <div>
    <div class="text-h5 mb-3">历史版本</div>
    <MemoryPicker @select="select" />
    <v-card>
      <v-card-text v-if="!item" class="text-medium-emphasis">选择一条群记忆查看 revision 历史。</v-card-text>
      <v-table v-else density="compact">
        <thead>
          <tr><th>revision</th><th>时间</th><th>状态</th><th>原因</th><th>正文</th></tr>
        </thead>
        <tbody>
          <tr v-for="revision in revisions" :key="revision.revision">
            <td>r{{ revision.revision }}</td>
            <td>{{ revision.changed_at }}</td>
            <td>{{ revision.status ?? "—" }}</td>
            <td>{{ revision.change_reason }}</td>
            <td class="body">{{ revision.content }}</td>
          </tr>
        </tbody>
      </v-table>
    </v-card>
  </div>
</template>

<style scoped>
.body {
  max-width: 420px;
  white-space: pre-wrap;
}
</style>
