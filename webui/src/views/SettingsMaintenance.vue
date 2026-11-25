<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { api, requestNonce, SettingsItem } from "../api/weave";
import { state } from "../store";
import { notify, reportError } from "../toast";

const schema = ref<SettingsItem[]>([]);
const current = ref<Record<string, unknown>>({});
const drafts = reactive<Record<string, unknown>>({});
const dirty = ref<Set<string>>(new Set());

const status = ref<Record<string, unknown> | null>(null);
const statusEntries = computed(() => Object.entries(status.value ?? {}));
const backups = ref<Record<string, unknown>[]>([]);
const backupError = ref("");

const restoreId = ref("");
const restoreMode = ref("merge");
const restoreDry = ref<Record<string, unknown> | null>(null);
const restoreReady = ref(false);

const canWrite = computed(() => Boolean(state.capabilities?.write && state.capabilities?.admin));

function initial(item: SettingsItem): unknown {
  const value = current.value[item.key];
  if (value !== undefined) return value;
  return item.default;
}

function display(item: SettingsItem): unknown {
  if (item.key in drafts) return drafts[item.key];
  const value = initial(item);
  if (item.type === "list") return (value as string[] | undefined)?.join(", ") ?? "";
  if (item.type === "dict") return JSON.stringify(value ?? {}, null, 2);
  return value;
}

function edit(item: SettingsItem, value: unknown): void {
  drafts[item.key] = value;
  dirty.value = new Set(dirty.value).add(item.key);
}

function parsed(item: SettingsItem): unknown {
  const raw = drafts[item.key];
  if (item.type === "int") return Number(raw);
  if (item.type === "float") return Number(raw);
  if (item.type === "list") {
    return String(raw ?? "").split(",").map((part) => part.trim()).filter(Boolean);
  }
  if (item.type === "dict") {
    return JSON.parse(String(raw ?? "{}"));
  }
  return raw;
}

async function load(): Promise<void> {
  try {
    const [schemaPayload, settingsPayload, maintenance] = await Promise.all([
      api.settingsSchema(), api.settings(), api.maintenanceStatus(),
    ]);
    schema.value = schemaPayload.items.filter((item) => item.writable);
    current.value = settingsPayload;
    status.value = maintenance;
    for (const key of Object.keys(drafts)) delete drafts[key];
    dirty.value = new Set();
  } catch (error) {
    reportError(error);
  }
  await loadBackups();
}

async function loadBackups(): Promise<void> {
  try {
    backups.value = (await api.backups()).items;
    backupError.value = "";
  } catch (error) {
    backups.value = [];
    backupError.value = `备份列表需要配置 api_admin_usernames：${String(error)}`;
  }
}

async function save(): Promise<void> {
  const values: Record<string, unknown> = {};
  const important: string[] = [];
  try {
    for (const key of dirty.value) {
      const item = schema.value.find((entry) => entry.key === key);
      if (!item) continue;
      values[key] = parsed(item);
      if (item.important) important.push(key);
    }
  } catch (error) {
    notify("列表/对象格式不正确", "error");
    return;
  }
  if (!Object.keys(values).length) return;
  try {
    const nonce = important.length
      ? await requestNonce("settings", [...important].sort().join(","))
      : undefined;
    const result = (await api.updateSettings(values, nonce)) as Record<string, unknown>;
    const reload = (result.reload_required as string[] | undefined) ?? [];
    notify(reload.length ? `已保存；重载插件后生效：${reload.join(", ")}` : "已保存并即时生效");
    await load();
  } catch (error) {
    reportError(error);
  }
}

async function createBackup(): Promise<void> {
  try {
    await api.createBackup();
    notify("备份已创建");
    await loadBackups();
  } catch (error) {
    reportError(error);
  }
}

async function reindex(): Promise<void> {
  try {
    await api.reindex();
    notify("索引重建已提交");
    await load();
  } catch (error) {
    reportError(error);
  }
}

async function dryRun(): Promise<void> {
  try {
    restoreDry.value = (await api.restoreBackup({
      backup_id: restoreId.value, mode: restoreMode.value, dry_run: true,
    })) as Record<string, unknown>;
    restoreReady.value = true;
  } catch (error) {
    restoreDry.value = null;
    restoreReady.value = false;
    reportError(error);
  }
}

async function confirmRestore(): Promise<void> {
  if (!restoreReady.value) return;
  try {
    const nonce = await requestNonce("restore", restoreId.value);
    await api.restoreBackup({
      backup_id: restoreId.value, mode: restoreMode.value, confirm: true,
    }, nonce);
    notify("恢复已完成");
    restoreReady.value = false;
    await load();
  } catch (error) {
    reportError(error);
  }
}

onMounted(load);
</script>

<template>
  <div>
    <div class="page-heading"><div><h1 class="text-h5">设置与维护 <span class="heading-sparkle" aria-hidden="true">⚙</span></h1><p>织忆小屋：把运行配置和维护工具收纳在一起，重要操作依然会经过确认。</p></div></div>

    <v-row>
      <v-col cols="12" md="7">
        <v-card class="weave-panel">
          <v-card-title class="d-flex justify-space-between align-center">
            <span>运行配置</span>
            <v-btn color="primary" :disabled="!canWrite || !dirty.size" @click="save">保存</v-btn>
          </v-card-title>
          <v-card-subtitle>
            密钥类配置只读；标记「重要」的项目需要一次性确认，保存后按提示重载插件生效。
          </v-card-subtitle>
          <v-card-text class="settings">
            <div v-for="item in schema" :key="item.key" class="setting-row">
              <div class="setting-label">
                <code>{{ item.key }}</code>
                <v-chip v-if="item.important" size="x-small" color="warning" variant="tonal" class="ml-2">重要</v-chip>
                <v-chip v-if="item.hot_apply" size="x-small" variant="tonal" class="ml-1">即时</v-chip>
              </div>
              <v-switch
                v-if="item.type === 'bool'"
                :model-value="Boolean(display(item))"
                density="compact"
                hide-details
                color="primary"
                :disabled="!canWrite"
                @update:model-value="edit(item, $event)"
              />
              <v-text-field
                v-else-if="item.type === 'int' || item.type === 'float'"
                :model-value="display(item)"
                type="number"
                density="compact"
                hide-details
                :disabled="!canWrite"
                @update:model-value="edit(item, $event)"
              />
              <v-textarea
                v-else-if="item.type === 'list' || item.type === 'dict'"
                :model-value="display(item)"
                rows="2"
                density="compact"
                hide-details
                :disabled="!canWrite"
                @update:model-value="edit(item, $event)"
              />
              <v-text-field
                v-else
                :model-value="display(item)"
                density="compact"
                hide-details
                :disabled="!canWrite"
                @update:model-value="edit(item, $event)"
              />
            </div>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" md="5">
        <v-card class="mb-4 weave-panel">
          <v-card-title>维护状态</v-card-title>
          <v-card-text>
            <div v-if="statusEntries.length" class="maintenance-status-list">
              <div v-for="([key, value]) in statusEntries" :key="key" class="maintenance-status-row">
                <span>{{ key.replace(/_/g, " ") }}</span>
                <strong>{{ value !== null && typeof value === "object" ? JSON.stringify(value) : String(value ?? "—") }}</strong>
              </div>
            </div>
            <div v-else class="weave-empty">暂时没有维护状态信息。</div>
            <details class="raw-status">
              <summary>查看原始状态数据</summary>
              <pre class="code weave-code">{{ JSON.stringify(status ?? {}, null, 2) }}</pre>
            </details>
            <div class="d-flex flex-wrap ga-2 mt-3">
              <v-btn variant="tonal" :disabled="!canWrite" @click="createBackup">创建备份</v-btn>
              <v-btn variant="tonal" color="warning" :disabled="!canWrite" @click="reindex">重建索引</v-btn>
            </div>
          </v-card-text>
        </v-card>

        <v-card class="weave-panel">
          <v-card-title>备份与恢复</v-card-title>
          <v-card-text>
            <div v-if="backupError" class="text-caption text-error mb-2">{{ backupError }}</div>
            <v-list v-else density="compact" class="mb-2">
              <v-list-item
                v-for="item in backups"
                :key="String(item.backup_id)"
                :title="String(item.backup_id)"
                :subtitle="String(item.created_at ?? '')"
                @click="restoreId = String(item.backup_id); restoreReady = false"
              />
              <v-list-item v-if="!backups.length" title="暂无备份" />
            </v-list>
            <v-text-field v-model="restoreId" label="backup_id" density="compact" />
            <v-select
              v-model="restoreMode"
              :items="['merge', 'revert', 'disaster-rebuild']"
              label="恢复模式"
              density="compact"
            />
            <div class="d-flex ga-2">
              <v-btn variant="tonal" :disabled="!canWrite || !restoreId" @click="dryRun">预演恢复</v-btn>
              <v-btn color="error" :disabled="!restoreReady" @click="confirmRestore">确认恢复</v-btn>
            </div>
            <pre v-if="restoreDry" class="code weave-code mt-2">{{ JSON.stringify(restoreDry, null, 2) }}</pre>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>
  </div>
</template>

<style scoped>
.heading-sparkle { color: var(--weave-accent); }
.maintenance-status-list { margin-bottom: 12px; }
.maintenance-status-row { display: flex; justify-content: space-between; gap: 12px; padding: 9px 0; border-bottom: 1px dashed var(--weave-line-soft); font-size: 12px; }
.maintenance-status-row span { color: var(--weave-muted); text-transform: capitalize; }
.maintenance-status-row strong { color: var(--weave-text-strong); text-align: right; overflow-wrap: anywhere; }
.raw-status { margin-top: 10px; color: var(--weave-muted); font-size: 11px; }
.raw-status summary { cursor: pointer; padding: 5px 0; }
.raw-status pre { margin-top: 8px; }
.settings {
  max-height: 620px;
  overflow: auto;
}

.setting-row {
  display: grid;
  grid-template-columns: minmax(200px, 1fr) minmax(160px, 1fr);
  gap: 12px;
  align-items: center;
  padding: 6px 0;
  border-bottom: 1px solid rgba(128, 128, 128, 0.15);
}

.setting-label {
  font-size: 12px;
}

.code {
  white-space: pre-wrap;
  font-size: 12px;
  max-height: 220px;
  overflow: auto;
}
</style>
