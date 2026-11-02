<template>
  <view class="page" :class="'theme-' + state.theme">
    <page-header />

    <!-- 当日预测总览 -->
    <view class="card">
      <view class="card-title">
        <text>预测总览 · {{ state.currentDate }}</text>
        <text class="sub" @click="refresh">刷新</text>
      </view>

      <view v-if="loading" class="empty"><text class="e-icon">⏳</text>加载中…</view>

      <block v-else-if="overview">
        <view class="metrics">
          <view class="metric">
            <view class="m-label">预估营业额</view>
            <view class="m-value">{{ money(overview.revenue) }}</view>
          </view>
          <view class="metric">
            <view class="m-label">食材总成本</view>
            <view class="m-value">{{ money(overview.cost) }}</view>
          </view>
          <view class="metric">
            <view class="m-label">毛利率</view>
            <view class="m-value">{{ pct(overview.gross_margin) }}</view>
          </view>
          <view class="metric">
            <view class="m-label">风险等级</view>
            <view class="m-value" :class="riskClass">{{ overview.risk }}</view>
          </view>
        </view>

        <view class="hint">
          共 {{ overview.dishCount }} 个菜品 · 数据来源 <text class="mono">GET /api/prediction</text>
        </view>

        <view class="btn btn-run" :class="{ 'btn-disabled': running }" @click="runPrediction">
          {{ running ? '预测生成中…' : (overview.exists ? '重新生成预测' : '执行预测') }}
        </view>
        <view v-if="running" class="run-progress">进度：{{ progress || '准备中…' }}</view>
      </block>

      <view v-else class="empty">
        <text class="e-icon">📭</text>
        {{ errMsg || '该日暂无预测结果' }}
        <view class="btn btn-run" :class="{ 'btn-disabled': running }" @click="runPrediction">
          {{ running ? '预测生成中…' : '执行预测' }}
        </view>
        <view v-if="running" class="run-progress">进度：{{ progress || '准备中…' }}</view>
      </view>
    </view>

    <!-- 外观 -->
    <view class="card">
      <view class="card-title"><text>外观</text></view>
      <view class="row">
        <text class="label">主题</text>
        <text class="value">{{ state.theme === 'dark' ? '深色' : '浅色' }}</text>
      </view>
      <view class="btn btn-sub" @click="toggleTheme">
        切换为{{ state.theme === 'dark' ? '浅色' : '深色' }}
      </view>
    </view>

    <!-- 开发进度 -->
    <view class="card">
      <view class="card-title"><text>开发进度</text></view>
      <view class="row"><text class="label">阶段一 · 后端完整化 + 登录鉴权</text><text class="value tag tag-success">已完成</text></view>
      <view class="row"><text class="label">阶段二 · 前端骨架 + 登录 + 主题</text><text class="value tag tag-success">已完成</text></view>
      <view class="row"><text class="label">阶段三 · 预测总览 + 两维度展示</text><text class="value tag tag-success">已完成</text></view>
          <view class="row"><text class="label">阶段四 · 手工预测 + 销量录入 + 对比</text><text class="value tag tag-success">已完成</text></view>
          <view class="row"><text class="label">阶段五 · 联调 + 缺漏补齐 + 微信小程序构建</text><text class="value tag tag-success">已完成</text></view>
    </view>

    <view class="btn btn-ghost" @click="logout">退出登录</view>
  </view>
</template>

<script setup>
import { ref, watch } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { state, toggleTheme, clearAuth, applyTabBar, setPredictionDates } from '../../store'
import { api } from '../../api'
import { poll } from '../../api/request'
import { useAuthGuard, errText } from '../../use/auth'
import pageHeader from '../../components/page-header.vue'
import { money, pct, riskTag } from '../../utils/format'

useAuthGuard()

const loading = ref(false)
const overview = ref(null) // { exists, revenue, cost, gross_margin, risk, dishCount }
const errMsg = ref('')
const running = ref(false)
const progress = ref('')

const riskClass = ref('tag-success')
function applyRiskClass(r) {
  riskClass.value = riskTag(r)
}

async function loadOverview() {
  if (state.currentStoreId == null) {
    overview.value = null
    errMsg.value = '请先选择门店'
    return
  }
  loading.value = true
  errMsg.value = ''
  try {
    const res = await api.prediction(state.currentStoreId, state.currentDate)
    overview.value = {
      exists: true,
      revenue: res.revenue,
      cost: res.cost,
      gross_margin: res.gross_margin,
      risk: (res.result && res.result.risk_level) || '低',
      dishCount: (res.result && res.result.dish_predictions ? Object.keys(res.result.dish_predictions).length : 0),
    }
    applyRiskClass(overview.value.risk)
  } catch (e) {
    if (e && e.code === 404) {
      overview.value = null
      errMsg.value = '该日暂无预测结果，点击下方按钮生成'
    } else {
      overview.value = null
      errMsg.value = errText(e, '加载失败')
    }
  } finally {
    loading.value = false
  }
}

async function runPrediction() {
  if (running.value) return
  if (state.currentStoreId == null) {
    uni.showToast({ title: '请先选择门店', icon: 'none' })
    return
  }
  running.value = true
  progress.value = '提交中…'
  try {
    const r = await api.runPredict(state.currentStoreId, state.currentDate, '')
    await poll(async () => {
      const st = await api.predictStatus(r.task_id)
      if (st && st.status === 'running') {
        progress.value = st.progress || '预测中…'
        return false
      }
      if (st && st.status === 'done') {
        progress.value = '完成'
        return true
      }
      if (st && st.status === 'error') {
        throw new Error(st.error || '预测失败')
      }
      return false
    }, 1500, 160)
    // 刷新：预测日期列表 + 总览
    try {
      const dates = await api.predictionDates(state.currentStoreId)
      setPredictionDates((dates && dates.dates) || [])
    } catch (e) {}
    await loadOverview()
    uni.showToast({ title: '预测已生成', icon: 'success' })
  } catch (e) {
    uni.showToast({ title: errText(e, '预测失败'), icon: 'none', duration: 2600 })
  } finally {
    running.value = false
    progress.value = ''
  }
}

function refresh() {
  loadOverview()
}

watch(() => [state.currentStoreId, state.currentDate], () => loadOverview())

onShow(() => {
  applyTabBar(state.theme)
  loadOverview()
})

function logout() {
  uni.showModal({
    title: '退出登录',
    content: '确定要退出吗？',
    success: (r) => {
      if (r.confirm) {
        clearAuth()
        uni.reLaunch({ url: '/pages/login/login' })
      }
    },
  })
}
</script>

<style scoped>
.metric .m-value.tag-danger,
.metric .m-value.tag-warn,
.metric .m-value.tag-success {
  font-size: 34rpx;
}
.btn-run {
  margin-top: 24rpx;
  background: var(--primary);
  color: #fff;
}
.run-progress {
  margin-top: 14rpx;
  font-size: 24rpx;
  color: var(--text-sub);
  text-align: center;
}
.empty .btn-run {
  display: inline-block;
  margin-top: 24rpx;
  padding: 0 48rpx;
}
</style>
