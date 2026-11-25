<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, groupParams, MemoryItem } from "../api/weave";
import { currentGroup, loadBase, state } from "../store";
import { notify, reportError } from "../toast";

const groupState = ref<Record<string, unknown> | null>(null);
const binding = ref<Record<string, unknown> | null>(null);
const code = ref("");
const personal = ref<MemoryItem[]>([]);
const candidates = ref<MemoryItem[]>([]);

const group = computed(() => currentGroup());
const canWrite = computed(() => Boolean(state.capabilities?.write));
const bound = computed(() => Boolean(binding.value?.bound));

const editOpen = ref(false);
const editItem = ref<MemoryItem | null>(null);
const editContent = ref("");

async function loadGroup(): Promise<void> {
  if (!group.value) return;
  try {
    groupState.value = await api.groupPersonalStatus(groupParams(group.value));
  } catch (error) {
    reportError(error);
  }
}

async function loadPersonal(): Promise<void> {
  if (!bound.value) {
    personal.value = [];
    candidates.value = [];
    return;
  }
  try {
    personal.value = (await api.personalMemories({ limit: 50 })).items;
    candidates.value = (await api.personalCandidates()).items;
  } catch (error) {
    reportError(error);
  }
}

async function load(): Promise<void> {
  try {
    binding.value = await api.bindingStatus();
  } catch (error) {
    reportError(error);
  }
  await Promise.all([loadGroup(), loadPersonal()]);
}

async function bind(): Promise<void> {
  try {
    await api.bind(code.value.trim());
    code.value = "";
    notify("已绑定当前 Dashboard 账号");
    await loadBase();
    await load();
  } catch (error) {
    reportError(error);
  }
}

async function unbind(): Promise<void> {
  try {
    await api.unbind();
    notify("已解除绑定");
    await loadBase();
    await load();
  } catch (error) {
    reportError(error);
  }
}

function beginEdit(item: MemoryItem): void {
  editItem.value = item;
  editContent.value = item.content;
  editOpen.value = true;
}

async function saveEdit(): Promise<void> {
  if (!editItem.value) return;
  try {
    await api.personalUpdate(editItem.value.id, {
      content: editContent.value,
      expected_revision: editItem.value.current_revision,
    });
    editOpen.value = false;
    notify("已保存新版本");
    await loadPersonal();
  } catch (error) {
    reportError(error);
  }
}

async function archive(item: MemoryItem): Promise<void> {
  try {
    await api.personalArchive(item.id);
    notify("已移入回收站");
    await loadPersonal();
  } catch (error) {
    reportError(error);
  }
}

async function confirmCandidate(item: MemoryItem): Promise<void> {
  try {
    await api.personalConfirm(item.id, item.current_revision);
    notify("候选已确认");
    await loadPersonal();
  } catch (error) {
    reportError(error);
  }
}

async function rejectCandidate(item: MemoryItem): Promise<void> {
  try {
    await api.personalReject(item.id, item.current_revision);
    notify("候选已拒绝");
    await loadPersonal();
  } catch (error) {
    reportError(error);
  }
}

watch(() => state.groupIndex, loadGroup);
onMounted(load);
</script>

<template>
  <div>
    <div class="text-h5 mb-3">群个人记忆与账号绑定</div>

    <v-row>
      <v-col cols="12" md="6">
        <v-card class="mb-3">
          <v-card-title>群级总开关</v-card-title>
          <v-card-text>
            <v-select
              v-model="state.groupIndex"
              :items="state.groups"
              item-title="group_id"
              label="群作用域"
              density="compact"
              class="mb-3"
            />
            <v-alert
              :type="groupState?.group_enabled ? 'success' : 'info'"
              variant="tonal"
              density="compact"
            >
              本群 Group personal：{{ groupState?.group_enabled ? "已开启" : "已关闭" }}
            </v-alert>
            <p class="text-caption text-medium-emphasis mt-3">
              群管理员在群内发送 <code>/织忆 群个人 开启</code> 或 <code>关闭</code>；
              成员本人发送 <code>/织忆 群个人 授权</code> 才会生效。
              控制台不代替成员授权，也不展示逐人名单。
            </p>
          </v-card-text>
        </v-card>

        <v-card>
          <v-card-title>Dashboard 账号绑定</v-card-title>
          <v-card-text>
            <template v-if="bound">
              <v-alert type="success" variant="tonal" density="compact" class="mb-3">
                已绑定 {{ binding?.platform }} / {{ binding?.tenant_id }} / {{ binding?.subject_id }}
              </v-alert>
              <v-btn variant="tonal" color="error" @click="unbind">解除绑定</v-btn>
            </template>
            <template v-else>
              <p class="text-caption text-medium-emphasis mb-2">
                在私聊发送 <code>/织忆 绑定</code> 获取 6 位绑定码，10 分钟内有效。
              </p>
              <div class="d-flex ga-2">
                <v-text-field v-model="code" label="绑定码" density="compact" maxlength="6" />
                <v-btn color="primary" :disabled="code.trim().length !== 6" @click="bind">绑定</v-btn>
              </div>
            </template>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" md="6">
        <v-card class="mb-3">
          <v-card-title>我的个人记忆（{{ personal.length }}）</v-card-title>
          <v-card-text v-if="!bound" class="text-medium-emphasis">
            绑定平台账号后可在此查看与编辑自己的个人记忆。
          </v-card-text>
          <v-list v-else density="compact" class="list">
            <v-list-item
              v-for="item in personal"
              :key="item.id"
              :title="item.content"
              :subtitle="`${item.memory_type} · rev ${item.current_revision} · ${item.updated_at}`"
            >
              <template #append>
                <v-btn size="small" variant="text" @click="beginEdit(item)">编辑</v-btn>
                <v-btn size="small" variant="text" color="error" @click="archive(item)">删除</v-btn>
              </template>
            </v-list-item>
            <v-list-item v-if="!personal.length" title="暂无个人记忆" />
          </v-list>
        </v-card>

        <v-card>
          <v-card-title>待确认候选（{{ candidates.length }}）</v-card-title>
          <v-list density="compact" class="list">
            <v-list-item
              v-for="item in candidates"
              :key="item.id"
              :title="item.content"
              :subtitle="`${item.memory_type} · rev ${item.current_revision}`"
            >
              <template #append>
                <v-btn size="small" variant="text" color="primary" @click="confirmCandidate(item)">确认</v-btn>
                <v-btn size="small" variant="text" color="error" @click="rejectCandidate(item)">拒绝</v-btn>
              </template>
            </v-list-item>
            <v-list-item v-if="!candidates.length" title="暂无候选" />
          </v-list>
        </v-card>
      </v-col>
    </v-row>

    <v-dialog v-model="editOpen" max-width="560">
      <v-card>
        <v-card-title>编辑个人记忆</v-card-title>
        <v-card-text><v-textarea v-model="editContent" rows="4" /></v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="editOpen = false">取消</v-btn>
          <v-btn color="primary" :disabled="!canWrite || !editContent.trim()" @click="saveEdit">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<style scoped>
.list {
  max-height: 320px;
  overflow: auto;
}
</style>
