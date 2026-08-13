<template>
  <section class="panel" aria-labelledby="draft-title">
    <h2 id="draft-title">
      回复草稿
    </h2>
    <p>仅创建或编辑草稿。本页面不提供批准、发送或外发状态操作。</p>
    <form autocomplete="off" @submit.prevent="save">
      <label for="reply-draft-content">草稿内容</label>
      <textarea id="reply-draft-content" v-model="content" maxlength="131072" rows="8" required />
      <GbosButton type="submit" :disabled="pending || !content.trim()">
        保存草稿
      </GbosButton>
    </form>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref } from "vue";
import GbosButton from "@/components/ui/GbosButton.vue";
defineProps<{ pending?: boolean }>();
const emit = defineEmits<{ save: [content: string] }>();
const content = ref("");
const save = () => { if (content.value.trim()) emit("save", content.value); };
onBeforeUnmount(() => { content.value = ""; });
</script>

<style scoped>
.panel { min-width: 0; overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.panel h2 { margin-top: 0; font-size: 18px; }
.panel form { display: grid; gap: 8px; }
.panel textarea { min-width: 0; max-width: 100%; padding: 10px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-control); resize: vertical; }
</style>
