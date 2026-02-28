import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { emptyPluginConfigSchema } from "openclaw/plugin-sdk";

const plugin = {
  id: "knowledge-preflight-router",
  name: "Knowledge Preflight Router",
  description: "Minimal runtime plugin for hard preflight routing config.",
  configSchema: emptyPluginConfigSchema(),
  register(api: OpenClawPluginApi) {
    api.runtime.log?.("[knowledge-preflight-router] plugin loaded");
  },
};

export default plugin;
