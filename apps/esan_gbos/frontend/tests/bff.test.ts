import { describe, expect, it, vi } from "vitest";

import {
  BFF_ENDPOINTS,
  BffError,
  createBffClient,
  createIdempotencyKey,
  type Fetcher,
} from "@/api/bff";

const ok = (data: unknown, requestId = "req-1") =>
  new Response(
    JSON.stringify({
      message: {
        data,
        meta: { request_id: requestId, schema_version: "1.0" },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );

describe("冻结 BFF client", () => {
  it("只暴露 OpenAPI 固定的八个方法路径", () => {
    expect(Object.values(BFF_ENDPOINTS)).toEqual([
      "/api/method/esan_gbos.api.v1.party.get_360",
      "/api/method/esan_gbos.api.v1.work_item.list",
      "/api/method/esan_gbos.api.v1.sample.get_status",
      "/api/method/esan_gbos.api.v1.sourcing.get_board",
      "/api/method/esan_gbos.api.v1.sample.create_project",
      "/api/method/esan_gbos.api.v1.sample.record_feedback",
      "/api/method/esan_gbos.api.v1.sourcing.create_from_demand",
      "/api/method/esan_gbos.api.v1.work_item.transition",
    ]);
  });

  it("GET 使用同源 Frappe session 并安全编码查询参数", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(ok({ name: "CUST-一号" }));
    const client = createBffClient({ fetcher, isOnline: () => true });

    const result = await client.getParty360("CUST-一号");

    expect(result.data).toEqual({ name: "CUST-一号" });
    expect(fetcher).toHaveBeenCalledWith(
      "/api/method/esan_gbos.api.v1.party.get_360?party=CUST-%E4%B8%80%E5%8F%B7",
      expect.objectContaining({
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
      }),
    );
  });

  it("工作项过滤器被 JSON 编码且 page_size 受契约范围约束", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(ok([]));
    const client = createBffClient({ fetcher, isOnline: () => true });

    await client.listWorkItems({
      filters: { team: "华东", business_status: "Open" },
      pageSize: 25,
    });

    const url = new URL(String(fetcher.mock.calls[0]?.[0]), "https://gbos.invalid");
    expect(JSON.parse(url.searchParams.get("filters") ?? "{}")).toEqual({
      team: "华东",
      business_status: "Open",
    });
    expect(url.searchParams.get("page_size")).toBe("25");
    expect(() => client.listWorkItems({ pageSize: 51 })).toThrow("page_size");
  });

  it("POST 添加 CSRF、幂等键和 revision，且使用表单编码", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(ok({ name: "SAMPLE-1" }));
    const client = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-123",
    });

    await client.createSampleProject({
      team: "TEAM-1",
      title: "柑橘样品",
      expected_revision: 0,
      idempotency_key: "idem-key-123",
      origin: "Manual",
    });

    const [, init] = fetcher.mock.calls[0] ?? [];
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: expect.objectContaining({
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Frappe-CSRF-Token": "csrf-123",
      }),
    });
    const body = new URLSearchParams(String(init?.body));
    expect(Object.fromEntries(body)).toMatchObject({
      expected_revision: "0",
      idempotency_key: "idem-key-123",
      team: "TEAM-1",
    });
  });

  it("正式 shell 可通过内存 bootstrap script 提供 CSRF", async () => {
    document.body.innerHTML = `
      <script id="gbos-bootstrap" type="application/json">
        {"user":"sales@example.invalid","roles":["Sales User"],"csrf_token":"csrf-bootstrap"}
      </script>
    `;
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(ok({ name: "WORK-1" }));
    const client = createBffClient({ fetcher, isOnline: () => true });

    await client.transitionWorkItem({
      name: "WORK-1",
      to_status: "Done",
      expected_revision: 1,
      idempotency_key: "idem-bootstrap-1",
    });

    expect(fetcher.mock.calls[0]?.[1]?.headers).toMatchObject({
      "X-Frappe-CSRF-Token": "csrf-bootstrap",
    });
  });

  it("没有 CSRF 或缺少命令控制字段时在网络前失败", async () => {
    const fetcher = vi.fn<Fetcher>();
    const withoutCsrf = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "",
    });

    await expect(
      withoutCsrf.transitionWorkItem({
        name: "WORK-1",
        to_status: "Done",
        expected_revision: 2,
        idempotency_key: "idem-key-234",
      }),
    ).rejects.toMatchObject({ code: "csrf_missing" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("离线时关闭读取与命令且给出中文可操作错误", async () => {
    const fetcher = vi.fn<Fetcher>();
    const client = createBffClient({ fetcher, isOnline: () => false });

    await expect(client.getSampleStatus("SAMPLE-1")).rejects.toMatchObject({
      code: "offline",
      displayMessage: "需要联网，请检查网络后重试。",
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("保留服务端中文错误和 request_id", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "permission_denied",
            message: "无权查看该客户",
            request_id: "req-denied",
            details: {},
          },
        }),
        { status: 403, headers: { "Content-Type": "application/json" } },
      ),
    );
    const client = createBffClient({ fetcher, isOnline: () => true });

    await expect(client.getParty360("CUST-2")).rejects.toEqual(
      expect.objectContaining({
        code: "permission_denied",
        displayMessage: "无权查看该客户",
        requestId: "req-denied",
        status: 403,
      }),
    );
  });

  it("拒绝不是 schema_version 1.0 的成功响应", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(
      new Response(
        JSON.stringify({
          data: {},
          meta: { request_id: "req-old", schema_version: "0.9" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const client = createBffClient({ fetcher, isOnline: () => true });

    await expect(client.getSourcingBoard()).rejects.toMatchObject({
      code: "schema_mismatch",
      requestId: "req-old",
    });
  });

  it("幂等键每次在内存中生成且满足长度约束", () => {
    const first = createIdempotencyKey();
    const second = createIdempotencyKey();
    expect(first.length).toBeGreaterThanOrEqual(8);
    expect(second).not.toBe(first);
  });

  it("网络异常规范化为不泄漏底层细节的中文错误", async () => {
    const fetcher = vi.fn<Fetcher>().mockRejectedValue(new TypeError("socket detail"));
    const client = createBffClient({ fetcher, isOnline: () => true });

    const promise = client.listWorkItems();
    await expect(promise).rejects.toBeInstanceOf(BffError);
    await expect(promise).rejects.toMatchObject({
      code: "network_error",
      displayMessage: "网络请求失败，请检查连接后重试。",
    });
  });
});
