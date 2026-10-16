import { ref, computed } from 'vue'

/**
 * 「局部加载失败」的统一收口。
 *
 * ## 为什么需要它
 *
 * 页面里有一大批**次要数据源**：门禁状态、保留期、等级标签、员工列表、复核选项、
 * meta-loop 报告…… 改造前它们的 `catch (e)` 里只写一句"忽略"注释，
 * 于是接口一失败（401 会话过期 / 后端未就绪 / 网络抖动），页面**只剩一块空白**，
 * 用户看到的是"这里没有数据"，而真实原因是**请求失败了**。
 * 实测三次排查（会话管理、评分中心、评估集）都出在这个模式上 —— 它把"错误"伪装成了"空数据"。
 *
 * ## 用法
 *
 *   const { failures, note, clear } = useLoadFailures()
 *   try { gate.value = await api.scoringRulesGate() }
 *   catch (e) { note('gate', '门禁状态', e) }      // 而不是只写一句"忽略"注释
 *
 * 模板里放一个 `<PartialLoadBanner :failures="failures" @retry="refresh" />`。
 * **主数据**（页面主体列表）仍然走 `useAsyncAction` 的 `error` ——
 * 它该"挡住视线"，不该降级成一条小横幅。
 */
export function useLoadFailures() {
  /** @type {import('vue').Ref<Array<{key: string, label: string, message: string}>>} */
  const failures = ref([])

  /** 记一条失败（同一 key 反复失败只保留最新那条，不会越堆越长）。 */
  function note(key, label, e) {
    const status = e && e.response && e.response.status
    const message = status ? `HTTP ${status}` : ((e && e.message) || String(e))
    const rec = { key, label, message }
    const i = failures.value.findIndex(f => f.key === key)
    if (i >= 0) failures.value.splice(i, 1, rec)
    else failures.value.push(rec)
  }

  /** 某个数据源这次成功了 → 把它的失败记录撤掉（避免"已经好了还挂着横幅"）。 */
  function clear(key) {
    if (failures.value.some(f => f.key === key)) {
      failures.value = failures.value.filter(f => f.key !== key)
    }
  }

  function clearAll() { failures.value = [] }

  /** 横幅上那行"哪些没加载出来"。 */
  const labels = computed(() => failures.value.map(f => f.label).join('、'))

  return { failures, note, clear, clearAll, labels }
}
