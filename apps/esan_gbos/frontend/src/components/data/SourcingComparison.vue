<template>
  <div class="sourcing-comparison" data-sourcing-comparison>
    <DemoBanner v-if="hasFixtureData" />
    <section
      v-for="lane in nonEmptyLanes"
      :key="lane.key"
      class="sourcing-lane"
      :aria-labelledby="`sourcing-lane-${lane.key}`"
    >
      <header class="sourcing-lane__header">
        <h2 :id="`sourcing-lane-${lane.key}`">
          {{ lane.label }}
        </h2>
      </header>
      <ul class="sourcing-events">
        <li v-for="(event, eventIndex) in lane.events" :key="event.name ?? eventIndex">
          <article class="sourcing-event">
            <header>
              <p class="eyebrow">
                {{ event.name }}
              </p>
              <h3>{{ event.title }}</h3>
            </header>
            <dl class="sourcing-event__facts">
              <div><dt>团队</dt><dd>{{ event.team }}</dd></div>
              <div><dt>需求信号</dt><dd>{{ event.demand_signal }}</dd></div>
              <div><dt>负责人</dt><dd>{{ event.owner_user }}</dd></div>
              <div><dt>已选供应商</dt><dd>{{ event.selected_supplier }}</dd></div>
              <div><dt>业务状态</dt><dd>{{ event.business_status }}</dd></div>
              <div><dt>审核状态</dt><dd>{{ event.review_status }}</dd></div>
              <div><dt>版本</dt><dd>{{ event.revision }}</dd></div>
              <div><dt>更新时间</dt><dd>{{ event.modified }}</dd></div>
            </dl>
            <section
              v-if="event.candidates.length"
              class="quote-snapshot"
              :aria-labelledby="`quote-${lane.key}-${eventIndex}`"
            >
              <h4 :id="`quote-${lane.key}-${eventIndex}`">
                报价快照
              </h4>
              <div class="quote-snapshot__table">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">
                        供应商
                      </th>
                      <th scope="col">
                        外部供应商 ID
                      </th>
                      <th scope="col">
                        报价
                      </th>
                      <th scope="col">
                        预计交期
                      </th>
                      <th scope="col">
                        候选状态
                      </th>
                      <th scope="col">
                        备注
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="(candidate, candidateIndex) in event.candidates"
                      :key="`${candidate.supplier_name ?? ''}-${candidateIndex}`"
                    >
                      <td>{{ candidate.supplier_name }}</td>
                      <td>{{ candidate.external_supplier_id }}</td>
                      <td>{{ formatQuotedPrice(candidate) }}</td>
                      <td>{{ formatLeadTimeDays(candidate) }}</td>
                      <td>{{ candidate.candidate_status }}</td>
                      <td>{{ candidate.notes }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </section>
          </article>
        </li>
      </ul>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

import DemoBanner from "@/components/DemoBanner.vue";
import {
  formatLeadTimeDays,
  formatQuotedPrice,
  isFixturePayload,
  type SourcingLanePresentation,
} from "@/presentation";

const props = defineProps<{
  lanes: readonly SourcingLanePresentation[];
}>();

const nonEmptyLanes = computed(() =>
  props.lanes.filter((lane) => lane.events.length > 0),
);
const hasFixtureData = computed(() => isFixturePayload(props.lanes));
</script>

<style scoped>
.sourcing-comparison,
.sourcing-events {
  display: grid;
  gap: 16px;
}

.sourcing-lane,
.sourcing-event {
  min-width: 0;
}

.sourcing-lane__header h2,
.sourcing-event h3,
.quote-snapshot h4 {
  margin: 0;
}

.sourcing-lane__header h2 {
  font-size: 18px;
}

.sourcing-events {
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}

.sourcing-event {
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.sourcing-event__facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin: 16px 0 0;
}

.sourcing-event__facts div {
  min-width: 0;
}

.sourcing-event__facts dt,
.sourcing-event__facts dd {
  margin: 0;
}

.sourcing-event__facts dt {
  color: var(--gbos-muted);
  font-size: 12px;
  font-weight: 700;
}

.sourcing-event__facts dd {
  min-height: 1.4em;
  margin-top: 3px;
  overflow-wrap: anywhere;
}

.quote-snapshot {
  margin-top: 18px;
}

.quote-snapshot__table {
  margin-top: 8px;
  overflow-x: auto;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
}

table {
  width: 100%;
  min-width: 720px;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 14px;
}

th,
td {
  padding: 10px 12px;
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
</style>
