<template>
  <section class="view">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          客户编号 · {{ id }}
        </p>
        <h1>客户 360</h1>
        <p>仅显示当前 Frappe session 获授权的客户上下文。</p>
      </div>
      <button class="button button--secondary" type="button" @click="load">
        刷新
      </button>
    </header>
    <StatePanel v-if="state === 'loading' || state === 'idle'" kind="loading" />
    <StatePanel
      v-else-if="state === 'offline'"
      kind="offline"
      :message="message"
      @retry="load"
    />
    <StatePanel
      v-else-if="state === 'permission'"
      kind="permission"
      :message="message"
      @retry="load"
    />
    <StatePanel
      v-else-if="state === 'error'"
      kind="error"
      :message="message"
      :request-id="requestId"
      @retry="load"
    />
    <StatePanel v-else-if="records.length === 0" kind="empty" @retry="load" />
    <RecordGrid v-else :records="records" />
  </section>
</template>

<script setup lang="ts">
import { computed, watch } from "vue";

import { useBffClient } from "@/api/injection";
import RecordGrid from "@/components/RecordGrid.vue";
import StatePanel from "@/components/StatePanel.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import { flattenParty360Payload } from "@/presentation";

const props = defineProps<{ id: string }>();
const client = useBffClient();
const resource = useOnlineResource(async () => {
  const response = await client.getParty360(props.id);
  return response.data;
});
const records = computed(
  () => flattenParty360Payload(resource.data.value).records,
);
const { state, message, requestId, load } = resource;
watch(
  () => props.id,
  () => {
    void load();
  },
);
</script>
