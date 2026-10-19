<script setup>
import { onMounted, onUnmounted } from 'vue';
import { installImageViewer } from '../../../plugins/docs/image-viewer.js';
let dispose;
onMounted(() => { const content = document.querySelector('.VPContent'); if (content) dispose = installImageViewer(content); });
onUnmounted(() => dispose?.());
</script>
<template></template>
