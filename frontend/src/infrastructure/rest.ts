import { IBackend, MAX_UPLOAD_BYTES, PAGE_SIZE } from "@/application/backend";
import type { IGroup, IMedia, IPage, IUser, IUploadResult, IVote } from "@/application/backend";

enum Status { NoContent = 204, Unauthorized = 401, NotFound = 404, Conflict = 409 }
enum Timing { RequestMs = 15000, UploadMs = 180000, PollMs = 1000, PollAttempts = 45, RefreshMarginMs = 30000 }
interface IConfig { identityUrl: string; catalogUrl: string; searchUrl: string }
interface IAccess { access_token: string; access_expires_at: string }
interface ISearch { resource_url: string; next_page_url?: string | null; results?: { representative_id: string }[]; degraded?: boolean }

const messages: Record<string, string> = {
  unauthorized: "Неверный логин или пароль. Возможно, сессия завершилась.",
  forbidden: "Для этого действия недостаточно прав.",
  conflict: "Действие уже выполнено или данные изменились. Обновите страницу.",
  stale_state: "Состав группы изменился. Откройте её заново.",
  invalid_input: "Проверьте введённые данные и формат файла.",
  invalid_login: "Логин: 3-32 символа, латинские буквы, цифры, _ и -.",
  invalid_password: "Пароль не соответствует требованиям сервера.",
  not_found: "Медиа уже удалено или ссылка устарела.",
  rate_limited: "Слишком много запросов. Попробуйте чуть позже.",
  unavailable: "Сервис временно недоступен. Попробуйте ещё раз.",
};

class ApiError extends Error {
  constructor(readonly status: number, code: string) { super(messages[code] ?? "Не удалось выполнить действие. Попробуйте ещё раз."); }
}

function sleep(signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(signal?.reason); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, Timing.PollMs);
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });
  });
}

/** REST-адаптер: токены хранятся в памяти, refresh - в HttpOnly cookie. */
export class RestBackend extends IBackend {
  private access: IAccess | null = null;
  private refreshing: Promise<void> | null = null;

  constructor(private readonly config: IConfig) {
    super();
    for (const value of Object.values(config)) {
      if (!["http:", "https:"].includes(new URL(value).protocol)) throw new Error("Некорректный адрес API.");
    }
  }

  private link(base: string, path: string): string {
    const target = new URL(path, base + "/");
    if (target.origin !== new URL(base).origin) throw new Error("Некорректная ссылка API.");
    return target.href;
  }

  private async request<T>(base: string, path: string, init: RequestInit = {}, auth = false, duplicate = false, timeout = Timing.RequestMs): Promise<T> {
    if (auth && this.access && Date.parse(this.access.access_expires_at) - Date.now() < Timing.RefreshMarginMs) await this.refresh();
    const headers = new Headers(init.headers);
    if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
    if (auth && this.access) headers.set("Authorization", "Bearer " + this.access.access_token);
    const signal = AbortSignal.any([AbortSignal.timeout(timeout), ...(init.signal ? [init.signal] : [])]);
    let response = await fetch(this.link(base, path), { ...init, headers, signal });
    if (response.status === Status.Unauthorized && auth && this.access) {
      await this.refresh();
      headers.set("Authorization", "Bearer " + this.access!.access_token);
      response = await fetch(this.link(base, path), { ...init, headers, signal });
    }
    if (response.status === Status.NoContent) return undefined as T;
    const body = await response.json();
    if (!response.ok && !(duplicate && response.status === Status.Conflict && body.duplicate)) {
      throw new ApiError(response.status, body.error?.code ?? body.code ?? "unavailable");
    }
    return body as T;
  }

  private refresh(): Promise<void> {
    if (!this.refreshing) {
      const csrf = document.cookie.split("; ").find(item => item.startsWith("memes_csrf="))?.split("=").slice(1).join("=");
      if (!csrf) { this.access = null; return Promise.reject(new ApiError(Status.Unauthorized, "unauthorized")); }
      this.refreshing = this.request<IAccess>(this.config.identityUrl, "/v1/auth/refresh", {
        method: "POST", credentials: "include", headers: { "X-CSRF-Token": decodeURIComponent(csrf) },
      }).then(tokens => { this.access = tokens; }).catch(error => {
        this.access = null;
        throw error;
      }).finally(() => { this.refreshing = null; });
    }
    return this.refreshing;
  }

  async restore(): Promise<IUser | null> {
    if (!document.cookie.split("; ").some(item => item.startsWith("memes_csrf="))) return null;
    try {
      await this.refresh();
      return await this.request<IUser>(this.config.identityUrl, "/v1/auth/me", {}, true);
    } catch (error) {
      if (error instanceof ApiError && error.status === Status.Unauthorized) return null;
      throw error;
    }
  }

  async signIn(login: string, password: string, register: boolean): Promise<IUser> {
    this.access = await this.request<IAccess>(this.config.identityUrl, "/v1/auth/" + (register ? "register" : "login"), {
      method: "POST", credentials: "include", body: JSON.stringify({ login, password }),
    });
    return this.request<IUser>(this.config.identityUrl, "/v1/auth/me", {}, true);
  }

  async signOut(): Promise<void> {
    await this.request(this.config.identityUrl, "/v1/auth/logout", { method: "POST", credentials: "include" }, true);
    this.access = null;
  }

  async feed(next?: string, signal?: AbortSignal): Promise<IPage> {
    const page = await this.request<{ items: IMedia[]; next_before: string | null; next_before_id: string | null }>(
      this.config.catalogUrl, next ?? "/v1/media?limit=" + PAGE_SIZE, { signal },
    );
    const cursor = page.next_before ? new URLSearchParams({ limit: String(PAGE_SIZE), before: page.next_before, before_id: page.next_before_id! }) : null;
    return { items: page.items, next: cursor ? "/v1/media?" + cursor : null };
  }

  async search(text: string, next?: string, signal?: AbortSignal): Promise<IPage> {
    let page = await this.request<ISearch>(this.config.searchUrl, next ?? "/v1/search", next ? { signal } : {
      method: "POST", body: JSON.stringify({ text, mode: "hybrid", page_size: PAGE_SIZE }), signal,
    });
    for (let attempt = 0; !page.results && attempt < Timing.PollAttempts; attempt++) {
      await sleep(signal);
      page = await this.request<ISearch>(this.config.searchUrl, page.resource_url, { signal });
    }
    if (!page.results) throw new Error("Поиск ещё обрабатывается. Попробуйте повторить запрос чуть позже.");
    const resolved = await Promise.all(page.results.map(async hit => {
      try { return await this.media(hit.representative_id, signal); }
      catch (error) { if (error instanceof ApiError && error.status === Status.NotFound) return null; throw error; }
    }));
    return { items: resolved.filter((item): item is IMedia => item !== null), next: page.next_page_url ?? null, degraded: page.degraded };
  }

  group(id: string, offset = 0, signal?: AbortSignal): Promise<IGroup> {
    return this.request(this.config.catalogUrl, "/v1/groups/" + encodeURIComponent(id) + "?limit=" + PAGE_SIZE + "&offset=" + offset, { signal });
  }
  media(id: string, signal?: AbortSignal): Promise<IMedia> {
    return this.request(this.config.catalogUrl, "/v1/media/" + encodeURIComponent(id), { signal });
  }

  async upload(file: File): Promise<IUploadResult> {
    if (!file.size || file.size > MAX_UPLOAD_BYTES) throw new Error("Выберите непустой файл размером до 100 МиБ.");
    const ticket = await this.request<{ upload_id: string; upload_url: string; fields: Record<string, string> }>(
      this.config.catalogUrl, "/v1/uploads", { method: "POST", body: JSON.stringify({ size_bytes: file.size }) }, true,
    );
    const form = new FormData();
    for (const [key, value] of Object.entries(ticket.fields)) form.append(key, value);
    form.append("file", file);
    const response = await fetch(ticket.upload_url, { method: "POST", body: form, credentials: "omit", signal: AbortSignal.timeout(Timing.UploadMs) });
    if (!response.ok) throw new Error("Хранилище не приняло файл. Попробуйте повторить загрузку.");
    return this.request(this.config.catalogUrl, "/v1/uploads/" + ticket.upload_id + "/complete", { method: "POST" }, true, true, Timing.UploadMs);
  }

  detach(membership: string): Promise<IVote> {
    return this.request(this.config.catalogUrl, "/v1/variations/memberships/" + membership + "/detach", { method: "POST" }, true);
  }
  withdraw(ballot: string): Promise<IVote> {
    return this.request(this.config.catalogUrl, "/v1/variations/ballots/" + ballot + "/vote", { method: "DELETE" }, true);
  }
  remove(id: string): Promise<void> {
    return this.request(this.config.catalogUrl, "/v1/media/" + id, { method: "DELETE" }, true);
  }
}
