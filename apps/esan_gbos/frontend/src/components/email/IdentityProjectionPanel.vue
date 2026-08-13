<template>
  <section class="panel" aria-labelledby="identity-title">
    <h2 id="identity-title">
      参与者身份
    </h2>
    <dl>
      <div><dt>参与者身份状态</dt><dd>{{ identityLabel }}</dd></div>
      <div><dt>客户 Party / Contact</dt><dd>{{ detail.identity_state === "confirmed" ? "已确认映射（受控详情不展开）" : "尚未确认" }}</dd></div>
    </dl>
    <a v-if="detail.identity_state !== 'confirmed'" href="/gbos/review">进入身份治理审核</a>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { EmailInboxDetail } from "@/api/email-gateway-types";
const props = defineProps<{ detail: EmailInboxDetail }>();
const identityLabel = computed(() => ({ unknown: "未知，等待人工确认", confirmed: "已由授权人员确认", revoked: "已撤销，不可归因" })[props.detail.identity_state]);
</script>

<style scoped>
.panel { min-width: 0; overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.panel h2 { margin-top: 0; font-size: 18px; }
.panel dl { display: grid; gap: 8px; }
.panel dl div { display: flex; justify-content: space-between; gap: 12px; }
.panel dt { color: var(--gbos-muted); }
.panel dd { margin: 0; text-align: right; }
.panel a { display: inline-flex; min-height: 40px; align-items: center; color: var(--gbos-accent-text); font-weight: 700; }
</style>
