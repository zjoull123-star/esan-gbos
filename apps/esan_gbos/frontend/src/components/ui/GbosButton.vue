<template>
  <Button
    v-bind="$attrs"
    class="gbos-button"
    :class="`gbos-button--${intent}`"
    :theme="frappeTheme"
    :variant="frappeVariant"
    :type="type"
    :loading="loading"
    :disabled="disabled"
    @click="emit('click', $event)"
  >
    <slot />
  </Button>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { Button } from "frappe-ui";

defineOptions({ inheritAttrs: false });

type ButtonIntent = "primary" | "secondary" | "danger";

const props = withDefaults(
  defineProps<{
    intent?: ButtonIntent;
    type?: "button" | "submit" | "reset";
    loading?: boolean;
    disabled?: boolean;
  }>(),
  {
    intent: "primary",
    type: "button",
    loading: false,
    disabled: false,
  },
);

const emit = defineEmits<{ click: [event: MouseEvent] }>();

const intentMap = {
  primary: { theme: "blue", variant: "solid" },
  secondary: { theme: "gray", variant: "outline" },
  danger: { theme: "red", variant: "solid" },
} as const;

const frappeTheme = computed(() => intentMap[props.intent].theme);
const frappeVariant = computed(() => intentMap[props.intent].variant);
</script>

<style scoped>
:global(.gbos-button) {
  min-height: 36px;
  border-radius: var(--gbos-radius-control);
  font-family: var(--gbos-font-sans);
  font-size: 14px;
  font-weight: 700;
}

:global(.gbos-button--primary:not(:disabled)) {
  color: #fff;
  background: var(--gbos-primary);
}

:global(.gbos-button--secondary:not(:disabled)) {
  color: var(--gbos-text);
  border-color: var(--gbos-border);
  background: var(--gbos-surface);
}

@media (max-width: 767px) {
  :global(.gbos-button) {
    min-height: 44px;
  }
}
</style>
