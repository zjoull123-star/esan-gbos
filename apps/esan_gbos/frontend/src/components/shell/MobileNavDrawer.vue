<template>
  <div
    v-if="open"
    class="mobile-nav-drawer__backdrop"
    @click.self="requestClose"
  >
    <section
      :id="drawerId"
      ref="dialog"
      class="mobile-nav-drawer"
      role="dialog"
      aria-modal="true"
      :aria-labelledby="titleId"
    >
      <header class="mobile-nav-drawer__header">
        <div>
          <span>ESAN GBOS</span>
          <h2 :id="titleId">
            工作区导航
          </h2>
        </div>
        <button
          class="mobile-nav-drawer__close"
          type="button"
          aria-label="关闭导航菜单"
          title="关闭导航菜单"
          @click="requestClose"
        >
          <span aria-hidden="true">×</span>
        </button>
      </header>

      <nav aria-label="移动端完整导航">
        <RouterLink
          class="mobile-nav-drawer__link"
          to="/gbos"
          :aria-current="route.path === '/gbos' ? 'page' : undefined"
          @click="requestClose"
        >
          <span class="mobile-nav-drawer__mark" aria-hidden="true">首</span>
          <span>产品首页</span>
        </RouterLink>
        <RouterLink
          v-for="item in navigation"
          :key="item.id"
          class="mobile-nav-drawer__link"
          :class="{ 'mobile-nav-drawer__link--current': isCurrent(item.to) }"
          :to="item.to"
          :aria-current="isCurrent(item.to) ? 'page' : undefined"
          @click="requestClose"
        >
          <span
            class="mobile-nav-drawer__mark"
            :data-icon="item.icon"
            aria-hidden="true"
          >{{ item.label.charAt(0) }}</span>
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>
    </section>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";

import type { NavigationItem } from "@/navigation";

const props = defineProps<{
  open: boolean;
  navigation: readonly NavigationItem[];
  drawerId: string;
  titleId: string;
  returnFocusId: string;
}>();

const emit = defineEmits<{
  close: [];
}>();

const route = useRoute();
const dialog = ref<HTMLElement>();
let bodyWasLocked = false;
let previousBodyOverflow = "";

const isCurrent = (path: string) =>
  route.path === path || route.path.startsWith(`${path}/`);

const focusMenuButton = async () => {
  await nextTick();
  document.getElementById(props.returnFocusId)?.focus();
};

const lockBody = () => {
  if (bodyWasLocked) {
    return;
  }
  previousBodyOverflow = document.body.style.overflow;
  document.body.style.overflow = "hidden";
  bodyWasLocked = true;
};

const unlockBody = () => {
  if (!bodyWasLocked) {
    return;
  }
  document.body.style.overflow = previousBodyOverflow;
  bodyWasLocked = false;
};

const requestClose = () => {
  emit("close");
};

const actionableElements = () =>
  Array.from(
    dialog.value?.querySelectorAll<HTMLElement>(
      "a[href], button:not([disabled])",
    ) ?? [],
  );

const handleKeydown = (event: KeyboardEvent) => {
  if (!props.open) {
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    requestClose();
    return;
  }
  if (event.key !== "Tab") {
    return;
  }

  const actions = actionableElements();
  const first = actions[0];
  const last = actions.at(-1);
  if (!first || !last) {
    event.preventDefault();
    return;
  }
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  } else if (!dialog.value?.contains(document.activeElement)) {
    event.preventDefault();
    first.focus();
  }
};

watch(
  () => props.open,
  async (open) => {
    if (open) {
      lockBody();
      await nextTick();
      dialog.value?.querySelector<HTMLElement>("a[href]")?.focus();
      return;
    }
    unlockBody();
    await focusMenuButton();
  },
);

watch(
  () => route.fullPath,
  (currentPath, previousPath) => {
    if (props.open && currentPath !== previousPath) {
      requestClose();
    }
  },
);

onMounted(() => {
  document.addEventListener("keydown", handleKeydown);
});

onBeforeUnmount(() => {
  document.removeEventListener("keydown", handleKeydown);
  unlockBody();
});
</script>

<style scoped>
.mobile-nav-drawer__backdrop {
  position: fixed;
  z-index: 900;
  inset: 0;
  display: none;
  align-items: stretch;
  background: rgb(11 18 32 / 58%);
}

.mobile-nav-drawer {
  width: min(88vw, 360px);
  height: 100%;
  overflow-y: auto;
  padding: max(20px, env(safe-area-inset-top)) 18px
    max(20px, env(safe-area-inset-bottom));
  color: #cbd5e1;
  background: var(--gbos-sidebar);
  box-shadow: 18px 0 48px rgb(11 18 32 / 32%);
}

.mobile-nav-drawer__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 22px;
}

.mobile-nav-drawer__header span {
  color: #94a3b8;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

.mobile-nav-drawer__header h2 {
  margin: 3px 0 0;
  color: white;
  font-size: 20px;
}

.mobile-nav-drawer__close {
  display: grid;
  width: 44px;
  min-width: 44px;
  height: 44px;
  padding: 0;
  place-items: center;
  border: 1px solid rgb(255 255 255 / 12%);
  border-radius: 12px;
  color: white;
  background: rgb(255 255 255 / 6%);
  cursor: pointer;
}

.mobile-nav-drawer__close span {
  color: inherit;
  font-size: 26px;
  font-weight: 400;
  line-height: 1;
}

.mobile-nav-drawer nav {
  display: grid;
  gap: 6px;
}

.mobile-nav-drawer__link {
  display: flex;
  min-height: 48px;
  align-items: center;
  gap: 12px;
  padding: 8px 10px;
  border-radius: 12px;
  color: #cbd5e1;
  font-size: 15px;
  font-weight: 700;
  text-decoration: none;
}

.mobile-nav-drawer__link:hover,
.mobile-nav-drawer__link--current,
.mobile-nav-drawer__link.router-link-active {
  color: white;
  background: rgb(108 92 231 / 24%);
}

.mobile-nav-drawer__mark {
  display: grid;
  width: 32px;
  height: 32px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 10px;
  color: #d8d3ff;
  background: rgb(255 255 255 / 8%);
  font-size: 12px;
}

@media (max-width: 767px) {
  .mobile-nav-drawer__backdrop {
    display: flex;
  }
}
</style>
