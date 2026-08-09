<template>
  <header class="app-topbar">
    <button
      id="mobile-navigation-menu-button"
      class="app-topbar__menu-button"
      type="button"
      aria-label="打开导航菜单"
      title="打开导航菜单"
      :aria-controls="drawerId"
      :aria-expanded="drawerOpen"
      @click="emit('open-navigation')"
    >
      <span aria-hidden="true" />
      <span aria-hidden="true" />
      <span aria-hidden="true" />
    </button>
    <div class="app-topbar__context">
      <span>当前页面</span>
      <strong>{{ currentPageLabel }}</strong>
    </div>
    <div class="app-topbar__session" :title="sessionLabel">
      <span class="app-topbar__avatar" aria-hidden="true">{{ sessionInitial }}</span>
      <span class="app-topbar__session-copy">
        <small>当前会话</small>
        <strong>{{ sessionLabel }}</strong>
      </span>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";

import type { NavigationItem } from "@/navigation";

const props = defineProps<{
  navigation: readonly NavigationItem[];
  sessionLabel: string;
  drawerId: string;
  drawerOpen: boolean;
}>();

const emit = defineEmits<{
  "open-navigation": [];
}>();

const route = useRoute();
const currentPageLabel = computed(() => {
  const item = props.navigation.find(
    (candidate) =>
      route.path === candidate.to || route.path.startsWith(`${candidate.to}/`),
  );
  if (item) {
    return item.label;
  }
  return route.path === "/gbos" ? "产品首页" : "业务详情";
});
const sessionInitial = computed(() =>
  props.sessionLabel.trim().charAt(0).toLocaleUpperCase() || "访",
);
</script>

<style scoped>
.app-topbar {
  display: flex;
  min-width: 0;
  min-height: 64px;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 12px clamp(20px, 3vw, 40px);
  border-bottom: 1px solid var(--gbos-border);
  background: rgb(255 255 255 / 92%);
}

.app-topbar__context,
.app-topbar__session-copy {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.app-topbar__menu-button {
  display: none;
  width: 44px;
  min-width: 44px;
  height: 44px;
  padding: 11px;
  border: 1px solid var(--gbos-border);
  border-radius: 12px;
  background: white;
  cursor: pointer;
}

.app-topbar__menu-button span {
  display: block;
  height: 2px;
  border-radius: 2px;
  background: var(--gbos-text);
}

.app-topbar__menu-button span + span {
  margin-top: 4px;
}

.app-topbar__context span,
.app-topbar__session-copy small {
  color: var(--gbos-muted);
  font-size: 11px;
}

.app-topbar__context strong {
  color: var(--gbos-text);
  font-size: 15px;
}

.app-topbar__session {
  display: flex;
  min-width: 0;
  max-width: min(48vw, 360px);
  align-items: center;
  gap: 9px;
}

.app-topbar__avatar {
  display: grid;
  width: 34px;
  height: 34px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 50%;
  color: var(--gbos-accent-text);
  background: #dff7f3;
  font-size: 13px;
  font-weight: 800;
}

.app-topbar__session-copy strong {
  overflow: hidden;
  color: var(--gbos-text);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 767px) {
  .app-topbar {
    position: sticky;
    z-index: 600;
    top: 0;
    min-height: 56px;
    gap: 10px;
    padding: 6px 12px;
  }

  .app-topbar__menu-button {
    display: block;
  }

  .app-topbar__context {
    flex: 1 1 auto;
  }

  .app-topbar__context span,
  .app-topbar__session-copy {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    padding: 0;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }

  .app-topbar__session {
    flex: 0 0 auto;
  }
}
</style>
