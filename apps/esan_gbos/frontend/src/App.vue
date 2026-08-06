<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="app-shell">
    <header class="app-header">
      <a class="brand" href="/gbos/ceo" aria-label="ESAN GBOS 首页">
        <span class="brand__mark" aria-hidden="true">E</span>
        <span>
          <strong>ESAN GBOS</strong>
          <small>治理型业务操作系统</small>
        </span>
      </a>
      <div class="session-chip" :title="sessionState.user">
        <span class="session-chip__dot" aria-hidden="true" />
        <span>{{ sessionLabel }}</span>
      </div>
    </header>

    <nav aria-label="工作台导航">
      <RouterLink
        v-for="item in navigation"
        :key="item.to"
        :to="item.to"
        class="nav-link"
      >
        {{ item.label }}
      </RouterLink>
    </nav>

    <main id="main-content" tabindex="-1">
      <StatePanel
        v-if="!online"
        kind="offline"
        @retry="recheckConnection"
      />
      <StatePanel
        v-else-if="!sessionState.authenticated || !allowed"
        kind="permission"
        :message="permissionMessage"
        @retry="goToAvailable"
      />
      <RouterView v-else />
    </main>

    <footer>
      <span>Gate 5 · 在线优先</span>
      <span>业务数据不进入浏览器持久存储</span>
    </footer>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import StatePanel from "@/components/StatePanel.vue";
import { navigationForRoles } from "@/navigation";
import { isRouteAllowed } from "@/router";
import { sessionState } from "@/session";

const route = useRoute();
const router = useRouter();
const online = ref(typeof navigator === "undefined" || navigator.onLine);
const navigation = computed(() => navigationForRoles(sessionState.roles));
const allowed = computed(() => isRouteAllowed(route.path, sessionState.roles));
const sessionLabel = computed(() =>
  sessionState.authenticated ? sessionState.user : "未登录",
);
const permissionMessage = computed(() =>
  sessionState.authenticated
    ? "当前角色无权查看此页面，请从可用工作台继续。"
    : "Frappe session 已失效，请重新登录后刷新页面。",
);
const recheckConnection = () => {
  online.value = typeof navigator === "undefined" || navigator.onLine;
};
const markOffline = () => {
  online.value = false;
};
const markOnline = () => {
  online.value = true;
};
onMounted(() => {
  window.addEventListener("offline", markOffline);
  window.addEventListener("online", markOnline);
});
onBeforeUnmount(() => {
  window.removeEventListener("offline", markOffline);
  window.removeEventListener("online", markOnline);
});
const goToAvailable = () => {
  const first = navigation.value[0];
  if (first) {
    void router.push(first.to);
  } else {
    window.location.assign("/login?redirect-to=/gbos/ceo");
  }
};
</script>
