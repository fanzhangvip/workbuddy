<script setup>
/**
 * 「部分数据加载失败」横幅 —— 配合 `composables/loadFailures.js` 使用。
 *
 * 为什么要有它：页面上十来个次要数据源原来都是静默 `catch`，接口一失败就只剩空白，
 * 看起来像"这里本来就没有数据"。这条横幅把"失败"和"没有数据"分成两件事说清楚，
 * 并给一个重试入口。
 *
 * 与 `useAsyncAction` 的 `error` 分工：
 *   · **主数据**失败 → 用 `error`（红色提示，因为它挡住主流程）
 *   · **次要数据**失败 → 用本横幅（黄色，页面其余部分仍可用）
 */
defineProps({
  /** `useLoadFailures()` 的 `failures`：`[{key, label, message}]` */
  failures: { type: Array, default: () => [] },
  retryLabel: { type: String, default: '重试' },
})
const emit = defineEmits(['retry'])
</script>

<template>
  <div v-if="failures.length" class="alert warn" style="font-size:12px;margin-bottom:12px">
    ⚠️ 部分数据加载失败（页面其余部分仍可用）：
    <span v-for="f in failures" :key="f.key" style="margin-left:8px;white-space:nowrap">
      {{ f.label }}<span class="muted">（{{ f.message }}）</span>
    </span>
    <button class="btn sm" style="margin-left:8px" @click="emit('retry')">{{ retryLabel }}</button>
  </div>
</template>
