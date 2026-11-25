<script setup lang="ts">
import { onMounted } from "vue";
import { routes } from "./router";
import { loadBase, state } from "./store";
import { toast, reportError } from "./toast";

const navItems = routes.filter((route) => route.meta);

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
    <v-app-bar flat border density="compact">
      <v-app-bar-title>织忆 · 记忆控制台</v-app-bar-title>
      <template v-if="state.capabilities">
        <v-chip class="mr-2" size="small" variant="tonal">
          {{ state.capabilities.admin ? "管理员" : "只读" }}
        </v-chip>
        <v-chip class="mr-2" size="small" variant="tonal">
          {{ state.capabilities.bound ? "已绑定平台账号" : "未绑定" }}
        </v-chip>
      </template>
      <v-btn variant="text" @click="refresh">刷新</v-btn>
    </v-app-bar>

    <v-navigation-drawer permanent width="200">
      <v-list nav density="compact">
        <v-list-item
          v-for="item in navItems"
          :key="item.path"
          :to="item.path"
          :title="String(item.meta?.title ?? '')"
        />
      </v-list>
    </v-navigation-drawer>

    <v-main>
      <v-container fluid class="pa-6">
        <v-alert
          v-if="state.error"
          type="error"
          variant="tonal"
          class="mb-4"
          :text="state.error"
        />
        <v-alert
          v-else-if="state.capabilities && !state.capabilities.write"
          type="info"
          variant="tonal"
          class="mb-4"
        >
          当前实例未配置 api_admin_usernames，写操作与管理入口不可用；只读页面仍可使用。
        </v-alert>
        <router-view />
      </v-container>
    </v-main>

    <v-snackbar v-model="toast.show" :color="toast.color" timeout="3200">
      {{ toast.text }}
    </v-snackbar>
  </v-app>
</template>
