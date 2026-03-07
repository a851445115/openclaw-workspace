import { describe, expect, it, vi } from "vitest";

import plugin from "../index.js";

describe("feishu-local plugin registration", () => {
  it("registers feishu_chat when chat tools are enabled", () => {
    const registerTool = vi.fn();
    const registerChannel = vi.fn();

    plugin.register({
      config: {
        channels: {
          feishu: {
            appId: "app_id",
            appSecret: "app_secret",
            tools: {
              doc: true,
              chat: true,
              wiki: false,
              drive: false,
              perm: false,
              scopes: false,
            },
          },
        },
      } as any,
      runtime: { log: vi.fn() } as any,
      registerChannel,
      registerTool,
      logger: { debug: vi.fn(), info: vi.fn() } as any,
    } as any);

    const names = registerTool.mock.calls.map((call) => call[0]?.name);
    expect(names).toContain("feishu_chat");
  });
});
