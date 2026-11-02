<template>
  <view class="sp">
    <picker
      mode="selector"
      :range="names"
      :value="index"
      :disabled="names.length <= 1"
      @change="onChange"
    >
      <view class="sp-inner">
        <text class="sp-label">门店</text>
        <text class="sp-value">{{ currentName || '暂无门店' }}</text>
        <text v-if="names.length > 1" class="sp-arrow">▾</text>
      </view>
    </picker>
  </view>
</template>

<script setup>
import { computed } from 'vue'
import { state, setStore } from '../store'

const names = computed(() => state.stores.map((s) => s.name || `门店${s.id}`))
const index = computed(() => {
  const i = state.stores.findIndex((s) => s.id === state.currentStoreId)
  return i >= 0 ? i : 0
})
const currentName = computed(() => names.value[index.value] || '')

function onChange(e) {
  const i = Number(e.detail.value)
  const s = state.stores[i]
  if (s) setStore(s.id)
}
</script>

<style scoped>
.sp {
  background: var(--bg-card);
  border: 1rpx solid var(--border);
  border-radius: 12rpx;
}

.sp-inner {
  display: flex;
  align-items: center;
  padding: 20rpx 24rpx;
}

.sp-label {
  font-size: 25rpx;
  color: var(--text-weak);
  margin-right: 16rpx;
}

.sp-value {
  flex: 1;
  font-size: 29rpx;
  font-weight: 600;
  color: var(--text);
}

.sp-arrow {
  font-size: 24rpx;
  color: var(--text-weak);
}
</style>
