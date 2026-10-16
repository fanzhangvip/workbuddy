import { onMounted, onUnmounted } from 'vue'

/**
 * 定时轮询（列表 / 状态自动刷新）。
 *
 * **为什么抽出来**：改造前 6 个页面各写一遍
 * `let timer = null` + `onMounted(setInterval)` + `onUnmounted(clearInterval)`，
 * 于是「组件卸载后定时器还在跑」这类问题要在 6 个文件里逐个排查。
 *
 * **间隔故意不统一**，这是各页面的业务选择，用参数表达：
 *
 *   App 15s（凭据状态看一眼就够）· Sync 2.5s（同步在跑，日志要看得快）
 *   Score/Auth 3s · Review 5s · Eval 8s
 *
 * 别抹平成同一个值 —— 那会把「同步页变卡」或「凭据状态变迟钝」二选一。
 *
 * 用法（在 `setup` 中调用，组件卸载时自动清理）::
 *
 *   usePolling(refresh, 5000)                  // 挂载即刷一次，之后每 5 秒
 *   usePolling(refresh, 5000, { immediate: false })
 *
 * 返回一个 `stop()`，用于「停掉轮询」（如进入编辑态时不再自动覆盖表单）。
 */
export function usePolling(fn, intervalMs, { immediate = true } = {}) {
  let timer = null
  const stop = () => {
    if (timer !== null) { clearInterval(timer); timer = null }
  }
  onMounted(() => {
    stop()                       // 防御：热重载下 onMounted 可能重入
    if (immediate) fn()
    timer = setInterval(fn, intervalMs)
  })
  onUnmounted(stop)
  return stop
}
