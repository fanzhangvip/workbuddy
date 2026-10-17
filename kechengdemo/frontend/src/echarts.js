/**
 * echarts 的**按需引入**（全项目唯一的注册点）。
 *
 * ## 为什么单独一个文件而不是各页面自己 import
 *
 * `echarts/core` 必须显式 `use(...)` 注册用到的图表与组件，**漏注册的表现是"图是空白的"
 * 或运行时报 "Component xxx not exists"** —— 那是最难查的一类前端问题。把"本项目到底
 * 用了 echarts 的哪几块"收在一个文件里，好处有三：
 *
 *   1. 新增图表只改这里一处（与后端"同一件事只有一个落点"同一条纪律）；
 *   2. 打包体积可控：`import 'echarts'` 全量约 1MB，按需只装树图/条形/关系图 + 提示框
 *      + 网格 + Canvas 渲染器；
 *   3. 版本升级时"要重新核对哪些注册项"有确切清单。
 *
 * ## 本项目用到什么
 *
 *   · `TreemapChart`  —— 兴趣点分布图的**树图**（squarified，面积 ∝ 客户数）
 *   · `BarChart`      —— 一级分类的**横向条形**（长度才是能精确读的通道）
 *   · `GraphChart`    —— 二级标签的**气泡云**（force 布局，半径 ∝ √客户数）
 *   · `TooltipComponent` / `GridComponent` —— 提示框（悬浮看提及次数）与直角坐标系
 *   · `CanvasRenderer` —— 渲染器（默认且体积最小）
 *
 * ⚠️ 改这个文件 = 改前端依赖面，请同步 `客户意向评分_架构设计说明书.md` 第十四章。
 */
import * as echarts from 'echarts/core'
import { BarChart, GraphChart, TreemapChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { LabelLayout } from 'echarts/features'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  TreemapChart, BarChart, GraphChart,
  TooltipComponent, GridComponent,
  LabelLayout,
  CanvasRenderer,
])

export default echarts
