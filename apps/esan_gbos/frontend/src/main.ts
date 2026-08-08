import { createApp } from "vue";

import App from "./App.vue";
import { createBffClient } from "./api/bff";
import { BFF_CLIENT_KEY } from "./api/injection";
import { createAppRouter } from "./router";
import "./styles.css";

const app = createApp(App);
const router = createAppRouter();

app.provide(BFF_CLIENT_KEY, createBffClient());
app.use(router);

void router.isReady().then(async () => {
  if (!router.currentRoute.value.matched.length) {
    await router.replace("/gbos");
  }
  app.mount("#app");
});
