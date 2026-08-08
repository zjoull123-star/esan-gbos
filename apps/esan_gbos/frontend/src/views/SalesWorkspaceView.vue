<template>
  <section class="view">
    <OperationalListTemplate>
      <template #header>
        <PageHeader
          eyebrow="ESAN GBOS · Gate 4"
          title="销售工作项"
          description="标题明确作为工作项 / 下一动作；仅显示当前权限范围内的服务端结果。"
        >
          <template #actions>
            <GbosButton
              v-if="cursor"
              data-first-page
              intent="secondary"
              @click="goFirstPage"
            >
              回到首页
            </GbosButton>
            <GbosButton data-refresh intent="secondary" @click="refresh">
              刷新
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template #list>
        <ResourceBoundary
          :state="state"
          :message="message"
          :request-id="requestId"
          :empty="items.length === 0"
          @retry="refresh"
        >
          <div data-work-items>
            <DemoBanner v-if="hasFixtureData" />
            <div class="work-item-table" data-desktop-table>
              <table>
                <thead>
                  <tr>
                    <th scope="col">
                      工作项 / 下一动作
                    </th>
                    <th scope="col">
                      团队
                    </th>
                    <th scope="col">
                      负责人
                    </th>
                    <th scope="col">
                      优先级
                    </th>
                    <th scope="col">
                      到期日
                    </th>
                    <th scope="col">
                      业务状态
                    </th>
                    <th scope="col">
                      审核状态
                    </th>
                    <th scope="col">
                      版本
                    </th>
                    <th scope="col">
                      相关记录
                    </th>
                    <th scope="col">
                      更新时间
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(item, index) in items" :key="item.name ?? index">
                    <td>
                      <strong>{{ item.title }}</strong>
                      <small>{{ item.name }}</small>
                    </td>
                    <td>{{ item.team }}</td>
                    <td>{{ item.assigned_to }}</td>
                    <td>{{ item.priority }}</td>
                    <td>{{ item.due_date }}</td>
                    <td>{{ item.business_status }}</td>
                    <td>{{ item.review_status }}</td>
                    <td>{{ item.revision }}</td>
                    <td>
                      <span>{{ referenceLabel(item) }}</span>
                      <RouterLink
                        v-if="workItemReferenceLink(item)"
                        class="text-link"
                        :to="workItemReferenceLink(item)?.href ?? ''"
                      >
                        {{ workItemReferenceLink(item)?.label }}
                      </RouterLink>
                    </td>
                    <td>{{ item.modified }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <ul class="work-item-mobile" data-mobile-list>
              <li v-for="(item, index) in items" :key="item.name ?? index">
                <dl>
                  <div>
                    <dt>工作项 / 下一动作</dt>
                    <dd><strong>{{ item.title }}</strong></dd>
                  </div>
                  <div><dt>编号</dt><dd>{{ item.name }}</dd></div>
                  <div><dt>团队</dt><dd>{{ item.team }}</dd></div>
                  <div><dt>负责人</dt><dd>{{ item.assigned_to }}</dd></div>
                  <div><dt>优先级</dt><dd>{{ item.priority }}</dd></div>
                  <div><dt>到期日</dt><dd>{{ item.due_date }}</dd></div>
                  <div><dt>业务状态</dt><dd>{{ item.business_status }}</dd></div>
                  <div><dt>审核状态</dt><dd>{{ item.review_status }}</dd></div>
                  <div><dt>版本</dt><dd>{{ item.revision }}</dd></div>
                  <div>
                    <dt>相关记录</dt>
                    <dd>
                      <span>{{ referenceLabel(item) }}</span>
                      <RouterLink
                        v-if="workItemReferenceLink(item)"
                        class="text-link"
                        :to="workItemReferenceLink(item)?.href ?? ''"
                      >
                        {{ workItemReferenceLink(item)?.label }}
                      </RouterLink>
                    </dd>
                  </div>
                  <div><dt>更新时间</dt><dd>{{ item.modified }}</dd></div>
                </dl>
              </li>
            </ul>
          </div>
        </ResourceBoundary>
      </template>

      <template #pagination>
        <GbosButton
          v-if="state === 'ready' && nextCursor"
          data-next-page
          intent="secondary"
          @click="goNextPage"
        >
          下一页
        </GbosButton>
      </template>
    </OperationalListTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";

import { useBffClient } from "@/api/injection";
import DemoBanner from "@/components/DemoBanner.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import {
  isFixturePayload,
  workItemReferenceLink,
  workItemsFromPayload,
  type WorkItemPresentation,
} from "@/presentation";

const client = useBffClient();
const cursor = ref<string>();
const resource = useOnlineResource(async () => {
  const response = await client.listWorkItems({
    cursor: cursor.value,
    pageSize: 25,
  });
  return {
    items: workItemsFromPayload(response.data),
    nextCursor: response.meta.next_cursor ?? undefined,
  };
});
const items = computed(() => resource.data.value?.items ?? []);
const nextCursor = computed(() => resource.data.value?.nextCursor);
const hasFixtureData = computed(() => isFixturePayload(items.value));
const { state, message, requestId, load } = resource;

const refresh = () => load();
const goFirstPage = () => {
  cursor.value = undefined;
  return load();
};
const goNextPage = () => {
  if (!nextCursor.value) {
    return;
  }
  cursor.value = nextCursor.value;
  return load();
};
const referenceLabel = (item: WorkItemPresentation) =>
  [item.reference_doctype, item.reference_name].filter(Boolean).join(" · ");
</script>

<style scoped>
.work-item-table {
  min-width: 0;
  overflow-x: auto;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.work-item-mobile {
  display: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
  list-style: none;
}

.work-item-mobile > li {
  padding: 14px;
  border-bottom: 1px solid var(--gbos-border);
}

.work-item-mobile > li:last-child {
  border-bottom: 0;
}

.work-item-mobile dl,
.work-item-mobile dt,
.work-item-mobile dd {
  margin: 0;
}

.work-item-mobile dl {
  display: grid;
  gap: 10px;
}

.work-item-mobile dl > div {
  display: grid;
  grid-template-columns: minmax(96px, 0.8fr) minmax(0, 1.4fr);
  gap: 12px;
}

.work-item-mobile dt {
  color: var(--gbos-muted);
  font-size: 12px;
  font-weight: 700;
}

.work-item-mobile dd {
  min-height: 1.4em;
  overflow-wrap: anywhere;
}

table {
  width: 100%;
  min-width: 1180px;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 14px;
}

th,
td {
  padding: 12px;
  border-bottom: 1px solid var(--gbos-border);
  text-align: start;
  overflow-wrap: anywhere;
  vertical-align: top;
}

th {
  color: var(--gbos-muted);
  background: var(--gbos-canvas);
  font-size: 12px;
}

tbody tr:last-child td {
  border-bottom: 0;
}

td strong,
td small,
td span,
td a {
  display: block;
}

td small {
  margin-top: 4px;
  color: var(--gbos-muted);
}

td a {
  margin-top: 5px;
}

@media (max-width: 767px) {
  .work-item-table {
    display: none;
  }

  .work-item-mobile {
    display: block;
  }
}
</style>
