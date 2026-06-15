import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    // `@/…` → `src/…` so feature/shared imports don't need long relative paths.
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
})
