<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, groupParams, MemoryItem } from "../api/weave";
import { currentGroup, state } from "../store";
import { reportError } from "../toast";

const emit = defineEmits<{ (event: "select", item: MemoryItem | null): void }>();

const items = ref<MemoryItem[]>([]);
const selectedId = ref<string>("");
const group = computed(() => currentGroup());

async function load(): Promise<void> {
  if (!group.value) return;
  try {
    items.value = (await api.memories({ ...groupParams(group.value), limit: 100 })).items;
    selectedId.value = "";
    emit("select", null);
  } catch (error) {
    reportError(error);
  }
}

function change(): void {
  emit("select", items.value.find((item) => item.id === selectedId.value) ?? null);
}

watch(() => state.groupIndex, load);
onMounted(load);
</script>

<template>
  <v-card class="mb-4 weave-panel">
    <v-card-text class="d-flex flex-wrap ga-3 align-center">
      <v-select
        v-model="state.groupIndex"
        :items="state.groups"
        item-title="group_id"
        label="群作用域"
        density="compact"
        hide-details
        style="max-width: 260px"
      />
      <v-select
        v-model="selectedId"
        :items="items"
        item-title="content"
        item-value="id"
        label="记忆"
        density="compact"
        hide-details
        style="max-width: 520px"
        @update:model-value="change"
      />
      <v-btn variant="tonal" @click="load">刷新</v-btn>
    </v-card-text>
  </v-card>
</template>
