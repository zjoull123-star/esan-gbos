<template>
  <nav class="mobile-bottom-nav" aria-label="移动端快捷导航">
    <RouterLink
      class="mobile-bottom-nav__item"
      to="/gbos"
      :aria-current="route.path === '/gbos' ? 'page' : undefined"
    >
      <span
        class="mobile-bottom-nav__mark"
        data-icon="home"
        aria-hidden="true"
      />
      <span>首页</span>
    </RouterLink>
    <RouterLink
      v-for="item in quickItems"
      :key="item.id"
      class="mobile-bottom-nav__item"
      :to="item.to"
      :aria-current="isCurrent(item.to) ? 'page' : undefined"
    >
      <span
        class="mobile-bottom-nav__mark"
        :data-icon="item.icon"
        aria-hidden="true"
      />
      <span>{{ compactLabel(item) }}</span>
    </RouterLink>
    <button
      v-if="showDrawerControl"
      class="mobile-bottom-nav__item"
      type="button"
      aria-label="更多"
      title="更多"
      @click="emit('open-navigation')"
    >
      <span
        class="mobile-bottom-nav__mark"
        data-icon="more"
        aria-hidden="true"
      />
      <span>更多</span>
    </button>
  </nav>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";

import type { NavigationItem } from "@/navigation";

const props = defineProps<{
  navigation: readonly NavigationItem[];
}>();

const emit = defineEmits<{
  "open-navigation": [];
}>();

const route = useRoute();
const isCeoNavigation = computed(() =>
  props.navigation.some((item) => item.id === "ceo"),
);
const ceoPriorityIds = ["sales", "communications", "review"] as const;
const otherPriorityIds = [
  "sales",
  "communications",
  "purchase",
  "product",
  "review",
  "integrations",
] as const;

const quickItems = computed(() => {
  const priorities = isCeoNavigation.value ? ceoPriorityIds : otherPriorityIds;
  return priorities
    .map((id) => props.navigation.find((item) => item.id === id))
    .filter((item): item is NavigationItem => Boolean(item))
    .slice(0, 3);
});

const showDrawerControl = computed(
  () => isCeoNavigation.value || props.navigation.length > quickItems.value.length,
);

const compactLabel = (item: NavigationItem) => {
  if (!isCeoNavigation.value) {
    return item.label;
  }
  return {
    sales: "销售",
    communications: "沟通",
    review: "审核",
  }[item.id] ?? item.label;
};

const isCurrent = (path: string) =>
  route.path === path || route.path.startsWith(`${path}/`);
</script>

<style scoped>
.mobile-bottom-nav {
  position: fixed;
  z-index: 700;
  right: 0;
  bottom: 0;
  left: 0;
  display: none;
  min-height: calc(60px + env(safe-area-inset-bottom));
  grid-template-columns: repeat(auto-fit, minmax(44px, 1fr));
  align-items: start;
  padding: 4px 6px max(4px, env(safe-area-inset-bottom));
  border-top: 1px solid var(--gbos-border);
  background: rgb(255 255 255 / 97%);
  box-shadow: 0 -8px 24px rgb(15 23 42 / 8%);
}

.mobile-bottom-nav__item {
  display: grid;
  min-width: 44px;
  min-height: 52px;
  align-content: center;
  justify-items: center;
  gap: 2px;
  padding: 4px;
  border: 0;
  border-radius: 10px;
  color: var(--gbos-muted);
  background: transparent;
  font: inherit;
  font-size: 10px;
  font-weight: 700;
  text-decoration: none;
  cursor: pointer;
}

.mobile-bottom-nav__item[aria-current="page"],
.mobile-bottom-nav__item.router-link-active {
  color: var(--gbos-primary);
  background: rgb(108 92 231 / 8%);
}

.mobile-bottom-nav__mark {
  display: grid;
  height: 24px;
  place-items: center;
}

.mobile-bottom-nav__mark::before {
  width: 12px;
  height: 12px;
  border: 2px solid currentcolor;
  border-radius: 4px;
  content: "";
}

.mobile-bottom-nav__mark[data-icon="more"]::before {
  width: 18px;
  height: 4px;
  border: 0;
  border-radius: 4px;
  background:
    radial-gradient(circle, currentcolor 2px, transparent 2.5px) left center / 6px
      4px repeat-x;
}

@media (max-width: 767px) {
  .mobile-bottom-nav {
    display: grid;
  }
}
</style>
