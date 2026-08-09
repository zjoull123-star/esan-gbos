<template>
  <StatePanel
    v-if="state === 'idle' || state === 'loading'"
    kind="loading"
  />
  <StatePanel
    v-else-if="state === 'offline'"
    kind="offline"
    :message="message"
    @retry="$emit('retry')"
  />
  <StatePanel
    v-else-if="state === 'permission'"
    kind="permission"
    :message="message"
    @retry="$emit('retry')"
  />
  <StatePanel
    v-else-if="state === 'error'"
    kind="error"
    :message="message"
    :request-id="requestId"
    @retry="$emit('retry')"
  />
  <StatePanel
    v-else-if="state === 'ready' && empty"
    kind="empty"
    :message="message"
    @retry="$emit('retry')"
  />
  <slot v-else-if="state === 'ready'" />
</template>

<script setup lang="ts">
import StatePanel from "@/components/StatePanel.vue";
import type { ResourceState } from "@/composables/useOnlineResource";

withDefaults(
  defineProps<{
    state: ResourceState;
    message?: string;
    requestId?: string;
    empty?: boolean;
  }>(),
  {
    message: undefined,
    requestId: undefined,
    empty: false,
  },
);

defineEmits<{ retry: [] }>();
</script>
