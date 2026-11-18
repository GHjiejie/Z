import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  // 保留编码器相对模块路径加载 .wasm 的方式。编码器在独立的 ES 模块 Worker 中运行。
  optimizeDeps: { exclude: ['@jsquash/webp'] },
  worker: { format: 'es' },
});
