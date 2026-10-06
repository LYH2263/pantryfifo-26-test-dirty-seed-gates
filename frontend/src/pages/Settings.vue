<template>
  <div>
    <h1>设置</h1>
    <pre>{{ s }}</pre>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const s = ref('')
onMounted(async () => { s.value = JSON.stringify(await api('/settings'), null, 2) })
</script>
