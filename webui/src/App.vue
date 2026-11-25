<script setup lang="ts">
import { computed, onMounted } from "vue";
import { useRoute } from "vue-router";
import { useDisplay } from "vuetify";
import { routes } from "./router";
import { loadBase, state } from "./store";
import { toast, reportError } from "./toast";

const route = useRoute();
const { smAndDown } = useDisplay();
const navItems = routes.filter((item) => item.meta);
const pageTitle = computed(() => String(route.meta.title ?? "织忆空间"));

async function refresh(): Promise<void> {
  try {
    await loadBase();
    toast.text = "数据已刷新";
    toast.color = "success";
    toast.show = true;
  } catch (error) {
    reportError(error);
  }
}

onMounted(refresh);
</script>

<template>
  <v-app>
    <v-navigation-drawer permanent width="244" :rail="smAndDown" :rail-width="72" class="weave-drawer">
      <div class="brand-block">
        <div class="brand-mark"><span>织</span><i aria-hidden="true">✦</i></div>
        <div class="brand-copy">
          <div class="brand-name">织忆 <span>WEAVE</span></div>
          <div class="brand-caption">把重要的记忆，轻轻收好</div>
        </div>
      </div>

      <div class="nav-caption">记忆工作台</div>
      <v-list nav class="weave-nav">
        <v-tooltip
          v-for="item in navItems"
          :key="item.path"
          :text="String(item.meta?.title ?? '')"
          location="end"
        >
          <template #activator="{ props: activatorProps }">
            <v-list-item
              v-bind="activatorProps"
              :to="item.path"
              :aria-label="String(item.meta?.title ?? '')"
              rounded="xl"
              class="weave-nav-item"
            >
              <template #prepend>
                <span class="nav-symbol" aria-hidden="true">{{ String(item.meta?.icon ?? "·") }}</span>
              </template>
              <v-list-item-title>{{ String(item.meta?.title ?? "") }}</v-list-item-title>
            </v-list-item>
          </template>
        </v-tooltip>
      </v-list>

      <div class="drawer-bottom">
        <div class="mascot-card">
          <div class="mascot-spark" aria-hidden="true">✧</div>
          <img class="mascot-illustration" src="./assets/weave-mascot.svg" alt="" aria-hidden="true" />
          <div class="mascot-title">记忆正在被好好保存</div>
          <div class="mascot-subtitle">每一个片段，都值得珍藏。</div>
        </div>
        <div class="drawer-version">WEAVE MEMORY · CONTROL DESK</div>
      </div>
    </v-navigation-drawer>

    <v-app-bar flat class="weave-topbar">
      <div class="topbar-heading">
        <div class="topbar-eyebrow">{{ pageTitle }} · YOUR MEMORY SPACE</div>
        <div class="topbar-title">织忆空间</div>
      </div>
      <v-spacer />
      <div v-if="state.capabilities" class="topbar-status">
        <v-chip size="small" variant="tonal" :color="state.capabilities.admin ? 'primary' : 'secondary'">
          <span class="status-dot" aria-hidden="true" />{{ state.capabilities.admin ? "管理员" : "只读模式" }}
        </v-chip>
        <v-chip size="small" variant="tonal" :color="state.capabilities.bound ? 'success' : undefined">
          {{ state.capabilities.bound ? "账号已绑定" : "尚未绑定" }}
        </v-chip>
      </div>
      <v-btn class="refresh-button ml-3" variant="tonal" @click="refresh">刷新</v-btn>
    </v-app-bar>

    <v-main class="weave-main">
      <v-container fluid class="weave-content">
        <v-alert
          v-if="state.error"
          type="error"
          variant="tonal"
          rounded="xl"
          class="mb-5"
          :text="state.error"
        />
        <v-alert
          v-else-if="state.capabilities && !state.capabilities.write"
          type="info"
          variant="tonal"
          rounded="xl"
          class="mb-5"
        >
          当前实例未配置 api_admin_usernames，写操作与管理入口不可用；只读页面仍可使用。
        </v-alert>
        <router-view />
      </v-container>
    </v-main>

    <v-snackbar v-model="toast.show" :color="toast.color" timeout="3200" location="bottom right" rounded="pill">
      {{ toast.text }}
    </v-snackbar>
  </v-app>
</template>
