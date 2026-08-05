import { onBeforeUnmount, onMounted, readonly, ref, shallowRef } from "vue";

import { BffError } from "@/api/bff";

export type ResourceState = "idle" | "loading" | "ready" | "offline" | "permission" | "error";

export const useOnlineResource = <T>(loader: () => Promise<T>) => {
  const data = shallowRef<T>();
  const state = ref<ResourceState>("idle");
  const message = ref("");
  const requestId = ref<string>();
  let generation = 0;

  const clear = () => {
    data.value = undefined;
  };

  const load = async () => {
    const currentGeneration = ++generation;
    clear();
    message.value = "";
    requestId.value = undefined;
    state.value = "loading";
    try {
      const result = await loader();
      if (currentGeneration !== generation) {
        return;
      }
      data.value = result;
      state.value = "ready";
    } catch (error) {
      if (currentGeneration !== generation) {
        return;
      }
      clear();
      if (error instanceof BffError) {
        message.value = error.displayMessage;
        requestId.value = error.requestId;
        if (error.code === "offline" || error.code === "network_error") {
          state.value = "offline";
        } else if (
          error.code === "permission_denied" ||
          error.code === "authentication_required" ||
          error.code === "scope_mismatch"
        ) {
          state.value = "permission";
        } else {
          state.value = "error";
        }
      } else {
        message.value = "暂时无法读取数据，请稍后重试。";
        state.value = "error";
      }
    }
  };

  const handleOffline = () => {
    generation += 1;
    clear();
    message.value = "需要联网，请检查网络后重试。";
    state.value = "offline";
  };

  onMounted(() => {
    window.addEventListener("offline", handleOffline);
    void load();
  });
  onBeforeUnmount(() => {
    window.removeEventListener("offline", handleOffline);
    generation += 1;
    clear();
  });

  return {
    data: readonly(data),
    state: readonly(state),
    message: readonly(message),
    requestId: readonly(requestId),
    load,
    clear,
  };
};
