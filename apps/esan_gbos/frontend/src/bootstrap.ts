export interface GbosBootstrap {
  user: string;
  roles: string[];
  csrf_token: string;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export const readGbosBootstrap = (): GbosBootstrap | undefined => {
  if (typeof document === "undefined") {
    return undefined;
  }
  const element = document.getElementById("gbos-bootstrap");
  if (!element || element.getAttribute("type") !== "application/json") {
    return undefined;
  }
  try {
    const value: unknown = JSON.parse(element.textContent ?? "");
    if (
      !isRecord(value) ||
      typeof value.user !== "string" ||
      !Array.isArray(value.roles) ||
      !value.roles.every((role) => typeof role === "string") ||
      typeof value.csrf_token !== "string"
    ) {
      return undefined;
    }
    return {
      user: value.user,
      roles: value.roles,
      csrf_token: value.csrf_token,
    };
  } catch {
    return undefined;
  }
};

export const clearGbosBootstrap = () => {
  if (typeof document !== "undefined") {
    document.getElementById("gbos-bootstrap")?.remove();
  }
};
