<template>
  <section class="view">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          LOCAL PILOT · 受控集成
        </p>
        <h1>集成状态</h1>
        <p>只展示运行状态、检查点和安全错误码。任何连接认证材料均不进入本页面。</p>
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
    <StatePanel v-else-if="connectors.length === 0" kind="empty" @retry="load" />
    <template v-else>
      <p v-if="commandMessage" class="notice notice--success" role="status">
        {{ commandMessage }}
      </p>
      <p v-if="commandError" class="notice notice--error" role="alert">
        {{ commandError }}
      </p>

      <article v-if="usage" class="usage-card" aria-labelledby="model-usage-title">
        <div>
          <p class="eyebrow">
            模型用量
          </p>
          <h2 id="model-usage-title">
            {{ usage.model }}
          </h2>
          <p>{{ usage.period }} · {{ usage.tokens.toLocaleString("zh-CN") }} tokens</p>
        </div>
        <dl class="status-list">
          <div>
            <dt>费用</dt>
            <dd>
              {{
                usage.cost.state === "known" && usage.cost.amount !== null
                  ? `${usage.cost.amount.toFixed(4)} USD`
                  : "USD / unknown"
              }}
            </dd>
          </div>
          <div><dt>软上限</dt><dd>{{ usage.soft_limit }}</dd></div>
          <div><dt>硬上限</dt><dd>{{ usage.hard_limit }}</dd></div>
          <div><dt>状态</dt><dd>{{ usage.state }}</dd></div>
        </dl>
      </article>

      <ul class="record-grid" aria-label="连接器状态">
        <li v-for="connector in connectors" :key="connector.instance_id">
          <article class="record-card">
            <p class="eyebrow">
              {{ connector.channel }}
            </p>
            <h2>{{ connector.instance_id }}</h2>
            <dl class="status-list">
              <div><dt>状态</dt><dd>{{ statusLabel(connector.status) }}</dd></div>
              <div><dt>检查点版本</dt><dd>{{ connector.checkpoint_version }}</dd></div>
              <div><dt>积压</dt><dd>{{ connector.backlog }}</dd></div>
              <div><dt>新鲜度</dt><dd>{{ freshnessLabel(connector.freshness) }}</dd></div>
              <div><dt>最近成功</dt><dd>{{ connector.last_success_at || "暂无" }}</dd></div>
              <div><dt>安全错误码</dt><dd>{{ connector.safe_error_code || "无" }}</dd></div>
            </dl>
            <div class="review-actions" aria-label="连接器操作">
              <button
                v-if="connector.status === 'enabled' || connector.status === 'error'"
                class="button button--secondary"
                type="button"
                :disabled="isSubmitting(connector.instance_id)"
                @click="runCommand('pause', connector)"
              >
                暂停
              </button>
              <button
                v-if="connector.status === 'paused'"
                class="button button--primary"
                type="button"
                :disabled="isSubmitting(connector.instance_id)"
                @click="runCommand('resume', connector)"
              >
                恢复
              </button>
              <button
                class="button button--danger"
                type="button"
                :disabled="isSubmitting(connector.instance_id)"
                @click="runCommand('replay', connector)"
              >
                重放
              </button>
            </div>
          </article>
        </li>
      </ul>
    </template>
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
import StatePanel from "@/components/StatePanel.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const client = useBffClient();
const submitting = ref(new Set<string>());
const updatedConnectors = ref<ConnectorStatus[]>([]);
const commandMessage = ref("");
const commandError = ref("");
const resource = useOnlineResource(async () => {
  updatedConnectors.value = [];
  const [status, usage] = await Promise.all([
    client.listIntegrationStatus(),
    client.getModelUsage(),
  ]);
  return { connectors: status.data.connectors, usage: usage.data };
});
const connectors = computed(() =>
  updatedConnectors.value.length
    ? updatedConnectors.value
    : (resource.data.value?.connectors ?? []),
);
const usage = computed(() => resource.data.value?.usage);
const { state, message, requestId, load } = resource;

const statusLabel = (status: ConnectorState) =>
  ({ enabled: "已启用", paused: "已暂停", error: "错误", disabled: "已停用" })[
    status
  ];
const freshnessLabel = (freshness: FreshnessState) =>
  ({ fresh: "新鲜", stale: "过期", unknown: "未知" })[freshness];
const isSubmitting = (instanceId: string) => submitting.value.has(instanceId);

const runCommand = async (
  action: "pause" | "resume" | "replay",
  connector: ConnectorStatus,
) => {
  if (isSubmitting(connector.instance_id)) {
    return;
  }
  const copy = {
    pause: `确认暂停 ${connector.channel}（${connector.instance_id}）？`,
    resume: `确认恢复 ${connector.channel}（${connector.instance_id}）？`,
    replay: `重放可能重复处理历史消息。确认重放 ${connector.channel}（${connector.instance_id}）？`,
  }[action];
  if (!window.confirm(copy)) {
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
      (error.code === "revision_conflict" || error.code === "idempotency_conflict")
    ) {
      await load();
    }
  } finally {
    const next = new Set(submitting.value);
    next.delete(connector.instance_id);
    submitting.value = next;
  }
};
</script>
