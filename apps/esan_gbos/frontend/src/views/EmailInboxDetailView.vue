<template>
  <section class="email-detail-view">
    <PageHeader
      eyebrow="CRM · GOVERNED EMAIL"
      title="邮件处理详情"
      description="安全投影、人工分配、会话建议与草稿编辑相互分离；本页面不提供外发控制。"
    >
      <template #actions>
        <a class="back-link" href="/gbos/email">返回收件箱</a>
        <GbosButton ref="refreshButton" data-detail-refresh intent="secondary" type="button" @click="refresh">
          刷新
        </GbosButton>
      </template>
    </PageHeader>

    <p v-if="commandPending" data-command-pending class="sr-status" role="status">
      正在保存受控操作…
    </p>
    <p v-if="commandMessage" ref="commandMessageElement" class="command-message" role="alert" tabindex="-1">
      {{ commandMessage }}
    </p>

    <ResourceBoundary
      :state="resource.state.value"
      :message="resource.message.value"
      :request-id="resource.requestId.value"
      :empty="!detail"
      @retry="refresh"
    >
      <div v-if="detail" class="detail-layout">
        <section class="panel detail-summary" aria-labelledby="email-safe-summary-title">
          <p>{{ modeLabel(detail.mailbox_role) }}</p>
          <h2 id="email-safe-summary-title">
            {{ detail.mailbox_label }}
          </h2>
          <p>{{ detail.safe_summary }}</p>
          <dl>
            <div><dt>接收邮箱</dt><dd>{{ detail.mailbox_label }}</dd></div>
            <div><dt>渠道账户负责人</dt><dd>当前安全详情未提供渠道账户负责人标签</dd></div>
            <div><dt>参与者身份状态</dt><dd>{{ identityLabel(detail.identity_state) }}</dd></div>
            <div><dt>客户 Party / Contact</dt><dd>当前安全详情未提供客户 Party / Contact 标签</dd></div>
            <div><dt>当前业务负责人</dt><dd>{{ detail.assignee_label || "未分配" }}</dd></div>
            <div><dt>版本</dt><dd>{{ detail.revision }}</dd></div>
          </dl>
        </section>

        <InboxAssignmentPanel
          :detail="detail"
          :pending="commandPending"
          @claim="claim"
          @reassign="reassign"
          @transition="transition"
        />
        <IdentityProjectionPanel :detail="detail" />
        <ThreadSuggestionPanel :pending="commandPending" />
        <ConversationTimeline :detail="detail" />
        <BusinessLinkPanel :pending="commandPending" @link="linkBusiness" />
        <ReplyDraftEditor :pending="commandPending || draftBlocked" @save="saveDraft" />
        <p v-if="draftBlocked" class="command-message" role="alert">
          草稿版本已冲突，而当前详情接口不能重新读取草稿版本；请离开本页后由管理员核对。
        </p>

        <section class="panel reveal-panel" aria-labelledby="reveal-title">
          <h2 id="reveal-title">
            受限原文
          </h2>
          <p><strong>授权原文默认隐藏</strong></p>
          <p>只有详情响应提供与本收件项绑定的合格证据后，才能发起一次性显式查看；当前安全投影不包含该引用。</p>
        </section>
      </div>
    </ResourceBoundary>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from "vue";

import { BffError } from "@/api/bff";
import { useEmailGatewayClient } from "@/api/email-gateway";
import type { EmailBusinessMode, EmailIdentityState } from "@/api/email-gateway-types";
import BusinessLinkPanel from "@/components/email/BusinessLinkPanel.vue";
import ConversationTimeline from "@/components/email/ConversationTimeline.vue";
import IdentityProjectionPanel from "@/components/email/IdentityProjectionPanel.vue";
import InboxAssignmentPanel from "@/components/email/InboxAssignmentPanel.vue";
import ReplyDraftEditor from "@/components/email/ReplyDraftEditor.vue";
import ThreadSuggestionPanel from "@/components/email/ThreadSuggestionPanel.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const props = defineProps<{ inboxItemRef: string }>();
const client = useEmailGatewayClient();
const commandPending = ref(false);
const commandMessage = ref("");
const commandMessageElement = ref<HTMLElement>();
const refreshButton = ref<InstanceType<typeof GbosButton>>();
const draftRef = ref(`DRF-ui-${Date.now().toString(36)}`);
const draftRevision = ref(0);
const draftBlocked = ref(false);
let commandGeneration = 0;

const resource = useOnlineResource(async () => (await client.getInboxItem(props.inboxItemRef)).data);
const detail = computed(() => resource.data.value?.inbox_item);
const modeLabel = (value: EmailBusinessMode) => ({ primary: "主入口", selective_archive: "选择性归档", migration: "迁移邮箱" })[value];
const identityLabel = (value: EmailIdentityState) => ({ unknown: "未知", confirmed: "已确认", revoked: "已撤销" })[value];
const idempotencyKey = (action: string) => `${action}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

const focusMessage = async () => { await nextTick(); commandMessageElement.value?.focus(); };
const focusRefresh = async () => {
  await nextTick();
  const candidate = (refreshButton.value as unknown as { $el?: unknown } | undefined)?.$el;
  if (candidate instanceof HTMLElement) candidate.focus();
  else document.querySelector<HTMLElement>("[data-detail-refresh]")?.focus();
};
const refresh = async () => {
  commandGeneration += 1;
  commandPending.value = false;
  commandMessage.value = "";
  await resource.load();
  if (resource.state.value === "permission") {
    draftRef.value = "";
    draftRevision.value = 0;
    draftBlocked.value = true;
    await focusRefresh();
  }
};
const runCommand = async <T,>(
  operation: () => Promise<T>,
  success: string,
  onSuccess?: (result: T) => void,
  onConflict?: () => void,
  reloadAfterSuccess = true,
) => {
  if (commandPending.value) return;
  const generation = ++commandGeneration;
  commandPending.value = true;
  commandMessage.value = "";
  try {
    const result = await operation();
    if (generation !== commandGeneration) return;
    onSuccess?.(result);
    commandMessage.value = success;
    if (reloadAfterSuccess) await resource.load();
    await focusMessage();
  } catch (error) {
    if (generation !== commandGeneration) return;
    if (error instanceof BffError) {
      commandMessage.value = error.displayMessage;
      if (error.status === 403 || error.code === "permission_denied") {
        draftRef.value = "";
        draftRevision.value = 0;
        draftBlocked.value = true;
        resource.clear();
      }
      if (error.status === 409 || error.code === "revision_conflict") {
        onConflict?.();
        await resource.load();
        await focusRefresh();
      } else {
        await focusMessage();
      }
    } else {
      commandMessage.value = "操作未完成，请刷新后重试。";
      await focusMessage();
    }
  } finally {
    if (generation === commandGeneration) commandPending.value = false;
  }
};
const claim = () => {
  if (!detail.value) return;
  void runCommand(() => client.claimInbox({
    inbox_item_ref: detail.value!.inbox_item_ref,
    expected_revision: detail.value!.revision,
    idempotency_key: idempotencyKey("claim"),
  }), "认领已记录。 ");
};
const reassign = (assigneeUserRef?: string) => {
  if (!detail.value) return;
  void runCommand(() => client.reassignInbox({
    inbox_item_ref: detail.value!.inbox_item_ref,
    ...(assigneeUserRef ? { assignee_user_ref: assigneeUserRef } : {}),
    expected_revision: detail.value!.revision,
    idempotency_key: idempotencyKey("reassign"),
  }), assigneeUserRef ? "负责人已更新。 " : "分配已取消。 ");
};
const transition = (targetState: import("@/api/email-gateway-types").EmailInboxState) => {
  if (!detail.value) return;
  void runCommand(() => client.transitionInbox({
    inbox_item_ref: detail.value!.inbox_item_ref,
    target_state: targetState,
    expected_revision: detail.value!.revision,
    idempotency_key: idempotencyKey("transition"),
  }), targetState === "assigned" ? "邮件已重新打开。 " : "处理状态已更新。 ");
};
const linkBusiness = (businessRef: string) => {
  if (!detail.value) return;
  void runCommand(() => client.linkBusiness({
    inbox_item_ref: detail.value!.inbox_item_ref,
    business_ref: businessRef,
    expected_revision: detail.value!.revision,
    idempotency_key: idempotencyKey("business-link"),
  }), "业务关联已保存。 ");
};
const saveDraft = (content: string) => {
  if (!detail.value || draftBlocked.value || !draftRef.value) return;
  void runCommand(() => client.saveDraft({
    inbox_item_ref: detail.value!.inbox_item_ref,
    draft_ref: draftRef.value,
    content,
    expected_revision: draftRevision.value,
    idempotency_key: idempotencyKey("draft"),
  }), "草稿已保存；尚未批准或发送。 ", (response) => {
    draftRef.value = response.data.draft.draft_ref;
    draftRevision.value = response.data.draft.revision;
  }, () => {
    draftRef.value = "";
    draftRevision.value = 0;
    draftBlocked.value = true;
  }, false);
};
onBeforeUnmount(() => {
  commandGeneration += 1;
  commandMessage.value = "";
  draftRef.value = "";
  draftRevision.value = 0;
  draftBlocked.value = true;
});
</script>

<style scoped>
.email-detail-view, .detail-layout, .panel { min-width: 0; }
.detail-layout { display: grid; grid-template-columns: repeat(2, minmax(min(100%, 300px), 1fr)); gap: 16px; }
.panel { overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.panel h2 { margin-top: 0; font-size: 18px; }
.detail-summary { grid-column: 1 / -1; }
.detail-summary > p:first-child { color: var(--gbos-accent-text); font-size: 12px; font-weight: 700; }
.detail-summary dl { display: grid; gap: 8px; }
.detail-summary dl div { display: flex; justify-content: space-between; gap: 12px; }
.detail-summary dt { color: var(--gbos-muted); }
.detail-summary dd { margin: 0; text-align: right; }
.back-link { display: inline-flex; min-height: 40px; align-items: center; color: var(--gbos-accent-text); font-weight: 700; }
.command-message { padding: 12px; border-inline-start: 4px solid var(--gbos-accent); background: var(--gbos-surface); }
.sr-status { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
@media (max-width: 900px) { .detail-layout { grid-template-columns: minmax(0, 1fr); } .detail-summary { grid-column: auto; } .detail-summary dl div { display: grid; gap: 4px; } .detail-summary dd { text-align: start; } }
</style>
