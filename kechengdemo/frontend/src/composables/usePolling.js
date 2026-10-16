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
 *   usePolling(refresh, 3000, { idle: 15000, idleWhen: () => !running.value })
 *
 * 返回一个 `stop()`，用于「停掉轮询」（如进入编辑态时不再自动覆盖表单）。
 * `stop()` 是**永久停**且**立即生效**：即使此刻正有一次请求在飞，它回来之后也不会
 * 再排下一拍（这一条靠 `disposed` 标记保证，见下）。
 *
 * ## 2026-10-16 两处加固（所有调用方共同受益）
 *
 * 1. **页面不可见时不打接口**：`document.visibilityState === 'hidden'` 就停，
 *    切回来的那一刻立刻补刷一次。改造前是无条件 `setInterval` —— 实测评分中心
 *    每 3 秒打 6 个接口（其中 `/metrics` 一次约 15 个子查询），页面挂在后台一整天
 *    也照打，一天约 17 万次请求。
 * 2. **`idle` / `idleWhen`：空闲时降频**。`idleWhen()` 返回 true 时用 `idle` 毫秒，
 *    否则用 `intervalMs`（例：有任务在跑 3 秒、空闲 15 秒）。间隔是各页面的业务选择，
 *    所以**不在这里写死**，由调用方通过参数表达。
 * 3. **卸载/`stop()` 之后不会再"复活"**：排下一拍前判 `disposed`。改成 setTimeout 链时
 *    漏了这个判断 —— 卸载瞬间若有一次请求在飞，它 `await` 回来后会把新定时器挂上，
 *    于是"路由已切走、组件已销毁，后台还在按旧间隔打接口"（与第 1 条要解决的浪费同类）。
 */
export function usePolling(fn, intervalMs, { immediate = true, idle = 0, idleWhen = null } = {}) {
  let timer = null
  let visible = typeof document === 'undefined' || document.visibilityState !== 'hidden'
  //: 组件已销毁 —— 排下一拍前必须看它。
  //  ⚠️ 为什么不能只靠 `stop()`：`tick` 是 `await fn()` 之后才排下一拍的，
  //  若卸载/stop 发生在"请求在飞"的瞬间，回来那一句 `setTimeout` 会把**新的**
  //  定时器挂上去 —— 组件没了、轮询却活着：路由已经切走，后台仍按旧间隔打接口
  //  （实测口径：评分中心 3 秒 6 个接口，`/metrics` 一次约 15 个子查询）。
  //  改造前用 `setInterval` 时不存在这个问题（clearInterval 之后没有"排下一拍"的逻辑），
  //  是换成 setTimeout 链时引入的，所以这里必须显式补上。
  let disposed = false

  const nextDelay = () => {
    if (idle && idleWhen) {
      try { if (idleWhen()) return idle } catch (e) { /* 判定失败按主间隔走 */ }
    }
    return intervalMs
  }
  const clearTimer = () => {
    if (timer !== null) { clearTimeout(timer); timer = null }
  }
  /** 停掉轮询（**永久**）：返回给调用方用的那个。 */
  const stop = () => { disposed = true; clearTimer() }
  const tick = async () => {
    timer = null
    if (!visible || disposed) return     // 隐藏时不打接口（切回来会立刻补一次）
    try { await fn() } catch (e) { /* 各页面自己把错误写进 error 提示 */ }
    if (visible && !disposed) timer = setTimeout(tick, nextDelay())
  }
  // 用 setTimeout 链而不是 setInterval：间隔要能随状态变（idle 降频），
  // 且不会出现「上一次还没回来就又发一次」的重叠。
  const onVisibility = () => {
    const was = visible
    visible = document.visibilityState !== 'hidden'
    if (visible && !was && !disposed) { clearTimer(); tick() }
  }
  onMounted(() => {
    disposed = false             // 热重载 / 复用时能重新开始
    clearTimer()                 // 防御：热重载下 onMounted 可能重入
    if (immediate) fn()
    timer = setTimeout(tick, nextDelay())
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility)
    }
  })
  onUnmounted(() => {
    disposed = true
    stop()
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibility)
    }
  })
  return stop
}
