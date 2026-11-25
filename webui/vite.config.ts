import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  base: "./",
  build: {
    outDir: "../pages/weave-memory",
    emptyOutDir: true,
    target: "es2020",
  },
});
