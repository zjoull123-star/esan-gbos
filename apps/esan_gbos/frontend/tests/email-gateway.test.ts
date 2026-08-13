import axe from "axe-core";
import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import {
  EMAIL_GATEWAY_CLIENT_KEY,
  EMAIL_GATEWAY_ENDPOINTS,
  createEmailGatewayClient,
  type EmailGatewayFetcher,
} from "@/api/email-gateway";
import EmailGatewayAdminView from "@/views/EmailGatewayAdminView.vue";
import EmailInboxView from "@/views/EmailInboxView.vue";

const okV5 = (data: unknown) =>
  new Response(
    JSON.stringify({
      message: { data, meta: { request_id: "req-v5", schema_version: "5.0" } },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );

const mailbox = {
  mailbox_ref: "MBX-01",
  display_label: "海湾销售主入口",
  provider_kind: "fake" as const,
  business_mode: "primary" as const,
  business_purpose: "sales_follow_up",
  default_team_label: "海湾销售组",
  account_owner_label: "邮箱负责人",
  inbound_enabled: true,
  outbound_enabled: false as const,
  status: "active" as const,
  config_revision: 3,
};

const inboxItem = {
  inbox_item_ref: "INB-01",
  mailbox_label: "海湾销售主入口",
  mailbox_role: "primary" as const,
  received_at: "2026-08-13T08:00:00Z",
  state: "identity_pending" as const,
  safe_summary: "新的销售咨询",
  team_label: "海湾销售组",
  revision: 1,
};

describe("Email Gateway v5 typed client", () => {
  it("uses only the seven frozen endpoints and no-store requests", async () => {
    expect(EMAIL_GATEWAY_ENDPOINTS).toEqual({
      mailboxList: "/api/method/esan_gbos.api.v5.email_admin.list",
      mailboxGet: "/api/method/esan_gbos.api.v5.email_admin.get",
      mailboxUpsert: "/api/method/esan_gbos.api.v5.email_admin.upsert",
      mailboxSetStatus: "/api/method/esan_gbos.api.v5.email_admin.set_status",
      connectorHealth: "/api/method/esan_gbos.api.v5.email_admin.get_connector_health",
      inboxList: "/api/method/esan_gbos.api.v5.email_inbox.list",
      inboxGet: "/api/method/esan_gbos.api.v5.email_inbox.get",
    });
    const fetcher = vi.fn<EmailGatewayFetcher>().mockImplementation((input) => {
      const path = new URL(String(input), "https://gbos.invalid").pathname;
      if (path === EMAIL_GATEWAY_ENDPOINTS.mailboxList) {
        return Promise.resolve(okV5({ mailboxes: [mailbox], next_cursor: null }));
      }
      return Promise.resolve(okV5({ inbox_items: [inboxItem], next_cursor: null }));
    });
    const client = createEmailGatewayClient({ fetcher, isOnline: () => true });

    await client.listMailboxes({ pageSize: 20 });
    await client.listInbox({ state: "identity_pending", pageSize: 25 });

    expect(fetcher).toHaveBeenCalledTimes(2);
    for (const [, init] of fetcher.mock.calls) {
      expect(init).toMatchObject({ method: "GET", cache: "no-store", credentials: "same-origin" });
      expect(init?.headers).toMatchObject({ "Cache-Control": "no-store", Pragma: "no-cache" });
    }
  });

  it("sends CSRF, expected revision and idempotency for safe status changes", async () => {
    const fetcher = vi.fn<EmailGatewayFetcher>().mockResolvedValue(okV5({ mailbox }));
    const client = createEmailGatewayClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-v5",
    });

    await client.setMailboxStatus({
      mailbox_ref: "MBX-01",
      action: "pause",
      expected_revision: 3,
      idempotency_key: "pause-mailbox-01",
    });

    const [, init] = fetcher.mock.calls[0] ?? [];
    expect(init).toMatchObject({
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "X-Frappe-CSRF-Token": "csrf-v5" },
    });
    expect(Object.fromEntries(new URLSearchParams(String(init?.body)))).toEqual({
      mailbox_ref: "MBX-01",
      action: "pause",
      expected_revision: "3",
      idempotency_key: "pause-mailbox-01",
    });
  });

  it("fails closed when downstream adds a sensitive field", async () => {
    const fetcher = vi.fn<EmailGatewayFetcher>().mockResolvedValue(
      okV5({ mailboxes: [{ ...mailbox, credential_ref: "protected-ref" }], next_cursor: null }),
    );
    const client = createEmailGatewayClient({ fetcher, isOnline: () => true });

    await expect(client.listMailboxes()).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects incomplete or invented mailbox authority before the network", async () => {
    const fetcher = vi.fn<EmailGatewayFetcher>();
    const client = createEmailGatewayClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-v5",
    });

    await expect(
      client.upsertMailbox({
        display_label: "主入口",
        provider_kind: "fake",
        business_mode: "primary",
        business_purpose: "sales_inquiry",
        provider_account_ref: "",
        observer_connector_instance_ref: "connector-1",
        default_team_ref: "TEAM-1",
        account_owner_user_ref: "",
        priority: 10,
        credential_ref: "inline-secret",
        inbound_enabled: false,
        outbound_enabled: false,
        expected_revision: 0,
        idempotency_key: "mailbox-create-invalid",
      } as never),
    ).rejects.toMatchObject({ code: "validation_error" });
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("Email inbox Phase 1 view", () => {
  it("renders safe summaries and details without Phase 2 controls or sensitive DOM", async () => {
    const client = {
      listInbox: vi.fn().mockResolvedValue({ data: { inbox_items: [inboxItem], next_cursor: null } }),
      getInboxItem: vi.fn().mockResolvedValue({
        data: {
          inbox_item: {
            ...inboxItem,
            assignee_label: null,
            identity_state: "unknown",
          },
        },
      }),
    };
    const host = document.createElement("div");
    document.body.append(host);
    const wrapper = mount(EmailInboxView, {
      attachTo: host,
      global: { provide: { [EMAIL_GATEWAY_CLIENT_KEY as symbol]: client } },
    });
    await flushPromises();
    await wrapper.get("[data-inbox-detail]").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("新的销售咨询");
    expect(wrapper.text()).toContain("身份待确认");
    expect(
      wrapper.findAll("button").map((button) => button.text()),
    ).not.toEqual(expect.arrayContaining(["认领", "合并", "新建草稿", "发送"]));
    for (const sensitive of [
      "person@example.invalid",
      "provider-message-01",
      "protected-ref",
      "raw message body",
    ]) {
      expect(wrapper.html()).not.toContain(sensitive);
    }
    expect((await axe.run(wrapper.element)).violations).toEqual([]);
    wrapper.unmount();
  });
});

describe("Email Gateway admin Phase 1 view", () => {
  it("creates another primary mailbox with outbound locked off", async () => {
    const created = { ...mailbox, mailbox_ref: "MBX-03", config_revision: 1 };
    const client = {
      listMailboxes: vi.fn().mockResolvedValue({
        data: { mailboxes: [mailbox], next_cursor: null },
      }),
      listConnectorHealth: vi.fn().mockResolvedValue({ data: { connector_health: [] } }),
      upsertMailbox: vi.fn().mockResolvedValue({ data: { mailbox: created } }),
      setMailboxStatus: vi.fn(),
    };
    const wrapper = mount(EmailGatewayAdminView, {
      global: { provide: { [EMAIL_GATEWAY_CLIENT_KEY as symbol]: client } },
    });
    await flushPromises();

    await wrapper.get("[data-mailbox-create] input[name='display_label']").setValue("新增主入口");
    await wrapper
      .get("[data-mailbox-create] select[name='business_purpose']")
      .setValue("sales_follow_up");
    await wrapper
      .get("[data-mailbox-create] input[name='provider_account_ref']")
      .setValue("provider-account-sales");
    await wrapper
      .get("[data-mailbox-create] input[name='observer_connector_instance_ref']")
      .setValue("OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV");
    await wrapper
      .get("[data-mailbox-create] input[name='default_team_ref']")
      .setValue("TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV");
    await wrapper
      .get("[data-mailbox-create] input[name='account_owner_user_ref']")
      .setValue("owner@example.invalid");
    await wrapper.get("[data-mailbox-create] input[name='priority']").setValue("10");
    await wrapper
      .get("[data-mailbox-create] input[name='credential_ref']")
      .setValue("secretref:v1/email-sales");
    await wrapper.get("[data-mailbox-create]").trigger("submit");
    await flushPromises();

    expect(client.upsertMailbox).toHaveBeenCalledWith(
      expect.objectContaining({
        display_label: "新增主入口",
        business_mode: "primary",
        provider_kind: "fake",
        business_purpose: "sales_follow_up",
        provider_account_ref: "provider-account-sales",
        observer_connector_instance_ref: "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        default_team_ref: "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        account_owner_user_ref: "owner@example.invalid",
        priority: 10,
        credential_ref: "secretref:v1/email-sales",
        outbound_enabled: false,
        expected_revision: 0,
      }),
    );
  });

  it("keeps multiple primary mailboxes and confirms revision-fenced status changes", async () => {
    const second = { ...mailbox, mailbox_ref: "MBX-02", display_label: "中国销售主入口" };
    const client = {
      listMailboxes: vi.fn().mockResolvedValue({
        data: { mailboxes: [mailbox, second], next_cursor: null },
      }),
      listConnectorHealth: vi.fn().mockResolvedValue({ data: { connector_health: [] } }),
      upsertMailbox: vi.fn(),
      setMailboxStatus: vi.fn().mockResolvedValue({
        data: { mailbox: { ...mailbox, status: "paused", config_revision: 4 } },
      }),
    };
    const host = document.createElement("div");
    document.body.append(host);
    const wrapper = mount(EmailGatewayAdminView, {
      attachTo: host,
      global: { provide: { [EMAIL_GATEWAY_CLIENT_KEY as symbol]: client } },
    });
    await flushPromises();

    expect(wrapper.findAll("[data-mailbox-mode='primary']")).toHaveLength(2);
    await wrapper.get("[data-mailbox='MBX-01'] [data-status-action='pause']").trigger("click");
    await flushPromises();
    await wrapper.get("[data-confirm-status]").trigger("click");
    await flushPromises();
    expect(client.setMailboxStatus).toHaveBeenCalledWith(
      expect.objectContaining({
        mailbox_ref: "MBX-01",
        action: "pause",
        expected_revision: 3,
      }),
    );
    expect(wrapper.html()).not.toContain("secretref:v1/");
    expect((await axe.run(wrapper.element)).violations).toEqual([]);
    wrapper.unmount();
  });

  it("declares responsive, overflow-safe 375/768/1440 structure", async () => {
    const source = await import("@/views/EmailGatewayAdminView.vue?raw");
    expect(source.default).toContain("minmax(min(100%, 300px), 1fr)");
    expect(source.default).toContain("@media (max-width: 767px)");
    expect(source.default).toContain("min-width: 0");
  });
});
