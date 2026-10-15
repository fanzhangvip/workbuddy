import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

const API = 'http://127.0.0.1:8848'

export default defineConfig(({ mode }) => {
  // 后端已启用应用层鉴权（HTTP Basic）。浏览器只在「顶层导航的 401」上弹登录框，
  // XHR 的 401 不会弹 —— 而开发模式下页面在 :5173、接口经代理打到 :8848，
  // 浏览器不会为 :5173/api 自动附带 :8848 的凭据。
  // 故这里（**仅开发服务器**）支持把凭据注入代理请求：
  //     frontend/.env.local  →  VITE_API_AUTH=admin:你的口令
  // 生产由后端直接托管 dist，浏览器原生弹窗即可，不涉及这一段。
  const env = loadEnv(mode, process.cwd(), '')
  const auth = env.VITE_API_AUTH || ''
  const authHeader = auth ? 'Basic ' + Buffer.from(auth).toString('base64') : ''

  return {
    plugins: [vue()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      proxy: {
        '/api': {
          target: API,
          changeOrigin: true,
          configure: (proxy) => {
            if (!authHeader) return
            proxy.on('proxyReq', (proxyReq) => {
              proxyReq.setHeader('Authorization', authHeader)
            })
          },
        },
      },
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
    },
  }
})
