<script setup>
/**
 * 聊天气泡 —— **全项目唯一的聊天记录渲染实现**。
 *
 * 为什么必须是一个组件（`AGENTS.md` 的 Reuse Before Rewrite 规则）：
 *   改造前这段渲染被抄了三份（ChatView / EvalView+ReviewView / ScoreView 的证据行），
 *   于是「多行消息被拆散」「左右方向判错」「引用回复被误读」「表情图片不显示」
 *   这一串**同一根因**的 bug，必须在 5 个地方各修一遍。
 *
 * 输入二选一（优先结构化）：
 *   · `messages` —— 接口返回的结构化消息，带 `media_url` / `is_media` / `role`。
 *     媒体能真正渲染出来靠的就是它（文本形态把 URL 剥掉了，见后端
 *     `scoring_service.build_display_messages` 的说明）。
 *   · `text` —— 纯文本回退（后端没给 messages 时），内部走 `parseConversation`。
 */
import { computed, ref } from 'vue'

import {
  fmtRelative, fmtTime, isInlineImage, mediaCard, mediaText,
  parseConversation, toBubbles,
} from '../api'

const props = defineProps({
  /** 结构化消息（优先） */
  messages: { type: Array, default: () => [] },
  /** 纯文本聊天记录（回退） */
  text: { type: String, default: '' },
  /** 紧凑模式：字更小、上限更窄 —— 给证据行 / 侧栏用 */
  compact: { type: Boolean, default: false },
  /** 空数据时的占位文案（留空则不显示） */
  empty: { type: String, default: '' },
})

// 媒体加载失败的消息（企微 CDN 链接有**时效**，过期就降级成占位，不显示破图）
const failed = ref({})
function mediaFail(m) {
  if (m && m.message_id) failed.value[m.message_id] = true
}

const bubbles = computed(() => {
  const msgs = props.messages
  if (Array.isArray(msgs) && msgs.length) {
    return toBubbles(msgs).map((b) => ({
      ...b,
      time: b.msg_time ? `${fmtTime(b.msg_time)}｜${fmtRelative(b.msg_time)}` : '',
    }))
  }
  return parseConversation(props.text || '')
})
</script>

<template>
  <div class="chat-bubbles" :class="{ compact }">
    <div v-for="(l, k) in bubbles" :key="l.message_id || k" class="bubble" :class="l.side">
      <span class="who">
        <em v-if="l.code" class="tag">{{ l.code }}</em>{{ l.role || l.who }}<em
          v-if="l.time" class="tag">{{ l.time }}</em>
      </span>
      <!-- 引用回复：被引用的原话在上、自己的话在下。
           企微用 `"原话"` + `------` + 答复 表达；不拆开读会误以为归属错了。 -->
      <div v-if="l.quote" class="quote">{{ l.quote }}</div>
      <!-- 媒体：图片/表情出图；语音/视频/文件给可点卡片（分类规则见 api.js 的 mediaKindOf） -->
      <template v-if="l.is_media && l.media_url && !failed[l.message_id]">
        <img v-if="isInlineImage(l)" class="b-img" :src="l.media_url"
             :alt="l.type_name" loading="lazy" @error="mediaFail(l)" />
        <a v-else class="b-file" :href="l.media_url" target="_blank" rel="noreferrer">
          {{ mediaCard(l).icon }} {{ mediaCard(l).name }}
          <span class="muted">{{ mediaCard(l).action }}</span>
        </a>
      </template>
      <div v-else-if="l.is_media" class="bd minor">
        {{ l.text || mediaText(l, false) }}
      </div>
      <div v-else class="bd">{{ l.text }}</div>
    </div>
    <div v-if="!bubbles.length && empty" class="muted" style="font-size:12px">{{ empty }}</div>
  </div>
</template>

<style scoped>
.chat-bubbles { display: flex; flex-direction: column; gap: 8px; }

.bubble { font-size: 12.5px; line-height: 1.6; max-width: 84%;
          padding: 7px 10px; border-radius: 10px;
          background: var(--panel-2); border: 1px solid var(--border);
          align-self: flex-start; }
/* 员工（销售）靠右、客户靠左 —— 与聊天软件一致 */
.bubble.right { margin-left: auto; background: var(--brand-weak); border-color: #d3e1ff; }

.bubble .who { display: block; font-size: 10.5px; color: var(--text-3); margin-bottom: 2px; }
.bubble.right .who { text-align: right; }
.bubble .who .tag { font-style: normal; font-size: 10px; color: var(--text-3);
                    background: var(--bg-2); border: 1px solid var(--border);
                    border-radius: 4px; padding: 0 4px; margin: 0 4px 0 0; }
.bubble.right .who .tag { margin: 0 0 0 4px; }

/* 引用块：被引用的对方原话 —— 视觉上与本人发言拉开，避免「归属看错」 */
.bubble .quote { border-left: 3px solid var(--border); padding: 3px 0 3px 8px;
                 margin: 4px 0 5px; color: var(--text-3); font-size: 12px;
                 background: rgba(0, 0, 0, .03); border-radius: 0 5px 5px 0;
                 white-space: pre-wrap; }
.bubble.right .quote { border-left: none; border-right: 3px solid var(--border);
                       padding: 3px 8px 3px 0; border-radius: 5px 0 0 5px; }

/* 正文保留换行：40.7% 的消息正文自带换行（实测），折叠后会糊成一整段 */
.bubble .bd { white-space: pre-wrap; }
.bubble .bd.minor { font-size: 12px; color: var(--text-3); }

.bubble .b-img { display: block; max-width: 220px; max-height: 220px; border-radius: 8px;
                 border: 1px solid var(--border); background: var(--bg-2); margin-top: 2px; }
.bubble .b-file { display: inline-block; margin-top: 2px; padding: 6px 10px; border-radius: 7px;
                  background: rgba(0, 0, 0, .05); font-size: 12.5px; text-decoration: none; }

/* 紧凑模式：给证据行 / 侧栏 —— 同结构，只是更小更窄 */
.compact { gap: 5px; }
.compact .bubble { font-size: 12px; max-width: 86%; padding: 5px 9px; }
.compact .bubble .b-img { max-width: 160px; max-height: 160px; }
</style>
