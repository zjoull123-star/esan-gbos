<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="app-shell">
    <WorkspaceSidebar :navigation="navigation" />
    <div class="app-shell__workspace">
      <AppTopbar
        :navigation="navigation"
        :session-label="sessionLabel"
      />
      <main id="main-content" tabindex="-1">
        <slot />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { NavigationItem } from "@/navigation";

import AppTopbar from "./AppTopbar.vue";
import WorkspaceSidebar from "./WorkspaceSidebar.vue";

defineProps<{
  navigation: readonly NavigationItem[];
  sessionLabel: string;
}>();
</script>

<style scoped>
.app-shell {
  display: grid;
  width: 100%;
  min-width: 0;
  min-height: 100dvh;
  grid-template-columns: 240px minmax(0, 1fr);
  margin: 0;
  color: var(--gbos-text);
  background: var(--gbos-canvas);
  font-family: var(--gbos-font-sans);
}

.app-shell__workspace {
  min-width: 0;
}

#main-content {
  width: 100%;
  min-width: 0;
  max-width: 1600px;
  margin-inline: auto;
  padding: 24px;
}

.app-shell :deep(:focus-visible) {
  outline-color: var(--gbos-primary);
}

.skip-link {
  position: fixed;
  z-index: 1000;
  top: 12px;
  left: 12px;
  padding: 10px 16px;
  border-radius: var(--gbos-radius-control);
  color: white;
  background: var(--gbos-primary);
  font-weight: 700;
  text-decoration: none;
  transform: translateY(-180%);
  transition: transform 160ms ease;
}

.skip-link:focus {
  transform: translateY(0);
}

@media (max-width: 800px) {
  .app-shell {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
