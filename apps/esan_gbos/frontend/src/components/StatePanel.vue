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
      <button
        v-if="kind !== 'loading'"
        class="button button--secondary"
        type="button"
        @click="$emit('retry')"
      >
        {{ content.action }}
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";

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
