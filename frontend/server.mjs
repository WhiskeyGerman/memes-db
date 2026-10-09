import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HTTP_STATUS = Object.freeze({ OK: 200, BAD_REQUEST: 400, FORBIDDEN: 403, NOT_FOUND: 404, METHOD_NOT_ALLOWED: 405, INTERNAL_ERROR: 500 });
const root = fileURLToPath(new URL("./dist", import.meta.url));
const config = { identityUrl: process.env.MEMES_IDENTITY_URL, catalogUrl: process.env.MEMES_CATALOG_URL, searchUrl: process.env.MEMES_SEARCH_URL };
for (const value of Object.values(config)) {
  if (!value || !["http:", "https:"].includes(new URL(value).protocol)) throw new Error("Задайте MEMES_IDENTITY_URL, MEMES_CATALOG_URL и MEMES_SEARCH_URL.");
}
const port = Number(process.env.PORT);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Задайте PORT от 1 до 65535.");
const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
const server = createServer(async (req, res) => {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "strict-origin-when-cross-origin");
  if (!["GET", "HEAD"].includes(req.method)) { res.writeHead(HTTP_STATUS.METHOD_NOT_ALLOWED); res.end(); return; }
  let pathname;
  try { pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname); }
  catch (error) { if (!(error instanceof URIError || error instanceof TypeError)) throw error; res.writeHead(HTTP_STATUS.BAD_REQUEST); res.end(); return; }
  if (pathname === "/config.js") {
    res.writeHead(HTTP_STATUS.OK, { "Content-Type": "text/javascript; charset=utf-8", "Cache-Control": "no-store" });
    res.end(req.method === "HEAD" ? undefined : "window.__MEMES_CONFIG__=" + JSON.stringify(config)); return;
  }
  const path = resolve(root, "." + (pathname === "/" ? "/index.html" : pathname));
  if (!path.startsWith(root + sep)) { res.writeHead(HTTP_STATUS.FORBIDDEN); res.end(); return; }
  try {
    const info = await stat(path);
    if (!info.isFile()) { res.writeHead(HTTP_STATUS.NOT_FOUND); res.end(); return; }
    res.writeHead(HTTP_STATUS.OK, { "Content-Type": (mime[extname(path)] ?? "application/octet-stream") + "; charset=utf-8", "Cache-Control": pathname.startsWith("/assets/") ? "public, max-age=31536000, immutable" : "no-cache" });
    if (req.method === "HEAD") { res.end(); return; }
    const stream = createReadStream(path);
    stream.on("error", error => { console.error(error.code); res.destroy(); });
    stream.pipe(res);
  } catch (error) {
    if (error.code !== "ENOENT" && error.code !== "EACCES") { console.error(error.code); res.writeHead(HTTP_STATUS.INTERNAL_ERROR); }
    else res.writeHead(HTTP_STATUS.NOT_FOUND);
    res.end();
  }
});
server.listen(port, "0.0.0.0", () => console.log(JSON.stringify({ event: "listening", port })));
for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, () => {
  server.close(error => { if (error) { console.error(error.message); process.exitCode = 1; } });
  server.closeIdleConnections();
});
