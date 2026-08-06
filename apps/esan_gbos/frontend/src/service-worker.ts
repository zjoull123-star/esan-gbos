/// <reference lib="webworker" />

import { clientsClaim } from "workbox-core";
import { ExpirationPlugin } from "workbox-expiration";
import {
  cleanupOutdatedCaches,
  createHandlerBoundToURL,
  precacheAndRoute,
} from "workbox-precaching";
import { NavigationRoute, registerRoute } from "workbox-routing";
import { CacheFirst, NetworkOnly } from "workbox-strategies";

declare const self: ServiceWorkerGlobalScope & {
  __WB_MANIFEST: Array<{ url: string; revision?: string | null }>;
};

self.skipWaiting();
clientsClaim();
cleanupOutdatedCaches();

// Only Vite's versioned static shell enters the precache manifest.
precacheAndRoute(self.__WB_MANIFEST);

// This route is registered first and must stay NetworkOnly. No API response,
// customer record, message, recording, token, or command is available offline.
registerRoute(
  ({ url }) => url.pathname.startsWith("/api/"),
  new NetworkOnly(),
);

registerRoute(
  ({ request, url }) =>
    url.origin === self.location.origin &&
    url.pathname.startsWith("/assets/esan_gbos/frontend/") &&
    ["script", "style", "font", "image"].includes(request.destination),
  new CacheFirst({
    cacheName: "esan-gbos-static-shell-v1",
    plugins: [
      new ExpirationPlugin({
        maxEntries: 40,
        maxAgeSeconds: 60 * 60 * 24 * 30,
        purgeOnQuotaError: true,
      }),
    ],
  }),
);

// Online navigations must come from Frappe so the authenticated bootstrap is
// current and never persisted. Only a network failure may use the empty,
// pre-cached shell; app views then fail closed with “需要联网”.
const offlineShellHandler = createHandlerBoundToURL("index.html");
registerRoute(
  new NavigationRoute(
    async (options) => {
      try {
        return await fetch(options.request);
      } catch {
        return offlineShellHandler(options);
      }
    },
    {
      denylist: [/^\/api\//u],
    },
  ),
);
