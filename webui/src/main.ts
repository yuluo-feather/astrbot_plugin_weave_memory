import { createApp } from "vue";
import { createVuetify } from "vuetify";
import * as components from "vuetify/components";
import * as directives from "vuetify/directives";
import "vuetify/styles";
import "./styles.css";
import App from "./App.vue";
import router from "./router";
import { isDark, onContextChange, waitReady } from "./api/bridge";

const vuetify = createVuetify({
  components,
  directives,
  theme: {
    defaultTheme: isDark() ? "dark" : "light",
    themes: {
      light: {
        dark: false,
        colors: {
          primary: "#6f5499", secondary: "#6f617e", success: "#437456",
          info: "#4f648c", warning: "#7f5d22", error: "#9e4658",
          background: "#f8f6fb", surface: "#fffefd",
        },
      },
      dark: {
        dark: true,
        colors: {
          primary: "#b7a0d9", secondary: "#b7aabd", success: "#8fc6a7",
          info: "#9baed6", warning: "#dfbd85", error: "#d894a1",
          background: "#211e29", surface: "#302a3a",
        },
      },
    },
  },
});

// Keep the dashboard palette aligned with AstrBot when its theme changes.
onContextChange((context) => {
  vuetify.theme.global.name.value = context.isDark ? "dark" : "light";
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
