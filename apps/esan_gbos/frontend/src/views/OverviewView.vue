<template>
  <section class="view" aria-labelledby="overview-title">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          ESAN GBOS · 角色入口
        </p>
        <h1 id="overview-title">
          产品总览
        </h1>
        <p>仅显示当前 Frappe 会话已经授权的业务工作台。</p>
      </div>
    </header>

    <dl class="metric-facts" aria-label="当前运行状态">
      <div>
        <dt>当前用户</dt>
        <dd>{{ sessionState.user }}</dd>
      </div>
      <div>
        <dt>运行方式</dt>
        <dd>在线优先 · 不保留业务离线快照</dd>
      </div>
    </dl>

    <ul class="record-grid" aria-label="已授权工作台">
      <li v-for="item in navigation" :key="item.id" class="record-card">
        <p class="eyebrow">
          {{ groupLabels[item.group] }}
        </p>
        <h2>{{ item.label }}</h2>
        <RouterLink class="button button--secondary" :to="item.to">
          进入{{ item.label }}
        </RouterLink>
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";

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
