import { createApp } from "vue";

import App from "./App.vue";
import { createBffClient } from "./api/bff";
import { BFF_CLIENT_KEY } from "./api/injection";
import { createAppRouter } from "./router";
import "./design/tokens.css";
import "./design/base.css";
import "./styles.css";

const app = createApp(App);
const router = createAppRouter();

app.provide(BFF_CLIENT_KEY, createBffClient());
app.use(router);

const readControllerNetworkState = async (): Promise<boolean | undefined> => {
  const controller = navigator.serviceWorker?.controller;
  if (!controller) {
    return undefined;
  }
  return new Promise((resolve) => {
    const channel = new MessageChannel();
    const timeout = window.setTimeout(() => resolve(undefined), 500);
    channel.port1.onmessage = (event: MessageEvent<unknown>) => {
      window.clearTimeout(timeout);
      const data = event.data;
      resolve(
        typeof data === "object" &&
          data !== null &&
          "online" in data &&
          typeof data.online === "boolean"
          ? data.online
          : undefined,
      );
    };
    controller.postMessage({ type: "GBOS_NETWORK_STATE_QUERY" }, [channel.port2]);
  });
};

void router.isReady().then(async () => {
  if (!router.currentRoute.value.matched.length) {
    await router.replace("/gbos");
  }
  const servedOfflineShell =
    document.documentElement.dataset.gbosOfflineShell === "true";
  const controllerOnline = await readControllerNetworkState();
  if (servedOfflineShell || !navigator.onLine || controllerOnline === false) {
    document.documentElement.dataset.gbosOfflineShell = "true";
  } else {
    delete document.documentElement.dataset.gbosOfflineShell;
  }
  app.mount("#app");
});
