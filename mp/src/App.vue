<script setup>
import { onLaunch, onShow, onHide } from '@dcloudio/uni-app'
import { loadStore, initTheme } from './store'

onLaunch(() => {
  // 读取本地缓存的登录态/门店/日期/主题
  loadStore()
  initTheme()
})

onShow(() => {})
onHide(() => {})
</script>

<style>
/* 全局兜底背景：未显式指定主题时跟随系统 */
page {
  background-color: #f5f7fa;
}

@media (prefers-color-scheme: dark) {
  page {
    background-color: #12151a;
  }
}
</style>
