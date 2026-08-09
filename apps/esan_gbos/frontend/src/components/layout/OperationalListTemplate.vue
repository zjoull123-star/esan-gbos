<template>
  <div class="operational-list-template">
    <div v-if="$slots.header" data-region="header">
      <slot name="header" />
    </div>
    <section
      v-if="$slots.filters"
      class="operational-list-template__filters"
      data-region="filters"
      aria-label="筛选条件"
    >
      <slot name="filters" />
    </section>
    <section class="operational-list-template__list" data-region="list">
      <slot name="list" />
    </section>
    <nav
      v-if="$slots.pagination"
      class="operational-list-template__pagination"
      data-region="pagination"
      aria-label="列表分页"
    >
      <slot name="pagination" />
    </nav>
  </div>
</template>

<style scoped>
.operational-list-template {
  display: grid;
  min-width: 0;
  gap: 16px;
}

.operational-list-template__filters,
.operational-list-template__list {
  min-width: 0;
}

.operational-list-template__filters {
  padding: 12px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.operational-list-template__pagination {
  display: flex;
  justify-content: flex-end;
  min-width: 0;
}
</style>
