<template>
  <ul class="inbox-list" aria-label="邮件安全摘要">
    <li v-for="item in items" :key="item.inbox_item_ref" class="inbox-card">
      <div class="inbox-card__heading">
        <div><p>{{ modeLabel(item.mailbox_role) }}</p><h2>{{ item.mailbox_label }}</h2></div>
        <span>{{ stateLabel(item.state) }}</span>
      </div>
      <p class="inbox-card__summary">
        {{ item.safe_summary }}
      </p>
      <dl>
        <div><dt>团队</dt><dd>{{ item.team_label || "待确定" }}</dd></div>
        <div><dt>接收时间</dt><dd>{{ item.received_at }}</dd></div>
      </dl>
      <a
        data-inbox-detail
        class="inbox-card__link"
        :href="detailHref(item.inbox_item_ref)"
        @click="openDetail($event, item.inbox_item_ref)"
      >查看安全详情</a>
    </li>
  </ul>
</template>

<script setup lang="ts">
import type { EmailBusinessMode, EmailInboxItem, EmailInboxState } from "@/api/email-gateway-types";

defineProps<{ items: readonly EmailInboxItem[] }>();
const detailHref = (reference: string) => `/gbos/email/${encodeURIComponent(reference)}`;
const openDetail = (event: MouseEvent, reference: string) => {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  window.history.pushState({}, "", detailHref(reference));
  window.dispatchEvent(new PopStateEvent("popstate"));
};
const modeLabel = (value: EmailBusinessMode) => ({ primary: "主入口", selective_archive: "选择性归档", migration: "迁移邮箱" })[value];
const stateLabel = (value: EmailInboxState) => ({
  identity_pending: "身份待确认", unassigned: "待分配", assigned: "处理中", draft: "草稿",
  waiting_internal: "等待内部", waiting_customer: "等待客户", converted: "已转化", closed: "已关闭",
  quarantined: "隔离", send_queued: "已进入受控外发队列", send_uncertain: "发送结果不确定",
})[value];
</script>

<style scoped>
.inbox-list { display: grid; gap: 12px; min-width: 0; margin: 0; padding: 0; list-style: none; }
.inbox-card { min-width: 0; overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.inbox-card__heading { display: flex; justify-content: space-between; gap: 12px; }
.inbox-card p, .inbox-card h2 { margin: 0; }
.inbox-card__heading p { color: var(--gbos-accent-text); font-size: 12px; font-weight: 700; }
.inbox-card h2 { font-size: 18px; }
.inbox-card__summary { margin-top: 12px !important; line-height: 1.55; }
.inbox-card dl { display: grid; gap: 8px; }
.inbox-card dl div { display: flex; justify-content: space-between; gap: 12px; }
.inbox-card dt { color: var(--gbos-muted); }
.inbox-card dd { margin: 0; text-align: right; }
.inbox-card__link { display: inline-flex; min-height: 40px; align-items: center; margin-top: 8px; color: var(--gbos-accent-text); font-weight: 700; }
</style>
