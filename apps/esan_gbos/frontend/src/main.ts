import { createApp } from "vue";

import App from "./App.vue";
import { createBffClient } from "./api/bff";
import { BFF_CLIENT_KEY } from "./api/injection";
import { navigationForRoles } from "./navigation";
import { createAppRouter } from "./router";
import { sessionState } from "./session";
import "./styles.css";

const app = createApp(App);
const router = createAppRouter();

app.provide(BFF_CLIENT_KEY, createBffClient());
app.use(router);

void router.isReady().then(async () => {
  if (!router.currentRoute.value.matched.length) {
    const first = navigationForRoles(sessionState.roles)[0];
    if (first) {
      await router.replace(first.to);
    }
  }
  app.mount("#app");
});
