<template>
  <div class="dashboard-template">
    <div v-if="$slots.header" data-region="header">
      <slot name="header" />
    </div>
    <div v-if="$slots.status" class="dashboard-template__status" data-region="status">
      <slot name="status" />
    </div>
    <section
      v-if="$slots.metrics"
      class="dashboard-template__metrics"
      data-region="metrics"
      aria-label="关键指标"
    >
      <slot name="metrics" />
    </section>
    <div class="dashboard-template__body">
      <section class="dashboard-template__main" data-region="main">
        <slot name="main" />
      </section>
      <aside v-if="$slots.aside" class="dashboard-template__aside" data-region="aside">
        <slot name="aside" />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.dashboard-template {
  display: grid;
  min-width: 0;
  gap: 16px;
}

.dashboard-template__status,
.dashboard-template__metrics,
.dashboard-template__main,
.dashboard-template__aside {
  min-width: 0;
}

.dashboard-template__body {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(0, 2fr) minmax(260px, 1fr);
  gap: 16px;
  align-items: start;
}

@media (max-width: 1023px) {
  .dashboard-template__body {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
