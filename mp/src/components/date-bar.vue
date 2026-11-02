<template>
  <view class="db">
    <view class="db-main">
      <view class="db-nav" @click="shift(-1)">‹</view>
      <picker mode="date" :value="state.currentDate" @change="onDateChange">
        <view class="db-date">{{ state.currentDate }}</view>
      </picker>
      <view class="db-nav" @click="shift(1)">›</view>
      <view class="db-today" @click="goToday">今天</view>
    </view>

    <picker
      v-if="state.predictionDates.length"
      mode="selector"
      :range="state.predictionDates"
      :value="pdIndex"
      @change="onPickExisting"
    >
      <view class="db-exist">
        已有预测 {{ state.predictionDates.length }} 天 · 快速跳转 ▾
      </view>
    </picker>
    <view v-else class="db-exist muted">该门店暂无预测记录</view>
  </view>
</template>

<script setup>
import { computed, watch } from 'vue'
import { state, setDate, setPredictionDates, shiftDate, today } from '../store'
import { api } from '../api'

const pdIndex = computed(() => {
  const i = state.predictionDates.indexOf(state.currentDate)
  return i >= 0 ? i : 0
})

async function loadDates() {
  if (state.currentStoreId == null) {
    setPredictionDates([])
    return
  }
  try {
    const res = await api.predictionDates(state.currentStoreId)
    setPredictionDates((res && res.dates) || [])
  } catch (e) {
    setPredictionDates([])
  }
}

watch(() => state.currentStoreId, loadDates, { immediate: true })

function shift(days) {
  setDate(shiftDate(state.currentDate, days))
}

function goToday() {
  setDate(today())
}

function onDateChange(e) {
  setDate(e.detail.value)
}

function onPickExisting(e) {
  const d = state.predictionDates[Number(e.detail.value)]
  if (d) setDate(d)
}
</script>

<style scoped>
.db {
  background: var(--bg-card);
  border: 1rpx solid var(--border);
  border-radius: 12rpx;
  padding: 16rpx 24rpx;
}

.db-main {
  display: flex;
  align-items: center;
}

.db-nav {
  width: 56rpx;
  height: 56rpx;
  line-height: 52rpx;
  text-align: center;
  font-size: 36rpx;
  color: var(--text-sub);
  background: var(--bg-sub);
  border-radius: 8rpx;
}

.db-nav:active {
  opacity: 0.6;
}

.db-date {
  flex: 1;
  text-align: center;
  font-size: 32rpx;
  font-weight: 700;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

.db-today {
  width: 72rpx;
  text-align: center;
  font-size: 23rpx;
  color: var(--primary);
}

.db-exist {
  margin-top: 14rpx;
  text-align: center;
  font-size: 24rpx;
  color: var(--primary);
}

.db-exist.muted {
  color: var(--text-weak);
}
</style>
