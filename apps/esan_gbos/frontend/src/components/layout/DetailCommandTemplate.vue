<template>
  <div class="detail-command-template">
    <div v-if="$slots.header" data-region="header">
      <slot name="header" />
    </div>
    <section
      v-if="$slots.facts"
      class="detail-command-template__facts"
      data-region="facts"
      aria-label="对象事实"
    >
      <slot name="facts" />
    </section>
    <div class="detail-command-template__body">
      <section class="detail-command-template__main" data-region="main">
        <slot name="main" />
      </section>
      <aside
        v-if="$slots.command"
        class="detail-command-template__command"
        data-region="command"
      >
        <slot name="command" />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.detail-command-template {
  display: grid;
  min-width: 0;
  gap: 16px;
}

.detail-command-template__facts,
.detail-command-template__main,
.detail-command-template__command {
  min-width: 0;
}

.detail-command-template__body {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
  gap: 16px;
  align-items: start;
}

.detail-command-template__command {
  position: sticky;
  top: 16px;
}

@media (max-width: 1023px) {
  .detail-command-template__body {
    grid-template-columns: minmax(0, 1fr);
  }

  .detail-command-template__command {
    position: static;
  }
}
</style>
