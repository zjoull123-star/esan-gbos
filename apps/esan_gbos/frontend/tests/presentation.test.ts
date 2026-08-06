import { describe, expect, it } from "vitest";

import {
  flattenParty360Payload,
  flattenSampleStatusPayload,
  flattenSourcingBoardPayload,
} from "@/presentation";

describe("BFF presentation adapters", () => {
  it("flattens the nested sample status payload and reads revision from project", () => {
    const result = flattenSampleStatusPayload({
      project: {
        name: "SAMPLE-1",
        title: "柑橘方向小样",
        business_status: "Sent",
        revision: 7,
        origin: "Fixture",
      },
      iterations: [{ name: "ITER-1", iteration_number: 1, revision: 2 }],
      shipments: [{ name: "SHIP-1", carrier: "DHL", business_status: "Sent" }],
      feedback: [{ name: "FEEDBACK-1", summary: "留香表现合格" }],
    });

    expect(result.revision).toBe(7);
    expect(result.businessStatus).toBe("Sent");
    expect(result.records.map((record) => record.presentation_section)).toEqual([
      "样品项目",
      "样品迭代",
      "寄样记录",
      "客户反馈",
    ]);
    expect(result.records[0]).toMatchObject({ origin: "Fixture" });
  });

  it("flattens sourcing lanes while retaining the board total", () => {
    const result = flattenSourcingBoardPayload({
      lanes: {
        Draft: [
          {
            name: "SRC-1",
            title: "玻璃瓶询源",
            origin: "Fixture",
            candidates: [
              {
                name: "CANDIDATE-1",
                supplier_name: "合成供应商 A",
                quoted_price: 12.5,
                currency: "USD",
                lead_time_days: 21,
                candidate_status: "Shortlisted",
              },
            ],
          },
        ],
        Invited: [{ name: "SRC-2", title: "香精询源" }],
        Collecting: [],
        Evaluating: [],
        Selected: [],
        Closed: [{ name: "SRC-3", title: "已完成询源" }],
        Cancelled: [{ name: "SRC-4", title: "已取消询源" }],
      },
      total: 4,
    });

    expect(result.total).toBe(4);
    expect(result.records).toEqual([
      expect.objectContaining({
        name: "SRC-1",
        sourcing_lane: "Draft",
        presentation_section: "草稿",
      }),
      expect.objectContaining({
        name: "CANDIDATE-1",
        sourcing_event: "SRC-1",
        sourcing_lane: "Draft",
        presentation_section: "草稿 · 候选供应商",
      }),
      expect.objectContaining({
        name: "SRC-2",
        sourcing_lane: "Invited",
        presentation_section: "已邀请",
      }),
      expect.objectContaining({
        name: "SRC-3",
        sourcing_lane: "Closed",
        presentation_section: "已关闭",
      }),
      expect.objectContaining({
        name: "SRC-4",
        sourcing_lane: "Cancelled",
        presentation_section: "已取消",
      }),
    ]);
  });

  it("flattens all party 360 singleton and collection sections", () => {
    const result = flattenParty360Payload({
      profile: { name: "PARTY-1", display_name: "海湾香氛贸易" },
      organization: { name: "ORG-1", organization_name: "Gulf Aroma LLC" },
      contact: { name: "CONTACT-1", full_name: "Mariam" },
      lead: { name: "LEAD-1", lead_name: "Dubai retail lead" },
      deal: { name: "DEAL-1", title: "2026 香氛项目" },
      product_briefs: [{ name: "BRIEF-1", title: "柑橘香型" }],
      samples: [{ name: "SAMPLE-1", title: "第一轮小样" }],
      demands: [{ name: "DEMAND-1", title: "迪拜交付" }],
    });

    expect(result.records.map((record) => record.presentation_section)).toEqual([
      "客户档案",
      "组织",
      "联系人",
      "销售线索",
      "商机",
      "产品简报",
      "样品项目",
      "客户需求",
    ]);
    expect(result.records.map((record) => record.name)).toEqual([
      "PARTY-1",
      "ORG-1",
      "CONTACT-1",
      "LEAD-1",
      "DEAL-1",
      "BRIEF-1",
      "SAMPLE-1",
      "DEMAND-1",
    ]);
  });
});
