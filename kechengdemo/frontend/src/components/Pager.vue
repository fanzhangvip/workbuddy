<script setup>
/**
 * 列表分页器 —— **三处列表页共用一个组件**（评估集 / 负分复核 / 评分中心）。
 *
 * 为什么（`AGENTS.md` 的 Reuse Before Rewrite）：改造前这三处各自写着
 * 「上一页 / N / M / 下一页」的同一段标记，连 `Math.ceil(total / page_size)`
 * 这种边界表达式都逐字相同，样式块也在三个 `<style scoped>` 里各一份。
 * 于是「分页器换个样式」要动三个文件，「页码算错」要修三遍。
 *
 * 用法::
 *
 *   <Pager :page="filters.page" :page-size="filters.page_size"
 *          :total="list.total" @go="goto" />
 *
 * 样式走全局 `.pager`（`styles.css` 的跨视图原语一节），组件内不再自带 scoped 样式。
 */
defineProps({
  page: { type: Number, default: 1 },
  /** 每页条数（后端分页参数） */
  pageSize: { type: Number, default: 20 },
  /** 总条数（后端返回的 total） */
  total: { type: Number, default: 0 },
  /** 是否额外显示「共 N 条」（宽表格用） */
  showTotal: { type: Boolean, default: false },
})

const emit = defineEmits(['go'])
</script>

<template>
  <!-- 只有一页时不显示（与改造前三处的行为一致） -->
  <div v-if="total > pageSize" class="pager">
    <button class="btn sm" :disabled="page <= 1" @click="emit('go', page - 1)">上一页</button>
    <span class="muted" style="font-size:12px">{{ page }} / {{ Math.ceil(total / pageSize)
      }}<template v-if="showTotal">（共 {{ total }}）</template></span>
    <button class="btn sm" :disabled="page >= Math.ceil(total / pageSize)"
            @click="emit('go', page + 1)">下一页</button>
  </div>
</template>
