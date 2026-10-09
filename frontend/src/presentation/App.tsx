import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { FILE_ACCEPT, FeatureState, MediaKind } from "@/application/backend";
import type { IBackend, IGroup, IMedia, IPage, IUser, IVote } from "@/application/backend";

const kindLabels = { image: "Фото", animation: "Анимация", video: "Видео", audio: "Аудио" };
const featureLabels: Record<string, string> = { preview: "Превью", ocr: "Текст", transcript: "Речь", visual_embedding: "Изображение", audio_embedding: "Звук", text_embedding: "Описание" };
const dateFormat = new Intl.DateTimeFormat("ru", { day: "numeric", month: "short" });
function errorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === "TimeoutError") return "Сервис долго отвечает. Попробуйте ещё раз.";
  if (error instanceof TypeError) return "Не удалось подключиться. Проверьте, запущен ли backend.";
  return error instanceof Error ? error.message : "Не удалось выполнить действие.";
}

function Icon({ name }: { name: "search" | "plus" | "arrow" | "close" }) {
  const paths = { search: "m21 21-5-5M10.5 18a7.5 7.5 0 1 0 0-15 7.5 7.5 0 0 0 0 15", plus: "M12 5v14M5 12h14", arrow: "M5 12h14m-6-6 6 6-6 6", close: "m6 6 12 12M6 18 18 6" };
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}

function Modal({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog ref={ref} aria-label={title} onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <div className="modal-head"><h2>{title}</h2><button className="icon-button" onClick={close} aria-label="Закрыть"><Icon name="close" /></button></div>{children}
  </dialog>;
}

function MediaView({ item, large = false }: { item: IMedia; large?: boolean }) {
  const [failed, setFailed] = useState(false);
  const preview = item.features.find(feature => feature.kind === "preview" && feature.state === FeatureState.Available)?.url;
  if (failed) return <div className="unavailable">Превью недоступно<br /><a href={item.url} target="_blank" rel="noreferrer">Открыть файл ↗</a></div>;
  if (item.media_kind === MediaKind.Audio) return <div className="audio-view"><span className="wave" aria-hidden="true">▂▅▇▃▆█▅▂▇▅▃</span><audio src={item.url} controls preload="none" onError={() => setFailed(true)} /></div>;
  if (item.media_kind === MediaKind.Video) return <video src={item.url} poster={preview} controls playsInline preload="metadata" onError={() => setFailed(true)} />;
  return <img src={large ? item.url : preview ?? item.url} loading={large ? "eager" : "lazy"} alt={kindLabels[item.media_kind] + " от " + item.author.display_name} onError={() => setFailed(true)} />;
}

function AuthForm({ submit }: { submit: (login: string, password: string, register: boolean) => Promise<void> }) {
  const [register, setRegister] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try { await submit(String(data.get("login")), String(data.get("password")), register); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(false); }
  }
  return <form className="auth-form" onSubmit={onSubmit}>
    <p>{register ? "Пополняйте коллекцию и помогайте находить вариации." : "Ваша коллекция начинается с одного мема."}</p>
    <label>Логин<input name="login" autoComplete="username" required minLength={3} maxLength={32} pattern="[a-zA-Z0-9_-]+" placeholder="meme_lover" /></label>
    <label>Пароль<input name="password" type="password" autoComplete={register ? "new-password" : "current-password"} required maxLength={128} /></label>
    {register && <small>Восстановления пароля пока нет. Сохраните его в надёжном месте.</small>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    <button className="button primary" disabled={busy}>{busy ? "Подождите…" : register ? "Создать аккаунт" : "Войти"}<Icon name="arrow" /></button>
    <button type="button" className="text-button" disabled={busy} onClick={() => { setRegister(!register); setError(""); }}>{register ? "Уже есть аккаунт? Войти" : "Нет аккаунта? Зарегистрироваться"}</button>
  </form>;
}

function Detail({ backend, item: initial, user, close, changed }: { backend: IBackend; item: IMedia; user: IUser | null; close: () => void; changed: () => void }) {
  const [item, setItem] = useState(initial);
  const [group, setGroup] = useState<IGroup | null>(null);
  const [vote, setVote] = useState<IVote | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setGroup(null);
    backend.group(item.group_id, 0, controller.signal).then(setGroup).catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)); });
    return () => controller.abort();
  }, [backend, item]);
  async function act(action: () => Promise<void>) {
    setBusy(true); setError("");
    try { await action(); } catch (failure) { setError(errorMessage(failure)); } finally { setBusy(false); }
  }
  return <Modal title="Тот самый мем" close={close}>
    <div className="detail-media"><MediaView key={item.media_id} item={item} large /></div>
    <div className="detail-meta"><span>@{item.author.display_name} · {dateFormat.format(new Date(item.published_at))}</span><a href={item.url} target="_blank" rel="noreferrer">Оригинал ↗</a></div>
    <div className="features">{item.features.map(feature => <span key={feature.kind} className={feature.state} title={feature.state === FeatureState.Failed ? "Этот способ поиска недоступен" : feature.state === FeatureState.Pending ? "Ещё обрабатывается" : "Результат обработки"}>{featureLabels[feature.kind] ?? feature.kind}{feature.state === FeatureState.Pending ? " ···" : feature.state === FeatureState.Failed ? " ×" : ""}</span>)}</div>
    <div className="variation-head"><h3>Вариации <span>{group?.member_count ?? "…"}</span></h3><button className="text-button" disabled={busy} onClick={() => void act(async () => { setItem(await backend.media(item.media_id)); })}>Обновить</button></div>
    {group && <div className="variations">{group.items.map(member => <button key={member.media_id} className={member.media_id === item.media_id ? "selected" : ""} onClick={() => { setItem(member); setVote(null); setConfirmDelete(false); }} aria-label={"Открыть вариацию от " + member.author.display_name}>
      {member.media_kind === MediaKind.Video || member.media_kind === MediaKind.Audio ? <span>{kindLabels[member.media_kind]}</span> : <img src={member.url} alt="" loading="lazy" />}
    </button>)}</div>}
    {group && group.items.length < group.member_count && <button className="text-button" disabled={busy} onClick={() => void act(async () => {
      const more = await backend.group(group.group_id, group.items.length);
      setGroup({ ...more, items: [...group.items, ...more.items] });
    })}>Ещё вариации</button>}
    {user && group && group.member_count > 1 && !vote && <button className="text-button" disabled={busy} onClick={() => void act(async () => {
      const result = await backend.detach(item.membership_id); setVote(result);
      if (result.applied) { setItem(await backend.media(item.media_id)); changed(); }
    })}>Это другой мем - проголосовать за открепление</button>}
    {vote && <p className="vote-note">{vote.applied ? "Медиа откреплено." : "Голос принят: " + vote.active_votes + " / " + vote.threshold}
      {!vote.applied && <button className="text-button" disabled={busy} onClick={() => void act(async () => { await backend.withdraw(vote.ballot_id); setVote(null); })}>Отозвать</button>}</p>}
    {user?.user_id === item.author.user_id && <div className="delete-row"><button className="text-button danger" disabled={busy} onClick={() => confirmDelete ? void act(async () => { await backend.remove(item.media_id); changed(); close(); }) : setConfirmDelete(true)}>{confirmDelete ? "Подтвердить удаление файла" : "Удалить моё медиа"}</button>{confirmDelete && <button className="text-button" onClick={() => setConfirmDelete(false)}>Отмена</button>}</div>}
    {error && <p className="inline-error" role="alert">{error}</p>}
  </Modal>;
}

export function App({ backend }: { backend: IBackend }) {
  const [user, setUser] = useState<IUser | null>(null);
  const [authOpen, setAuthOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeQuery, setActiveQuery] = useState("");
  const [page, setPage] = useState<IPage>({ items: [], next: null });
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<IMedia | null>(null);
  const [revision, setRevision] = useState(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const pagination = useRef<AbortController | null>(null);

  useEffect(() => { let alive = true; backend.restore().then(value => { if (alive) setUser(value); }).catch(failure => { if (alive) setError(errorMessage(failure)); }); return () => { alive = false; }; }, [backend]);
  useEffect(() => {
    const controller = new AbortController();
    pagination.current?.abort();
    setLoading(true); setError(""); setPage({ items: [], next: null });
    (activeQuery ? backend.search(activeQuery, undefined, controller.signal) : backend.feed(undefined, controller.signal))
      .then(setPage).catch(failure => { if (!controller.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); pagination.current?.abort(); };
  }, [backend, activeQuery, revision]);
  useEffect(() => {
    let controller: AbortController;
    function openHash() {
      controller?.abort();
      controller = new AbortController();
      const signal = controller.signal;
      const id = location.hash.startsWith("#media=") ? location.hash.slice("#media=".length) : "";
      if (!id) { setSelected(null); return; }
      backend.media(id, signal).then(value => { if (!signal.aborted) setSelected(value); })
        .catch(failure => { if (!signal.aborted) setError(errorMessage(failure)); });
    }
    openHash(); window.addEventListener("hashchange", openHash);
    return () => { controller?.abort(); window.removeEventListener("hashchange", openHash); };
  }, [backend]);

  async function act(label: string, action: () => Promise<void>) {
    setWorking(label); setError(""); setNotice("");
    try { await action(); } catch (failure) { setError(errorMessage(failure)); } finally { setWorking(""); }
  }
  function open(item: IMedia) { setSelected(item); location.hash = "media=" + item.media_id; }
  function refresh() { setRevision(value => value + 1); }
  async function more() {
    if (!page.next) return;
    const controller = new AbortController(); pagination.current = controller;
    setLoading(true); setError("");
    try {
      const next = activeQuery ? await backend.search(activeQuery, page.next, controller.signal) : await backend.feed(page.next, controller.signal);
      if (!controller.signal.aborted) setPage(previous => ({ ...next, items: [...previous.items, ...next.items.filter(item => !previous.items.some(existing => existing.media_id === item.media_id))] }));
    } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
    finally { if (!controller.signal.aborted) setLoading(false); }
  }
  return <div className="app">
    <header><a className="brand" href="#" onClick={() => { setActiveQuery(""); setQuery(""); }}><span className="brand-symbol">м</span>мембанк<span className="brand-dot">®</span></a>
      <div className="header-actions">{user ? <><span className="user-name">@{user.login}</span><button className="text-button" disabled={!!working} onClick={() => void act("Выходим…", async () => { await backend.signOut(); setUser(null); })}>Выйти</button></> : <button className="text-button" onClick={() => setAuthOpen(true)}>Войти</button>}
        <button className="button primary upload-button" disabled={!!working} onClick={() => user ? fileInput.current?.click() : setAuthOpen(true)}><Icon name="plus" /><span>Загрузить мем</span></button>
        <input ref={fileInput} className="hidden" type="file" accept={FILE_ACCEPT} onChange={event => {
          const file = event.currentTarget.files?.[0]; event.currentTarget.value = "";
          if (file) void act("Загружаем мем…", async () => {
            const result = await backend.upload(file);
            setNotice(result.duplicate ? "Этот мем уже есть в коллекции. Открыли его для вас." : "Мем опубликован! Возможности поиска появятся по мере обработки.");
            open(await backend.media(result.media_id)); refresh();
          });
        }} />
      </div>
    </header>
    <main>
      <section className="hero"><div className="hero-copy"><div className="eyebrow"><span /> КОЛЛЕКТИВНАЯ ПАМЯТЬ ИНТЕРНЕТА</div><h1>Найдётся<br />тот самый <em>мем.</em></h1><p>Помните картинку, но забыли слова?<br />Опишите её - остальное найдём.</p></div>
        <div className="hero-art" aria-hidden="true"><div className="art-card art-back"><span>это база.</span><div className="art-grid" /></div><div className="art-card art-front"><span className="art-label">ТОТ САМЫЙ</span><div className="cat"><i /><i /><div className="cat-eyes">⌐■_■</div></div><span className="art-caption">сохранён в памяти</span></div><span className="art-star">✳</span><span className="art-dot" /></div>
      </section>
      <form className="search-form" onSubmit={event => { event.preventDefault(); setActiveQuery(query.trim()); if (query.trim() === activeQuery) refresh(); }}>
        <Icon name="search" /><input aria-label="Поиск мемов" maxLength={2000} placeholder="Кот за ноутбуком, всё горит, но всё нормально…" value={query} onChange={event => setQuery(event.target.value)} />
        {query && <button type="button" className="icon-button" aria-label="Очистить поиск" onClick={() => { setQuery(""); setActiveQuery(""); }}><Icon name="close" /></button>}
        <button className="button primary" type="submit">Найти<Icon name="arrow" /></button>
      </form>
      <div className="collection-heading"><div><span className="eyebrow dark">ХОРОШЕЕ НЕ ТЕРЯЕТСЯ</span><h2>{activeQuery ? "Результаты поиска" : "Свежие поступления"}</h2></div><button className="text-button" disabled={loading} onClick={refresh}>Обновить ↻</button></div>
      {working && <p className="notice" role="status"><span className="spinner" />{working}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}
      {error && <div className="error" role="alert">{error}<button className="text-button" onClick={() => setError("")}>Закрыть</button></div>}
      {page.degraded && <p className="notice">Поиск по описанию пока недоступен. Показываем совпадения по тексту.</p>}
      <div className="media-grid">{page.items.map(item => <article className="media-card" key={item.media_id}>
        <div className="card-media"><MediaView item={item} /></div><div className="card-footer"><div><span className="kind">{kindLabels[item.media_kind]}</span><span className="author">@{item.author.display_name}</span></div><button className="icon-button" aria-label="Открыть мем и вариации" onClick={() => open(item)}><Icon name="arrow" /></button></div>
      </article>)}</div>
      {loading && !page.items.length && <div className="empty" role="status"><span className="spinner" /><h3>{activeQuery ? "Ищем тот самый мем…" : "Собираем коллекцию…"}</h3>{activeQuery && <p>Поиск по описанию может занять немного времени.</p>}</div>}
      {!loading && !page.items.length && !error && <div className="empty"><span className="empty-symbol">✳</span><h3>{activeQuery ? "Пока не нашли" : "Здесь начинается коллекция"}</h3><p>{activeQuery ? "Попробуйте другие слова или более короткое описание." : "Загрузите первый мем. Хорошее должно сохраняться."}</p>{!activeQuery && <button className="button primary" onClick={() => user ? fileInput.current?.click() : setAuthOpen(true)}>Добавить первый мем<Icon name="plus" /></button>}</div>}
      {page.next && <div className="load-more"><button className="button secondary" disabled={loading} onClick={() => void more()}>{loading ? "Загружаем…" : "Ещё немного мемов"}<Icon name="arrow" /></button></div>}
    </main>
    <footer><span>мембанк</span><p>Интернет забывает. Мы сохраняем.</p><span>RU / EN</span></footer>
    {authOpen && <Modal title="Добро пожаловать" close={() => setAuthOpen(false)}><AuthForm submit={async (login, password, register) => { setUser(await backend.signIn(login, password, register)); setAuthOpen(false); }} /></Modal>}
    {selected && <Detail key={selected.media_id} backend={backend} item={selected} user={user} changed={refresh} close={() => { location.hash = ""; setSelected(null); }} />}
  </div>;
}
