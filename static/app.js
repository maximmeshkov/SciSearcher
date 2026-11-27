"use strict";

const byId = (id) => document.getElementById(id);
const scopes = new Set(["all", "full_text", "pdf"]);
const requestedScope = new URL(location.href).searchParams.get("scope");
const contentNames = {
  full_text: "Полный текст",
  abstract: "Аннотация",
  metadata: "Метаданные",
};
const pdfProblemNames = {
  failed: "Ошибка загрузки PDF",
  no_text: "Текст из PDF не извлечён",
};
const parseNames = {
  pending: "В очереди разбора",
  parsing: "Разбирается",
  failed: "Ошибка разбора",
};
const uploadFormats = {
  pdf: "pdf", djvu: "djvu", djv: "djvu", md: "md", markdown: "md", txt: "txt", xml: "xml", jats: "xml",
};

function fileFormat(name) {
  return uploadFormats[name.split(".").at(-1).toLowerCase()];
}

// One line about the PDF for the card and the preview: the parse queue wins over the download state.
function pdfState(article) {
  if (parseNames[article.parse_status]) {
    return {
      label: parseNames[article.parse_status],
      detail: article.parse_status === "failed" ? article.parse_error || "" : "",
      className: article.parse_status === "failed" ? "badge warning" : "badge active",
    };
  }
  const label = pdfProblemNames[article.download_status];
  return label ? { label, detail: article.download_error || "", className: "badge warning" } : null;
}
const runNames = {
  queued: "В очереди",
  running: "Идёт сбор",
  partial: "С замечаниями",
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
  scope: scopes.has(requestedScope) ? requestedScope : "all",
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
  if (kind === "upload") return "Свой файл";
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
  if (state.scope === "all") url.searchParams.delete("scope");
  else url.searchParams.set("scope", state.scope);
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
    byId("welcome-message").textContent =
      "Создай библиотеку: с поиском по источникам или для своих файлов.";
    return;
  }
  byId("library-name").textContent = library.name;
  const sources = [...new Set(library.sources.map((source) => sourceLabel(source.kind)))];
  const frequency = library.enabled ? "каждые " + library.interval_hours + " ч" : "ручной запуск";
  byId("library-summary").textContent = sources.length ? sources.join(", ") + " · " + frequency
    : "Без поиска · файлы добавляются вручную";
  byId("run-library").hidden = !sources.length;
  document.querySelector(".run-strip").hidden = !sources.length;
  byId("keywords").replaceChildren(...library.keywords.map((word) => make("span", "tag", word)));
  for (const [id, count] of [
    ["stat-articles", library.article_count],
    ["stat-full-text", library.full_text_count],
    ["stat-pdf", library.pdf_count],
  ]) {
    byId(id).textContent = Number.isInteger(count) ? numberFormat.format(count) : "—";
  }
  const waiting = library.parse_waiting || 0;
  const failed = library.parse_failed || 0;
  byId("stat-parse-box").hidden = !(waiting || failed);
  byId("stat-parse").textContent = numberFormat.format(waiting) + (failed ? " · ошибок " + numberFormat.format(failed) : "");
  const status = library.last_status;
  const runStatus = byId("run-status");
  runStatus.className = runClass(status);
  runStatus.textContent = runNames[status] || status || "Запусков ещё не было";
  byId("run-meta").textContent = library.last_run_at ? "Последний запуск " + timestamp(library.last_run_at) : "";
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
    const pdf = make("a", "", "Файл ↗");
    pdf.href = articlePath(article.id) + "/pdf";
    pdf.target = "_blank";
    pdf.rel = "noopener";
    links.append(pdf);
  }
  const pdf = pdfState(article);
  if (pdf) {
    const badge = make("span", pdf.className, pdf.label);
    if (pdf.detail) badge.title = pdf.detail;
    links.append(badge);
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
  const query = new URLSearchParams({ search: state.search, offset: state.offset, scope: state.scope });
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
    const emptyMessage = state.search ? "По запросу ничего не найдено."
      : state.scope === "full_text" ? "В этой библиотеке пока нет извлечённых полных текстов."
      : state.scope === "pdf" ? "В этой библиотеке пока нет локальных файлов."
      : selected()?.sources.length ? "Пока здесь нет статей. Запусти поиск по источникам."
      : "Пока здесь нет файлов. Добавь их кнопкой «Добавить файлы» или «Добавить папку».";
    show(resultsMessage, items.length ? "" : emptyMessage, "secondary");
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
      } else if (current && ["last_status", "last_run_at", "article_count", "full_text_count", "pdf_count"]
          .some((key) => previous[key] !== current[key])) {
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
  byId("retention-note").hidden = !library;
  const fields = byId("library-form").elements;
  fields.name.value = library?.name || "";
  fields.keywords.value = library?.keywords.join("\n") || "";
  fields.exclude.value = library?.exclude.join("\n") || "";
  fields.match.value = library?.match || "any";
  fields.lookback_days.value = library?.lookback_days || 30;
  fields.all_history.checked = library?.lookback_days === 0;
  fields.lookback_days.disabled = fields.all_history.checked;
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
  const keywords = lines(fields.keywords);
  const payload = {
    name: fields.name.value.trim(),
    keywords,
    exclude: lines(fields.exclude),
    match: fields.match.value,
    // Without phrases there is nothing to search: the library is filled by hand and keeps no sources.
    sources: keywords.length ? sources : [],
    lookback_days: fields.all_history.checked ? 0 : Number(fields.lookback_days.value),
    interval_hours: Number(fields.interval_hours.value),
    enabled: fields.enabled.checked,
    download_open_access: fields.download_open_access.checked,
  };
  if (!payload.name) {
    show(errorMessage, "Укажи название библиотеки.");
    return;
  }
  if (payload.sources.some((source) => !state.sourceTypes.some((type) => type.kind === source.kind))) {
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
  const pdfLine = pdfState(article);
  show(byId("preview-download"), pdfLine ? [pdfLine.label, pdfLine.detail].filter(Boolean).join(" · ") : "");
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
  const format = fileFormat(file.name);
  const status = byId("attach-status");
  if (!format) {
    show(status, "Укажи PDF, DjVu, Markdown, TXT или JATS XML.", "form-error");
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
      article.download_status = "downloaded";
      article.download_error = "";
      article.parse_status = result.parse_status || null;
      article.parse_error = "";
      setPreviewFiles(article);
      byId("attach-form").reset();
      const message = result.parse_status ? "Файл добавлен и поставлен в очередь разбора."
        : result.content_kind === "full_text" ? "Полный текст добавлен."
        : "Файл добавлен. Извлечённый полный текст недоступен.";
      show(status, message, "secondary");
      void loadText(article);
    }
    void loadArticles();
    void refreshLibraries();
  } catch (error) {
    if (article === state.article) show(status, error.message, "form-error");
  } finally {
    if (article === state.article) byId("attach-button").disabled = false;
  }
}

// Send the chosen files to the library one by one; unsupported files (e.g. from a folder) are skipped.
async function uploadFiles(input) {
  const library = selected();
  const chosen = Array.from(input.files);
  input.value = "";
  if (!library || !chosen.length) return;
  const files = chosen.filter((file) => fileFormat(file.name));
  const buttons = [byId("add-files"), byId("add-folder")];
  for (const button of buttons) button.disabled = true;
  let added = 0;
  let repeated = 0;
  const failed = [];
  try {
    for (const [index, file] of files.entries()) {
      showNotice("Загрузка " + (index + 1) + " из " + files.length + ": " + file.name);
      try {
        const result = await api(libraryPath(library.id) + "/files?" + new URLSearchParams({ name: file.name }), {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: file,
        });
        if (result.duplicate) repeated += 1;
        else added += 1;
      } catch (error) {
        failed.push(file.name + ": " + error.message);
      }
    }
  } finally {
    for (const button of buttons) button.disabled = false;
  }
  const report = ["Добавлено файлов: " + added];
  if (repeated) report.push("уже были в библиотеке: " + repeated);
  if (chosen.length > files.length) report.push("пропущено неподходящих: " + (chosen.length - files.length));
  if (failed.length) report.push("ошибок: " + failed.length + " (" + failed.slice(0, 3).join("; ") + ")");
  showNotice(report.join(" · "), failed.length > 0);
  void refreshLibraries();
  void loadArticles();
}

byId("libraries").addEventListener("click", (event) => {
  const button = event.target.closest("[data-library]");
  if (button) selectLibrary(button.dataset.library);
});
byId("new-library").addEventListener("click", () => showEditor());
byId("welcome-create").addEventListener("click", () => showEditor());
byId("edit-library").addEventListener("click", () => showEditor(selected()));
byId("add-source").addEventListener("click", () => newSource().focus());
const libraryForm = byId("library-form");
libraryForm.addEventListener("submit", saveLibrary);
libraryForm.elements.all_history.addEventListener("change", (event) => {
  libraryForm.elements.lookback_days.disabled = event.target.checked;
});
byId("run-library").addEventListener("click", runNow);
byId("add-files").addEventListener("click", () => byId("upload-files").click());
byId("add-folder").addEventListener("click", () => byId("upload-folder").click());
for (const id of ["upload-files", "upload-folder"]) {
  byId(id).addEventListener("change", (event) => void uploadFiles(event.target));
}
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
const scopeFilter = byId("scope-filter");
for (const option of scopeFilter.querySelectorAll("input")) {
  option.checked = option.value === state.scope;
}
scopeFilter.addEventListener("change", (event) => {
  if (!scopes.has(event.target.value)) return;
  state.scope = event.target.value;
  state.offset = 0;
  rememberSelection();
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
