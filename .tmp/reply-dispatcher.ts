import {
  createReplyPrefixContext,
  createTypingCallbacks,
  logTypingFailure,
  type ClawdbotConfig,
  type ReplyPayload,
  type RuntimeEnv,
} from "openclaw/plugin-sdk";
import { resolveFeishuAccount } from "./accounts.js";
import type { MentionTarget } from "./mention.js";
import { getFeishuRuntime } from "./runtime.js";
import { sendMessageFeishu } from "./send.js";
import { addTypingIndicator, removeTypingIndicator, type TypingIndicatorState } from "./typing.js";

const FINAL_TEXT_RETRY_MAX_ATTEMPTS = 4;
const FINAL_TEXT_RETRY_BASE_DELAY_MS = 1200;
const FINAL_TEXT_REPLY_GONE_CODES = ["230011", "231003"];
const finalTextDeliveryQueues = new Map<string, Promise<void>>();

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function shouldFallbackToFreshMessage(error: unknown): boolean {
  const message = String(error ?? "");
  return FINAL_TEXT_REPLY_GONE_CODES.some((code) => message.includes(code));
}

type SendFinalTextParams = {
  cfg: ClawdbotConfig;
  accountId?: string;
  chatId: string;
  text: string;
  replyToMessageId?: string;
  mentions?: MentionTarget[];
  textChunkLimit: number;
  chunkMode: string;
  tableMode: string;
  core: ReturnType<typeof getFeishuRuntime>;
};

async function sendFinalText(params: SendFinalTextParams): Promise<void> {
  const converted = params.core.channel.text.convertMarkdownTables(params.text, params.tableMode);
  let first = true;
  for (const chunk of params.core.channel.text.chunkTextWithMode(
    converted,
    params.textChunkLimit,
    params.chunkMode,
  )) {
    await sendMessageFeishu({
      cfg: params.cfg,
      to: params.chatId,
      text: chunk,
      replyToMessageId: first ? params.replyToMessageId : undefined,
      mentions: first ? params.mentions : undefined,
      accountId: params.accountId,
    });
    first = false;
  }
}

function queueFinalTextWithConsistency(params: {
  cfg: ClawdbotConfig;
  core: ReturnType<typeof getFeishuRuntime>;
  accountKey: string;
  accountId?: string;
  chatId: string;
  text: string;
  replyToMessageId?: string;
  mentions?: MentionTarget[];
  textChunkLimit: number;
  chunkMode: string;
  tableMode: string;
  log?: (message: string) => void;
  error?: (message: string) => void;
}) {
  const queueKey = `${params.accountKey}:${params.chatId}`;
  const previous = finalTextDeliveryQueues.get(queueKey) ?? Promise.resolve();
  let current: Promise<void>;
  current = previous
    .catch(() => {})
    .then(async () => {
      let replyToId = params.replyToMessageId;
      for (let attempt = 1; attempt <= FINAL_TEXT_RETRY_MAX_ATTEMPTS; attempt++) {
        try {
          await sendFinalText({
            cfg: params.cfg,
            accountId: params.accountId,
            chatId: params.chatId,
            text: params.text,
            replyToMessageId: replyToId,
            mentions: params.mentions,
            textChunkLimit: params.textChunkLimit,
            chunkMode: params.chunkMode,
            tableMode: params.tableMode,
            core: params.core,
          });
          params.log?.(
            `feishu[${params.accountKey}] final consistency delivered (attempt=${attempt}, fallbackToFresh=${replyToId ? "no" : "yes"})`,
          );
          return;
        } catch (error) {
          if (replyToId && shouldFallbackToFreshMessage(error)) {
            replyToId = undefined;
          }
          if (attempt >= FINAL_TEXT_RETRY_MAX_ATTEMPTS) {
            params.error?.(
              `feishu[${params.accountKey}] final consistency delivery failed after ${attempt} attempts: ${String(error)}`,
            );
            return;
          }
          await sleep(FINAL_TEXT_RETRY_BASE_DELAY_MS * attempt);
        }
      }
    })
    .finally(() => {
      if (finalTextDeliveryQueues.get(queueKey) === current) {
        finalTextDeliveryQueues.delete(queueKey);
      }
    });
  finalTextDeliveryQueues.set(queueKey, current);
}

export type CreateFeishuReplyDispatcherParams = {
  cfg: ClawdbotConfig;
  agentId: string;
  runtime: RuntimeEnv;
  chatId: string;
  replyToMessageId?: string;
  mentionTargets?: MentionTarget[];
  accountId?: string;
};

export function createFeishuReplyDispatcher(params: CreateFeishuReplyDispatcherParams) {
  const core = getFeishuRuntime();
  const { cfg, agentId, chatId, replyToMessageId, mentionTargets, accountId } = params;
  const account = resolveFeishuAccount({ cfg, accountId });
  const prefixContext = createReplyPrefixContext({ cfg, agentId });

  let typingState: TypingIndicatorState | null = null;
  const typingCallbacks = createTypingCallbacks({
    start: async () => {
      if (!replyToMessageId) {
        return;
      }
      typingState = await addTypingIndicator({ cfg, messageId: replyToMessageId, accountId });
    },
    stop: async () => {
      if (!typingState) {
        return;
      }
      await removeTypingIndicator({ cfg, state: typingState, accountId });
      typingState = null;
    },
    onStartError: (err) =>
      logTypingFailure({
        log: (message) => params.runtime.log?.(message),
        channel: "feishu",
        action: "start",
        error: err,
      }),
    onStopError: (err) =>
      logTypingFailure({
        log: (message) => params.runtime.log?.(message),
        channel: "feishu",
        action: "stop",
        error: err,
      }),
  });

  const textChunkLimit = core.channel.text.resolveTextChunkLimit(cfg, "feishu", accountId, {
    fallbackLimit: 4000,
  });
  const chunkMode = core.channel.text.resolveChunkMode(cfg, "feishu");
  const tableMode = core.channel.text.resolveMarkdownTableMode({ cfg, channel: "feishu" });

  const { dispatcher, replyOptions, markDispatchIdle } =
    core.channel.reply.createReplyDispatcherWithTyping({
      responsePrefix: prefixContext.responsePrefix,
      responsePrefixContextProvider: prefixContext.responsePrefixContextProvider,
      humanDelay: core.channel.reply.resolveHumanDelayConfig(cfg, agentId),
      onReplyStart: () => {
        void typingCallbacks.onReplyStart?.();
      },
      deliver: async (payload: ReplyPayload, info) => {
        if (info?.kind !== "final") {
          return;
        }
        const text = payload.text ?? "";
        if (!text.trim()) {
          return;
        }
        try {
          await sendFinalText({
            cfg,
            accountId,
            chatId,
            text,
            replyToMessageId,
            mentions: mentionTargets,
            textChunkLimit,
            chunkMode,
            tableMode,
            core,
          });
        } catch (error) {
          params.runtime.error?.(
            `feishu[${account.accountId}] final reply failed, queued for consistency delivery: ${String(error)}`,
          );
          queueFinalTextWithConsistency({
            cfg,
            core,
            accountKey: account.accountId,
            accountId,
            chatId,
            text,
            replyToMessageId,
            mentions: mentionTargets,
            textChunkLimit,
            chunkMode,
            tableMode,
            log: params.runtime.log,
            error: params.runtime.error,
          });
        }
      },
      onError: async (error, info) => {
        params.runtime.error?.(
          `feishu[${account.accountId}] ${info.kind} reply failed: ${String(error)}`,
        );
        typingCallbacks.onIdle?.();
      },
      onIdle: async () => {
        typingCallbacks.onIdle?.();
      },
      onCleanup: () => {
        typingCallbacks.onCleanup?.();
      },
    });

  return {
    dispatcher,
    replyOptions: {
      ...replyOptions,
      onModelSelected: prefixContext.onModelSelected,
      onPartialReply: undefined,
    },
    markDispatchIdle,
  };
}
