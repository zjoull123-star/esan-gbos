import { readonly, shallowReactive } from "vue";

import { clearGbosBootstrap, readGbosBootstrap } from "./bootstrap";

export interface GbosSession {
  user: string;
  roles: string[];
  authenticated: boolean;
}

interface FrappeHost {
  session?: { user?: string };
  boot?: { user?: { roles?: string[] } };
}

export const readFrappeSession = (): GbosSession => {
  const bootstrap = readGbosBootstrap();
  const host = globalThis as typeof globalThis & { frappe?: FrappeHost };
  const user = bootstrap?.user ?? host.frappe?.session?.user ?? "Guest";
  const roles =
    bootstrap?.roles ??
    host.frappe?.boot?.user?.roles?.filter(
      (role): role is string => typeof role === "string",
    ) ??
    [];
  return {
    user,
    roles,
    authenticated: user !== "Guest",
  };
};

const state = shallowReactive<GbosSession>(readFrappeSession());

export const sessionState = readonly(state);

export const refreshSession = () => {
  const current = readFrappeSession();
  state.user = current.user;
  state.roles = current.roles;
  state.authenticated = current.authenticated;
};

export const clearSession = () => {
  clearGbosBootstrap();
  state.user = "Guest";
  state.roles = [];
  state.authenticated = false;
};
