<template>
  <section
    class="state-panel"
    :class="`state-panel--${kind}`"
    :role="kind === 'error' ? 'alert' : 'status'"
    :aria-live="kind === 'error' ? 'assertive' : 'polite'"
    aria-atomic="true"
  >
    <span class="state-panel__icon" aria-hidden="true">{{ content.icon }}</span>
    <div>
      <h2>{{ content.title }}</h2>
      <p>{{ message || content.detail }}</p>
      <p v-if="requestId" class="state-panel__request">
        请求编号：<code>{{ requestId }}</code>
      </p>
      <GbosButton
        v-if="kind !== 'loading'"
        intent="secondary"
        type="button"
        @click="$emit('retry')"
      >
        {{ content.action }}
      </GbosButton>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";

import GbosButton from "@/components/ui/GbosButton.vue";

type StateKind = "loading" | "empty" | "permission" | "offline" | "error";

const props = defineProps<{
  kind: StateKind;
  message?: string;
  requestId?: string;
}>();

defineEmits<{ retry: [] }>();

const copy = {
  loading: {
    icon: "···",
    title: "正在读取最新数据",
    detail: "请稍候，页面不会使用离线业务快照。",
    action: "",
  },
  empty: {
    icon: "○",
    title: "暂无符合条件的数据",
    detail: "可以清除筛选条件或稍后重新读取。",
    action: "清除筛选并重试",
  },
  permission: {
    icon: "锁",
    title: "当前角色无权查看此页面",
    detail: "请选择导航中可用的工作台；如职责已变更，请联系管理员。",
    action: "返回可用工作台",
  },
  offline: {
    icon: "断",
    title: "需要联网",
    detail: "为保护客户与业务数据，离线时不显示敏感快照。",
    action: "重新连接",
  },
  error: {
    icon: "!",
    title: "暂时无法读取数据",
    detail: "请重试；若问题持续，请将请求编号提供给管理员。",
    action: "重新读取",
  },
} as const;

const content = computed(() => copy[props.kind]);
</script>

<style scoped>
.state-panel {
  display: grid;
  min-width: 0;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 16px;
  padding: clamp(18px, 4vw, 28px);
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.state-panel__icon {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 50%;
  color: var(--gbos-surface);
  background: var(--gbos-primary);
  font-weight: 800;
}

.state-panel--error .state-panel__icon {
  background: rgb(190 24 93);
}

.state-panel h2,
.state-panel p {
  margin: 0;
}

.state-panel h2 {
  font-size: 18px;
  line-height: 1.4;
}

.state-panel p {
  max-width: 54rem;
  margin-top: 6px;
  color: var(--gbos-muted);
  line-height: 1.6;
}

.state-panel :deep(.gbos-button) {
  margin-top: 14px;
}

.state-panel__request code {
  overflow-wrap: anywhere;
}

@media (max-width: 520px) {
  .state-panel {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
