import { createRouter, createWebHashHistory } from "vue-router";
import Overview from "./views/Overview.vue";
import Explorer from "./views/Explorer.vue";
import History from "./views/History.vue";
import Sources from "./views/Sources.vue";
import Debugger from "./views/Debugger.vue";
import SettingsMaintenance from "./views/SettingsMaintenance.vue";
import ConsentScope from "./views/ConsentScope.vue";

export const routes = [
  { path: "/", redirect: "/overview" },
  { path: "/overview", component: Overview, meta: { title: "总览" } },
  { path: "/explorer", component: Explorer, meta: { title: "记忆浏览" } },
  { path: "/history", component: History, meta: { title: "历史" } },
  { path: "/sources", component: Sources, meta: { title: "来源" } },
  { path: "/debugger", component: Debugger, meta: { title: "检索调试" } },
  { path: "/settings", component: SettingsMaintenance, meta: { title: "设置与维护" } },
  { path: "/consent", component: ConsentScope, meta: { title: "群个人与绑定" } },
];

export default createRouter({
  history: createWebHashHistory(),
  routes,
});
