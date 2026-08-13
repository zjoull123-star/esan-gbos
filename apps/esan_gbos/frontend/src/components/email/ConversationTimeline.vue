<template>
  <section class="panel" aria-labelledby="timeline-title">
    <h2 id="timeline-title">
      会话时间线
    </h2>
    <p>当前安全详情只提供本收件项；完整会话成员与事件暂不可用。</p>
    <ol>
      <li><time :datetime="detail.received_at">{{ detail.received_at }}</time><p>{{ detail.safe_summary }}</p></li>
    </ol>
  </section>
</template>

<script setup lang="ts">
import type { EmailInboxDetail } from "@/api/email-gateway-types";
defineProps<{ detail: EmailInboxDetail }>();
</script>

<style scoped>
.panel { min-width: 0; overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.panel h2 { margin-top: 0; font-size: 18px; }
.panel ol { margin: 0; padding-inline-start: 20px; }
.panel time { color: var(--gbos-muted); }
</style>
