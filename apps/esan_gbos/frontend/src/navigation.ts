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
] as const;

export const navigationForRoles = (roles: readonly string[]) => {
  if (roles.includes("GBOS Admin")) {
    return [...WORKSPACE_NAVIGATION];
  }
  return WORKSPACE_NAVIGATION.filter((item) =>
    item.roles.some((role) => roles.includes(role)),
  );
};
