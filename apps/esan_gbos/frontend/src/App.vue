<template>
  <AppShell :navigation="navigation" :session-label="sessionLabel">
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
  </AppShell>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import StatePanel from "@/components/StatePanel.vue";
import AppShell from "@/components/shell/AppShell.vue";
import {
  defaultWorkspaceForRoles,
  navigationForRoles,
} from "@/navigation";
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
  const first = defaultWorkspaceForRoles(sessionState.roles);
  if (first) {
    void router.push(first);
  } else {
    window.location.assign("/login?redirect-to=/gbos");
  }
};
</script>
