<template>
  <FormControl
    v-bind="$attrs"
    class="gbos-field"
    :label="label"
    :description="description"
    :type="type"
    :model-value="modelValue"
    :required="required"
    size="md"
    variant="outline"
    @update:model-value="$emit('update:modelValue', $event)"
  />
</template>

<script setup lang="ts">
import { FormControl } from "frappe-ui";

defineOptions({ inheritAttrs: false });

type FieldType =
  | "date"
  | "datetime-local"
  | "email"
  | "month"
  | "number"
  | "password"
  | "search"
  | "tel"
  | "text"
  | "time"
  | "url"
  | "week"
  | "textarea";

withDefaults(
  defineProps<{
    label: string;
    description?: string;
    type?: FieldType;
    modelValue?: string | number;
    required?: boolean;
  }>(),
  {
    description: undefined,
    type: "text",
    modelValue: "",
    required: false,
  },
);

defineEmits<{ "update:modelValue": [value: string | number] }>();
</script>

<style scoped>
.gbos-field {
  color: var(--gbos-text);
  font-family: var(--gbos-font-sans);
  font-size: 14px;
}

.gbos-field :deep(input),
.gbos-field :deep(textarea) {
  border-radius: var(--gbos-radius-control);
}

@media (max-width: 767px) {
  .gbos-field :deep(input),
  .gbos-field :deep(textarea) {
    min-height: 44px;
  }
}
</style>
