import axe from "axe-core";
import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import {
  EMAIL_GATEWAY_CLIENT_KEY,
  EMAIL_GATEWAY_ENDPOINTS,
  createEmailGatewayClient,
  type EmailGatewayFetcher,
} from "@/api/email-gateway";
import { BffError } from "@/api/bff";
import InboxQueueTabs from "@/components/email/InboxQueueTabs.vue";
import EmailGatewayAdminView from "@/views/EmailGatewayAdminView.vue";
import EmailInboxDetailView from "@/views/EmailInboxDetailView.vue";
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
  it("uses exactly the frozen 17-operation surface and no-store requests", async () => {
    expect(EMAIL_GATEWAY_ENDPOINTS).toEqual({
      mailboxList: "/api/method/esan_gbos.api.v5.email_admin.list_mailboxes",
      mailboxGet: "/api/method/esan_gbos.api.v5.email_admin.get_mailbox",
      ruleList: "/api/method/esan_gbos.api.v5.email_admin.list_rules",
      connectorHealth: "/api/method/esan_gbos.api.v5.email_admin.connector_health",
      mailboxUpsert: "/api/method/esan_gbos.api.v5.email_admin.upsert_mailbox",
      mailboxSetStatus: "/api/method/esan_gbos.api.v5.email_admin.set_mailbox_status",
      ruleUpsert: "/api/method/esan_gbos.api.v5.email_admin.upsert_rule",
      inboxList: "/api/method/esan_gbos.api.v5.email_inbox.list",
      inboxGet: "/api/method/esan_gbos.api.v5.email_inbox.get",
      inboxClaim: "/api/method/esan_gbos.api.v5.email_inbox.claim",
      inboxReassign: "/api/method/esan_gbos.api.v5.email_inbox.reassign",
      inboxTransition: "/api/method/esan_gbos.api.v5.email_inbox.transition",
      inboxMerge: "/api/method/esan_gbos.api.v5.email_inbox.merge",
      inboxSplit: "/api/method/esan_gbos.api.v5.email_inbox.split",
      inboxLinkBusiness: "/api/method/esan_gbos.api.v5.email_inbox.link_business",
      inboxSaveDraft: "/api/method/esan_gbos.api.v5.email_inbox.save_draft",
      inboxReveal: "/api/method/esan_gbos.api.v5.email_inbox.reveal",
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
    const fetcher = vi.fn<EmailGatewayFetcher>().mockResolvedValue(okV5({
      mailbox: { ...mailbox, config_revision: 4 },
    }));
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

  it("accepts one bounded email address only for the mailbox upsert request", async () => {
    const rawAddress = "mailbox-raw-sentinel@example.invalid";
    const fetcher = vi.fn<EmailGatewayFetcher>().mockResolvedValue(okV5({
      mailbox: { ...mailbox, config_revision: 4 },
    }));
    const client = createEmailGatewayClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-v5",
    });

    await client.upsertMailbox({
      canonical_mailbox_address: rawAddress,
      display_label: "主入口",
      provider_kind: "fake",
      business_mode: "primary",
      business_purpose: "sales_follow_up",
      provider_account_ref: "provider-account-sales",
      observer_connector_instance_ref: "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
      default_team_ref: "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
      account_owner_user_ref: "owner@example.invalid",
      priority: 10,
      credential_ref: "secretref:v1/email-sales",
      inbound_enabled: false,
      outbound_enabled: false,
      expected_revision: 3,
      idempotency_key: "mailbox-create-valid",
    });

    const [, init] = fetcher.mock.calls[0] ?? [];
    const params = new URLSearchParams(String(init?.body));
    const body = Object.fromEntries(params);
    expect(body.canonical_mailbox_address).toBe(rawAddress);
    expect(params.getAll("canonical_mailbox_address")).toEqual([rawAddress]);

    await expect(client.upsertMailbox({
      canonical_mailbox_address: "not-an-email",
      display_label: "主入口",
      provider_kind: "fake",
      business_mode: "primary",
      business_purpose: "sales_follow_up",
      provider_account_ref: "provider-account-sales",
      observer_connector_instance_ref: "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
      default_team_ref: "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
      account_owner_user_ref: "owner@example.invalid",
      priority: 10,
      credential_ref: "secretref:v1/email-sales",
      inbound_enabled: false,
      outbound_enabled: false,
      expected_revision: 3,
      idempotency_key: "mailbox-create-invalid-address",
    })).rejects.toMatchObject({ code: "validation_error" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("fails closed when downstream adds a sensitive field", async () => {
    const fetcher = vi.fn<EmailGatewayFetcher>().mockResolvedValue(
      okV5({ mailboxes: [{ ...mailbox, credential_ref: "protected-ref" }], next_cursor: null }),
    );
    const client = createEmailGatewayClient({ fetcher, isOnline: () => true });

    await expect(client.listMailboxes()).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("rejects raw identifiers, duplicate refs, unknown queue enums and malformed pagination", async () => {
    const unsafeResponses = [
      { inbox_items: [{ ...inboxItem, safe_summary: "person@example.invalid" }], next_cursor: null },
      { inbox_items: [inboxItem, inboxItem], next_cursor: null },
      { inbox_items: [{ ...inboxItem, state: "invented" }], next_cursor: null },
      { inbox_items: [inboxItem], next_cursor: "x".repeat(513) },
    ];
    for (const data of unsafeResponses) {
      const client = createEmailGatewayClient({
        fetcher: vi.fn<EmailGatewayFetcher>().mockResolvedValue(okV5(data)),
        isOnline: () => true,
      });
      await expect(client.listInbox()).rejects.toMatchObject({ code: "invalid_response" });
    }
  });

  it("preserves closed v5 error codes so 403 and 409 can fail closed", async () => {
    const errorResponse = (status: number, code: string) =>
      new Response(
        JSON.stringify({
          message: {
            error: {
              code,
              message: "safe error",
              request_id: "req-error",
              details: {},
            },
          },
        }),
        { status, headers: { "Content-Type": "application/json" } },
      );
    const forbidden = createEmailGatewayClient({
      fetcher: vi.fn<EmailGatewayFetcher>().mockResolvedValue(errorResponse(403, "permission_denied")),
      isOnline: () => true,
    });
    const conflict = createEmailGatewayClient({
      fetcher: vi.fn<EmailGatewayFetcher>().mockResolvedValue(errorResponse(409, "revision_conflict")),
      isOnline: () => true,
    });

    await expect(forbidden.getInboxItem("INB-01")).rejects.toMatchObject({
      code: "permission_denied",
      requestId: "req-error",
      status: 403,
    });
    await expect(conflict.getInboxItem("INB-01")).rejects.toMatchObject({
      code: "revision_conflict",
      requestId: "req-error",
      status: 409,
    });
  });

  it("supports create/edit-only drafts and governed commands without a send operation", async () => {
    const fetcher = vi.fn<EmailGatewayFetcher>().mockImplementation((input) => {
      const path = new URL(String(input), "https://gbos.invalid").pathname;
      if (path === EMAIL_GATEWAY_ENDPOINTS.inboxSaveDraft) {
        return Promise.resolve(okV5({ draft: { draft_ref: "DRF-01", revision: 2, state: "editable" } }));
      }
      return Promise.resolve(okV5({ inbox_item: { inbox_item_ref: "INB-01", state: "assigned", revision: 2 } }));
    });
    const client = createEmailGatewayClient({ fetcher, isOnline: () => true, getCsrfToken: () => "csrf-v5" });

    await client.claimInbox({ inbox_item_ref: "INB-01", expected_revision: 1, idempotency_key: "claim-0001" });
    await client.saveDraft({ inbox_item_ref: "INB-01", draft_ref: "DRF-01", content: "受控草稿", expected_revision: 1, idempotency_key: "draft-0001" });

    expect("send" in client).toBe(false);
    expect(fetcher.mock.calls.map(([input]) => new URL(String(input), "https://gbos.invalid").pathname)).toEqual([
      EMAIL_GATEWAY_ENDPOINTS.inboxClaim,
      EMAIL_GATEWAY_ENDPOINTS.inboxSaveDraft,
    ]);
  });

  it("rejects a command response whose revision did not advance", async () => {
    const client = createEmailGatewayClient({
      fetcher: vi.fn<EmailGatewayFetcher>().mockResolvedValue(okV5({
        inbox_item: { inbox_item_ref: "INB-01", state: "assigned", revision: 1 },
      })),
      isOnline: () => true,
      getCsrfToken: () => "csrf-v5",
    });

    await expect(client.claimInbox({
      inbox_item_ref: "INB-01",
      expected_revision: 1,
      idempotency_key: "claim-0001",
    })).rejects.toMatchObject({ code: "invalid_response" });
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

describe("Email inbox operator views", () => {
  it("renders all approved queues with tab/list semantics", () => {
    const wrapper = mount(InboxQueueTabs, { props: { modelValue: "all" } });
    expect(wrapper.get("[role='tablist']")).toBeTruthy();
    expect(wrapper.findAll("[role='tab']").map((tab) => tab.text())).toEqual([
      "全部", "身份待确认", "待分配", "首次回复将到期", "草稿", "发送失败或不确定",
      "等待客户", "等待内部", "已转化", "已关闭", "隔离区",
    ]);
  });

  it("renders safe list summaries and navigates to a separate detail route", async () => {
    const client = {
      listInbox: vi.fn().mockResolvedValue({ data: { inbox_items: [inboxItem], next_cursor: null } }),
    };
    const host = document.createElement("div");
    document.body.append(host);
    const wrapper = mount(EmailInboxView, {
      attachTo: host,
      global: { provide: { [EMAIL_GATEWAY_CLIENT_KEY as symbol]: client } },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("新的销售咨询");
    expect(wrapper.text()).toContain("身份待确认");
    expect(wrapper.get("[data-inbox-detail]").attributes("href")).toBe("/gbos/email/INB-01");
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

  it("distinguishes mailbox, channel owner, identity, customer and assignee while reveal stays hidden", async () => {
    const client = {
      getInboxItem: vi.fn().mockResolvedValue({
        data: { inbox_item: { ...inboxItem, assignee_label: "当前业务负责人", identity_state: "confirmed" } },
      }),
      claimInbox: vi.fn(),
      reassignInbox: vi.fn(),
      transitionInbox: vi.fn(),
      mergeInbox: vi.fn(),
      splitConversation: vi.fn(),
      linkBusiness: vi.fn(),
      saveDraft: vi.fn(),
      revealEvidence: vi.fn(),
    };
    const host = document.createElement("div");
    document.body.append(host);
    const wrapper = mount(EmailInboxDetailView, {
      attachTo: host,
      props: { inboxItemRef: "INB-01" },
      global: { provide: { [EMAIL_GATEWAY_CLIENT_KEY as symbol]: client } },
    });
    await flushPromises();
    for (const label of ["接收邮箱", "渠道账户负责人", "参与者身份状态", "客户 Party / Contact", "当前业务负责人"]) {
      expect(wrapper.text()).toContain(label);
    }
    expect(wrapper.text()).toContain("授权原文默认隐藏");
    expect(wrapper.html()).not.toContain("raw message body");
    expect(wrapper.findAll("button").map((button) => button.text())).not.toEqual(expect.arrayContaining(["批准", "发送"]));
    expect((await axe.run(wrapper.element)).violations).toEqual([]);
    wrapper.unmount();
    host.remove();
  });

  it("clears protected detail on 403", async () => {
    const getInboxItem = vi.fn()
      .mockResolvedValueOnce({ data: { inbox_item: { ...inboxItem, safe_summary: "仅授权可见摘要", assignee_label: null, identity_state: "unknown" } } })
      .mockRejectedValueOnce(new BffError("permission_denied", { status: 403 }));
    const wrapper = mount(EmailInboxDetailView, {
      props: { inboxItemRef: "INB-01" },
      global: { provide: { [EMAIL_GATEWAY_CLIENT_KEY as symbol]: { getInboxItem } } },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("仅授权可见摘要");
    await wrapper.get("[data-detail-refresh]").trigger("click");
    await flushPromises();
    expect(wrapper.text()).not.toContain("仅授权可见摘要");
    expect(wrapper.text()).toContain("当前角色无权执行此操作");
  });

  it("clears stale command state and performs one bounded reload on 409", async () => {
    const getInboxItem = vi.fn().mockResolvedValue({
      data: { inbox_item: { ...inboxItem, state: "unassigned", assignee_label: null, identity_state: "unknown" } },
    });
    const claimInbox = vi.fn().mockRejectedValue(new BffError("revision_conflict", { status: 409 }));
    const wrapper = mount(EmailInboxDetailView, {
      props: { inboxItemRef: "INB-01" },
      global: { provide: { [EMAIL_GATEWAY_CLIENT_KEY as symbol]: { getInboxItem, claimInbox } } },
    });
    await flushPromises();
    await wrapper.get("[data-claim-inbox]").trigger("click");
    await flushPromises();
    expect(getInboxItem).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("数据已被他人更新");
    expect(wrapper.find("[data-command-pending]").exists()).toBe(false);
  });
});

describe("Email Gateway admin Phase 1 view", () => {
  it("creates another primary mailbox with outbound locked off", async () => {
    const rawAddress = "mailbox-raw-sentinel@example.invalid";
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
    const addressInput = wrapper.get("[data-mailbox-create] input[name='canonical_mailbox_address']");
    expect(addressInput.attributes()).toMatchObject({
      type: "email",
      autocomplete: "off",
      spellcheck: "false",
    });
    await addressInput.setValue(rawAddress);
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
        canonical_mailbox_address: rawAddress,
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
    expect(addressInput.element).toHaveProperty("value", "");
    expect(wrapper.text()).not.toContain(rawAddress);
  });

  it("clears the real address after a failed mailbox request without auditing or displaying it", async () => {
    const rawAddress = "mailbox-failure-sentinel@example.invalid";
    const client = {
      listMailboxes: vi.fn().mockResolvedValue({ data: { mailboxes: [mailbox], next_cursor: null } }),
      listConnectorHealth: vi.fn().mockResolvedValue({ data: { connector_health: [] } }),
      listRules: vi.fn().mockResolvedValue({ data: { rules: [] } }),
      upsertMailbox: vi.fn().mockRejectedValue(new BffError("internal_error", { status: 503 })),
      setMailboxStatus: vi.fn(),
    };
    const wrapper = mount(EmailGatewayAdminView, {
      global: { provide: { [EMAIL_GATEWAY_CLIENT_KEY as symbol]: client } },
    });
    await flushPromises();

    await wrapper.get("[data-mailbox-create] input[name='display_label']").setValue("失败入口");
    const addressInput = wrapper.get("[data-mailbox-create] input[name='canonical_mailbox_address']");
    await addressInput.setValue(rawAddress);
    await wrapper.get("[data-mailbox-create] input[name='provider_account_ref']").setValue("provider-account-sales");
    await wrapper.get("[data-mailbox-create] input[name='observer_connector_instance_ref']").setValue("OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV");
    await wrapper.get("[data-mailbox-create] input[name='default_team_ref']").setValue("TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV");
    await wrapper.get("[data-mailbox-create] input[name='account_owner_user_ref']").setValue("owner@example.invalid");
    await wrapper.get("[data-mailbox-create] input[name='credential_ref']").setValue("secretref:v1/email-sales");
    await wrapper.get("[data-mailbox-create]").trigger("submit");
    await flushPromises();

    expect(client.upsertMailbox).toHaveBeenCalledWith(expect.objectContaining({
      canonical_mailbox_address: rawAddress,
    }));
    expect(addressInput.element).toHaveProperty("value", "");
    expect(wrapper.text()).not.toContain(rawAddress);
    expect(wrapper.get(".email-audit-section").text()).not.toContain(rawAddress);
    expect(window.location.href).not.toContain(rawAddress);
    expect(Object.values(localStorage)).not.toContain(rawAddress);
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
