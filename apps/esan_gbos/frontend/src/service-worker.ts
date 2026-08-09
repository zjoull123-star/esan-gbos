/// <reference lib="webworker" />

import { clientsClaim, type RouteHandlerCallbackOptions } from "workbox-core";
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

const offlineClientIds = new Set<string>();

const messageSourceId = (source: ExtendableMessageEvent["source"]) =>
  source && "id" in source && typeof source.id === "string"
    ? source.id
    : undefined;

self.addEventListener("message", (event) => {
  const data: unknown = event.data;
  const sourceId = messageSourceId(event.source);
  if (
    typeof data === "object" &&
    data !== null &&
    "type" in data &&
    data.type === "GBOS_NETWORK_STATE_QUERY"
  ) {
    event.ports[0]?.postMessage({
      online:
        Boolean(sourceId) &&
        !offlineClientIds.has(sourceId ?? "") &&
        self.navigator.onLine,
    });
    return;
  }
  if (
    typeof data === "object" &&
    data !== null &&
    "type" in data &&
    data.type === "GBOS_NETWORK_STATE" &&
    "online" in data &&
    typeof data.online === "boolean" &&
    sourceId
  ) {
    if (data.online) {
      offlineClientIds.delete(sourceId);
    } else {
      offlineClientIds.add(sourceId);
    }
    event.ports[0]?.postMessage({ acknowledged: true });
  }
});

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
const offlineShellResponse = async (options: RouteHandlerCallbackOptions) => {
  const cachedShell = await offlineShellHandler(options);
  const headers = new Headers(cachedShell.headers);
  headers.delete("content-length");
  headers.set("Cache-Control", "no-store");
  const markedShell = (await cachedShell.text()).replace(
    "<html ",
    '<html data-gbos-offline-shell="true" ',
  );
  return new Response(markedShell, {
    status: cachedShell.status,
    statusText: cachedShell.statusText,
    headers,
  });
};
registerRoute(
  new NavigationRoute(
    async (options) => {
      const eventClientId =
        "clientId" in options.event &&
        typeof options.event.clientId === "string"
          ? options.event.clientId
          : undefined;
      const clientReportedOffline =
        Boolean(eventClientId) && offlineClientIds.has(eventClientId ?? "");
      if (clientReportedOffline || !self.navigator.onLine) {
        return offlineShellResponse(options);
      }
      try {
        return await fetch(options.request);
      } catch {
        return offlineShellResponse(options);
      }
    },
    {
      denylist: [/^\/api\//u],
    },
  ),
);
