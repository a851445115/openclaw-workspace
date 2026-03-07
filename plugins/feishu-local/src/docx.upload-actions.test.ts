import { beforeEach, describe, expect, it, vi } from "vitest";

const createFeishuClientMock = vi.hoisted(() => vi.fn());
const uploadImageFeishuMock = vi.hoisted(() => vi.fn());
const uploadFileFeishuMock = vi.hoisted(() => vi.fn());

vi.mock("./client.js", () => ({
  createFeishuClient: createFeishuClientMock,
}));

vi.mock("./runtime.js", () => ({
  getFeishuRuntime: () => ({ channel: { media: { fetchRemoteMedia: vi.fn() } } }),
}));

vi.mock("./media.js", () => ({
  uploadImageFeishu: uploadImageFeishuMock,
  uploadFileFeishu: uploadFileFeishuMock,
}));

import { registerFeishuDocTools } from "./docx.js";

describe("feishu_doc upload actions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    createFeishuClientMock.mockReturnValue({
      docx: {
        document: { convert: vi.fn() },
        documentBlock: { list: vi.fn(), patch: vi.fn() },
        documentBlockChildren: { create: vi.fn() },
      },
      drive: { media: { uploadAll: vi.fn() } },
      application: { scope: { list: vi.fn() } },
    });
    uploadImageFeishuMock.mockResolvedValue({ imageKey: "img_key_1" });
    uploadFileFeishuMock.mockResolvedValue({ fileKey: "file_key_1" });
  });

  it("supports upload_image action", async () => {
    const registerTool = vi.fn();
    registerFeishuDocTools({
      config: {
        channels: {
          feishu: {
            appId: "app_id",
            appSecret: "app_secret",
          },
        },
      } as any,
      logger: { debug: vi.fn(), info: vi.fn() } as any,
      registerTool,
    } as any);

    const feishuDocTool = registerTool.mock.calls
      .map((call) => call[0])
      .find((tool) => tool.name === "feishu_doc");

    const result = await feishuDocTool.execute("tool-call", {
      action: "upload_image",
      image_path: "/tmp/test.png",
    } as any);

    expect(uploadImageFeishuMock).toHaveBeenCalledWith(
      expect.objectContaining({ image: "/tmp/test.png" }),
    );
    expect(result.details).toMatchObject({ imageKey: "img_key_1" });
  });

  it("supports upload_file action", async () => {
    const registerTool = vi.fn();
    registerFeishuDocTools({
      config: {
        channels: {
          feishu: {
            appId: "app_id",
            appSecret: "app_secret",
          },
        },
      } as any,
      logger: { debug: vi.fn(), info: vi.fn() } as any,
      registerTool,
    } as any);

    const feishuDocTool = registerTool.mock.calls
      .map((call) => call[0])
      .find((tool) => tool.name === "feishu_doc");

    const result = await feishuDocTool.execute("tool-call", {
      action: "upload_file",
      file_path: "/tmp/test.pdf",
      file_name: "test.pdf",
      file_type: "pdf",
    } as any);

    expect(uploadFileFeishuMock).toHaveBeenCalledWith(
      expect.objectContaining({ file: "/tmp/test.pdf", fileName: "test.pdf", fileType: "pdf" }),
    );
    expect(result.details).toMatchObject({ fileKey: "file_key_1" });
  });
});
