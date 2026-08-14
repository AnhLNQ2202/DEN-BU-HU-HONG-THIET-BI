import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const productionOutDir = fileURLToPath(
  new URL("../src/asset_compensation/web/static/dist", import.meta.url),
);

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  return {
    // Production is served by Flask below /static/dist/. Containers can opt in
    // to a root-based Vite dev server without changing the production build.
    base: env.VITE_BASE || "/static/dist/",
    plugins: [react()],
    server: {
      host: env.VITE_DEV_HOST || "127.0.0.1",
      port: Number(env.VITE_DEV_PORT || 5173),
      strictPort: true,
      proxy: {
        "/api": {
          target: env.VITE_API_PROXY || "http://127.0.0.1:5000",
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: productionOutDir,
      emptyOutDir: true,
      cssCodeSplit: false,
      sourcemap: false,
      rollupOptions: {
        output: {
          entryFileNames: "app.js",
          chunkFileNames: "assets/[name]-[hash].js",
          assetFileNames: (assetInfo) => (
            assetInfo.name?.endsWith(".css") ? "app.css" : "assets/[name]-[hash][extname]"
          ),
        },
      },
    },
  };
});
