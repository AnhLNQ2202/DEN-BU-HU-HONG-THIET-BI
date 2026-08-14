import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const productionOutDir = fileURLToPath(
  new URL("../src/asset_compensation/web/static/dist", import.meta.url),
);

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  return {
    base: "/static/dist/",
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
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
