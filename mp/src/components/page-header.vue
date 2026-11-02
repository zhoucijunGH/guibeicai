<template>
  <view class="ph">
    <store-picker />
    <date-bar v-if="showDate" />
  </view>
</template>

<script setup>
import storePicker from './store-picker.vue'
import dateBar from './date-bar.vue'

defineProps({
  showDate: { type: Boolean, default: true },
})
</script>

<style scoped>
.ph {
  margin-bottom: 24rpx;
}

.ph > view:first-child {
  margin-bottom: 16rpx;
}
</style>
