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

// ============================================================================
// Reasoning Text Processing Functions
// ============================================================================

const REASONING_MESSAGE_PREFIX = "🧠 思考过程\n";
// 修复：移除 \b 单词边界，使用 lookahead 确保标签名后是 > 或空格
// 这样可以匹配 <think中文内容</think答案 等无空格的情况
const THINKING_TAG_RE = /<\s*(\/?)\s*(?:think(?:ing)?|thought|antthinking)(?=[\s>\/]|$)/gi;
const REASONING_TAG_PREFIXES = ["<think", "<thinking", "<thought", "<antthinking"];

/**
 * Find code regions in text to avoid processing inside code blocks
 */
function findCodeRegions(text: string): Array<{ start: number; end: number }> {
  const regions: Array<{ start: number; end: number }> = [];
  const fenceRegex = /```[\s\S]*?```/g;
  let match;
  while ((match = fenceRegex.exec(text)) !== null) {
    regions.push({ start: match.index, end: match.index + match[0].length });
  }
  return regions;
}

/**
 * Check if position is inside a code region
 */
function isInsideCode(position: number, regions: Array<{ start: number; end: number }>): boolean {
  for (const region of regions) {
    if (position >= region.start && position < region.end) {
      return true;
    }
  }
  return false;
}

/**
 * Extract thinking content from tagged stream, excluding code blocks
 */
function extractThinkingFromTaggedStreamOutsideCode(text: string): string {
  if (!text) return "";
  const codeRegions = findCodeRegions(text);
  let result = "";
  let inThinking = false;
  let lastIndex = 0;

  const regex = new RegExp(THINKING_TAG_RE.source, "gi");
  let match;
  while ((match = regex.exec(text)) !== null) {
    const idx = match.index ?? 0;
    if (isInsideCode(idx, codeRegions)) continue;
    if (inThinking) result += text.slice(lastIndex, idx);
    inThinking = !(match[1] === "/");
    lastIndex = idx + match[0].length;
  }
  if (inThinking) result += text.slice(lastIndex);
  return result.trim();
}

/**
 * Strip reasoning/thinking tags from text
 */
function stripReasoningTagsFromText(
  text: string,
  options?: { mode?: string; trim?: string },
): string {
  if (!text) return "";
  const codeRegions = findCodeRegions(text);
  let result = "";
  let inThinking = false;
  let lastIndex = 0;

  const regex = new RegExp(THINKING_TAG_RE.source, "gi");
  let match;
  while ((match = regex.exec(text)) !== null) {
    const idx = match.index ?? 0;
    if (isInsideCode(idx, codeRegions)) continue;
    if (!inThinking) result += text.slice(lastIndex, idx);
    inThinking = !(match[1] === "/");
    lastIndex = idx + match[0].length;
  }
  if (!inThinking) result += text.slice(lastIndex);

  const trimmed = result.trim();
  return options?.trim === "both" ? trimmed : result;
}

/**
 * Format reasoning message with prefix
 */
function formatReasoningMessage(content: string): string {
  return `${REASONING_MESSAGE_PREFIX}${content}`;
}

/**
 * Check if text is a partial reasoning tag prefix (incomplete streaming)
 */
function isPartialReasoningTagPrefix(text: string): boolean {
  const trimmed = text.trimStart().toLowerCase();
  if (!trimmed.startsWith("<")) return false;
  if (trimmed.includes(">")) return false;
  return REASONING_TAG_PREFIXES.some((prefix) => prefix.startsWith(trimmed));
}

/**
 * Split text into reasoning and answer parts
 * Similar to Telegram's splitTelegramReasoningText function
 */
function splitFeishuReasoningText(text: string): {
  reasoningText?: string;
  answerText?: string;
} {
  if (typeof text !== "string") return {};
  const trimmed = text.trim();

  // Check if text is still being streamed (partial tag)
  if (isPartialReasoningTagPrefix(trimmed)) return {};

  // Check if text starts with reasoning prefix (already formatted)
  if (trimmed.startsWith(REASONING_MESSAGE_PREFIX) && trimmed.length > 11) {
    const contentAfterPrefix = trimmed.slice(REASONING_MESSAGE_PREFIX.length).trim();
    return {
      reasoningText: trimmed,
      answerText: contentAfterPrefix || undefined,
    };
  }

  // Extract reasoning from tags (e.g., <think韵</think韵)
  const taggedReasoning = extractThinkingFromTaggedStreamOutsideCode(text);

  // Strip reasoning tags from answer
  const strippedAnswer = stripReasoningTagsFromText(text, {
    mode: "strict",
    trim: "both",
  });

  // If no reasoning found and text unchanged, return as answer only
  if (!taggedReasoning && strippedAnswer === text) {
    return { answerText: text };
  }

  return {
    reasoningText: taggedReasoning ? formatReasoningMessage(taggedReasoning) : undefined,
    // 如果 strippedAnswer 为空但原始文本不为空，使用原始文本作为 answer（避免消息丢失）
    answerText: strippedAnswer || (trimmed ? trimmed : undefined),
  };
}

// ============================================================================
// Constants - Configurable via cfg.channels.feishu.retry
// ============================================================================

const DEFAULT_RETRY_ATTEMPTS = 4;
const DEFAULT_RETRY_DELAY_MS = 1200;
const DEFAULT_RETRYABLE_ERRORS = [
  "230011", "231003", "500", "502", "503", "ETIMEDOUT", "ECONNRESET",
];

// Get retry config from cfg (with defaults)
function getRetryConfig(cfg: ClawdbotConfig) {
  const feishuConfig = cfg.channels?.feishu as Record<string, unknown> | undefined;
  const retryConfig = feishuConfig?.retry as Record<string, unknown> | undefined;
  return {
    maxAttempts: (retryConfig?.maxRetries as number) ?? DEFAULT_RETRY_ATTEMPTS,
    baseDelayMs: (retryConfig?.retryDelay as number) ?? DEFAULT_RETRY_DELAY_MS,
    retryableErrors: (retryConfig?.retryableErrors as string[]) ?? DEFAULT_RETRYABLE_ERRORS,
  };
}

// Legacy constants for compatibility
const FINAL_TEXT_RETRY_MAX_ATTEMPTS = DEFAULT_RETRY_ATTEMPTS;
const FINAL_TEXT_RETRY_BASE_DELAY_MS = DEFAULT_RETRY_DELAY_MS;
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
  onFailure?: (error: Error) => void;
}) {
  // Get retry config from cfg
  const retryConfig = getRetryConfig(params.cfg);
  const queueKey = `${params.accountKey}:${params.chatId}`;
  const previous = finalTextDeliveryQueues.get(queueKey) ?? Promise.resolve();
  let current: Promise<void>;
  current = previous
    .catch(() => {})
    .then(async () => {
      let replyToId = params.replyToMessageId;
      for (let attempt = 1; attempt <= retryConfig.maxAttempts; attempt++) {
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
          if (attempt >= retryConfig.maxAttempts) {
            const errorMsg = `feishu[${params.accountKey}] final consistency delivery failed after ${attempt} attempts: ${String(error)}`;
            params.error?.(errorMsg);
            // 修复：重试失败后调用 onFailure 回调，通知上层发送失败
            params.onFailure?.(error instanceof Error ? error : new Error(String(error)));
            return;
          }
          await sleep(retryConfig.baseDelayMs * attempt);
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

        // Split text into reasoning and answer parts
        const { reasoningText, answerText } = splitFeishuReasoningText(text);

        // Send reasoning first if present
        if (reasoningText?.trim()) {
          try {
            const convertedReasoning = core.channel.text.convertMarkdownTables(
              reasoningText,
              tableMode,
            );
            for (const chunk of core.channel.text.chunkTextWithMode(
              convertedReasoning,
              textChunkLimit,
              chunkMode,
            )) {
              await sendMessageFeishu({
                cfg,
                to: chatId,
                text: chunk,
                replyToMessageId,
                mentions: mentionTargets, // Include mentions in reasoning message
                accountId,
              });
              break; // Only attach mentions to first chunk
            }
            params.runtime.log?.(
              `feishu[${account.accountId}] reasoning delivered (${reasoningText.length} chars)`,
            );
          } catch (error) {
            params.runtime.error?.(
              `feishu[${account.accountId}] reasoning delivery failed: ${String(error)}`,
            );
          }
        }

        // Send answer after reasoning
        if (answerText?.trim()) {
          try {
            await sendFinalText({
              cfg,
              accountId,
              chatId,
              text: answerText,
              replyToMessageId,
              mentions: mentionTargets,
              textChunkLimit,
              chunkMode,
              tableMode,
              core,
            });
            params.runtime.log?.(
              `feishu[${account.accountId}] answer delivered (${answerText.length} chars)`,
            );
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
              text: answerText,
              replyToMessageId,
              mentions: mentionTargets,
              textChunkLimit,
              chunkMode,
              tableMode,
              log: params.runtime.log,
              error: params.runtime.error,
              // 修复：添加失败回调，在重试全部失败后发送明确的错误通知
              onFailure: async (err) => {
                params.runtime.error?.(
                  `feishu[${account.accountId}] final consistency delivery failed permanently: ${err.message}`,
                );
                // 尝试发送一条简短的错误通知给用户
                try {
                  await sendMessageFeishu({
                    cfg,
                    to: chatId,
                    text: `⚠️ 消息发送失败：回复内容较长或网络不稳定，请稍后重试或简化请求。`,
                    accountId,
                  });
                } catch {
                  // 如果连错误通知都发送失败，只能记录日志
                  params.runtime.error?.(
                    `feishu[${account.accountId}] failed to send error notification to user`,
                  );
                }
              },
            });
          }
        } else if (reasoningText?.trim()) {
          // Only reasoning was sent, no answer - this is the bug case!
          params.runtime.log?.(
            `feishu[${account.accountId}] WARNING: only reasoning delivered, no answer text found`,
          );
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
