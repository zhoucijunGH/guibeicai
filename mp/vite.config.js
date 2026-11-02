import { defineConfig } from 'vite'
import uni from '@dcloudio/vite-plugin-uni'

// 桂备菜小程序端（uni-app + Vue3）
// 后端 FastAPI 默认跑在 http://127.0.0.1:8000，已开启 CORS(allow_origins=*)，
// 故前端直接用绝对地址请求，无需 vite 代理（H5 与小程序行为一致）。
export default defineConfig({
  plugins: [uni()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: false,
  },
})
