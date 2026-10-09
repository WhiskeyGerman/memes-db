import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const config = {
    identityUrl: env.MEMES_IDENTITY_URL,
    catalogUrl: env.MEMES_CATALOG_URL,
    searchUrl: env.MEMES_SEARCH_URL,
  };
  if (command === "serve" && Object.values(config).some(value => !value)) {
    throw new Error("Создайте .env по образцу .env.example.");
  }
  return {
    resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
    server: { host: "localhost", port: Number(env.PORT), strictPort: true },
    plugins: [{
      name: "runtime-config",
      configureServer(server) {
        server.middlewares.use("/config.js", (_req, res) => {
          res.setHeader("Content-Type", "text/javascript; charset=utf-8");
          res.setHeader("Cache-Control", "no-store");
          res.end("window.__MEMES_CONFIG__=" + JSON.stringify(config));
        });
      },
    }],
  };
});
