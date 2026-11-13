'use strict';

let DATA, SOURCE_MAP, route = 'hearing', currentParams = new URLSearchParams(), lastSourceTrigger;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const norm = (s) => String(s || '').toLocaleLowerCase('ru').replace(/ё/g, 'е').replace(/(\d)[\s\u00a0](?=\d)/g, '$1').replace(/(\d),(\d)/g, '$1.$2').replace(/\s+/g, ' ').trim();
const array = (v) => v == null ? [] : Array.isArray(v) ? v : [v];
const paragraphs = (v) => array(v).filter(Boolean).map((s) => '<p>' + esc(s) + '</p>').join('');
const list = (v) => array(v).filter(Boolean).length ? '<ul class="detail-list">' + array(v).filter(Boolean).map((s) => '<li>' + esc(s) + '</li>').join('') + '</ul>' : '';
const routeURL = (name, values = {}) => {
  const p = new URLSearchParams(values);
  return '#' + name + (p.size ? '?' + p.toString() : '');
};
const sourceLabel = (s) => 'Том ' + s.volume + ', стр. ' + s.page + (s.case_sheet != null ? ' · л.д. ' + s.case_sheet : '');
const sourceLink = (id) => {
  const s = SOURCE_MAP.get(id);
  if (!s) return '';
  const p = Object.fromEntries(currentParams);
  p.source = id;
  return '<a class="source-link" href="' + esc(routeURL(route, p)) + '" data-source="' + esc(id) + '">' + esc(sourceLabel(s)) + ' ↗</a>';
};
const sourceLinks = (ids) => '<div class="source-links">' + [...new Set(array(ids))].map(sourceLink).join('') + '</div>';
const quoteHTML = (q) => '<blockquote class="quote">' + esc(q.text) + '</blockquote>' +
  (q.note ? '<p class="quote-note">' + esc(q.note) + '</p>' : '') +
  sourceLinks(q.source_page_ids || q.sources);
const questionText = (q) => typeof q === 'string' ? q : q.question || q.text || '';
const questionList = (v) => '<ol>' + array(v).map((q) => '<li>' +
  (typeof q === 'object' && q.addressee ? '<span class="asked-of">' + esc(q.addressee) + '</span>' : '') +
  esc(questionText(q)) + '</li>').join('') + '</ol>';
const badge = (c) => {
  if (c.kind === 'confirmed_text_difference') return '<span class="badge">Расхождение в источниках</span>';
  if (c.kind === 'calculation') return '<span class="badge note">Сверка расчёта</span>';
  if (c.kind === 'correction') return '<span class="badge gap">Поправка к прежнему выводу</span>';
  return '<span class="badge gap">Нужно уточнить</span>';
};

function questionCard(c, i, open = false) {
  const branches = [['yes', 'Если ответит «да»'], ['no', 'Если ответит «нет»'], ['dont_remember', 'Если не помнит']];
  return '<article class="card question-card" id="' + esc(c.slug) + '" data-card="' + esc(c.slug) + '">' +
    '<div class="card-header"><span class="card-index">Вопросы · ' + esc(c.addressee) + '</span>' + badge(c) + '</div>' +
    '<h2>' + esc(c.title) + '</h2><p class="card-summary">' + esc(c.summary) + '</p>' +
    '<div class="question-main">' + questionList(c.main_questions) + '</div>' +
    (c.purpose ? '<p class="purpose">' + esc(c.purpose) + '</p>' : '') +
    '<details' + (open ? ' open' : '') + '><summary>Цитаты, страницы и уточняющие вопросы</summary>' +
    c.quotes.map(quoteHTML).join('') +
    branches.map(([key, title]) => c.followups && array(c.followups[key]).length ? '<div class="branch"><h3>' + title + '</h3>' + questionList(c.followups[key]) + '</div>' : '').join('') +
    (c.counterpoints.length ? '<h3>Что может объяснять расхождение</h3>' + list(c.counterpoints) : '') +
    (c.limits.length ? '<h3>Что важно учесть</h3>' + list(c.limits) : '') +
    (c.asking_conditions.length ? '<h3>Условия для этого вопроса</h3>' + list(c.asking_conditions) : '') +
    '</details></article>';
}

function noteCard(c) {
  return '<article class="card" id="' + esc(c.slug || c.id) + '">' +
    '<div class="card-header">' + (c.kind ? badge(c) : '') + '</div><h2>' + esc(c.title) + '</h2>' +
    paragraphs(c.body) +
    (array(c.questions).length ? '<div class="question-main">' + questionList(c.questions) + '</div>' : '') +
    array(c.quotes).map(quoteHTML).join('') +
    sourceLinks(c.source_page_ids) + '</article>';
}

function hero(eyebrow, title, intro) {
  return '<section class="hero"><div class="eyebrow">' + esc(eyebrow) + '</div><h1>' + esc(title) + '</h1><p class="lead">' + esc(intro) + '</p></section>';
}

function renderQuestions(hearingOnly) {
  let cards = DATA.questions;
  const person = currentParams.get('person') || 'Все';
  if (hearingOnly) cards = cards.filter((c) => norm(c.addressee).includes(norm(DATA.ui.hearing_person_match))).sort((a,b) => a.hearing_priority - b.hearing_priority);
  else if (person !== 'Все') cards = cards.filter((c) => norm(c.addressee).includes(norm(person)));
  const filter = currentParams.get('filter') || '';
  if (filter) cards = cards.filter((c) => norm(JSON.stringify(c)).includes(norm(filter)));
  let html = hero('Подготовка с представителем',
    hearingOnly ? DATA.ui.hearing_title : 'Вопросы по участникам',
    hearingOnly ? 'Вопросы о документах, счетах и разных версиях событий. Цитаты можно сверить с исходными страницами.' :
      DATA.ui.questions_intro);
  if (hearingOnly) {
    html += '<div class="metrics"><div class="metric"><strong>' + cards.length + '</strong><span>' + esc(DATA.ui.hearing_count_label) + '</span></div><div class="metric"><strong>' +
      DATA.questions.length + '</strong><span>блоков всего</span></div><div class="metric"><strong>' +
      DATA.sources.length + '</strong><span>исходных страниц</span></div></div>';
  } else {
    html += '<div class="filters" aria-label="Участник">' + DATA.ui.person_filters.map((name) =>
      '<button type="button" data-person="' + name + '" class="' + (name === person ? 'selected' : '') + '">' + name + '</button>').join('') + '</div>';
  }
  if (!hearingOnly) html += '<input class="local-search" id="question-filter" type="search" aria-label="Поиск в вопросах" placeholder="Сузить список: УПД, подпись, счёт…" value="' + esc(filter) + '">';
  html += '<details class="question-rules"><summary>Что учесть перед допросом</summary>' +
    paragraphs(DATA.question_rules.global_note) + list(DATA.question_rules.global_rules) + '</details>' +
    '<div class="section-top"><h2>Вопросы и подтверждающие цитаты</h2><span>' + cards.length + ' блоков</span></div>';
  html += cards.length ? cards.map((c, i) => questionCard(c, i, currentParams.get('card') === c.slug)).join('') :
    '<p class="empty">В этой подборке совпадений нет. Попробуйте другое имя или более короткий запрос.</p>';
  return html;
}

function renderSection(name) {
  const s = DATA.sections.find((x) => x.id === name);
  if (!s) return hero('Разбор материалов', 'Раздел готовится', 'Основные вопросы и цитируемые страницы уже доступны в меню.');
  let html = hero('Разбор материалов', s.title, s.intro) + array(s.cards).map(noteCard).join('');
  if (name === 'overview' && DATA.people.length) {
    html += '<div class="section-top"><h2>Участники и их роль в материалах</h2></div><div class="people-grid">' +
      DATA.people.map((p) => '<article class="card person"><h3>' + esc(p.name) + '</h3><span class="badge note">' +
        esc(p.role) + '</span>' + paragraphs(p.description) + sourceLinks(p.source_page_ids) + '</article>').join('') + '</div>';
  }
  if (name === 'next' && DATA.sections.some((x) => x.id === 'procedure')) {
    html += '<div class="guide"><a href="#procedure">Условия допроса и процессуальные поправки</a></div>';
  }
  return html;
}

function renderSources() {
  const volume = currentParams.get('volume') || 'Все';
  const rows = DATA.sources.filter((s) => volume === 'Все' || String(s.volume) === volume);
  return hero('Проверка цитат', 'Исходные страницы', 'Полные страницы из документов, на которые ссылается разбор. Открывайте их рядом с вопросом или отдельно.') +
    '<div class="guide"><p>«Стр.» означает страницу полного PDF-тома. «Л.д.» показан только там, где номер листа дела подтверждён. По номеру PDF он не вычислялся.</p></div>' +
    '<div class="filters" aria-label="Том">' + ['Все', ...new Set(DATA.sources.map((s) => String(s.volume)))].map((v) =>
      '<button type="button" data-volume="' + v + '" class="' + (v === volume ? 'selected' : '') + '">' +
      (v === 'Все' ? 'Все тома' : 'Том ' + v) + '</button>').join('') + '</div>' +
    '<div class="plain-list">' + rows.map((s) => '<a class="source-row" data-source="' + s.id + '" href="' +
      esc(routeURL('sources', {volume,source:s.id})) + '"><span><strong>' + esc(sourceLabel(s)) +
      '</strong><small>' + esc(s.used_for || 'Полная страница источника') + '</small></span><span class="row-arrow" aria-hidden="true">↗</span></a>').join('') + '</div>';
}

function renderDownloads() {
  return hero('Материалы для чтения и печати', 'PDF и полные материалы', 'Основной разбор и четыре подборки исходных страниц. На сайте те же связи доступны через кнопки рядом с цитатами.') +
    '<div class="plain-list">' + DATA.reports.map((r) => '<a class="download-row" href="' + esc(r.url) +
      '" target="_blank" rel="noopener"><span><strong>' + esc(r.title) + '</strong><small>' + r.pages +
      ' стр. · ' + (r.bytes / 1024 / 1024).toLocaleString('ru', {maximumFractionDigits:1}) +
      ' МБ</small></span><span class="row-arrow" aria-hidden="true">↗</span></a>').join('') + '</div>' +
    '<div class="guide"><p>В PDF сохранён полный разбор, включая процессуальные условия и ограничения. Дату и повестку ближайшего заседания нужно подтвердить последними судебными документами.</p></div>';
}

function excerpt(text, needle) {
  text = String(text).replace(/\s+/g, ' ');
  const at = norm(text).indexOf(norm(needle));
  const start = Math.max(0, at - 85), end = Math.min(text.length, Math.max(at, 0) + 190);
  return (start ? '…' : '') + text.slice(start, end) + (end < text.length ? '…' : '');
}

function renderSearch() {
  const q = currentParams.get('q') || '';
  const words = norm(q).split(' ').filter(Boolean);
  const results = words.length ? DATA.search.filter((r) => words.every((w) => norm(r.text + ' ' + r.title).includes(w))) : [];
  return hero('Поиск по рабочей подборке', 'Результаты поиска', q ? 'Запрос: «' + q + '». Поиск охватывает разбор, вопросы и приведённые цитаты.' : 'Введите фамилию, сумму или фрагмент цитаты.') +
    '<p class="small muted">' + results.length + ' совпадений</p>' +
    (results.length ? results.map((r) => '<article class="card search-result"><span class="result-type">' +
      esc(r.section_title) + '</span><a href="' + esc(r.url) + '"><h2>' + esc(r.title) +
      '</h2></a><p class="card-summary">' + esc(excerpt(r.text, q)) + '</p></article>').join('') :
      '<p class="empty">Совпадений нет. Попробуйте фамилию без инициалов, часть слова или сумму без пробелов.</p>');
}

function parseRoute() {
  const hash = location.hash.slice(1) || 'hearing';
  const sep = hash.indexOf('?');
  route = sep < 0 ? hash : hash.slice(0, sep);
  route = DATA.ui.route_aliases[route] || route;
  currentParams = new URLSearchParams(sep < 0 ? '' : hash.slice(sep + 1));
  if (!['hearing','questions','overview','finance','corrections','sources','next','downloads','search','procedure'].includes(route)) route = 'hearing';
}

function render() {
  const oldRoute = route;
  parseRoute();
  const viewParams = new URLSearchParams(currentParams); viewParams.delete('source');
  const viewKey = route + '?' + viewParams.toString();
  if ($('main').dataset.viewKey === viewKey) { updateSource(); return; }
  $('main').dataset.viewKey = viewKey;
  document.querySelectorAll('[data-nav]').forEach((a) => a.classList.toggle('active', a.dataset.nav === route));
  $('global-search').value = route === 'search' ? currentParams.get('q') || '' : '';
  const titleMap = {hearing:DATA.ui.hearing_title,questions:'Все вопросы',overview:'Картина дела',finance:'Деньги и документы',corrections:'Что нужно исправить',sources:'Исходные страницы',next:'Чего пока не хватает',downloads:'PDF',search:'Поиск',procedure:'Условия допроса'};
  document.title = (titleMap[route] || 'Материалы') + ' · ' + DATA.ui.title_suffix;
  // Rendering only the current section keeps long quote lists out of the reading path.
  $('main').innerHTML = route === 'hearing' || route === 'questions' ? renderQuestions(route === 'hearing') :
    route === 'sources' ? renderSources() : route === 'downloads' ? renderDownloads() :
      route === 'search' ? renderSearch() : renderSection(route);
  updateSource();
  if (oldRoute !== route) window.scrollTo({top:0,behavior:'instant'});
  const focusCard = currentParams.get('card');
  if (focusCard && oldRoute !== route) setTimeout(() => document.getElementById(focusCard)?.scrollIntoView({block:'start'}), 0);
}

function updateSource() {
  const s = SOURCE_MAP.get(currentParams.get('source'));
  const open = !!s;
  $('source-panel').hidden = !open;
  $('source-backdrop').hidden = !open;
  document.body.classList.toggle('source-open', open);
  const modal = open && window.matchMedia('(max-width:1250px)').matches;
  document.querySelector('.shell').inert = modal;
  $('source-panel').setAttribute('role', modal ? 'dialog' : 'complementary');
  if (modal) $('source-panel').setAttribute('aria-modal','true');
  else $('source-panel').removeAttribute('aria-modal');
  if (!s) return;
  $('source-title').textContent = 'Том ' + s.volume + ', страница ' + s.page;
  $('source-locator').textContent = (s.case_sheet != null ? 'Лист дела: ' + s.case_sheet + '. ' : 'Лист дела не подтверждён. ') +
    (s.local_page !== s.page ? 'Страница внутри части PDF: ' + s.local_page + '. ' : '') +
    'Показана полная исходная страница.';
  $('source-pdf').href = s.pdf;
  $('source-jpg').href = s.image;
  $('source-image').src = s.image;
  $('source-image').alt = sourceLabel(s);
}

function changeParams(values) {
  const p = Object.fromEntries(currentParams);
  for (const [key, value] of Object.entries(values)) value == null ? delete p[key] : p[key] = value;
  location.hash = routeURL(route, p).slice(1);
}

function closeSource() {
  changeParams({source:null});
  setTimeout(() => lastSourceTrigger?.focus(), 0);
}

function toast(text) {
  $('toast').textContent = text;
  $('toast').hidden = false;
  setTimeout(() => $('toast').hidden = true, 2200);
}

document.addEventListener('click', (ev) => {
  const source = ev.target.closest('[data-source]');
  if (source) {
    ev.preventDefault();
    lastSourceTrigger = source;
    const p = Object.fromEntries(currentParams); p.source = source.dataset.source;
    history.pushState(null, '', routeURL(route, p));
    parseRoute(); updateSource();
    $('close-source').focus({preventScroll:true});
    return;
  }
  const person = ev.target.closest('[data-person]');
  if (person) changeParams({person:person.dataset.person,card:null});
  const volume = ev.target.closest('[data-volume]');
  if (volume) changeParams({volume:volume.dataset.volume,source:null});
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && !$('source-panel').hidden) closeSource();
});
document.addEventListener('change', (ev) => {
  if (ev.target.id === 'question-filter') changeParams({filter:ev.target.value,card:null});
});
$('search-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  location.hash = routeURL('search', {q:$('global-search').value.trim()}).slice(1);
});
$('close-source').addEventListener('click', closeSource);
$('source-backdrop').addEventListener('click', closeSource);
$('copy-source').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(location.href); toast('Ссылка на страницу скопирована'); }
  catch { toast('Скопируйте адрес из строки браузера'); }
});
window.addEventListener('hashchange', render);
window.addEventListener('popstate', render);

fetch('assets/content.json', {cache:'no-store'}).then((r) => {
  if (!r.ok) throw new Error('http');
  return r.json();
}).then((data) => {
  DATA = data;
  $('brand-subtitle').textContent = DATA.ui.brand_subtitle;
  document.querySelector('[data-nav="hearing"]').textContent = DATA.ui.hearing_title;
  SOURCE_MAP = new Map(DATA.sources.map((s) => [s.id,s]));
  render();
}).catch(() => {
  $('main').innerHTML = '<h1>Не удалось открыть разбор</h1><p>Обновите страницу. Если проблема сохраняется, можно открыть <a href="assets/reports/analysis.pdf">полный PDF</a>.</p>';
});
