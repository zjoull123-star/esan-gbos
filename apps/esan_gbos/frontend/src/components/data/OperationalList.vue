<template>
  <div class="operational-list">
    <div class="operational-list__desktop">
      <table>
        <thead>
          <tr>
            <th v-for="column in columns" :key="column.key" scope="col">
              {{ column.label }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td v-for="column in columns" :key="column.key">
              {{ displayValue(row.values[column.key]) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <ul class="operational-list__mobile" data-mobile-list>
      <li v-for="row in rows" :key="row.id">
        <dl>
          <div
            v-for="column in columns"
            :key="column.key"
            class="operational-list__mobile-cell"
            :data-label="column.label"
          >
            <dt>{{ column.label }}</dt>
            <dd>{{ displayValue(row.values[column.key]) }}</dd>
          </div>
        </dl>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
export type OperationalColumn = {
  key: string;
  label: string;
};

export type OperationalCellValue = string | number | null | undefined;

export type OperationalRow = {
  id: string;
  values: Record<string, OperationalCellValue>;
};

defineProps<{
  columns: readonly OperationalColumn[];
  rows: readonly OperationalRow[];
}>();

const displayValue = (value: OperationalCellValue) =>
  value === null || value === undefined ? "" : String(value);
</script>

<style scoped>
.operational-list {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.operational-list__desktop {
  min-width: 0;
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 14px;
}

th,
td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--gbos-border);
  text-align: start;
  overflow-wrap: anywhere;
  vertical-align: top;
}

th {
  color: var(--gbos-muted);
  background: var(--gbos-canvas);
  font-size: 12px;
  font-weight: 700;
}

tbody tr:last-child td {
  border-bottom: 0;
}

.operational-list__mobile {
  display: none;
  margin: 0;
  padding: 0;
  list-style: none;
}

@media (max-width: 767px) {
  .operational-list__desktop {
    display: none;
  }

  .operational-list__mobile {
    display: block;
  }

  .operational-list__mobile > li {
    padding: 14px;
    border-bottom: 1px solid var(--gbos-border);
  }

  .operational-list__mobile > li:last-child {
    border-bottom: 0;
  }

  .operational-list__mobile dl,
  .operational-list__mobile dt,
  .operational-list__mobile dd {
    margin: 0;
  }

  .operational-list__mobile dl {
    display: grid;
    gap: 10px;
  }

  .operational-list__mobile-cell {
    display: grid;
    grid-template-columns: minmax(88px, 0.8fr) minmax(0, 1.4fr);
    gap: 12px;
    font-size: 14px;
  }

  .operational-list__mobile dt {
    color: var(--gbos-muted);
    font-size: 12px;
    font-weight: 700;
  }

  .operational-list__mobile dd {
    min-width: 0;
    overflow-wrap: anywhere;
  }
}
</style>
