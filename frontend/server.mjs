import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { performance } from "node:perf_hooks";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HTTP_STATUS = Object.freeze({ OK: 200, BAD_REQUEST: 400, FORBIDDEN: 403, NOT_FOUND: 404, METHOD_NOT_ALLOWED: 405, INTERNAL_ERROR: 500 });
const LOG_LEVEL = Object.freeze({ INFO: "INFO", ERROR: "ERROR", CRITICAL: "CRITICAL" });
const SEVERITY = Object.freeze({ INFO: 9, ERROR: 17, CRITICAL: 21 });
const EVENT = Object.freeze({ REQUEST: "http_request_completed", STARTED: "server_started", ERROR: "server_error" });
const ROUTE = Object.freeze({ ROOT: "/", CONFIG: "/config.js", ASSETS: "/assets/*", UNMATCHED: "unmatched" });
const EXIT_CODE = Object.freeze({ FAILURE: 1 });
const LIMIT = Object.freeze({ MIN_PORT: 1, MAX_PORT: 65535 });
const ERROR_CODE = Object.freeze({ MISSING: "ENOENT", DENIED: "EACCES" });
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function log(level, event, message, fields = {}) {
  process.stdout.write(JSON.stringify({
    timestamp: new Date().toISOString(), level, severity_number: SEVERITY[level],
    message, msg_template: message, "event.name": event,
    "service.name": "frontend", "service.version": process.env.MEMES_RELEASE_ID ?? "development",
    "deployment.environment.name": process.env.MEMES_DEPLOYMENT_ENVIRONMENT ?? "development",
    ...fields,
  }) + "\n");
}
function configurationError() {
  log(LOG_LEVEL.CRITICAL, EVENT.ERROR, "Некорректная конфигурация frontend.");
  process.exit(EXIT_CODE.FAILURE);
}
function errorFields(error) {
  return { "error.type": error.name, "error.message": error.message, "error.code": error.code, "error.stack": error.stack };
}
const root = fileURLToPath(new URL("./dist", import.meta.url));
const config = { identityUrl: process.env.MEMES_IDENTITY_URL, catalogUrl: process.env.MEMES_CATALOG_URL, searchUrl: process.env.MEMES_SEARCH_URL };
for (const value of Object.values(config)) {
  try {
    if (!value || !["http:", "https:"].includes(new URL(value).protocol)) configurationError();
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    configurationError();
  }
}
const port = Number(process.env.PORT);
if (!Number.isInteger(port) || port < LIMIT.MIN_PORT || port > LIMIT.MAX_PORT) configurationError();
const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
const server = createServer(async (req, res) => {
  const started = performance.now();
  const suppliedId = req.headers["x-request-id"];
  const requestId = typeof suppliedId === "string" && UUID_PATTERN.test(suppliedId) ? suppliedId : randomUUID();
  let route = ROUTE.UNMATCHED;
  let resourcePath;
  res.setHeader("X-Request-ID", requestId);
  res.once("finish", () => log(
    res.statusCode >= HTTP_STATUS.INTERNAL_ERROR ? LOG_LEVEL.ERROR : LOG_LEVEL.INFO,
    EVENT.REQUEST, "HTTP-запрос завершён.",
    { request_id: requestId, method: ["GET", "HEAD"].includes(req.method) ? req.method : "OTHER", route, "url.path": resourcePath, status: res.statusCode, duration_ms: performance.now() - started },
  ));
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "strict-origin-when-cross-origin");
  if (!["GET", "HEAD"].includes(req.method)) { res.writeHead(HTTP_STATUS.METHOD_NOT_ALLOWED); res.end(); return; }
  let pathname;
  try { pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname); }
  catch (error) { if (!(error instanceof URIError || error instanceof TypeError)) throw error; res.writeHead(HTTP_STATUS.BAD_REQUEST); res.end(); return; }
  resourcePath = pathname;
  route = pathname === ROUTE.ROOT ? ROUTE.ROOT : pathname === ROUTE.CONFIG ? ROUTE.CONFIG : pathname.startsWith("/assets/") ? ROUTE.ASSETS : ROUTE.UNMATCHED;
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
    stream.on("error", error => {
      log(LOG_LEVEL.ERROR, EVENT.ERROR, "Не удалось передать статический файл.", { request_id: requestId, "url.path": resourcePath, ...errorFields(error) });
      res.destroy();
    });
    stream.pipe(res);
  } catch (error) {
    if (error.code !== ERROR_CODE.MISSING && error.code !== ERROR_CODE.DENIED) {
      log(LOG_LEVEL.ERROR, EVENT.ERROR, "Не удалось прочитать статический файл.", { request_id: requestId, "url.path": resourcePath, ...errorFields(error) });
      res.writeHead(HTTP_STATUS.INTERNAL_ERROR);
    }
    else res.writeHead(HTTP_STATUS.NOT_FOUND);
    res.end();
  }
});
server.on("error", error => {
  log(LOG_LEVEL.CRITICAL, EVENT.ERROR, "Ошибка HTTP-сервера.", errorFields(error));
  process.exitCode = EXIT_CODE.FAILURE;
});
server.listen(port, "0.0.0.0", () => log(LOG_LEVEL.INFO, EVENT.STARTED, "HTTP-сервер запущен.", { port }));
for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, () => {
  server.close(error => {
    if (error) {
      log(LOG_LEVEL.ERROR, EVENT.ERROR, "Не удалось завершить HTTP-сервер.", errorFields(error));
      process.exitCode = EXIT_CODE.FAILURE;
    }
  });
  server.closeIdleConnections();
});
