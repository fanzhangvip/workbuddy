import { ref } from 'vue'

/**
 * 一次「异步动作」的三件套：`busy` / `error` / `msg`。
 *
 * **为什么要有它**：改造前 4 个视图手写 `busy.value = true … busy.value = false`
 * 共 **45 处**、`catch (e) { error.value = e.message }` 共 **35 处**
 * （用 `.workbuddy/tmp/clone_scan_fe.py` 可复现）。于是「按钮转圈怎么显示」
 * 「错误怎么提示」这类横切关注点要改 4 个文件，漏一处就表现不一致。
 *
 * **语义与手写完全一致**（刻意的，便于逐处替换、不夹带行为变化）：
 *   · `run()` 进入：已在忙 → 直接返回（等价于各处的 `if (busy.value) return`）；
 *     否则置忙、清空 `error`（可选同时清 `msg`）；
 *   · `run()` 退出：**无论成功、异常还是提前 return，都会复位 `busy`**；
 *   · 回调抛异常 → 写进 `error`（可加前缀），**不再向外抛**（与各处手写一致）。
 *
 * 用法::
 *
 *   const { busy, error, msg, run } = useAsyncAction()
 *
 *   async function save() {
 *     await run(async () => {
 *       const res = await api.save(payload)
 *       if (!res.ok) { error.value = res.reason || '保存失败'; return }
 *       msg.value = '已保存'
 *     })
 *   }
 *
 * 需要「保留上一条成功提示」时传 `run(fn, { clearMsg: false })`。
 */
export function useAsyncAction({ errorPrefix = '' } = {}) {
  const busy = ref(false)
  const error = ref('')
  const msg = ref('')

  async function run(fn, { clearMsg = true } = {}) {
    if (busy.value) return undefined
    busy.value = true
    error.value = ''
    if (clearMsg) msg.value = ''
    try {
      return await fn()
    } catch (e) {
      error.value = errorPrefix + ((e && e.message) ? e.message : String(e))
      return undefined
    } finally {
      busy.value = false
    }
  }

  /** 清空提示（各视图的 `reset()` 里那行）。 */
  function reset() {
    error.value = ''
    msg.value = ''
  }

  return { busy, error, msg, run, reset }
}
