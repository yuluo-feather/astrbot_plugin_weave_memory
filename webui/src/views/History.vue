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
    <div class="page-heading"><div><h1 class="text-h5">历史版本 <span class="heading-sparkle" aria-hidden="true">◷</span></h1><p>记忆时间胶囊：每一次修改都会留下痕迹，随时回看记忆的来时路。</p></div></div>
    <MemoryPicker @select="select" />
    <v-card class="weave-panel">
      <v-card-text v-if="!item" class="weave-empty"><div class="empty-history" aria-hidden="true">◷</div>先选择一条群记忆，再来看看它经历过哪些变化。</v-card-text>
      <v-card-text v-else-if="!revisions.length" class="weave-empty">这条记忆暂时还没有可展示的历史版本。</v-card-text>
      <v-card-text v-else class="revision-timeline">
        <article v-for="revision in revisions" :key="revision.revision" class="revision-entry">
          <div class="revision-marker" />
          <div class="revision-topline">
            <strong>Revision {{ revision.revision }}</strong>
            <v-chip size="x-small" variant="tonal" color="primary">{{ revision.status ?? "版本记录" }}</v-chip>
          </div>
          <div class="revision-meta">{{ revision.changed_at }} <span>·</span> {{ revision.change_reason }}</div>
          <div class="revision-content">{{ revision.content }}</div>
        </article>
      </v-card-text>
    </v-card>
  </div>
</template>

<style scoped>
.heading-sparkle, .empty-history { color: var(--weave-accent); }
.empty-history { margin-bottom: 8px; font-size: 28px; }
.revision-timeline { max-height: 68vh; overflow-y: auto; padding: 18px 24px 8px !important; }
.revision-entry { position: relative; margin-left: 7px; padding: 0 0 23px 23px; border-left: 2px solid var(--weave-track); }
.revision-entry:last-child { border-left-color: transparent; padding-bottom: 4px; }
.revision-marker { position: absolute; top: 1px; left: -7px; width: 12px; height: 12px; border: 3px solid var(--weave-ring); border-radius: 50%; background: var(--weave-accent); box-shadow: 0 0 0 1px var(--weave-track); }
.revision-topline { display: flex; align-items: center; gap: 10px; color: var(--weave-text-strong); font-size: 13px; }
.revision-meta { margin-top: 5px; color: var(--weave-muted); font-size: 11px; }
.revision-content { margin-top: 10px; padding: 12px 14px; border: 1px solid var(--weave-line-soft); border-radius: 12px; background: var(--weave-soft-panel); color: var(--weave-text); font-size: 12px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
