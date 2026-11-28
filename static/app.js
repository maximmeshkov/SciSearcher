"use strict";

const byId = (id) => document.getElementById(id);
const scopes = new Set(Array.from(byId("scope-filter").options, (option) => option.value));
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

function discoveryState(article) {
  if (article.content_kind === "full_text" || article.local_pdf) return null;
  const labels = {
    queued: "В очереди поиска файла", checking: "Проверяем доступные копии",
    no_copy: "Источники пока не вернули доступную копию", failed: "Ошибка поиска копии",
  };
  const label = labels[article.discovery_status];
  if (!label) return null;
  const details = [label, article.discovery_detail];
  if (["no_copy", "failed"].includes(article.discovery_status) && article.discovery_next_try > 0) {
    details.push("Следующая проверка " + dateFormat.format(new Date(article.discovery_next_try * 1000)));
  }
  return { label, details: details.filter(Boolean).join(" · ") };
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
  scopeExplicit: scopes.has(requestedScope),
  offset: 0,
  limit: 25,
  editorId: null,
  article: null,
  exclusion: null,
  articleRequest: 0,
  listRequest: 0,
  runRequest: 0,
  refresh: null,
  sourceTypes: [],
  sourceRequest: null,
  view: new URL(location.href).searchParams.get("view") === "research" ? "research" : "catalog",
  questionRequest: 0,
  settings: null,
  statusRequest: 0,
  resolving: new Set(),
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
  if (!state.scopeExplicit) url.searchParams.delete("scope");
  else url.searchParams.set("scope", state.scope);
  if (state.view === "research") url.searchParams.set("view", "research");
  else url.searchParams.delete("view");
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
    byId("welcome-connect").hidden = false;
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
    ["stat-pdf", library.pdf_count], ["stat-metadata", library.metadata_only_count],
  ]) {
    byId(id).textContent = Number.isInteger(count) ? numberFormat.format(count) : "—";
  }
  updateScopeControls(library);
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
  state.scopeExplicit = false;
  applyDefaultScope();
  state.search = "";
  state.offset = 0;
  byId("search").value = "";
  resetResearch();
  rememberSelection();
  renderNavigation();
  renderLibrary();
  showNotice("");
  void loadArticles(true);
  void loadIndexStatus();
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
  const excluded = state.scope === "excluded";
  if (excluded) {
    open.disabled = true;
    open.className = "excluded-title";
  } else {
    open.addEventListener("click", () => openArticle(article));
  }
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
  if (article.local_pdf && !excluded) {
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
  const discovery = discoveryState(article);
  if (discovery) {
    const badge = make("span", "badge", discovery.label);
    badge.title = discovery.details;
    links.append(badge);
  }
  if (links.childElementCount) card.append(links);
  if (excluded) {
    const actions = make("div", "action-row article-management");
    if (article.library_excluded) actions.append(restoreButton(article, false));
    if (article.global_excluded) actions.append(restoreButton(article, true));
    if (article.local_pdf || article.content_kind === "full_text") {
      const purge = make("button", "text-button", "Удалить сохранённые файлы и текст");
      purge.type = "button";
      purge.addEventListener("click", () => showExclusion(article, true));
      actions.append(purge);
    }
    card.append(actions);
  }
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
    if (offset > 0 && !items.length && total > 0) {
      state.offset = Math.floor((total - 1) / limit) * limit;
      return loadArticles(true);
    }
    state.offset = offset;
    if (state.article && byId("article-dialog").open) {
      const updated = items.find((item) => item.id === state.article.id);
      if (updated) {
        const changedText = updated.content_kind !== state.article.content_kind;
        Object.assign(state.article, updated);
        setPreviewFiles(state.article);
        if (changedText) void loadText(state.article);
      }
    }
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
      : state.scope === "excluded" ? "В чёрном списке пока нет статей."
      : state.scope !== "all" ? "Статей с этим статусом нет."
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
      if (!previous || previous.id !== selected()?.id) applyDefaultScope();
      rememberSelection();
      renderNavigation();
      renderLibrary();
      const current = selected();
      if (current && (!previous || previous.id !== current.id)) {
        resetResearch();
        void loadIndexStatus();
        await loadArticles(true);
      } else if (current && ((state.view === "catalog" && !byId("catalog-recovery").hidden) ||
          ["last_status", "last_run_at", "article_count", "full_text_count", "pdf_count",
          "parse_waiting", "parse_failed", "excluded_count", "download_failed_count", "no_pdf_count"]
          .some((key) => previous[key] !== current[key]))) {
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
  show(byId("preview-discovery"), discoveryState(article)?.details || "");
  byId("retry-article").hidden = article.content_kind === "full_text" || article.local_pdf;
  byId("retry-article").disabled = state.resolving.has(state.id + "/" + article.id);
  byId("reparse-article").hidden = !article.local_pdf || article.parse_status !== "failed";
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
  show(byId("preview-folder"), article.folder ? "Папка в каталоге данных NAS · " + article.folder : "");
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
scopeFilter.value = state.scope;
scopeFilter.addEventListener("change", (event) => selectScope(event.target.value));
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

function sendJson(method, data) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) };
}

function scopedArticlePath(libraryId, articleId) {
  return libraryPath(libraryId) + "/articles/" + encodeURIComponent(articleId);
}

function updateView() {
  const research = state.view === "research";
  byId("research-panel").hidden = !research;
  byId("catalog-panel").hidden = research;
  byId("research-view").setAttribute("aria-pressed", String(research));
  byId("indexed-stat").setAttribute("aria-pressed", String(research));
  byId("catalog-view").setAttribute("aria-pressed", String(!research));
  for (const button of document.querySelectorAll(".stat[data-scope]")) {
    button.setAttribute("aria-pressed", String(!research && state.scope === button.dataset.scope));
  }
}

function setView(view) {
  state.view = view;
  updateView();
  rememberSelection();
  if (view === "research") {
    void loadIndexStatus();
    if (!state.settings) void api("/api/rag/settings").then(updateModelChoices).catch(() => {});
  }
}

function selectScope(value) {
  if (!scopes.has(value)) return;
  state.scope = value;
  state.scopeExplicit = true;
  state.offset = 0;
  scopeFilter.value = value;
  setView("catalog");
  updateScopeControls(selected());
  if (!byId("catalog-recovery").hidden) void loadIndexStatus();
  void loadArticles(true);
}

function applyDefaultScope() {
  if (!state.scopeExplicit) state.scope = selected()?.full_text_count > 0 ? "full_text" : "all";
  scopeFilter.value = state.scope;
}

function updateScopeControls(library) {
  const counts = {
    all: library?.article_count, full_text: library?.full_text_count, metadata_only: library?.metadata_only_count,
    pdf: library?.pdf_count, no_pdf: library?.no_pdf_count, parse_pending: library?.parse_waiting,
    parse_failed: library?.parse_failed, download_failed: library?.download_failed_count, excluded: library?.excluded_count,
  };
  for (const option of byId("scope-filter").options) {
    option.dataset.label ??= option.textContent;
    const count = counts[option.value];
    option.textContent = option.dataset.label + (Number.isInteger(count) ? " · " + numberFormat.format(count) : "");
  }
  byId("catalog-recovery").hidden = !["metadata_only", "no_pdf", "download_failed"].includes(state.scope);
  byId("retry-library").disabled = state.resolving.has(state.id + "/");
  updateView();
}

function resetResearch() {
  state.questionRequest++;
  byId("research-question").value = "";
  byId("research-result").hidden = true;
  byId("question-form").querySelectorAll("button").forEach((button) => { button.disabled = false; });
  show(byId("question-status"), "");
  show(byId("rag-issue"), "");
  byId("rag-status").textContent = "";
  byId("stat-indexed").textContent = "—";
  byId("connect-index").hidden = true;
  byId("discovery-status").textContent = "Поиск доступных копий для статей библиотеки.";
}

const indexStates = {
  idle: "Ожидание новых текстов", indexing: "Идёт индексация",
  waiting: "Ожидание сервера моделей", unconfigured: "Требуется настройка индекса",
};

async function loadIndexStatus() {
  const libraryId = state.id;
  if (!libraryId) return;
  const request = ++state.statusRequest;
  try {
    const result = await api(libraryPath(libraryId) + "/rag/status");
    if (libraryId !== state.id || request !== state.statusRequest) return;
    byId("stat-indexed").textContent = Number.isInteger(result.indexed) ? numberFormat.format(result.indexed) : "—";
    byId("connect-index").hidden = result.qdrant_url !== "";
    const details = [];
    const discovery = result.discovery;
    if (discovery) {
      const counters = [
        ["unchecked", "ожидают проверки"], ["queued", "в очереди"], ["checking", "проверяются"],
        ["recovered", "найдено копий"], ["no_copy", "копия пока не найдена"], ["failed", "ошибок"],
      ].filter(([key]) => discovery[key] > 0);
      byId("discovery-status").textContent = counters.map(([key, label]) =>
        label + " · " + numberFormat.format(discovery[key])).join(" / ") || "Очередь поиска доступных копий пуста.";
    }
    if (Number.isInteger(result.indexed) && Number.isInteger(result.full_text)) {
      details.push("В индексе " + numberFormat.format(result.indexed) + " из " + numberFormat.format(result.full_text) + " полных текстов");
    }
    if (Number.isInteger(result.pending) && result.pending > 0) details.push("в очереди " + numberFormat.format(result.pending));
    if (indexStates[result.state]) details.push(indexStates[result.state]);
    byId("rag-status").textContent = details.join(" · ");
    show(byId("rag-issue"), result.last_error || "");
  } catch (error) {
    if (libraryId === state.id && request === state.statusRequest) show(byId("rag-issue"), error.message);
  }
}

function restoreButton(article, globalScope) {
  const libraryId = state.id;
  const button = make("button", "button", globalScope ? "Снять глобальное исключение" : "Снять исключение библиотеки");
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(scopedArticlePath(libraryId, article.id) + "/restore", sendJson("POST", { global_scope: globalScope }));
      if (state.id !== libraryId) return;
      showNotice("Исключение снято.");
      await refreshLibraries();
      await loadArticles();
    } catch (error) {
      if (state.id === libraryId) showNotice(error.message, true);
    } finally { button.disabled = false; }
  });
  return button;
}

function showExclusion(article, purgeOnly = false) {
  if (!article) return;
  state.exclusion = { article, libraryId: state.id };
  byId("exclusion-form").reset();
  byId("exclusion-title").textContent = article.title || article.doi || "Статья";
  byId("exclusion-heading").textContent = purgeOnly ? "Удалить файлы статьи" : "Исключить статью";
  byId("exclusion-help").textContent = purgeOnly ? "Карточка останется в чёрном списке. Для повторной работы с текстом понадобится снова получить файл."
    : "Статья исчезнет из каталога и из поиска по текстам. Вернуть её можно в чёрном списке.";
  byId("confirm-exclude").textContent = purgeOnly ? "Удалить файлы и текст" : "Исключить";
  byId("exclusion-scope").value = purgeOnly && article.global_excluded ? "global" : "library";
  byId("exclusion-scope").disabled = purgeOnly;
  byId("purge-files").checked = purgeOnly;
  byId("purge-files").disabled = purgeOnly;
  byId("purge-confirmation").hidden = !purgeOnly;
  byId("confirm-purge").required = purgeOnly;
  byId("confirm-exclude").disabled = false;
  show(byId("exclusion-error"), "");
  modal("exclusion-dialog");
}

async function excludeArticle(event) {
  event.preventDefault();
  const current = state.exclusion;
  if (!current) return;
  const { article, libraryId: id } = current;
  const purge = byId("purge-files").checked;
  if (purge && !byId("confirm-purge").checked) return;
  byId("confirm-exclude").disabled = true;
  try {
    await api(scopedArticlePath(id, article.id) + "/exclude",
      sendJson("POST", { global_scope: byId("exclusion-scope").value === "global", purge }));
    if (id !== state.id) return;
    if (current === state.exclusion) {
      byId("exclusion-dialog").close();
      if (state.article === article && byId("article-dialog").open) byId("article-dialog").close();
    }
    resetResearch();
    showNotice(purge ? "Статья исключена. Её сохранённые файлы и текст удалены." : "Статья добавлена в чёрный список.");
    await refreshLibraries();
    await loadArticles();
  } catch (error) {
    if (current === state.exclusion) show(byId("exclusion-error"), error.message);
  } finally { byId("confirm-exclude").disabled = false; }
}

async function resolveFullText(article) {
  const id = state.id;
  const key = id + "/" + (article?.id || "");
  if (state.resolving.has(key)) return;
  const button = byId(article ? "retry-article" : "retry-library");
  const path = article ? scopedArticlePath(id, article.id) : libraryPath(id);
  state.resolving.add(key);
  button.disabled = true;
  try {
    const result = await api(path + "/resolve", { method: "POST" });
    if (id !== state.id) return;
    const message = result.queued ? "Статей поставлено в очередь поиска копий · " + numberFormat.format(result.queued)
      : "Нет статей, которым требуется поиск файла.";
    if (article && article === state.article) show(byId("attach-status"), message, "secondary");
    else showNotice(message);
    await refreshLibraries();
    await loadIndexStatus();
    await loadArticles();
  } catch (error) {
    if (id !== state.id) return;
    if (article && article === state.article) show(byId("attach-status"), error.message, "form-error");
    else showNotice(error.message, true);
  } finally {
    state.resolving.delete(key);
    if (id === state.id && (!article || article === state.article)) button.disabled = false;
  }
}

let reader = null;

function readerMode(mode) {
  if (!reader) return;
  reader.mode = mode;
  for (const key of ["text", "pdf", "image"]) {
    byId("reader-" + key + "-button").setAttribute("aria-pressed", String(mode === key));
  }
  byId("reader-frame").hidden = mode !== "pdf";
  byId("reader-extracted").hidden = mode !== "text";
  byId("reader-image").hidden = mode !== "image";
  byId("reader-more").hidden = mode !== "text" || reader.offset === null;
  if (mode === "pdf" && !byId("reader-frame").hasAttribute("src")) byId("reader-frame").src = reader.pdf;
  show(byId("reader-status"), mode === "text" ? reader.status : "");
}

async function loadReaderText(append = false) {
  const current = reader;
  const query = new URLSearchParams({ offset: append ? current.offset : 0 });
  if (current.page) query.set("page", current.page);
  byId("reader-more").disabled = true;
  try {
    const response = await api(scopedArticlePath(current.library, current.source.article_id) + "/passages?" + query);
    if (current !== reader) return;
    const target = byId("reader-extracted");
    if (!append) target.replaceChildren();
    for (const part of response.passages) {
      if (!current.page && part.page) target.append(make("h3", "", "Страница " + part.page));
      target.append(make("p", "", part.text));
    }
    current.offset = response.next_offset;
    current.status = target.childElementCount ? "" : "Текст для этой страницы не извлечён.";
  } catch (error) {
    if (current !== reader) return;
    current.status = "Не удалось получить текст. " + error.message;
  } finally {
    if (current === reader) {
      byId("reader-more").disabled = false;
      readerMode(current.mode);
    }
  }
}

function openSource(source) {
  const page = Number.isSafeInteger(source.page) && source.page > 0 ? source.page : null;
  const pdf = source.pdf_url ? articlePath(source.article_id) + "/pdf?inline=1" + (page ? "#page=" + page : "") : "";
  const image = page && source.page_image_url ? articlePath(source.article_id) + "/pages/" + page + ".png" : "";
  reader = { source, library: state.id, page, pdf, offset: null, status: "Загружаем текст источника…" };
  byId("reader-title").textContent = source.title || "Источник";
  byId("reader-page").textContent = [page && "Страница " + numberFormat.format(page), source.section,
    source.doi && "DOI " + source.doi].filter(Boolean).join(" · ");
  const original = byId("reader-link");
  original.hidden = !pdf;
  if (pdf) original.href = pdf;
  else original.removeAttribute("href");
  byId("reader-pdf-button").hidden = !pdf;
  byId("reader-image-button").hidden = !image;
  byId("reader-frame").removeAttribute("src");
  byId("reader-extracted").replaceChildren(make("p", "", source.text));
  if (image) {
    byId("reader-image").alt = "Страница " + page + " оригинала статьи";
    byId("reader-image").src = image;
  }
  readerMode(image ? "image" : "text");
  modal("reader-dialog");
  void loadReaderText();
}

function renderResearch(answer, sources, model) {
  const list = byId("citations");
  list.replaceChildren();
  const entries = new Map();
  for (const [index, source] of sources.entries()) {
    const number = source.number || index + 1;
    const card = make("li", "citation");
    card.tabIndex = -1;
    entries.set(String(number), card);
    const top = make("div", "citation-heading");
    top.append(make("span", "citation-number", "[" + number + "]"), make("h4", "", source.title || "Статья"));
    card.append(top);
    const meta = [source.doi && "DOI " + source.doi, source.section, source.page && "с. " + source.page].filter(Boolean);
    if (meta.length) card.append(make("p", "secondary", meta.join(" · ")));
    card.append(make("blockquote", "", source.text));
    if (Number.isInteger(source.article_id)) {
      const open = make("button", "text-button", source.page ? "Открыть страницу " + source.page : "Прочитать источник");
      open.type = "button";
      open.addEventListener("click", () => openSource(source));
      card.append(open);
    }
    list.append(card);
  }
  const output = byId("research-answer");
  output.replaceChildren();
  const fragments = (answer || "").split(/(\[\d+\])/g);
  for (const fragment of fragments) {
    const target = /^\[(\d+)\]$/.exec(fragment);
    const card = target && entries.get(target[1]);
    if (!card) { output.append(document.createTextNode(fragment)); continue; }
    const jump = make("button", "citation-jump", fragment);
    jump.type = "button";
    jump.setAttribute("aria-label", "К источнику " + target[1]);
    jump.addEventListener("click", () => { card.focus(); card.scrollIntoView({ block: "nearest" }); });
    output.append(jump);
  }
  byId("answer-heading").hidden = !answer;
  output.hidden = !answer;
  show(byId("answer-model"), model ? "Модель ответа · " + model : "");
  byId("sources-panel").hidden = !sources.length;
  byId("research-result").hidden = !answer && !sources.length;
}

async function askLibrary(event) {
  event.preventDefault();
  const query = byId("research-question").value.trim();
  if (!query || !state.id) return;
  const request = ++state.questionRequest;
  const action = event.submitter?.value === "search" ? "search" : "ask";
  const payload = { query };
  if (action === "ask" && byId("question-model").value) payload.model = byId("question-model").value;
  const buttons = byId("question-form").querySelectorAll("button");
  for (const button of buttons) button.disabled = true;
  byId("research-result").hidden = true;
  show(byId("question-status"), action === "ask" ? "Ищем источники и готовим ответ…" : "Ищем фрагменты…", "secondary");
  try {
    const result = await api(libraryPath(state.id) + "/rag/" + action, sendJson("POST", payload));
    if (request !== state.questionRequest) return;
    const sources = Array.isArray(result) ? result : result.sources || [];
    renderResearch(result.answer, sources, result.model);
    show(byId("question-status"), sources.length || result.answer ? "" : "В индексе этой библиотеки подходящих фрагментов пока нет.", "secondary");
  } catch (error) {
    if (request === state.questionRequest) show(byId("question-status"), error.message, "form-error");
  } finally {
    if (request === state.questionRequest) for (const button of buttons) button.disabled = false;
  }
}

async function reparseArticle() {
  const article = state.article;
  if (!article) return;
  const id = state.id;
  const button = byId("reparse-article");
  button.disabled = true;
  try {
    await api(scopedArticlePath(id, article.id) + "/reparse", { method: "POST" });
    if (state.article === article) {
      article.parse_status = "pending";
      article.parse_error = "";
      setPreviewFiles(article);
      show(byId("attach-status"), "Файл снова в очереди разбора MinerU.", "secondary");
    }
    if (id === state.id) await Promise.all([refreshLibraries(), loadArticles()]);
  } catch (error) {
    if (state.article === article) show(byId("attach-status"), error.message, "form-error");
  } finally { button.disabled = false; }
}

function updateModelChoices(settings) {
  state.settings = settings;
  const list = byId("question-model");
  const previous = list.value;
  list.replaceChildren(new Option("По настройкам", ""));
  const enabled = settings.profiles.filter((profile) => profile.enabled);
  const names = new Set(enabled.map((profile) => profile.chat_model).filter(Boolean));
  if (settings.chat_model) names.add(settings.chat_model);
  for (const name of names) list.add(new Option(name, name));
  if (names.has(previous)) list.value = previous;
}

function refreshModelSuggestions() {
  const rows = [...byId("model-profiles").children];
  for (const role of ["chat", "embedding"]) {
    const values = rows.flatMap((row) => [
      row.querySelector('[name="' + role + '_model"]').value.trim(),
      ...Array.from(row.querySelector("datalist")?.options || [], (option) => option.value),
    ]);
    byId(role + "-choices").replaceChildren(...Array.from(new Set(values.filter(Boolean)), (value) => new Option(value, value)));
  }
}

let profileSequence = 0;
function addProfile(profile, savedIndex) {
  const row = byId("profile-template").content.firstElementChild.cloneNode(true);
  profile ||= {
    name: "", base_url: "", embedding_model: "", chat_model: "", priority: 10, enabled: true,
    concurrency: 1, chat_mode: "openai", max_tokens: 768,
    days: [0, 1, 2, 3, 4, 5, 6], start: "", end: "", timezone: "Europe/Moscow",
  };
  const field = (key) => row.querySelector('[name="' + key + '"]');
  for (const key of ["name", "base_url", "embedding_model", "chat_model", "start", "end", "timezone", "priority", "concurrency"]) {
    field(key).value = profile[key] ?? (key === "concurrency" ? 1 : "");
  }
  field("enabled").checked = profile.enabled;
  field("chat_mode").value = profile.chat_mode || "openai";
  field("max_tokens").value = profile.max_tokens ?? 768;
  const days = row.querySelector(".days");
  ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].forEach((name, day) => {
    const label = make("label");
    const checkbox = make("input");
    checkbox.type = "checkbox";
    checkbox.value = day;
    checkbox.checked = profile.days.includes(day);
    label.append(checkbox, make("span", "", name));
    days.append(label);
  });
  const schedule = days.closest("details");
  schedule.open = Boolean(profile.start || profile.end || profile.days.length !== 7);
  row.querySelector(".remove-profile").addEventListener("click", () => {
    row.remove();
    refreshModelSuggestions();
  });
  const discovery = row.querySelector(".discover-models");
  const status = row.querySelector(".discovered-models");
  const choices = make("datalist");
  choices.id = "profile-models-" + (++profileSequence);
  row.append(choices);
  for (const role of ["embedding_model", "chat_model"]) field(role).setAttribute("list", choices.id);
  const updateDiscovery = () => {
    discovery.disabled = savedIndex === undefined || field("base_url").value.trim() !== profile.base_url;
    status.textContent = discovery.disabled ? "Для запроса списка сначала сохрани адрес сервера." : "";
  };
  updateDiscovery();
  field("base_url").addEventListener("input", updateDiscovery);
  discovery.addEventListener("click", async () => {
    discovery.disabled = true;
    status.textContent = "Запрашиваем список моделей…";
    try {
      const result = await api("/api/rag/profiles/" + savedIndex + "/models");
      choices.replaceChildren(...result.models.map((name) => new Option(name, name)));
      status.textContent = result.models.length ? "Доступно моделей · " + result.models.length + ". Выбери нужные в полях выше."
        : "Сервер вернул пустой список.";
      refreshModelSuggestions();
    } catch (error) { status.textContent = error.message; }
    finally { discovery.disabled = field("base_url").value.trim() !== profile.base_url; }
  });
  row.addEventListener("change", refreshModelSuggestions);
  byId("model-profiles").append(row);
  return row;
}

function readProfile(row) {
  const field = (key) => row.querySelector('[name="' + key + '"]');
  const profile = {
    enabled: field("enabled").checked, priority: Number(field("priority").value), concurrency: Number(field("concurrency").value),
    chat_mode: field("chat_mode").value, max_tokens: Number(field("max_tokens").value),
    days: [...row.querySelectorAll(".days input:checked")].map((checkbox) => Number(checkbox.value)),
  };
  for (const key of ["name", "base_url", "embedding_model", "chat_model", "start", "end", "timezone"]) {
    profile[key] = field(key).value.trim();
  }
  return profile;
}

let modelsRequest = 0;
async function showModels() {
  const request = ++modelsRequest;
  const dialog = modal("models-dialog");
  byId("models-fields").disabled = true;
  byId("save-models").disabled = true;
  byId("model-profiles").replaceChildren();
  show(byId("models-status"), "Загрузка конфигурации NAS…", "secondary");
  try {
    const settings = await api("/api/rag/settings");
    if (!dialog.open || request !== modelsRequest) return;
    updateModelChoices(settings);
    byId("embedding-model").value = settings.embedding_model;
    byId("chat-model").value = settings.chat_model;
    byId("qdrant-url").value = settings.qdrant_url || "";
    settings.profiles.forEach((profile, index) => addProfile(profile, index));
    refreshModelSuggestions();
    byId("models-fields").disabled = false;
    byId("save-models").disabled = false;
    if (!settings.qdrant_url) byId("qdrant-url").focus();
    show(byId("models-status"), "");
  } catch (error) {
    if (request === modelsRequest) show(byId("models-status"), error.message, "form-error");
  }
}

async function saveModels(event) {
  event.preventDefault();
  const payload = {
    embedding_model: byId("embedding-model").value.trim(),
    chat_model: byId("chat-model").value.trim(),
    qdrant_url: byId("qdrant-url").value.trim(),
    profiles: [...byId("model-profiles").children].map(readProfile),
  };
  const button = byId("save-models");
  const request = ++modelsRequest;
  button.disabled = true;
  show(byId("models-status"), "Сохраняем конфигурацию…", "secondary");
  try {
    const settings = await api("/api/rag/settings", sendJson("PUT", payload));
    if (request !== modelsRequest) return;
    updateModelChoices(settings);
    byId("models-dialog").close();
    showNotice("Настройки моделей сохранены на NAS.");
    if (state.id) void loadIndexStatus();
  } catch (error) {
    if (request === modelsRequest) show(byId("models-status"), error.message, "form-error");
  } finally { if (request === modelsRequest) button.disabled = false; }
}

byId("reparse-article").addEventListener("click", reparseArticle);
byId("research-settings").addEventListener("click", showModels);
byId("welcome-connect").addEventListener("click", showModels);
byId("connect-index-button").addEventListener("click", showModels);
byId("open-model-settings").addEventListener("click", showModels);
byId("models-form").addEventListener("submit", saveModels);
byId("add-profile").addEventListener("click", () => addProfile().querySelector('[name="name"]').focus());
byId("catalog-view").addEventListener("click", () => setView("catalog"));
byId("indexed-stat").addEventListener("click", () => setView("research"));
byId("research-view").addEventListener("click", () => setView("research"));
for (const button of document.querySelectorAll(".stat[data-scope]")) {
  button.addEventListener("click", () => selectScope(button.dataset.scope));
}
byId("question-form").addEventListener("submit", askLibrary);
for (const mode of ["text", "pdf", "image"]) {
  byId("reader-" + mode + "-button").addEventListener("click", () => readerMode(mode));
}
byId("reader-image").addEventListener("error", () => {
  if (!reader) return;
  byId("reader-image-button").hidden = true;
  readerMode("text");
});
byId("reader-more").addEventListener("click", () => { if (reader?.offset != null) void loadReaderText(true); });
byId("reader-dialog").addEventListener("close", () => {
  reader = null;
  byId("reader-frame").removeAttribute("src");
  byId("reader-image").removeAttribute("src");
});
byId("exclude-article").addEventListener("click", () => showExclusion(state.article));
byId("exclusion-dialog").addEventListener("close", () => { state.exclusion = null; });
byId("purge-files").addEventListener("change", (event) => {
  byId("purge-confirmation").hidden = !event.target.checked;
  byId("confirm-purge").required = event.target.checked;
  byId("confirm-purge").checked = false;
});
byId("exclusion-form").addEventListener("submit", excludeArticle);
byId("retry-article").addEventListener("click", () => state.article && resolveFullText(state.article));
byId("retry-library").addEventListener("click", () => resolveFullText(null));

updateView();
if (state.view === "research") void api("/api/rag/settings").then(updateModelChoices).catch(() => {});
void loadSourceTypes().catch(() => null).then(refreshLibraries);
setInterval(() => {
  if (document.visibilityState === "visible") {
    void refreshLibraries();
    void loadIndexStatus();
  }
}, 8000);
