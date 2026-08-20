import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Served from FastAPI at /admin in production (see app/main.py), so the build's own
// asset URLs must carry that prefix too - and in dev, proxying /admin/api keeps the
// browser on one origin instead of needing CORS.
export default defineConfig({
  base: '/admin/',
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/admin/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
