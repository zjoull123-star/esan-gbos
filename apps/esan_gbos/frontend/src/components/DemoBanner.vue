<template>
  <aside class="demo-banner" role="note" aria-label="演示数据提示">
    <strong>演示数据</strong>
    <span>仅用于本地 Gate 验证，不得用于真实业务决定。</span>
  </aside>
</template>

<style scoped>
.demo-banner {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid rgb(180 120 20 / 55%);
  border-radius: var(--gbos-radius-control);
  color: rgb(120 74 8);
  background: rgb(245 177 54 / 16%);
  font-size: 13px;
  line-height: 1.5;
}

.demo-banner strong {
  flex: 0 0 auto;
  padding: 3px 7px;
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-surface);
  background: rgb(120 74 8);
  font-size: 11px;
  letter-spacing: 0.06em;
}

@media (max-width: 520px) {
  .demo-banner {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
