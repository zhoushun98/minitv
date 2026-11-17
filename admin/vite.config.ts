import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  // admin.sanaoman.asia 由 Nginx 把非 /admin/ 路径兜底到 /admin/index.html，
  // 所以构建产物也必须放在 /admin/ 下。
  // Dev stays at "/" so http://127.0.0.1:5173/dramas works; Vite otherwise
  // rejects any path outside the public base and prints the error the user hit.
  base: command === "build" ? "/admin/" : "/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/admin/v1": "http://127.0.0.1:18110",
      "/partner/v1": "http://127.0.0.1:18110",
      "/v1": "http://127.0.0.1:18110",
    },
  },
}));
