import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // Exposes the server on the local network automatically
    port: 5173,
    fs: { strict: false }
  },
  optimizeDeps: {
    include: ['@patternfly/react-core', '@patternfly/react-table', '@patternfly/react-icons']
  }
})

