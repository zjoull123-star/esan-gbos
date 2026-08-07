import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

import {
  hasFullNavigationAccess,
  WORKSPACE_NAVIGATION,
} from "./navigation";

const CEO_ROLES = ["CEO"] as const;
const SALES_ROLES = ["Sales Manager", "Sales User"] as const;
const PURCHASE_ROLES = ["Purchase Manager", "Buyer"] as const;
const PRODUCT_ROLES = ["Product/R&D"] as const;
const REVIEW_ROLES = ["Reviewer"] as const;
const INTEGRATION_ROLES = ["Integration Admin"] as const;
const COMMUNICATION_ROLES = [...CEO_ROLES, ...SALES_ROLES] as const;

export const APP_ROUTES = [
  {
    path: "/gbos/ceo",
    name: "ceo",
    component: () => import("./views/WorkspaceView.vue"),
    props: { workspace: "ceo" },
    meta: { roles: CEO_ROLES },
  },
  {
    path: "/gbos/sales",
    name: "sales",
    component: () => import("./views/WorkspaceView.vue"),
    props: { workspace: "sales" },
    meta: { roles: SALES_ROLES },
  },
  {
    path: "/gbos/purchase",
    name: "purchase",
    component: () => import("./views/WorkspaceView.vue"),
    props: { workspace: "purchase" },
    meta: { roles: PURCHASE_ROLES },
  },
  {
    path: "/gbos/product",
    name: "product",
    component: () => import("./views/WorkspaceView.vue"),
    props: { workspace: "product" },
    meta: { roles: PRODUCT_ROLES },
  },
  {
    path: "/gbos/review",
    name: "review",
    component: () => import("./views/ReviewQueueView.vue"),
    meta: { roles: REVIEW_ROLES },
  },
  {
    path: "/gbos/integrations",
    name: "integrations",
    component: () => import("./views/IntegrationsView.vue"),
    meta: { roles: INTEGRATION_ROLES },
  },
  {
    path: "/gbos/communications",
    name: "communications",
    component: () => import("./views/CommunicationsView.vue"),
    meta: { roles: COMMUNICATION_ROLES },
  },
  {
    path: "/gbos/communications/:id",
    name: "communication-detail",
    component: () => import("./views/CommunicationDetailView.vue"),
    props: true,
    meta: { roles: COMMUNICATION_ROLES },
  },
  {
    path: "/gbos/review/:id",
    name: "review-detail",
    component: () => import("./views/ReviewDetailView.vue"),
    props: true,
    meta: { roles: REVIEW_ROLES },
  },
  {
    path: "/gbos/party/:id",
    name: "party-detail",
    component: () => import("./views/PartyDetailView.vue"),
    props: true,
    meta: { roles: [...CEO_ROLES, ...SALES_ROLES] },
  },
  {
    path: "/gbos/sample/:id",
    name: "sample-detail",
    component: () => import("./views/SampleDetailView.vue"),
    props: true,
    meta: { roles: [...CEO_ROLES, ...SALES_ROLES, ...PRODUCT_ROLES] },
  },
] as const satisfies readonly RouteRecordRaw[];

const pathMatches = (pattern: string, actual: string) => {
  const escaped = pattern
    .split("/")
    .map((segment) => (segment.startsWith(":") ? "[^/]+" : segment.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&")))
    .join("/");
  return new RegExp(`^${escaped}/?$`, "u").test(actual);
};

export const isRouteAllowed = (path: string, roles: readonly string[]) => {
  if (roles.includes("GBOS Admin")) {
    return true;
  }
  if (
    hasFullNavigationAccess(roles) &&
    WORKSPACE_NAVIGATION.some((item) => pathMatches(item.to, path))
  ) {
    return true;
  }
  const route = APP_ROUTES.find((candidate) => pathMatches(candidate.path, path));
  if (!route) {
    return false;
  }
  const allowed = route.meta.roles as readonly string[];
  return allowed.some((role) => roles.includes(role));
};

export const createAppRouter = () =>
  createRouter({
    history: createWebHistory(),
    routes: [...APP_ROUTES],
    scrollBehavior: () => ({ top: 0 }),
  });
