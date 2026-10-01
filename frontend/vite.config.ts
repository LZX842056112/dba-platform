import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 开发期把 /api 代理到后端（生产由网关统一路由）
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // ECharts 按需注册后主包明显变小（见 src/lib/echarts.ts）
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // 拆 vendor：react 生态与 echarts 各自成包，利于缓存与并行加载
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom', '@tanstack/react-query', 'zustand'],
          echarts: ['echarts'],
        },
      },
    },
  },
});
