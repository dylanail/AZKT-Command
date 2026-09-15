import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// Dev proxies /api,/auth to the loopback FastAPI. In prod nginx serves the
// static bundle at dash.arizonakeitrucks.com and proxies the API host.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["icons/apple-touch-icon.png", "gen/*.png"],
      manifest: {
        name: "AZKT",
        short_name: "AZKT",
        description: "Arizona Kei Trucks operations",
        // Light canvas is the default theme; dark is a persisted toggle (#07080d) applied at runtime.
        theme_color: "#dfe6f3",
        background_color: "#dfe6f3",
        display: "standalone",
        orientation: "portrait",
        start_url: "/",
        icons: [
          { src: "icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      workbox: {
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api\//, /^\/auth\//],
        runtimeCaching: [
          {
            urlPattern: ({ url }) => url.pathname.startsWith("/api"),
            handler: "NetworkFirst",
            options: { cacheName: "api", networkTimeoutSeconds: 5 },
          },
        ],
      },
    }),
  ],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: true },
      "/auth": { target: "http://127.0.0.1:8787", changeOrigin: true },
    },
  },
});
