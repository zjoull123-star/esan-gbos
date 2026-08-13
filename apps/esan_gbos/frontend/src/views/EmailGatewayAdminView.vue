<template>
  <section class="email-gateway-admin-view">
    <OperationalListTemplate>
      <template #header>
        <PageHeader
          eyebrow="EMAIL GATEWAY · PHASE 1"
          title="邮件网关配置台"
          description="管理多个并列主入口及其安全状态。所有变更都携带当前 revision；本阶段外发始终关闭。"
        >
          <template #actions>
            <GbosButton intent="secondary" type="button" @click="refreshAll">
              刷新全部
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template #list>
        <p v-if="notice" class="email-gateway-notice" role="status">
          {{ notice }}
        </p>
        <p v-if="commandError" class="email-gateway-error" role="alert">
          {{ commandError }}
        </p>
        <form data-mailbox-create class="email-mailbox-create" @submit.prevent="createMailbox">
          <div>
            <h2>新增邮箱入口</h2>
            <p>新邮箱默认使用模拟接入、主入口与关闭外发；启用前仍需单独确认。</p>
          </div>
          <label>
            显示名称
            <input
              v-model="createForm.displayLabel"
              name="display_label"
              required
              maxlength="240"
            >
          </label>
          <label>
            接入类型
            <select v-model="createForm.providerKind" name="provider_kind" required>
              <option value="fake">模拟接入</option>
              <option value="imap_smtp">IMAP / SMTP</option>
              <option value="wecom_app_mail">企业微信应用邮箱</option>
            </select>
          </label>
          <label>
            业务角色
            <select v-model="createForm.businessMode" name="business_mode" required>
              <option value="primary">主入口</option>
              <option value="selective_archive">选择性归档</option>
              <option value="migration">迁移邮箱</option>
            </select>
          </label>
          <label>
            业务用途
            <select v-model="createForm.businessPurpose" name="business_purpose" required>
              <option value="sales_follow_up">销售跟进</option>
              <option value="customer_service">客户服务</option>
              <option value="business_operations">业务运营</option>
              <option value="procurement_coordination">采购协同</option>
              <option value="product_sample_management">样品管理</option>
              <option value="risk_review">风险审核</option>
            </select>
          </label>
          <label>
            账号引用
            <input
              v-model="createForm.providerAccountRef"
              name="provider_account_ref"
              required
              maxlength="256"
            >
          </label>
          <label>
            连接器引用
            <input
              v-model="createForm.observerConnectorInstanceRef"
              name="observer_connector_instance_ref"
              required
              maxlength="140"
            >
          </label>
          <label>
            默认团队
            <input v-model="createForm.defaultTeamRef" name="default_team_ref" required maxlength="140">
          </label>
          <label>
            账号负责人
            <input
              v-model="createForm.accountOwnerUserRef"
              name="account_owner_user_ref"
              required
              maxlength="140"
            >
          </label>
          <label>
            优先级
            <input v-model.number="createForm.priority" name="priority" type="number" min="0" max="1000" required>
          </label>
          <label>
            凭据引用
            <input v-model="createForm.credentialRef" name="credential_ref" required maxlength="128">
          </label>
          <GbosButton type="submit">
            新增主入口
          </GbosButton>
        </form>
        <ResourceBoundary
          :state="mailboxResource.state.value"
          :message="mailboxBoundaryMessage"
          :request-id="mailboxResource.requestId.value"
          :empty="mailboxes.length === 0"
          @retry="mailboxResource.load"
        >
          <ul class="email-mailbox-grid" aria-label="邮箱配置">
            <li
              v-for="mailbox in mailboxes"
              :key="mailbox.mailbox_ref"
              class="email-mailbox-card"
              :data-mailbox="mailbox.mailbox_ref"
              :data-mailbox-mode="mailbox.business_mode"
            >
              <div class="email-mailbox-card__heading">
                <div>
                  <p>{{ providerLabel(mailbox.provider_kind) }}</p>
                  <h2>{{ mailbox.display_label }}</h2>
                </div>
                <span>{{ statusLabel(mailbox.status) }}</span>
              </div>
              <dl>
                <div><dt>业务角色</dt><dd>{{ modeLabel(mailbox.business_mode) }}</dd></div>
                <div><dt>业务用途</dt><dd>{{ mailbox.business_purpose }}</dd></div>
                <div><dt>默认团队</dt><dd>{{ mailbox.default_team_label || "待设置" }}</dd></div>
                <div><dt>账号负责人</dt><dd>{{ mailbox.account_owner_label || "待设置" }}</dd></div>
                <div><dt>收件</dt><dd>{{ mailbox.inbound_enabled ? "允许" : "关闭" }}</dd></div>
                <div><dt>外发</dt><dd>关闭</dd></div>
                <div><dt>配置版本</dt><dd>{{ mailbox.config_revision }}</dd></div>
              </dl>
              <div class="email-mailbox-actions" aria-label="邮箱安全状态操作">
                <GbosButton
                  v-if="mailbox.status === 'draft' || mailbox.status === 'paused'"
                  data-status-action="enable"
                  type="button"
                  @click="requestStatus(mailbox, 'enable')"
                >
                  启用
                </GbosButton>
                <GbosButton
                  v-if="mailbox.status === 'active' || mailbox.status === 'error'"
                  data-status-action="pause"
                  intent="secondary"
                  type="button"
                  @click="requestStatus(mailbox, 'pause')"
                >
                  暂停
                </GbosButton>
                <GbosButton
                  v-if="mailbox.status !== 'revoked'"
                  data-status-action="revoke"
                  intent="danger"
                  type="button"
                  @click="requestStatus(mailbox, 'revoke')"
                >
                  撤销
                </GbosButton>
              </div>
            </li>
          </ul>
        </ResourceBoundary>

        <section class="email-health-section" aria-labelledby="email-health-title">
          <h2 id="email-health-title">
            连接器实时健康状态
          </h2>
          <ResourceBoundary
            :state="healthResource.state.value"
            :message="healthBoundaryMessage"
            :request-id="healthResource.requestId.value"
            :empty="health.length === 0"
            @retry="healthResource.load"
          >
            <ul class="email-health-grid" aria-label="连接器健康状态">
              <li v-for="item in health" :key="item.mailbox_ref">
                <strong>{{ item.mailbox_label }}</strong>
                <span>{{ healthLabel(item.status) }} · {{ freshnessLabel(item.freshness) }}</span>
                <span>积压 {{ item.backlog }}</span>
                <span>{{ item.last_success_at || "暂无成功时间" }}</span>
                <span>{{ item.safe_error_code || "无安全错误" }}</span>
              </li>
            </ul>
          </ResourceBoundary>
        </section>

        <section class="email-rules-section" aria-labelledby="email-rules-title">
          <h2 id="email-rules-title">
            分配规则
          </h2>
          <p>规则优先，无法确定时进入人工认领。团队与负责人必须通过服务端权限校验。</p>
          <form data-rule-upsert class="email-rule-form" autocomplete="off" @submit.prevent="saveRule">
            <label>团队引用<input v-model.trim="ruleForm.teamRef" name="team_ref" maxlength="140" required></label>
            <label>邮箱引用<input v-model.trim="ruleForm.mailboxRef" name="mailbox_ref" maxlength="140" required></label>
            <label>负责人引用<input v-model.trim="ruleForm.ownerUserRef" name="owner_user_ref" maxlength="140" required></label>
            <label>优先级<input v-model.number="ruleForm.priority" name="priority" type="number" min="0" max="1000" required></label>
            <label class="email-rule-form__check"><input v-model="ruleForm.enabled" name="enabled" type="checkbox">启用规则</label>
            <GbosButton type="submit">
              保存规则
            </GbosButton>
          </form>
          <ResourceBoundary
            :state="ruleResource.state.value"
            :message="ruleBoundaryMessage"
            :request-id="ruleResource.requestId.value"
            :empty="rules.length === 0"
            @retry="ruleResource.load"
          >
            <ul class="email-rule-grid" aria-label="分配规则">
              <li v-for="rule in rules" :key="rule.rule_ref">
                <strong>{{ rule.team_label || "未命名团队" }}</strong>
                <span>邮箱 {{ mailboxLabel(rule.mailbox_ref) }}</span>
                <span>负责人 {{ rule.owner_label || "待设置" }}</span>
                <span>优先级 {{ rule.priority }} · {{ rule.enabled ? "已启用" : "已停用" }}</span>
                <span>版本 {{ rule.revision }}</span>
              </li>
            </ul>
          </ResourceBoundary>
        </section>

        <section class="email-audit-section" aria-labelledby="email-audit-title">
          <h2 id="email-audit-title">
            审计与版本
          </h2>
          <p>服务端保留权威审计。浏览器只显示本次打开页面后的安全操作结果，不持久化配置、标识或业务数据。</p>
          <ul v-if="auditEvents.length" aria-label="本次页面操作记录">
            <li v-for="event in auditEvents" :key="event.id">
              {{ event.label }}
            </li>
          </ul>
          <p v-else>
            本次页面尚无配置变更。
          </p>
        </section>
      </template>
    </OperationalListTemplate>

    <div
      v-if="pendingStatus"
      class="email-status-confirm"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="email-status-confirm-title"
    >
      <div>
        <h2 id="email-status-confirm-title">
          确认邮箱状态变更
        </h2>
        <p>
          将“{{ pendingStatus.mailbox.display_label }}”执行“{{
            actionLabel(pendingStatus.action)
          }}”，依据配置版本 {{ pendingStatus.mailbox.config_revision }}。
        </p>
        <div>
          <GbosButton intent="secondary" type="button" @click="closeStatusDialog">
            取消
          </GbosButton>
          <GbosButton
            data-confirm-status
            intent="danger"
            type="button"
            @click="confirmStatus"
          >
            确认变更
          </GbosButton>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, reactive, ref } from "vue";

import { BffError } from "@/api/bff";
import { useEmailGatewayClient } from "@/api/email-gateway";
import type {
  EmailBusinessMode,
  EmailBusinessPurpose,
  EmailConnectorHealthState,
  EmailFreshnessState,
  EmailMailbox,
  EmailMailboxAction,
  EmailMailboxStatus,
  EmailProviderKind,
  EmailRoutingRule,
} from "@/api/email-gateway-types";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

interface PendingStatus {
  mailbox: EmailMailbox;
  action: EmailMailboxAction;
}

const client = useEmailGatewayClient();
const pendingStatus = ref<PendingStatus>();
const replacements = ref(new Map<string, EmailMailbox>());
const notice = ref("");
const commandError = ref("");
const statusTrigger = ref<HTMLElement>();
const auditEvents = ref<{ id: string; label: string }[]>([]);
const ruleReplacements = ref(new Map<string, EmailRoutingRule>());
const ruleForm = reactive({ teamRef: "", mailboxRef: "", ownerUserRef: "", priority: 10, enabled: true });
const createForm = reactive<{
  displayLabel: string;
  providerKind: EmailProviderKind;
  businessMode: EmailBusinessMode;
  businessPurpose: EmailBusinessPurpose;
  providerAccountRef: string;
  observerConnectorInstanceRef: string;
  defaultTeamRef: string;
  accountOwnerUserRef: string;
  priority: number;
  credentialRef: string;
}>({
  displayLabel: "",
  providerKind: "fake",
  businessMode: "primary",
  businessPurpose: "sales_follow_up",
  providerAccountRef: "",
  observerConnectorInstanceRef: "",
  defaultTeamRef: "",
  accountOwnerUserRef: "",
  priority: 10,
  credentialRef: "",
});
const createIdempotencyKey = ref(`mailbox-create-${Date.now()}`);
const mailboxResource = useOnlineResource(async () => {
  replacements.value = new Map();
  const response = await client.listMailboxes({ pageSize: 25 });
  return response.data;
});
const healthResource = useOnlineResource(async () => {
  const response = await client.listConnectorHealth();
  return response.data;
});
const ruleResource = useOnlineResource(async () => {
  ruleReplacements.value = new Map();
  const response = await client.listRules(25);
  return response.data;
});
const mailboxes = computed(() =>
  (mailboxResource.data.value?.mailboxes ?? []).map(
    (mailbox) => replacements.value.get(mailbox.mailbox_ref) ?? mailbox,
  ),
);
const health = computed(() => healthResource.data.value?.connector_health ?? []);
const rules = computed(() => (ruleResource.data.value?.rules ?? []).map((rule) => ruleReplacements.value.get(rule.rule_ref) ?? rule));
const mailboxBoundaryMessage = computed(() =>
  mailboxResource.state.value === "ready" && mailboxes.value.length === 0
    ? "当前没有已配置邮箱。"
    : mailboxResource.message.value,
);
const healthBoundaryMessage = computed(() =>
  healthResource.state.value === "ready" && health.value.length === 0
    ? "当前没有可展示的连接器健康状态。"
    : healthResource.message.value,
);
const ruleBoundaryMessage = computed(() => ruleResource.state.value === "ready" && rules.value.length === 0
  ? "当前没有已配置的分配规则。" : ruleResource.message.value);

const providerLabel = (value: EmailProviderKind) =>
  ({ fake: "模拟接入", imap_smtp: "IMAP / SMTP", wecom_app_mail: "企业微信应用邮箱" })[
    value
  ];
const modeLabel = (value: EmailBusinessMode) =>
  ({ primary: "主入口", selective_archive: "选择性归档", migration: "迁移邮箱" })[
    value
  ];
const statusLabel = (value: EmailMailboxStatus) =>
  ({ draft: "草稿", active: "已启用", paused: "已暂停", revoked: "已撤销", error: "异常" })[
    value
  ];
const healthLabel = (value: EmailConnectorHealthState) =>
  ({ healthy: "健康", degraded: "降级", paused: "已暂停", revoked: "已撤销", unknown: "未知" })[
    value
  ];
const freshnessLabel = (value: EmailFreshnessState) =>
  ({ fresh: "新鲜", stale: "过期", unknown: "未知" })[value];
const actionLabel = (value: EmailMailboxAction) =>
  ({ enable: "启用", pause: "暂停", revoke: "撤销" })[value];
const mailboxLabel = (reference: string) => mailboxes.value.find((mailbox) => mailbox.mailbox_ref === reference)?.display_label ?? "受控邮箱";
const addAudit = (label: string) => {
  auditEvents.value = [{ id: `${Date.now()}-${auditEvents.value.length}`, label }, ...auditEvents.value].slice(0, 8);
};
const requestStatus = (mailbox: EmailMailbox, action: EmailMailboxAction) => {
  statusTrigger.value = document.activeElement instanceof HTMLElement ? document.activeElement : undefined;
  pendingStatus.value = { mailbox, action };
  notice.value = "";
  commandError.value = "";
};
const closeStatusDialog = async () => {
  pendingStatus.value = undefined;
  await nextTick();
  statusTrigger.value?.focus();
};
const confirmStatus = async () => {
  const pending = pendingStatus.value;
  if (!pending) {
    return;
  }
  try {
    const response = await client.setMailboxStatus({
      mailbox_ref: pending.mailbox.mailbox_ref,
      action: pending.action,
      expected_revision: pending.mailbox.config_revision,
      idempotency_key: `mailbox-${pending.action}-${pending.mailbox.mailbox_ref}-${pending.mailbox.config_revision}`,
    });
    replacements.value = new Map(replacements.value).set(
      response.data.mailbox.mailbox_ref,
      response.data.mailbox,
    );
    notice.value = `邮箱已${actionLabel(pending.action)}。`;
    addAudit(`邮箱“${pending.mailbox.display_label}”状态已${actionLabel(pending.action)}。`);
  } catch (error) {
    commandError.value = error instanceof BffError ? error.displayMessage : "状态变更未完成，请刷新后重试。";
    if (error instanceof BffError && error.status === 409) await mailboxResource.load();
  } finally {
    await closeStatusDialog();
  }
};
const createMailbox = async () => {
  notice.value = "";
  commandError.value = "";
  try {
    const response = await client.upsertMailbox({
      display_label: createForm.displayLabel,
      provider_kind: createForm.providerKind,
      business_mode: createForm.businessMode,
      business_purpose: createForm.businessPurpose,
      provider_account_ref: createForm.providerAccountRef,
      observer_connector_instance_ref: createForm.observerConnectorInstanceRef,
      default_team_ref: createForm.defaultTeamRef,
      account_owner_user_ref: createForm.accountOwnerUserRef,
      priority: createForm.priority,
      credential_ref: createForm.credentialRef,
      inbound_enabled: false,
      outbound_enabled: false,
      expected_revision: 0,
      idempotency_key: createIdempotencyKey.value,
    });
    replacements.value = new Map(replacements.value).set(
      response.data.mailbox.mailbox_ref,
      response.data.mailbox,
    );
    createForm.displayLabel = "";
    createForm.providerAccountRef = "";
    createForm.observerConnectorInstanceRef = "";
    createForm.defaultTeamRef = "";
    createForm.accountOwnerUserRef = "";
    createForm.priority = 10;
    createForm.credentialRef = "";
    createIdempotencyKey.value = `mailbox-create-${Date.now()}`;
    notice.value = "新邮箱入口已保存为关闭外发的安全配置。";
    addAudit(`邮箱“${response.data.mailbox.display_label}”配置已保存。`);
  } catch (error) {
    commandError.value = error instanceof BffError ? error.displayMessage : "邮箱入口未保存，请检查内容后重试。";
    if (error instanceof BffError && error.status === 409) await mailboxResource.load();
  }
};
const saveRule = async () => {
  notice.value = "";
  commandError.value = "";
  try {
    const response = await client.upsertRule({
      team_ref: ruleForm.teamRef,
      mailbox_ref: ruleForm.mailboxRef,
      owner_user_ref: ruleForm.ownerUserRef,
      priority: ruleForm.priority,
      enabled: ruleForm.enabled,
      expected_revision: 0,
      idempotency_key: `rule-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
    });
    ruleReplacements.value = new Map(ruleReplacements.value).set(response.data.rule.rule_ref, response.data.rule);
    notice.value = "分配规则已保存。";
    addAudit(`分配规则已保存，版本 ${response.data.rule.revision}。`);
    ruleForm.teamRef = "";
    ruleForm.mailboxRef = "";
    ruleForm.ownerUserRef = "";
    ruleForm.priority = 10;
  } catch (error) {
    commandError.value = error instanceof BffError ? error.displayMessage : "分配规则未保存，请检查后重试。";
    if (error instanceof BffError && error.status === 409) await ruleResource.load();
  }
};
const refreshAll = () => {
  void mailboxResource.load();
  void healthResource.load();
  void ruleResource.load();
};
</script>

<style scoped>
.email-gateway-admin-view,
.email-mailbox-grid,
.email-mailbox-card,
.email-health-section,
.email-health-grid,
.email-rules-section,
.email-rule-grid,
.email-audit-section {
  min-width: 0;
}

.email-gateway-notice,
.email-gateway-error {
  padding: 10px 12px;
  border-radius: var(--gbos-radius-control);
}

.email-mailbox-create {
  display: grid;
  grid-template-columns: minmax(0, 1fr) repeat(2, minmax(160px, 0.6fr)) auto;
  gap: 12px;
  align-items: end;
  min-width: 0;
  margin-bottom: 16px;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.email-mailbox-create h2,
.email-mailbox-create p {
  margin: 0;
}

.email-mailbox-create p {
  margin-top: 4px;
  color: var(--gbos-muted);
  font-size: 13px;
}

.email-mailbox-create label {
  display: grid;
  min-width: 0;
  gap: 6px;
  color: var(--gbos-muted);
  font-size: 13px;
  font-weight: 700;
}

.email-mailbox-create input {
  width: 100%;
  min-width: 0;
  min-height: 40px;
  padding: 8px 10px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-surface);
}

.email-gateway-notice {
  color: var(--gbos-success-text);
  background: var(--gbos-success-soft);
}

.email-gateway-error {
  color: var(--gbos-danger-text);
  background: var(--gbos-danger-soft);
}

.email-mailbox-grid,
.email-health-grid,
.email-rule-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.email-mailbox-card,
.email-health-grid li,
.email-rule-grid li {
  overflow-wrap: anywhere;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.email-mailbox-card__heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.email-mailbox-card p,
.email-mailbox-card h2,
.email-health-section h2,
.email-rules-section h2,
.email-audit-section h2 {
  margin: 0;
}

.email-mailbox-card p {
  color: var(--gbos-accent-text);
  font-size: 12px;
  font-weight: 700;
}

.email-mailbox-card h2,
.email-health-section h2,
.email-rules-section h2,
.email-audit-section h2 {
  font-size: 18px;
}

.email-mailbox-card dl {
  display: grid;
  gap: 8px;
}

.email-mailbox-card dl div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.email-mailbox-card dt {
  color: var(--gbos-muted);
}

.email-mailbox-card dd {
  margin: 0;
  text-align: right;
}

.email-mailbox-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.email-health-section,
.email-rules-section,
.email-audit-section {
  margin-top: 20px;
}

.email-health-grid,
.email-rule-grid {
  margin-top: 12px;
}

.email-health-grid li,
.email-rule-grid li {
  display: grid;
  gap: 6px;
}

.email-rule-form {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr)) auto auto;
  gap: 12px;
  align-items: end;
  margin: 12px 0;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.email-rule-form label { display: grid; min-width: 0; gap: 6px; color: var(--gbos-muted); font-size: 13px; font-weight: 700; }
.email-rule-form input { min-width: 0; min-height: 40px; padding: 8px 10px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-control); }
.email-rule-form__check { grid-template-columns: auto 1fr; align-items: center; }
.email-audit-section ul { margin: 0; padding-inline-start: 20px; }

.email-status-confirm {
  position: fixed;
  z-index: 60;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 16px;
  background: rgb(11 18 32 / 56%);
}

.email-status-confirm > div {
  width: min(440px, 100%);
  padding: 20px;
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.email-status-confirm h2,
.email-status-confirm p {
  margin: 0;
}

.email-status-confirm p {
  margin-top: 8px;
  line-height: 1.55;
}

.email-status-confirm > div > div {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 20px;
}

@media (max-width: 767px) {
  .email-mailbox-create {
    grid-template-columns: minmax(0, 1fr);
  }

  .email-rule-form { grid-template-columns: minmax(0, 1fr); }

  .email-mailbox-card__heading,
  .email-mailbox-card dl div {
    align-items: flex-start;
    flex-direction: column;
  }

  .email-mailbox-card dd {
    text-align: left;
  }
}
</style>
