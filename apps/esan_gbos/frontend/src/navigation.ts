export interface NavigationItem {
  label: string;
  to: string;
  roles: readonly string[];
}

export const WORKSPACE_NAVIGATION: readonly NavigationItem[] = [
  { label: "经营总览", to: "/gbos/ceo", roles: ["CEO"] },
  {
    label: "销售协同",
    to: "/gbos/sales",
    roles: ["Sales Manager", "Sales User"],
  },
  {
    label: "采购协同",
    to: "/gbos/purchase",
    roles: ["Purchase Manager", "Buyer"],
  },
  { label: "产品与样品", to: "/gbos/product", roles: ["Product/R&D"] },
  { label: "审核队列", to: "/gbos/review", roles: ["Reviewer"] },
  {
    label: "集成状态",
    to: "/gbos/integrations",
    roles: ["Integration Admin"],
  },
  {
    label: "沟通观察",
    to: "/gbos/communications",
    roles: ["CEO", "Sales Manager", "Sales User"],
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
