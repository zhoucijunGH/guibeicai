import { createSSRApp } from 'vue'
import App from './App.vue'
import './theme.css'

// 桂备菜小程序端入口（uni-app + Vue3 + Vite）
export function createApp() {
  const app = createSSRApp(App)
  return { app }
}
