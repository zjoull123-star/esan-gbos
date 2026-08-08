<template>
  <aside class="workspace-sidebar">
    <RouterLink class="workspace-sidebar__brand" to="/gbos">
      <span class="workspace-sidebar__mark" aria-hidden="true">E</span>
      <span>
        <strong>ESAN GBOS</strong>
        <small>治理型业务操作系统</small>
      </span>
    </RouterLink>

    <nav aria-label="工作区导航">
      <section
        v-for="group in visibleGroups"
        :key="group.id"
        class="workspace-sidebar__group"
      >
        <h2>{{ group.label }}</h2>
        <ul>
          <li v-for="item in group.items" :key="item.id">
            <RouterLink
              class="workspace-sidebar__link"
              :class="{ 'workspace-sidebar__link--current': isCurrent(item.to) }"
              :to="item.to"
              :aria-current="isCurrent(item.to) ? 'page' : undefined"
            >
              <span class="workspace-sidebar__link-mark" aria-hidden="true" />
              <span>{{ item.label }}</span>
            </RouterLink>
          </li>
        </ul>
      </section>
    </nav>
  </aside>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";

import type { NavigationGroupId, NavigationItem } from "@/navigation";

const props = defineProps<{
  navigation: readonly NavigationItem[];
}>();

const route = useRoute();
const groups: readonly { id: NavigationGroupId; label: string }[] = [
  { id: "management", label: "经营管理" },
  { id: "operations", label: "业务协同" },
  { id: "intelligence", label: "智能与审核" },
  { id: "system", label: "系统与集成" },
];

const visibleGroups = computed(() =>
  groups
    .map((group) => ({
      ...group,
      items: props.navigation.filter((item) => item.group === group.id),
    }))
    .filter((group) => group.items.length > 0),
);

const isCurrent = (path: string) =>
  route.path === path || route.path.startsWith(`${path}/`);
</script>

<style scoped>
.workspace-sidebar {
  position: sticky;
  top: 0;
  display: flex;
  width: 240px;
  min-width: 0;
  height: 100dvh;
  flex-direction: column;
  overflow-y: auto;
  padding: 22px 16px;
  color: #cbd5e1;
  background: var(--gbos-sidebar);
}

.workspace-sidebar__brand {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 12px;
  margin: 0 4px 26px;
  color: white;
  text-decoration: none;
}

.workspace-sidebar__mark {
  display: grid;
  width: 38px;
  height: 38px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 12px;
  color: white;
  background: var(--gbos-primary);
  font-size: 19px;
  font-weight: 800;
}

.workspace-sidebar__brand strong,
.workspace-sidebar__brand small {
  display: block;
}

.workspace-sidebar__brand strong {
  font-size: 14px;
  letter-spacing: 0.03em;
}

.workspace-sidebar__brand small {
  margin-top: 2px;
  color: #94a3b8;
  font-size: 10px;
}

nav {
  display: block;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}

.workspace-sidebar__group + .workspace-sidebar__group {
  margin-top: 20px;
}

.workspace-sidebar__group h2 {
  margin: 0 10px 7px;
  color: #94a3b8;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.workspace-sidebar__group ul {
  display: grid;
  gap: 3px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.workspace-sidebar__link {
  display: flex;
  min-height: 42px;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border-radius: 12px;
  color: #cbd5e1;
  font-size: 14px;
  font-weight: 650;
  text-decoration: none;
  transition:
    color 160ms ease,
    background-color 160ms ease;
}

.workspace-sidebar__link:hover {
  color: white;
  background: rgb(255 255 255 / 7%);
}

.workspace-sidebar__link--current,
.workspace-sidebar__link.router-link-active {
  color: white;
  background: rgb(108 92 231 / 24%);
}

.workspace-sidebar__link-mark {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: #64748b;
}

.workspace-sidebar__link--current .workspace-sidebar__link-mark,
.workspace-sidebar__link.router-link-active .workspace-sidebar__link-mark {
  background: #a99df3;
}

@media (max-width: 800px) {
  .workspace-sidebar {
    position: static;
    width: 100%;
    height: auto;
  }
}
</style>
