<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, groupParams, MemoryItem, requestNonce } from "../api/weave";
import { currentGroup, state } from "../store";
import { notify, reportError } from "../toast";

const tab = ref<"active" | "archived">("active");
const type = ref("");
const query = ref("");
const items = ref<MemoryItem[]>([]);
const selected = ref<MemoryItem | null>(null);
const history = ref<Record<string, any> | null>(null);
const operations = ref<Record<string, unknown>[]>([]);
const loading = ref(false);

const createOpen = ref(false);
const createContent = ref("");
const createLabel = ref("话题");

const editOpen = ref(false);
const editContent = ref("");

const purgeOpen = ref(false);
const purgeText = ref("");

const canWrite = computed(() => Boolean(state.capabilities?.write && state.capabilities?.admin));
const group = computed(() => currentGroup());

async function load(): Promise<void> {
  if (!group.value) return;
  loading.value = true;
  try {
    const params = groupParams(group.value);
    const result = tab.value === "active"
      ? await api.memories({ ...params, memory_type: type.value || undefined, limit: 50 })
      : await api.archived({ ...params, limit: 50 });
    items.value = result.items;
    selected.value = null;
    history.value = null;
    operations.value = [];
  } catch (error) {
    reportError(error);
  } finally {
    loading.value = false;
  }
}

async function search(): Promise<void> {
  if (!group.value || !query.value.trim()) {
    await load();
    return;
  }
  loading.value = true;
  try {
    const trace = await api.debug({
      ...groupParams(group.value), query: query.value.trim(), limit: 20,
    });
    const ids: string[] = [];
    for (const id of trace.injected_ids ?? []) { if (!ids.includes(id)) ids.push(id); }
    for (const candidate of trace.candidates ?? []) {
      if (candidate.filter_reason === "eligible" && candidate.id && !ids.includes(candidate.id)) {
        ids.push(candidate.id);
      }
    }
    const loaded = await Promise.all(ids.slice(0, 20).map(async (id) => {
      try { return await api.memory(id, groupParams(group.value!)); } catch { return null; }
    }));
    items.value = loaded.filter((item): item is MemoryItem => item !== null);
    selected.value = null;
  } catch (error) {
    reportError(error);
  } finally {
    loading.value = false;
  }
}

async function open(item: MemoryItem): Promise<void> {
  if (!group.value) return;
  const params = groupParams(group.value);
  selected.value = item;
  try {
    history.value = await api.history(item.id, params);
    operations.value = (await api.operations(item.id, params)).items;
  } catch (error) {
    reportError(error);
  }
}

async function create(): Promise<void> {
  if (!group.value) return;
  try {
    await api.createMemory({
      ...groupParams(group.value), content: createContent.value, group_label: createLabel.value,
    });
    createOpen.value = false;
    createContent.value = "";
    notify("群记忆已创建");
    await load();
  } catch (error) {
    reportError(error);
  }
}

function beginEdit(): void {
  if (!selected.value) return;
  editContent.value = selected.value.content;
  editOpen.value = true;
}

async function saveEdit(): Promise<void> {
  if (!group.value || !selected.value) return;
  try {
    await api.updateMemory(selected.value.id, {
      ...groupParams(group.value),
      content: editContent.value,
      expected_revision: selected.value.current_revision,
    });
    editOpen.value = false;
    notify("已保存新版本");
    await load();
  } catch (error) {
    reportError(error);
  }
}

async function archive(): Promise<void> {
  if (!group.value || !selected.value) return;
  try {
    await api.archiveMemory(selected.value.id, groupParams(group.value));
    notify("已移入回收站");
    await load();
  } catch (error) {
    reportError(error);
  }
}

async function restore(item: MemoryItem): Promise<void> {
  if (!group.value) return;
  try {
    const nonce = await requestNonce("restore", item.id);
    await api.restoreMemory(item.id, groupParams(group.value), nonce);
    notify("已恢复");
    await load();
  } catch (error) {
    reportError(error);
  }
}

function beginPurge(item: MemoryItem): void {
  selected.value = item;
  purgeText.value = "";
  purgeOpen.value = true;
}

async function purge(): Promise<void> {
  if (!group.value || !selected.value) return;
  try {
    const nonce = await requestNonce("purge", selected.value.id);
    await api.purgeMemory(selected.value.id, groupParams(group.value), nonce);
    purgeOpen.value = false;
    notify("已彻底删除，正文不可恢复");
    await load();
  } catch (error) {
    reportError(error);
  }
}

watch([tab, type], load);
watch(() => state.groupIndex, load);
onMounted(load);
</script>

<template>
  <div>
    <div class="d-flex align-center justify-space-between mb-3">
      <div class="text-h5">记忆浏览</div>
      <v-btn v-if="canWrite" color="primary" @click="createOpen = true">新建群记忆</v-btn>
    </div>

    <v-card class="mb-3">
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
        <v-btn-toggle v-model="tab" mandatory density="compact">
          <v-btn value="active">当前</v-btn>
          <v-btn value="archived">回收站</v-btn>
        </v-btn-toggle>
        <v-select
          v-if="tab === 'active'"
          v-model="type"
          :items="['', 'FACT', 'PREFERENCE', 'EVENT']"
          label="类型"
          density="compact"
          hide-details
          style="max-width: 140px"
        />
        <v-text-field
          v-model="query"
          label="关键词 / 语义检索"
          density="compact"
          hide-details
          style="max-width: 320px"
          @keydown.enter="search"
        />
        <v-btn variant="tonal" :loading="loading" @click="search">检索</v-btn>
      </v-card-text>
    </v-card>

    <v-row>
      <v-col cols="12" md="5">
        <v-card>
          <v-card-title>{{ tab === "active" ? "当前记忆" : "回收站" }}（{{ items.length }}）</v-card-title>
          <v-list density="compact" class="list">
            <v-list-item
              v-for="item in items"
              :key="item.id"
              :active="selected?.id === item.id"
              @click="open(item)"
            >
              <v-list-item-title>{{ item.content }}</v-list-item-title>
              <v-list-item-subtitle>
                {{ item.memory_type }} · rev {{ item.current_revision }} · {{ item.updated_at }}
              </v-list-item-subtitle>
              <template v-if="tab === 'archived'" #append>
                <v-btn size="small" variant="text" @click.stop="restore(item)">恢复</v-btn>
                <v-btn size="small" variant="text" color="error" @click.stop="beginPurge(item)">彻底删除</v-btn>
              </template>
            </v-list-item>
            <v-list-item v-if="!items.length" title="暂无可显示的群记忆" />
          </v-list>
        </v-card>
      </v-col>

      <v-col cols="12" md="7">
        <v-card>
          <v-card-title class="d-flex align-center justify-space-between">
            <span>记忆详情</span>
            <span v-if="selected" class="text-caption text-medium-emphasis">{{ selected.status }}</span>
          </v-card-title>
          <v-card-text v-if="!selected" class="text-medium-emphasis">选择左侧条目查看详情。</v-card-text>
          <v-card-text v-else>
            <div class="text-body-1 mb-3">{{ selected.content }}</div>
            <div class="text-caption text-medium-emphasis mb-3">
              类型 {{ selected.memory_type }} · 来源 {{ selected.source_type }} ·
              revision {{ selected.current_revision }} · 更新 {{ selected.updated_at }}
            </div>
            <div v-if="canWrite" class="d-flex ga-2 mb-4">
              <v-btn size="small" variant="tonal" @click="beginEdit">编辑</v-btn>
              <v-btn v-if="selected.status === 'ACTIVE'" size="small" variant="tonal" @click="archive">移入回收站</v-btn>
            </div>
            <v-divider class="mb-3" />
            <div class="text-subtitle-2 mb-2">历史版本</div>
            <v-table density="compact">
              <tbody>
                <tr v-for="revision in history?.revisions ?? []" :key="revision.revision">
                  <td>r{{ revision.revision }}</td>
                  <td>{{ revision.changed_at }}</td>
                  <td>{{ revision.change_reason }}</td>
                </tr>
              </tbody>
            </v-table>
            <div class="text-subtitle-2 my-2">来源</div>
            <v-table density="compact">
              <tbody>
                <tr v-for="source in history?.sources ?? []" :key="source.id ?? source.captured_at">
                  <td>{{ source.source_type }}</td>
                  <td>{{ source.operator_type }}</td>
                  <td>{{ source.captured_at }}</td>
                </tr>
              </tbody>
            </v-table>
            <div class="text-subtitle-2 my-2">审计</div>
            <v-table density="compact">
              <tbody>
                <tr v-for="entry in operations" :key="String(entry.audit_id)">
                  <td>{{ entry.operation_type }}</td>
                  <td>{{ entry.operator_type }}</td>
                  <td>{{ entry.occurred_at }}</td>
                </tr>
              </tbody>
            </v-table>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-dialog v-model="createOpen" max-width="560">
      <v-card>
        <v-card-title>新建群记忆</v-card-title>
        <v-card-text>
          <v-select v-model="createLabel" :items="['话题', '约定', '氛围', '经历']" label="类型标签" />
          <v-textarea v-model="createContent" label="记忆内容" rows="4" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="createOpen = false">取消</v-btn>
          <v-btn color="primary" :disabled="!createContent.trim()" @click="create">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="editOpen" max-width="560">
      <v-card>
        <v-card-title>编辑记忆（生成新 revision）</v-card-title>
        <v-card-text>
          <v-textarea v-model="editContent" rows="4" />
          <div class="text-caption text-medium-emphasis">
            保存后 revision 从 {{ selected?.current_revision }} 变为 {{ (selected?.current_revision ?? 0) + 1 }}
          </div>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="editOpen = false">取消</v-btn>
          <v-btn color="primary" :disabled="!editContent.trim()" @click="saveEdit">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="purgeOpen" max-width="520">
      <v-card>
        <v-card-title>彻底删除</v-card-title>
        <v-card-text>
          <p>此操作会物理删除正文，且不可恢复。请输入 <code>PURGE {{ selected?.id }}</code> 确认。</p>
          <v-text-field v-model="purgeText" label="确认文本" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="purgeOpen = false">取消</v-btn>
          <v-btn
            color="error"
            :disabled="purgeText.trim() !== `PURGE ${selected?.id}`"
            @click="purge"
          >彻底删除</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<style scoped>
.list {
  max-height: 640px;
  overflow: auto;
}
</style>
