import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import frappeui from "frappe-ui/vite";
import { VitePWA } from "vite-plugin-pwa";
import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "/assets/esan_gbos/frontend/",
  plugins: [
    frappeui({
      frappeProxy: false,
      jinjaBootData: false,
      buildConfig: false,
    }),
    vue(),
    VitePWA({
      strategies: "injectManifest",
      srcDir: "src",
      filename: "service-worker.ts",
      injectRegister: "auto",
      registerType: "autoUpdate",
      scope: "/gbos/",
      manifest: false,
      injectManifest: {
        globPatterns: ["**/*.{js,css,html,svg,png,webmanifest}"],
      },
    }),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    manifest: true,
    sourcemap: true,
  },
  preview: {
    headers: {
      "Service-Worker-Allowed": "/gbos/",
    },
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
    setupFiles: ["./tests/setup.ts"],
    css: true,
    restoreMocks: true,
    server: {
      deps: {
        inline: [/frappe-ui/],
      },
    },
  },
});
