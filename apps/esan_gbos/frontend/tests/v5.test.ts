import { describe, expect, it } from "vitest";

import { navigationForRoles } from "@/navigation";
import { APP_ROUTES, EMAIL_INBOX_DETAIL_ROUTE, isRouteAllowed } from "@/router";

describe("Email Gateway v5 navigation", () => {
  it("freezes the three deep links with backend-role parity", () => {
    const inbox = APP_ROUTES.find((route) => route.path === "/gbos/email");
    const detail = EMAIL_INBOX_DETAIL_ROUTE;
    const admin = APP_ROUTES.find((route) => route.path === "/gbos/email-gateway");

    expect(inbox?.meta.roles).toEqual([
      "CEO",
      "Sales Manager",
      "Sales User",
      "Reviewer",
    ]);
    expect(detail?.meta.roles).toEqual(inbox?.meta.roles);
    expect(detail?.props).toBe(true);
    expect(admin?.meta.roles).toEqual(["Integration Admin"]);
    expect(isRouteAllowed("/gbos/email", ["Sales User"])).toBe(true);
    expect(isRouteAllowed("/gbos/email", ["Buyer"])).toBe(false);
    expect(isRouteAllowed("/gbos/email", ["Integration Admin"])).toBe(false);
    expect(isRouteAllowed("/gbos/email/INB-01", ["Sales User"])).toBe(true);
    expect(isRouteAllowed("/gbos/email/INB-01", ["Integration Admin"])).toBe(false);
    expect(isRouteAllowed("/gbos/email-gateway", ["Integration Admin"])).toBe(true);
    expect(isRouteAllowed("/gbos/email-gateway", ["CEO"])).toBe(false);
    expect(isRouteAllowed("/gbos/email-gateway", ["GBOS Admin"])).toBe(true);
  });

  it("shows exactly the links each role can deep-link to", () => {
    expect(navigationForRoles(["Sales User"]).map((item) => item.to)).toContain("/gbos/email");
    expect(navigationForRoles(["Sales User"]).map((item) => item.to)).not.toContain(
      "/gbos/email-gateway",
    );
    expect(navigationForRoles(["Integration Admin"]).map((item) => item.to)).toContain(
      "/gbos/email-gateway",
    );
    expect(navigationForRoles(["Integration Admin"]).map((item) => item.to)).not.toContain(
      "/gbos/email",
    );
    expect(navigationForRoles(["CEO"]).map((item) => item.to)).not.toContain(
      "/gbos/email-gateway",
    );
  });
});
