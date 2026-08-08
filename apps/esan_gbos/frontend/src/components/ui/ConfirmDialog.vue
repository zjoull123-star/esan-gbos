<template>
  <dialog
    ref="dialogRef"
    class="confirm-dialog"
    role="alertdialog"
    aria-modal="true"
    :aria-labelledby="titleId"
    :aria-describedby="descriptionId"
    @cancel.prevent="cancel"
  >
    <div class="confirm-dialog__surface">
      <h2 :id="titleId">
        {{ title }}
      </h2>
      <p :id="descriptionId">
        {{ message }}
      </p>
      <div class="confirm-dialog__actions">
        <GbosButton
          ref="cancelButtonRef"
          data-action="cancel"
          intent="secondary"
          type="button"
          @click="cancel"
        >
          {{ cancelLabel }}
        </GbosButton>
        <GbosButton
          data-action="confirm"
          intent="danger"
          type="button"
          @click="confirm"
        >
          {{ confirmLabel }}
        </GbosButton>
      </div>
    </div>
  </dialog>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, useId, watch } from "vue";

import GbosButton from "./GbosButton.vue";

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    title: string;
    message: string;
    confirmLabel?: string;
    cancelLabel?: string;
  }>(),
  {
    confirmLabel: "确认",
    cancelLabel: "取消",
  },
);

const emit = defineEmits<{
  "update:modelValue": [value: boolean];
  confirm: [];
  cancel: [];
}>();

const instanceId = useId();
const titleId = `${instanceId}-confirm-title`;
const descriptionId = `${instanceId}-confirm-description`;
const dialogRef = ref<HTMLDialogElement>();
const cancelButtonRef = ref<{ $el?: HTMLElement }>();
let returnFocus: HTMLElement | null = null;
let unmounting = false;

const requestClose = () => emit("update:modelValue", false);

const confirm = () => {
  emit("confirm");
  requestClose();
};

const cancel = () => {
  emit("cancel");
  requestClose();
};

const closeNativeDialog = () => {
  const dialog = dialogRef.value;
  if (!dialog?.open) {
    return;
  }
  if (typeof dialog.close === "function") {
    try {
      dialog.close();
      return;
    } catch {
      // Fall back to clearing the open state for incomplete dialog implementations.
    }
  }
  dialog.removeAttribute("open");
};

const restoreReturnFocus = () => {
  if (returnFocus?.isConnected) {
    returnFocus.focus();
  }
  returnFocus = null;
};

watch(
  () => props.modelValue,
  async (open) => {
    if (open) {
      returnFocus = document.activeElement as HTMLElement | null;
      await nextTick();
      if (unmounting) {
        return;
      }
      const dialog = dialogRef.value;
      if (dialog && !dialog.open) {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          dialog.setAttribute("open", "");
        }
      }
      const cancelButton =
        cancelButtonRef.value?.$el ??
        dialog?.querySelector<HTMLElement>("[data-action='cancel']");
      cancelButton?.focus();
      return;
    }

    closeNativeDialog();
    restoreReturnFocus();
  },
  { immediate: true, flush: "post" },
);

onBeforeUnmount(() => {
  unmounting = true;
  closeNativeDialog();
  restoreReturnFocus();
});
</script>

<style scoped>
.confirm-dialog {
  z-index: 50;
  width: min(440px, calc(100vw - 32px));
  max-width: 100%;
  padding: 0;
  border: 0;
  border-radius: var(--gbos-radius-card);
  color: var(--gbos-text);
  background: transparent;
  box-shadow: var(--gbos-shadow-card);
}

.confirm-dialog::backdrop {
  background: rgb(11 18 32 / 56%);
}

.confirm-dialog__surface {
  padding: 20px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.confirm-dialog h2,
.confirm-dialog p {
  margin: 0;
}

.confirm-dialog h2 {
  font-size: 18px;
  line-height: 1.4;
}

.confirm-dialog p {
  margin-top: 8px;
  color: var(--gbos-muted);
  font-size: 14px;
  line-height: 1.55;
}

.confirm-dialog__actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 20px;
}
</style>
