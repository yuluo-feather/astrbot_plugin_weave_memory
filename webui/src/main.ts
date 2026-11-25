import { createApp } from "vue";
import { createVuetify } from "vuetify";
import * as components from "vuetify/components";
import * as directives from "vuetify/directives";
import "vuetify/styles";
import App from "./App.vue";
import router from "./router";
import { isDark, waitReady } from "./api/bridge";

const vuetify = createVuetify({
  components,
  directives,
  theme: { defaultTheme: isDark() ? "dark" : "light" },
});

async function boot(): Promise<void> {
  try {
    await waitReady();
  } catch {
    // The page still renders a clear "open from AstrBot" error on API calls.
  }
  createApp(App).use(router).use(vuetify).mount("#app");
}

void boot();
