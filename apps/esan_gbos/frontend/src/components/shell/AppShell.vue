<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="app-shell">
    <WorkspaceSidebar :navigation="navigation" />
    <div class="app-shell__workspace">
      <AppTopbar
        :navigation="navigation"
        :session-label="sessionLabel"
        :drawer-id="drawerId"
        :drawer-open="drawerOpen"
        @open-navigation="openDrawer"
      />
      <main id="main-content" tabindex="-1">
        <slot />
      </main>
    </div>
    <MobileBottomNav
      :navigation="navigation"
      @open-navigation="openDrawer"
    />
    <MobileNavDrawer
      :open="drawerOpen"
      :navigation="navigation"
      :drawer-id="drawerId"
      :title-id="drawerTitleId"
      :return-focus-id="menuButtonId"
      @close="closeDrawer"
    />
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";

import type { NavigationItem } from "@/navigation";

import AppTopbar from "./AppTopbar.vue";
import MobileBottomNav from "./MobileBottomNav.vue";
import MobileNavDrawer from "./MobileNavDrawer.vue";
import WorkspaceSidebar from "./WorkspaceSidebar.vue";

defineProps<{
  navigation: readonly NavigationItem[];
  sessionLabel: string;
}>();

const drawerId = "mobile-navigation-drawer";
const drawerTitleId = "mobile-navigation-drawer-title";
const menuButtonId = "mobile-navigation-menu-button";
const drawerOpen = ref(false);

const openDrawer = () => {
  drawerOpen.value = true;
};

const closeDrawer = () => {
  drawerOpen.value = false;
};
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

@media (min-width: 768px) and (max-width: 1199px) {
  .app-shell {
    grid-template-columns: 72px minmax(0, 1fr);
  }
}

@media (max-width: 767px) {
  .app-shell {
    grid-template-columns: minmax(0, 1fr);
  }

  #main-content {
    padding: 16px 16px calc(76px + env(safe-area-inset-bottom));
  }
}
</style>
