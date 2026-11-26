"use strict";

const byId = (id) => document.getElementById(id);
const contentNames = {
  full_text: "Полный текст",
  abstract: "Аннотация",
  metadata: "Метаданные",
};
const runNames = {
  queued: "В очереди",
  running: "Идёт поиск",
  partial: "Есть ошибки",
  success: "Завершён",
  succeeded: "Завершён",
  complete: "Завершён",
  completed: "Завершён",
  ok: "Завершён",
  error: "Ошибка",
  failed: "Ошибка",
};
const numberFormat = new Intl.NumberFormat("ru");
const dateFormat = new Intl.DateTimeFormat("ru", { dateStyle: "medium", timeStyle: "short" });
const state = {
  libraries: [],
  id: new URL(location.href).searchParams.get("library"),
  search: "",
  offset: 0,
  limit: 25,
  editorId: null,
  article: null,
  articleRequest: 0,
  listRequest: 0,
  runRequest: 0,
  refresh: null,
  sourceTypes: [],
  sourceRequest: null,
};

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function show(element, text, className = "") {
  element.hidden = !text;
  element.textContent = text;
  if (className) element.className = className;
}

function timestamp(value) {
  if (!value) return "";
  const iso = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value)
    ? value.replace(" ", "T") + "Z"
    : value;
  const date = new Date(iso);
  return Number.isNaN(date.valueOf()) ? value : dateFormat.format(date);
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
  } catch {
    return null;
  }
  return null;
}

async function api(path, options) {
  const result = await fetch(path, options);
  const body = await result.json().catch(() => null);
  if (!result.ok) throw new Error(body?.error || ("Ошибка сервера " + result.status));
  if (body === null) throw new Error("Сервер вернул нечитаемый ответ");
  return body;
}

function libraryPath(id) {
  return "/api/libraries/" + encodeURIComponent(id);
}

function articlePath(id) {
  return "/api/articles/" + encodeURIComponent(id);
}

function selected() {
  return state.libraries.find((library) => String(library.id) === state.id);
}

function sourceLabel(kind) {
  return state.sourceTypes.find((type) => type.kind === kind)?.label || kind;
}

function loadSourceTypes() {
  if (state.sourceTypes.length) return Promise.resolve(state.sourceTypes);
  if (state.sourceRequest) return state.sourceRequest;
  state.sourceRequest = api("/api/sources").then((types) => {
    state.sourceTypes = types;
    return types;
  }).finally(() => { state.sourceRequest = null; });
  return state.sourceRequest;
}

function rememberSelection() {
  const url = new URL(location.href);
  if (state.id) url.searchParams.set("library", state.id);
  else url.searchParams.delete("library");
  if (location.href !== url.href) history.replaceState(null, "", url);
}

function modal(id) {
  const dialog = byId(id);
  if (!dialog.open) dialog.showModal();
  return dialog;
}

function showNotice(message, error = false) {
  show(byId("notice"), message, error ? "notice error" : "notice");
}

function renderNavigation() {
  const navigation = byId("libraries");
  const signature = JSON.stringify(state.libraries.map((library) => [
    library.id, library.name, library.article_count, library.enabled, String(library.id) === state.id,
  ]));
  if (navigation.dataset.signature === signature) return;
  navigation.dataset.signature = signature;
  const list = make("ul", "library-list");
  for (const library of state.libraries) {
    const item = make("li");
    const button = make("button", "library-item");
    button.type = "button";
    button.dataset.library = String(library.id);
    button.append(make("span", "library-label", library.name),
      make("span", "library-count", numberFormat.format(library.article_count || 0)));
    if (String(library.id) === state.id) button.setAttribute("aria-current", "page");
    item.append(button);
    list.append(item);
  }
  const focusedId = document.activeElement?.dataset.library;
  navigation.replaceChildren(list);
  if (focusedId) {
    for (const button of navigation.querySelectorAll(".library-item")) {
      if (button.dataset.library === focusedId) button.focus();
    }
  }
}

function updateExportLinks() {
  if (!state.id) return;
  const filter = byId("full-text-only").checked ? "?full_text=1" : "";
  byId("export-md").href = libraryPath(state.id) + "/export.md" + filter;
  byId("export-jsonl").href = libraryPath(state.id) + "/export.jsonl" + filter;
}

function runClass(status) {
  if (status === "queued" || status === "running") return "badge active";
  if (["partial", "failed", "error"].includes(status)) return "badge warning";
  if (status) return "badge success";
  return "badge";
}

function renderLibrary() {
  const library = selected();
  byId("welcome").hidden = Boolean(library);
  byId("workspace").hidden = !library;
  if (!library) {
    byId("welcome-create").hidden = false;
    byId("welcome-message").textContent = "Создай библиотеку и задай для неё ключевые слова и источники.";
    return;
  }
  byId("library-name").textContent = library.name;
  const sources = [...new Set(library.sources.map((source) => sourceLabel(source.kind)))];
  const frequency = library.enabled ? "каждые " + library.interval_hours + " ч" : "ручной запуск";
  byId("library-summary").textContent = sources.join(", ") + " · " + frequency;
  byId("keywords").replaceChildren(...library.keywords.map((word) => make("span", "tag", word)));
  const status = library.last_status;
  const runStatus = byId("run-status");
  runStatus.className = runClass(status);
  runStatus.textContent = runNames[status] || status || "Запусков ещё не было";
  byId("run-meta").textContent = timestamp(library.last_run_at);
  byId("run-library").disabled = status === "queued" || status === "running";
  updateExportLinks();
}

function selectLibrary(id) {
  if (id === state.id) return;
  state.id = id;
  state.search = "";
  state.offset = 0;
  byId("search").value = "";
  rememberSelection();
  renderNavigation();
  renderLibrary();
  showNotice("");
  void loadArticles(true);
}

function articleMeta(article) {
  return [
    article.authors?.join(", "),
    article.venue,
    article.published,
  ].filter(Boolean).join(" · ");
}

function articleCard(article) {
  const card = make("li", "article-card");
  const heading = make("h3");
  const open = make("button", "article-open", article.title || article.doi || "Статья");
  open.type = "button";
  open.dataset.article = String(article.id);
  open.addEventListener("click", () => openArticle(article));
  heading.append(open);
  const kind = article.content_kind;
  const top = make("div", "article-top");
  top.append(heading, make("span", kind === "full_text" ? "badge success" : "badge",
    contentNames[kind] || kind));
  card.append(top);

  const details = articleMeta(article);
  if (details) card.append(make("p", "article-meta", details));
  if (article.abstract) card.append(make("p", "article-abstract", article.abstract.replace(/\s+/g, " ")));

  const links = make("div", "article-links");
  if (article.doi) links.append(make("span", "doi", "DOI " + article.doi));
  if (article.sources?.length) {
    links.append(make("span", "secondary", article.sources.map(sourceLabel).join(", ")));
  }
  if (article.local_pdf) {
    const pdf = make("a", "", "PDF ↗");
    pdf.href = articlePath(article.id) + "/pdf";
    pdf.target = "_blank";
    pdf.rel = "noopener";
    links.append(pdf);
  }
  if (links.childElementCount) card.append(links);
  return card;
}

async function loadArticles(indicate = false) {
  if (!state.id) return;
  const id = state.id;
  const request = ++state.listRequest;
  const list = byId("articles");
  const resultsMessage = byId("results-message");
  if (indicate) {
    list.dataset.signature = "";
    list.replaceChildren();
    byId("result-count").textContent = "";
    byId("pagination").hidden = true;
    show(resultsMessage, "Загрузка статей…", "secondary");
  }
  const query = new URLSearchParams({ search: state.search, offset: state.offset });
  try {
    const response = await api(libraryPath(id) + "/articles?" + query);
    if (request !== state.listRequest || id !== state.id) return;
    const {items, total, limit, offset} = response;
    state.limit = limit;
    state.offset = offset;
    byId("result-count").textContent = numberFormat.format(total);
    const signature = JSON.stringify(items);
    if (list.dataset.signature !== signature) {
      const focusedId = document.activeElement?.dataset.article;
      list.replaceChildren(...items.map(articleCard));
      list.dataset.signature = signature;
      if (focusedId) {
        for (const button of list.querySelectorAll(".article-open")) {
          if (button.dataset.article === focusedId) button.focus();
        }
      }
    }
    show(resultsMessage, items.length > 0 ? "" : state.search
      ? "По запросу ничего не найдено." : "Пока здесь нет статей. Запусти поиск по источникам.", "secondary");
    byId("pagination").hidden = total <= limit;
    byId("previous-page").disabled = offset === 0;
    byId("next-page").disabled = offset + limit >= total;
    byId("page-range").textContent = numberFormat.format(offset + 1) + "–" +
      numberFormat.format(offset + items.length) + " из " + numberFormat.format(total);
  } catch (error) {
    if (request !== state.listRequest || id !== state.id) return;
    show(resultsMessage, "Не удалось получить статьи. " + error.message, "form-error");
  }
}

function refreshLibraries() {
  if (state.refresh) return state.refresh;
  state.refresh = (async () => {
    try {
      const previous = selected();
      state.libraries = await api("/api/libraries");
      if (!selected()) state.id = state.libraries.length ? String(state.libraries[0].id) : null;
      rememberSelection();
      renderNavigation();
      renderLibrary();
      const current = selected();
      if (current && (!previous || previous.id !== current.id)) {
        await loadArticles(true);
      } else if (current && (previous.last_status !== current.last_status ||
          previous.last_run_at !== current.last_run_at || previous.article_count !== current.article_count)) {
        await loadArticles();
      }
      if (current && byId("runs-dialog").open) void loadRuns();
    } catch (error) {
      if (!state.libraries.length) byId("welcome").hidden = true;
      showNotice("Не удалось получить библиотеки. " + error.message, true);
    }
  })().finally(() => { state.refresh = null; });
  return state.refresh;
}

function newSource(source) {
  const container = byId("editor-sources");
  const row = make("div", "source-row");
  const kindField = make("label", "", "Тип источника");
  const select = make("select");
  for (const type of state.sourceTypes) {
    const option = make("option", "", type.label);
    option.value = type.kind;
    select.append(option);
  }
  const initial = source?.kind || state.sourceTypes.find((type) => type.mode === "feed")?.kind ||
    state.sourceTypes[0]?.kind;
  if (initial && !state.sourceTypes.some((type) => type.kind === initial)) {
    const option = make("option", "", initial + " · недоступен");
    option.value = initial;
    select.append(option);
  }
  select.value = initial || "";
  select.dataset.kind = "";
  kindField.append(select);
  const fields = make("div", "source-fields");
  const description = make("p", "secondary source-description");

  const remove = make("button", "button button-icon remove-source", "×");
  remove.type = "button";
  remove.setAttribute("aria-label", "Убрать источник");
  remove.addEventListener("click", () => {
    const target = row.nextElementSibling || row.previousElementSibling;
    row.remove();
    (target?.querySelector("select") || byId("add-source")).focus();
  });

  function renderFields(values = {}) {
    const type = state.sourceTypes.find((item) => item.kind === select.value);
    show(description, type?.description || (type ? "" : "Выбери доступный источник."));
    fields.replaceChildren();
    for (const field of type?.fields || []) {
      const label = make("label", "", field.label);
      const input = make("input");
      input.type = field.type === "url" ? "url" : "text";
      input.required = field.required;
      input.dataset.sourceField = field.key;
      if (field.max_length > 0) input.maxLength = field.max_length;
      if (field.placeholder) input.placeholder = field.placeholder;
      input.value = values[field.key] || "";
      label.append(input);
      fields.append(label);
    }
    fields.hidden = !fields.childElementCount;
  }

  select.addEventListener("change", () => renderFields());
  renderFields(source);
  row.append(kindField, fields, remove, description);
  container.append(row);
  return select;
}

async function showEditor(library) {
  try {
    await loadSourceTypes();
    if (!state.sourceTypes.length) throw new Error("На сервере нет доступных источников.");
  } catch (error) {
    showNotice("Не удалось загрузить список источников. " + error.message, true);
    return;
  }
  state.editorId = library?.id ?? null;
  byId("editor-heading").textContent = library ? "Настройки библиотеки" : "Новая библиотека";
  const fields = byId("library-form").elements;
  fields.name.value = library?.name || "";
  fields.keywords.value = library?.keywords.join("\n") || "";
  fields.exclude.value = library?.exclude.join("\n") || "";
  fields.match.value = library?.match || "any";
  fields.lookback_days.value = library?.lookback_days ?? 30;
  fields.interval_hours.value = library?.interval_hours ?? 24;
  fields.enabled.checked = library?.enabled ?? true;
  fields.download_open_access.checked = library?.download_open_access ?? true;
  byId("editor-sources").replaceChildren();
  const defaults = state.sourceTypes.filter((type) =>
    type.mode === "search" && type.fields.every((field) => !field.required)).slice(0, 2);
  for (const source of library?.sources || (defaults.length ? defaults : [state.sourceTypes[0]])) newSource(source);
  show(byId("editor-error"), "");
  modal("library-dialog");
  fields.name.focus();
}

function lines(input) {
  return input.value.split(/\r?\n/).map((part) => part.trim()).filter(Boolean);
}

async function saveLibrary(event) {
  event.preventDefault();
  const fields = event.currentTarget.elements;
  const errorMessage = byId("editor-error");
  const sources = Array.from(byId("editor-sources").children, (row) => {
    const kind = row.querySelector("[data-kind]").value;
    const parameters = Array.from(row.querySelectorAll("[data-source-field]"),
      (field) => [field.dataset.sourceField, field.value.trim()]).filter(([, value]) => value);
    return { ...Object.fromEntries(parameters), kind };
  });
  const payload = {
    name: fields.name.value.trim(),
    keywords: lines(fields.keywords),
    exclude: lines(fields.exclude),
    match: fields.match.value,
    sources,
    lookback_days: Number(fields.lookback_days.value),
    interval_hours: Number(fields.interval_hours.value),
    enabled: fields.enabled.checked,
    download_open_access: fields.download_open_access.checked,
  };
  if (!payload.name || !payload.keywords.length || !sources.length) {
    show(errorMessage, "Укажи название, хотя бы одно ключевое слово и хотя бы один источник.");
    return;
  }
  if (sources.some((source) => !state.sourceTypes.some((type) => type.kind === source.kind))) {
    show(errorMessage, "Замени или убери недоступный источник.");
    return;
  }
  const id = state.editorId;
  byId("save-library").disabled = true;
  show(errorMessage, "");
  try {
    const library = await api(id === null ? "/api/libraries" : libraryPath(id), {
      method: id === null ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const index = state.libraries.findIndex((item) => String(item.id) === String(library.id));
    if (index === -1) state.libraries.push(library);
    else state.libraries[index] = library;
    byId("library-dialog").close();
    if (String(library.id) !== state.id) selectLibrary(String(library.id));
    else {
      renderNavigation();
      renderLibrary();
    }
    showNotice(id === null ? "Библиотека создана." : "Настройки сохранены.");
  } catch (error) {
    show(errorMessage, error.message);
  } finally {
    byId("save-library").disabled = false;
  }
}

async function runNow() {
  const library = selected();
  if (!library) return;
  byId("run-library").disabled = true;
  showNotice("");
  try {
    const job = await api(libraryPath(library.id) + "/run", {method: "POST"});
    library.last_status = job.status;
    if (String(library.id) === state.id) renderLibrary();
    await refreshLibraries();
  } catch (error) {
    showNotice(error.message, true);
    renderLibrary();
  }
}

async function loadRuns() {
  const id = state.id;
  const request = ++state.runRequest;
  const list = byId("runs");
  try {
    const runs = await api(libraryPath(id) + "/runs");
    if (request !== state.runRequest || id !== state.id) return;
    const signature = JSON.stringify(runs);
    if (list.dataset.signature === signature) return;
    const openErrors = new Set(Array.from(list.querySelectorAll("details[open]"), (item) => item.dataset.runId));
    list.dataset.signature = signature;
    list.replaceChildren();
    if (!runs.length) {
      list.append(make("p", "secondary", "Запусков ещё не было."));
      return;
    }
    for (const run of runs) {
      const item = make("section", "run-entry");
      const header = make("div", "run-entry-header");
      header.append(make("span", runClass(run.status), runNames[run.status] || run.status),
        make("span", "secondary", timestamp(run.started_at)));
      item.append(header, make("p", "run-stats",
        "Найдено " + (run.found ?? 0) + " · Добавлено " + (run.added ?? 0) +
        " · Скачано " + (run.downloaded ?? 0)));
      if (run.errors?.length) {
        const details = make("details");
        details.dataset.runId = String(run.id);
        details.open = openErrors.has(String(run.id));
        const messages = make("ul");
        messages.append(...run.errors.map((error) => make("li", "", error)));
        details.append(make("summary", "", "Подробности ошибок"), messages);
        item.append(details);
      }
      list.append(item);
    }
  } catch (error) {
    if (request !== state.runRequest || id !== state.id) return;
    list.dataset.signature = "";
    list.replaceChildren(make("p", "form-error", error.message));
  }
}

function setPreviewFiles(article) {
  const link = byId("preview-link");
  const href = safeUrl(article.url);
  link.hidden = !href;
  if (href) link.href = href;
  else link.removeAttribute("href");
  const pdf = byId("preview-pdf");
  pdf.hidden = !article.local_pdf;
  pdf.href = articlePath(article.id) + "/pdf";
  const kind = byId("preview-kind");
  kind.textContent = contentNames[article.content_kind] || article.content_kind;
  kind.className = article.content_kind === "full_text" ? "badge success" : "badge";
}

async function loadText(article) {
  const request = ++state.articleRequest;
  const preview = byId("preview-text");
  const status = byId("preview-status");
  preview.textContent = "";
  show(status, "Загрузка текста…", "secondary");
  try {
    const result = await api(articlePath(article.id) + "/text");
    if (request !== state.articleRequest) return;
    article.content_kind = result.content_kind;
    setPreviewFiles(article);
    preview.textContent = result.text || "";
    preview.hidden = !result.text;
    show(status, result.text ? "" : "Текст для этой статьи пока недоступен.", "secondary");
  } catch (error) {
    if (request !== state.articleRequest) return;
    preview.hidden = true;
    show(status, error.message, "form-error");
  }
}

function openArticle(article) {
  state.article = { ...article };
  byId("preview-heading").textContent = article.title || article.doi || "Статья";
  byId("preview-meta").textContent = [articleMeta(article), article.doi].filter(Boolean).join(" · ");
  byId("attach-form").reset();
  byId("attach-button").disabled = false;
  show(byId("attach-status"), "");
  setPreviewFiles(article);
  modal("article-dialog");
  void loadText(state.article);
}

async function attachFile(event) {
  event.preventDefault();
  const file = byId("attach-file").files[0];
  const article = state.article;
  if (!file || !article) return;
  const formats = { pdf: "pdf", md: "md", markdown: "md", txt: "txt", xml: "xml", jats: "xml" };
  const format = formats[file.name.split(".").at(-1).toLowerCase()];
  const status = byId("attach-status");
  if (!format) {
    show(status, "Укажи PDF, Markdown, TXT или JATS XML.", "form-error");
    return;
  }
  byId("attach-button").disabled = true;
  show(status, "Файл загружается и обрабатывается…", "secondary");
  try {
    const result = await api(articlePath(article.id) + "/file?format=" + format, {
      method: "PUT",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    if (article === state.article) {
      article.content_kind = result.content_kind;
      article.local_pdf = result.local_pdf;
      setPreviewFiles(article);
      byId("attach-form").reset();
      const message = result.content_kind === "full_text" ? "Полный текст добавлен."
        : "Файл добавлен. Извлечённый полный текст недоступен.";
      show(status, message, "secondary");
      void loadText(article);
    }
    void loadArticles();
  } catch (error) {
    if (article === state.article) show(status, error.message, "form-error");
  } finally {
    if (article === state.article) byId("attach-button").disabled = false;
  }
}

byId("libraries").addEventListener("click", (event) => {
  const button = event.target.closest("[data-library]");
  if (button) selectLibrary(button.dataset.library);
});
byId("new-library").addEventListener("click", () => showEditor());
byId("welcome-create").addEventListener("click", () => showEditor());
byId("edit-library").addEventListener("click", () => showEditor(selected()));
byId("add-source").addEventListener("click", () => newSource().focus());
byId("library-form").addEventListener("submit", saveLibrary);
byId("run-library").addEventListener("click", runNow);
byId("full-text-only").addEventListener("change", updateExportLinks);
byId("show-runs").addEventListener("click", () => {
  const list = byId("runs");
  list.dataset.signature = "";
  list.replaceChildren(make("p", "secondary", "Загрузка…"));
  modal("runs-dialog");
  void loadRuns();
});
byId("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.search = byId("search").value.trim();
  state.offset = 0;
  void loadArticles(true);
});
byId("previous-page").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  void loadArticles(true);
});
byId("next-page").addEventListener("click", () => {
  state.offset += state.limit;
  void loadArticles(true);
});
byId("attach-form").addEventListener("submit", attachFile);
byId("article-dialog").addEventListener("close", () => {
  state.article = null;
  state.articleRequest += 1;
});
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => byId(button.dataset.close).close());
}

void loadSourceTypes().catch(() => null).then(refreshLibraries);
setInterval(() => {
  if (document.visibilityState === "visible") void refreshLibraries();
}, 8000);
