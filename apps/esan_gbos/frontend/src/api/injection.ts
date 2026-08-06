import { inject, type InjectionKey } from "vue";

import type { BffClient } from "./bff";

export const BFF_CLIENT_KEY: InjectionKey<BffClient> = Symbol("gbos-bff-client");

export const useBffClient = () => {
  const client = inject(BFF_CLIENT_KEY);
  if (!client) {
    throw new Error("GBOS BFF client 未注册");
  }
  return client;
};
