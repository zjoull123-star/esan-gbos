<template>
  <section class="view overview-view">
    <PageHeader
      title="产品总览"
      eyebrow="ESAN GBOS · 角色入口"
      description="仅显示当前 Frappe 会话已经授权的业务工作台。"
    />

    <dl class="overview-runtime" aria-label="当前运行状态">
      <div>
        <dt>当前用户</dt>
        <dd>{{ sessionState.user }}</dd>
      </div>
      <div>
        <dt>运行方式</dt>
        <dd>在线优先 · 不保留业务离线快照</dd>
      </div>
    </dl>

    <ul class="overview-modules" aria-label="已授权工作台">
      <li v-for="item in navigation" :key="item.id">
        <article class="overview-module">
          <div>
            <p class="eyebrow">
              {{ groupLabels[item.group] }}
            </p>
            <h2>{{ item.label }}</h2>
          </div>
          <RouterLink class="button button--secondary" :to="item.to">
            进入{{ item.label }}
          </RouterLink>
        </article>
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";

import PageHeader from "@/components/layout/PageHeader.vue";
import {
  navigationForRoles,
  type NavigationGroupId,
} from "@/navigation";
import { sessionState } from "@/session";

const navigation = computed(() => navigationForRoles(sessionState.roles));
const groupLabels: Record<NavigationGroupId, string> = {
  management: "经营管理",
  operations: "业务协同",
  intelligence: "智能与审核",
  system: "系统与集成",
};
</script>

<style scoped>
.overview-view {
  display: grid;
  gap: 16px;
}

.overview-runtime {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
  gap: 8px;
  margin: 0;
}

.overview-runtime > div {
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--gbos-border, var(--line));
  border-radius: 12px;
  background: var(--gbos-surface, var(--paper));
}

.overview-runtime dt {
  color: var(--gbos-muted, var(--muted));
  font-size: 12px;
  font-weight: 700;
}

.overview-runtime dd {
  margin: 4px 0 0;
  color: var(--gbos-text, var(--forest));
  font-size: 14px;
  font-weight: 650;
  overflow-wrap: anywhere;
}

.overview-modules {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.overview-module {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--gbos-border, var(--line));
  border-radius: 14px;
  background: var(--gbos-surface, var(--paper));
}

.overview-module h2 {
  margin: 3px 0 0;
  color: var(--gbos-text, var(--forest));
  font-size: 17px;
}

@media (max-width: 520px) {
  .overview-module {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
