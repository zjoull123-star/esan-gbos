<template>
  <section class="integrations-view">
    <OperationalListTemplate>
      <template #header>
        <PageHeader
          eyebrow="LOCAL PILOT · 受控集成"
          title="集成状态"
          description="连接器运行状态与模型用量分别读取；页面只展示检查点、积压、新鲜度和安全错误码。"
        >
          <template #actions>
            <GbosButton intent="secondary" type="button" @click="refreshAll">
              刷新全部
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template #list>
        <div class="integration-resource-stack">
          <section
            class="integration-resource-section"
            data-integration-resource="usage"
            aria-labelledby="model-usage-title"
          >
            <div class="integration-section-heading">
              <div>
                <p>MODEL GOVERNANCE</p>
                <h2 id="model-usage-title">
                  模型用量
                </h2>
              </div>
              <span>用量读取失败不会改变连接器状态</span>
            </div>
            <ResourceBoundary
              :state="usageState"
              :message="usageBoundaryMessage"
              :request-id="usageRequestId"
              :empty="!usage"
              @retry="usageResource.load"
            >
              <article v-if="usage" class="integration-card integration-card--usage">
                <div class="integration-card__heading">
                  <div>
                    <p>{{ usage.period }}</p>
                    <h3>{{ usage.model }}</h3>
                  </div>
                  <span>{{ formatTokens(usage.tokens, usage.token_state) }}</span>
                </div>
                <dl class="integration-label-rows">
                  <div>
                    <dt>费用</dt>
                    <dd>
                      {{
                        usage.cost.amount !== null
                          ? `${usage.cost.amount.toFixed(4)} ${usage.cost.currency}${usage.cost.state === "partial" ? "（部分）" : ""}`
                          : `${usage.cost.currency} / unknown`
                      }}
                    </dd>
                  </div>
                  <div><dt>软上限</dt><dd>{{ usage.soft_limit_usd.toFixed(2) }} USD</dd></div>
                  <div><dt>硬上限</dt><dd>{{ usage.hard_limit_usd.toFixed(2) }} USD</dd></div>
                  <div><dt>状态</dt><dd>{{ usage.state }}</dd></div>
                </dl>
              </article>
            </ResourceBoundary>
          </section>

          <section
            class="integration-resource-section"
            data-integration-resource="connectors"
            aria-labelledby="connector-status-title"
          >
            <div class="integration-section-heading">
              <div>
                <p>CONTROLLED CONNECTORS</p>
                <h2 id="connector-status-title">
                  连接器状态
                </h2>
              </div>
              <span>所有命令均携带当前 revision 与幂等键</span>
            </div>
            <p v-if="commandMessage" class="integration-notice integration-notice--success" role="status">
              {{ commandMessage }}
            </p>
            <p v-if="commandError" class="integration-notice integration-notice--error" role="alert">
              {{ commandError }}
            </p>
            <ResourceBoundary
              :state="statusState"
              :message="statusBoundaryMessage"
              :request-id="statusRequestId"
              :empty="connectors.length === 0"
              @retry="statusResource.load"
            >
              <ul class="integration-card-list" aria-label="连接器状态">
                <li
                  v-for="connector in connectors"
                  :key="connector.instance_id"
                  class="integration-card"
                  data-connector
                >
                  <div class="integration-card__heading">
                    <div>
                      <p>{{ connector.channel }}</p>
                      <h3>{{ connector.instance_id }}</h3>
                    </div>
                    <span>{{ statusLabel(connector.status) }}</span>
                  </div>
                  <dl class="integration-label-rows">
                    <div><dt>状态</dt><dd>{{ statusLabel(connector.status) }}</dd></div>
                    <div><dt>revision</dt><dd>{{ connector.revision }}</dd></div>
                    <div><dt>检查点版本</dt><dd>{{ connector.checkpoint_version }}</dd></div>
                    <div><dt>积压</dt><dd>{{ connector.backlog }}</dd></div>
                    <div><dt>新鲜度</dt><dd>{{ freshnessLabel(connector.freshness) }}</dd></div>
                    <div><dt>最近成功</dt><dd>{{ connector.last_success_at || "暂无" }}</dd></div>
                    <div><dt>安全错误码</dt><dd>{{ connector.safe_error_code || "无" }}</dd></div>
                  </dl>
                  <div class="integration-actions" aria-label="连接器操作">
                    <GbosButton
                      v-if="connector.status === 'enabled' || connector.status === 'error'"
                      data-command="pause"
                      intent="secondary"
                      type="button"
                      :loading="isSubmitting(connector.instance_id)"
                      :disabled="isSubmitting(connector.instance_id)"
                      @click="requestCommand('pause', connector)"
                    >
                      暂停
                    </GbosButton>
                    <GbosButton
                      v-if="connector.status === 'paused'"
                      data-command="resume"
                      intent="primary"
                      type="button"
                      :loading="isSubmitting(connector.instance_id)"
                      :disabled="isSubmitting(connector.instance_id)"
                      @click="requestCommand('resume', connector)"
                    >
                      恢复
                    </GbosButton>
                    <GbosButton
                      data-command="replay"
                      intent="danger"
                      type="button"
                      :loading="isSubmitting(connector.instance_id)"
                      :disabled="isSubmitting(connector.instance_id)"
                      @click="requestCommand('replay', connector)"
                    >
                      重放
                    </GbosButton>
                  </div>
                </li>
              </ul>
            </ResourceBoundary>
          </section>
        </div>
      </template>
    </OperationalListTemplate>

    <ConfirmDialog
      v-model="commandConfirmOpen"
      :title="commandConfirmCopy.title"
      :message="commandConfirmCopy.message"
      :confirm-label="commandConfirmCopy.confirmLabel"
      @confirm="confirmCommand"
      @cancel="clearCommandConfirmation"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";

import { BffError, createIdempotencyKey } from "@/api/bff";
import { useBffClient } from "@/api/injection";
import type {
  ConnectorState,
  ConnectorStatus,
  FreshnessState,
} from "@/api/types";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import ConfirmDialog from "@/components/ui/ConfirmDialog.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

type ConnectorAction = "pause" | "resume" | "replay";
interface PendingCommand {
  action: ConnectorAction;
  connector: ConnectorStatus;
}

const client = useBffClient();
const submitting = ref(new Set<string>());
const updatedConnectors = ref<ConnectorStatus[]>([]);
const commandMessage = ref("");
const commandError = ref("");
const commandConfirmOpen = ref(false);
const pendingCommand = ref<PendingCommand>();

const statusResource = useOnlineResource(async () => {
  updatedConnectors.value = [];
  const response = await client.listIntegrationStatus();
  return response.data;
});
const usageResource = useOnlineResource(async () => {
  const response = await client.getModelUsage();
  return response.data;
});

const connectors = computed(() =>
  updatedConnectors.value.length
    ? updatedConnectors.value
    : (statusResource.data.value?.connectors ?? []),
);
const usage = computed(() => usageResource.data.value);
const statusState = statusResource.state;
const statusRequestId = statusResource.requestId;
const statusBoundaryMessage = computed(() =>
  statusState.value === "ready" && connectors.value.length === 0
    ? "当前没有可展示的连接器。"
    : statusResource.message.value,
);
const usageState = usageResource.state;
const usageRequestId = usageResource.requestId;
const usageBoundaryMessage = computed(() =>
  usageState.value === "ready" && !usage.value
    ? "当前没有可展示的模型用量。"
    : usageResource.message.value,
);

const statusLabel = (status: ConnectorState) =>
  ({ enabled: "已启用", paused: "已暂停", error: "错误", disabled: "已停用" })[
    status
  ];
const freshnessLabel = (freshness: FreshnessState) =>
  ({ fresh: "新鲜", stale: "过期", unknown: "未知" })[freshness];
const formatTokens = (
  tokens: number | null,
  state: "known" | "partial" | "unknown",
) => {
  if (tokens === null || state === "unknown") {
    return "tokens 未知";
  }
  return `${tokens.toLocaleString("zh-CN")} tokens${state === "partial" ? "（部分）" : ""}`;
};
const isSubmitting = (instanceId: string) => submitting.value.has(instanceId);

const commandConfirmCopy = computed(() => {
  const command = pendingCommand.value;
  if (!command) {
    return {
      title: "确认连接器操作",
      message: "请确认是否执行该连接器操作。",
      confirmLabel: "确认",
    };
  }
  const target = `${command.connector.channel}（${command.connector.instance_id}）`;
  return {
    pause: {
      title: "暂停连接器",
      message: `确认暂停 ${target}？暂停后不会继续消费新消息。`,
      confirmLabel: "确认暂停",
    },
    resume: {
      title: "恢复连接器",
      message: `确认恢复 ${target}？恢复后将从当前检查点继续。`,
      confirmLabel: "确认恢复",
    },
    replay: {
      title: "重放连接器",
      message: `重放可能重复处理历史消息。确认重放 ${target}？`,
      confirmLabel: "确认重放",
    },
  }[command.action];
});

const refreshAll = () => {
  commandMessage.value = "";
  commandError.value = "";
  void Promise.all([statusResource.load(), usageResource.load()]);
};
const requestCommand = (
  action: ConnectorAction,
  connector: ConnectorStatus,
) => {
  if (isSubmitting(connector.instance_id)) {
    return;
  }
  pendingCommand.value = { action, connector: { ...connector } };
  commandConfirmOpen.value = true;
};
const clearCommandConfirmation = () => {
  pendingCommand.value = undefined;
};
const confirmCommand = () => {
  const command = pendingCommand.value;
  pendingCommand.value = undefined;
  if (command) {
    void runCommand(command.action, command.connector);
  }
};

const runCommand = async (
  action: ConnectorAction,
  connector: ConnectorStatus,
) => {
  if (isSubmitting(connector.instance_id)) {
    return;
  }
  commandMessage.value = "";
  commandError.value = "";
  submitting.value = new Set(submitting.value).add(connector.instance_id);
  const command = {
    instance_id: connector.instance_id,
    expected_revision: connector.revision,
    idempotency_key: createIdempotencyKey(),
  };
  try {
    const response =
      action === "pause"
        ? await client.pauseIntegration(command)
        : action === "resume"
          ? await client.resumeIntegration(command)
          : await client.replayIntegration(command);
    updatedConnectors.value = connectors.value.map((item) =>
      item.instance_id === connector.instance_id ? response.data : { ...item },
    );
    commandMessage.value =
      action === "replay" ? "重放请求已受理。" : "连接器状态已更新。";
  } catch (error) {
    commandError.value =
      error instanceof BffError
        ? error.displayMessage
        : "暂时无法执行操作，请刷新后重试。";
    if (
      error instanceof BffError &&
      (error.status === 409 ||
        error.code === "revision_conflict" ||
        error.code === "idempotency_conflict")
    ) {
      await statusResource.load();
    }
  } finally {
    const next = new Set(submitting.value);
    next.delete(connector.instance_id);
    submitting.value = next;
  }
};
</script>

<style scoped>
.integrations-view,
.integration-resource-stack,
.integration-card-list {
  min-width: 0;
}

.integration-resource-stack {
  display: grid;
  gap: 16px;
}

.integration-resource-section {
  display: grid;
  min-width: 0;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.integration-section-heading,
.integration-card__heading,
.integration-actions {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.integration-section-heading p,
.integration-section-heading h2,
.integration-section-heading span,
.integration-card__heading p,
.integration-card__heading h3,
.integration-card__heading span,
.integration-notice {
  margin: 0;
}

.integration-section-heading p,
.integration-card__heading p {
  color: var(--gbos-accent-text);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
}

.integration-section-heading h2,
.integration-card__heading h3 {
  margin-top: 3px;
  color: var(--gbos-text);
  line-height: 1.35;
}

.integration-section-heading h2 {
  font-size: 20px;
}

.integration-card__heading h3 {
  font-size: 17px;
}

.integration-section-heading span,
.integration-card__heading span {
  color: var(--gbos-muted);
  font-size: 13px;
  line-height: 1.5;
}

.integration-card-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.integration-card {
  display: grid;
  min-width: 0;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-canvas);
}

.integration-card--usage {
  max-width: 720px;
}

.integration-label-rows {
  display: grid;
  gap: 7px;
  margin: 0;
}

.integration-label-rows > div {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(104px, 0.7fr) minmax(0, 1.3fr);
  gap: 10px;
  padding-top: 7px;
  border-top: 1px solid var(--gbos-border);
}

.integration-label-rows dt,
.integration-label-rows dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.integration-label-rows dt {
  color: var(--gbos-muted);
  font-size: 12px;
  font-weight: 700;
}

.integration-label-rows dd {
  color: var(--gbos-text);
  font-size: 13px;
  line-height: 1.45;
}

.integration-actions {
  flex-wrap: wrap;
  justify-content: flex-start;
}

.integration-notice {
  padding: 10px 12px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-canvas);
  font-size: 13px;
  line-height: 1.55;
}

.integration-notice--success {
  border-color: var(--gbos-accent);
}

.integration-notice--error {
  border-color: var(--gbos-primary);
}

@media (max-width: 767px) {
  .integration-section-heading,
  .integration-card__heading {
    flex-direction: column;
  }

  .integration-label-rows > div {
    grid-template-columns: minmax(0, 1fr);
    gap: 3px;
  }

  .integration-actions {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
