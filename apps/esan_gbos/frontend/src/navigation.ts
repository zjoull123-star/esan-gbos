export type NavigationGroupId =
  | "management"
  | "operations"
  | "intelligence"
  | "system";

export interface NavigationItem {
  id: string;
  label: string;
  to: string;
  icon: string;
  group: NavigationGroupId;
  roles: readonly string[];
}

export const WORKSPACE_NAVIGATION: readonly NavigationItem[] = [
  {
    id: "ceo",
    label: "经营总览",
    to: "/gbos/ceo",
    icon: "chart-bar",
    group: "management",
    roles: ["CEO"],
  },
  {
    id: "sales",
    label: "销售协同",
    to: "/gbos/sales",
    icon: "users",
    group: "operations",
    roles: ["Sales Manager", "Sales User"],
  },
  {
    id: "purchase",
    label: "采购协同",
    to: "/gbos/purchase",
    icon: "shopping-bag",
    group: "operations",
    roles: ["Purchase Manager", "Buyer"],
  },
  {
    id: "product",
    label: "产品与样品",
    to: "/gbos/product",
    icon: "beaker",
    group: "operations",
    roles: ["Product/R&D"],
  },
  {
    id: "review",
    label: "审核队列",
    to: "/gbos/review",
    icon: "clipboard-check",
    group: "intelligence",
    roles: ["Reviewer"],
  },
  {
    id: "integrations",
    label: "集成状态",
    to: "/gbos/integrations",
    icon: "plug",
    group: "system",
    roles: ["Integration Admin"],
  },
  {
    id: "communications",
    label: "沟通观察",
    to: "/gbos/communications",
    icon: "message-circle",
    group: "intelligence",
    roles: ["CEO", "Sales Manager", "Sales User", "Integration Admin"],
  },
] as const;

export const hasFullNavigationAccess = (roles: readonly string[]) =>
  roles.some((role) => role === "GBOS Admin" || role === "CEO");

export const navigationForRoles = (roles: readonly string[]) => {
  if (hasFullNavigationAccess(roles)) {
    return [...WORKSPACE_NAVIGATION];
  }
  return WORKSPACE_NAVIGATION.filter((item) =>
    item.roles.some((role) => roles.includes(role)),
  );
};

export const defaultWorkspaceForRoles = (roles: readonly string[]) =>
  navigationForRoles(roles)[0]?.to;
