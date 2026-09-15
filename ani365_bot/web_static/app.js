(() => {
  const telegram = window.Telegram && window.Telegram.WebApp;
  const root = document.getElementById('app');
  const initData = telegram ? telegram.initData : '';
  if (telegram) { telegram.ready(); telegram.expand(); }
  const state = { library: [], selected: null, episode: null, translation: null, quality: null, player: null, view: 'home', hls: null, hlsLoad: null, batchDownload: null };
  const esc = value => String(value ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (initData) headers['X-Telegram-Init-Data'] = initData;
    if (options.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    if (!response.ok) { const body = await response.json().catch(() => ({})); const detail = typeof body.detail === 'string' ? body.detail : Array.isArray(body.detail) ? body.detail.map(item => item?.msg || 'Некорректные данные.').join(' ') : 'Не удалось выполнить запрос.'; throw new Error(detail); }
    return response.status === 204 ? null : response.json();
  }
  const fail = error => { root.innerHTML = `<p class="error">${esc(error.message || error)}</p>`; };
  const navItems = [
    ['home', '⌂', 'Главная'], ['recommendations', '✦', 'Советы'], ['search', '⌕', 'Каталог'], ['hentai', '18+', 'Hentai365'], ['library', '▦', 'Моё'], ['downloads', '⇩', 'Загрузки'],
  ];
  function addChrome() {
    if (state.view === 'player' || root.querySelector('.app-topbar')) return;
    root.insertAdjacentHTML('afterbegin', `<header class="app-topbar"><button class="brand" data-nav="home" aria-label="На главную"><span class="brand-mark">▶</span><span>ani<b>365</b></span></button><button class="top-settings" data-nav="settings" aria-label="Настройки">⚙</button></header>`);
    root.insertAdjacentHTML('beforeend', `<nav class="bottom-nav" aria-label="Основная навигация">${navItems.map(([id, icon, label]) => `<button class="${state.view === id ? 'active' : ''}" data-nav="${id}"><span>${icon}</span>${label}</button>`).join('')}</nav>`);
    root.querySelectorAll('[data-nav]').forEach(button => {
      const target = button.dataset.nav;
      button.onclick = () => ({ home, recommendations, search, hentai, library, downloads, settings }[target] || home)();
    });
  }
  const chromeObserver = new MutationObserver(() => addChrome());
  chromeObserver.observe(root, { childList: true });
  function back() { telegram?.BackButton.hide(); home(); }
  function useBack() { telegram?.BackButton.show(); telegram?.BackButton.onClick(back); }
  root.addEventListener('click', async event => {
    if (event.target.id !== 'save-shiki-status' || !state.selected) return;
    try {
      await api(`/api/library/${state.selected.item.series_id}/shikimori-status`, {
        method: 'PATCH', body: JSON.stringify({status: root.querySelector('#shiki-status').value}),
      });
      details(state.selected.item.series_id);
    } catch (error) { fail(error); }
  });
  async function home() {
    try {
      state.view = 'home';
      telegram?.BackButton.hide();
      const data = await api('/api/library'); state.library = data.items;
      const resume = data.continue?.[0];
      const resumeText = resume?.playback ? `${Math.floor(resume.playback.position_seconds / 60)}:${String(Math.floor(resume.playback.position_seconds % 60)).padStart(2, '0')}` : '';
      root.innerHTML = `<div class="home-page">${resume ? `<section class="continue-hero"><img src="/assets/anime-night.webp" alt="" aria-hidden="true"><div class="hero-copy"><p class="eyebrow">Продолжить</p><p class="hero-meta">Серия ${esc(resume.last_watched_episode_number || '')}${resume.last_available_episode_number ? ` из ${esc(resume.last_available_episode_number)}` : ''}</p><h1>${esc(resume.title)}</h1>${resumeText ? `<p class="hero-description">Вы остановились на ${resumeText}. Плеер продолжит с того же места.</p>` : ''}<button class="action hero-action" data-series="${esc(resume.series_id)}">▶ Смотреть</button></div></section>` : `<section class="welcome-hero"><p class="eyebrow">Anime365</p><h1>Ваше аниме<br>в одном месте</h1><p>Найдите тайтл, добавьте его в библиотеку и продолжайте просмотр с того же места.</p><button class="action" id="search">Найти аниме</button></section>`}<section class="section-heading"><div><p class="eyebrow">Ваша очередь</p><h2>Смотрю сейчас</h2></div><button class="text-link" id="library">Все ›</button></section>${cards(data.items)}${data.new_episodes?.length ? `<section class="new-episodes"><span>✦</span><div><h2>Новые серии</h2><p>У ${esc(data.new_episodes.length)} ${data.new_episodes.length === 1 ? 'тайтла появилась новая серия.' : 'тайтлов появились новые серии.'}</p><button class="text-link" id="library-updates">Посмотреть обновления ›</button></div></section>` : ''}</div>`;
      const searchButton = document.getElementById('search'); if (searchButton) searchButton.onclick = search;
      const libraryButton = document.getElementById('library'); if (libraryButton) libraryButton.onclick = library;
      const updatesButton = document.getElementById('library-updates'); if (updatesButton) updatesButton.onclick = library;
      root.querySelectorAll('[data-series]').forEach(button => button.onclick = () => details(Number(button.dataset.series)));
    } catch (error) { fail(error); }
  }
  function cards(items, resume = false) {
    if (!items.length) return '<p class="empty">Пока здесь пусто.</p>';
    return `<section class="grid">${items.map(item => `<article class="card"><button data-series="${item.series_id}">${item.poster_url ? `<img class="poster" src="${esc(item.poster_url)}" alt="" loading="lazy">` : ''}<strong>${esc(item.title)}</strong><div class="meta">${esc(item.last_watched_episode_number || 0)} / ${esc(item.last_available_episode_number || '?')}${resume && item.playback ? ` · ${Math.floor(item.playback.position_seconds / 60)}:${String(Math.floor(item.playback.position_seconds % 60)).padStart(2,'0')}` : ''}${item.shikimori_status ? ` · ${esc(shikimoriStatus(item.shikimori_status))}` : ''}</div></button></article>`).join('')}</section>`;
  }
  function catalogCards(items, provider = 'anime365') {
    if (!items.length) return '<p class="empty">Ничего не найдено.</p>';
    const label = provider === 'hentai365' ? 'Hentai365' : 'Anime365';
    return `<section class="grid">${items.map(item => `<article class="card"><button data-catalog-series="${esc(item.series_id)}">${item.poster_url ? `<img class="poster" src="${esc(item.poster_url)}" alt="" loading="lazy">` : `<div class="poster catalog-placeholder">${label}</div>`}<strong>${esc(item.title)}</strong><div class="meta">${esc(item.year || 'Год не указан')}${item.series_type ? ` · ${esc(item.series_type)}` : ''}</div></button></article>`).join('')}</section>`;
  }
  const shikimoriStatus = status => ({planned:'Запланировано',watching:'Смотрю',rewatching:'Пересматриваю',completed:'Просмотрено',on_hold:'Отложено',dropped:'Брошено'}[status] || status);
  async function library() {
    try {
      state.view = 'library';
      const data = await api('/api/library');
      const groups = [['watching', 'Смотрю'], ['rewatching', 'Пересматриваю'], ['planned', 'Запланировано'], ['on_hold', 'Отложено'], ['dropped', 'Брошено'], ['completed', 'Просмотрено']];
      const hentaiItems = data.hentai_items || [];
      root.innerHTML = '<section class="page-heading"><p class="eyebrow">Личная коллекция</p><div><h1>Моё аниме</h1><span class="count-badge">' + esc(data.items?.length || 0) + ' тайтлов</span></div></section><label class="search-field"><span>⌕</span><input id="library-search" placeholder="Поиск по названию"></label><div id="library-groups"></div><button class="action secondary back">Назад</button>';
      useBack(); root.querySelector('.back').onclick = home;
      const search = root.querySelector('#library-search');
      const render = () => {
        const query = search.value.trim().toLocaleLowerCase();
        const sections = groups.map(([status, label]) => {
          const items = (data.groups?.[status] || []).filter(item => !query || String(item.title || '').toLocaleLowerCase().includes(query));
          return items.length ? `<details class="library-group" open><summary>${label} · ${items.length}</summary>${cards(items)}</details>` : '';
        }).join('') + (() => { const items = hentaiItems.filter(item => !query || String(item.title || '').toLocaleLowerCase().includes(query)); return items.length ? `<details class="library-group"><summary>Hentai365 · ${items.length}</summary>${cards(items)}</details>` : ''; })();
        root.querySelector('#library-groups').innerHTML = sections || '<p class="empty">Ничего не найдено.</p>';
        root.querySelectorAll('[data-series]').forEach(button => button.onclick = () => details(Number(button.dataset.series)));
      };
      search.addEventListener('input', render); render();
    } catch (error) { fail(error); }
  }
  function recommendationCards(items) {
    return `<section class="grid">${items.map(item => `<article class="card"><button data-recommendation="${esc(item.anime365_series_id)}">${item.poster_url ? `<img class="poster" src="${esc(item.poster_url)}" alt="" loading="lazy">` : '<div class="poster catalog-placeholder">Anime365</div>'}<strong>${esc(item.title)}</strong><div class="meta">${esc(item.year || 'Год не указан')}${item.series_type ? ` · ${esc(item.series_type)}` : ''}</div><p class="recommendation-reason">${esc(item.reason)}</p></button></article>`).join('')}</section>`;
  }
  async function recommendations(offset = 0, accumulated = []) {
    try {
      state.view = 'recommendations';
      const data = await api(`/api/recommendations?offset=${encodeURIComponent(offset)}`);
      const items = [...accumulated, ...(data.items || [])];
      const updated = data.generated_at ? new Date(Number(data.generated_at) * 1000).toLocaleString('ru-RU') : '';
      let content;
      if (!data.connected) content = '<p class="empty">Подключите Shikimori в настройках, чтобы получать персональные рекомендации.</p>';
      else if ((data.rated_completed || 0) < 5) content = `<p class="empty">Поставьте оценки хотя бы пяти просмотренным тайтлам в Shikimori. Сейчас: ${esc(data.rated_completed || 0)}.</p>`;
      else if (!items.length) content = '<p class="empty">Лента готовится. Она обновляется раз в неделю после импорта Shikimori.</p>';
      else content = `${recommendationCards(items)}${data.next_offset !== null ? '<button class="action secondary" id="more-recommendations">Показать ещё</button>' : ''}`;
      root.innerHTML = `<section class="page-heading"><p class="eyebrow">Для вас</p><h1>Рекомендации</h1><p class="meta">${updated ? `Обновлено: ${esc(updated)}.` : 'На основе оценок и жанров Shikimori.'}</p></section>${content}<button class="action secondary back">Назад</button>`;
      useBack(); root.querySelector('.back').onclick = home;
      const more = root.querySelector('#more-recommendations');
      if (more) more.onclick = () => recommendations(data.next_offset, items);
      root.querySelectorAll('[data-recommendation]').forEach(button => button.onclick = async () => {
        try {
          const item = items.find(row => Number(row.anime365_series_id) === Number(button.dataset.recommendation));
          if (!item) throw new Error('Рекомендация больше недоступна.');
          await api('/api/library', {method:'POST', body:JSON.stringify({
            series_id:item.anime365_series_id, title:item.title, year:item.year == null ? null : String(item.year),
            series_type:item.series_type, provider:'anime365',
          })});
          await details(item.anime365_series_id);
        } catch(error) { fail(error); }
      });
    } catch(error) { fail(error); }
  }
  async function search(provider = 'anime365') {
    state.view = provider === 'hentai365' ? 'hentai' : 'search';
    const label = provider === 'hentai365' ? 'Hentai365' : 'Anime365';
    root.innerHTML = `<section class="page-heading"><p class="eyebrow">${label}</p><h1>Каталог</h1></section><label class="search-field"><span>⌕</span><input id="query" placeholder="Название на русском или английском"></label><button class="action search-action" id="go">Найти</button><section id="results"></section><button class="action secondary back">Назад</button>`;
    useBack(); root.querySelector('.back').onclick = back;
    root.querySelector('#go').onclick = async () => { try { const endpoint = provider === 'hentai365' ? '/api/hentai/catalog' : '/api/catalog'; const data = await api(`${endpoint}?query=${encodeURIComponent(root.querySelector('#query').value)}`); root.querySelector('#results').innerHTML = catalogCards(data.items, provider); root.querySelectorAll('[data-catalog-series]').forEach(button => button.onclick = async () => { try { const item = data.items.find(row => Number(row.series_id) === Number(button.dataset.catalogSeries)); if (!item) throw new Error('Выбранный тайтл больше не найден. Повторите поиск.'); await api('/api/library', { method:'POST', body: JSON.stringify({ series_id:item.series_id, title:item.title, year:item.year == null ? null : String(item.year), series_type:item.series_type, provider }) }); await details(item.series_id); } catch(error) { fail(error); } }); } catch(error) { fail(error); } };
  }
  const hentai = () => search('hentai365');
  function backgroundImportText(item) {
    if (!item) return 'Фоновый импорт будет запущен после подключения.';
    const label = {queued:'в очереди',running:'выполняется',ready:'завершён',failed:'ожидает повторной попытки'}[item.state] || item.state;
    const count = item.imported_count ? ` · ${item.imported_count} тайтлов` : '';
    return `Фоновый импорт: ${label}${count}.`;
  }
  async function settings() { try { state.view = 'settings'; const data = await api('/api/shikimori/status'); root.innerHTML = `<section class="page-heading"><p class="eyebrow">Профиль</p><h1>Настройки</h1></section><section class="panel"><h2>Shikimori</h2><p class="meta">${data.connected ? 'Подключён' : data.configured ? 'Не подключён' : 'OAuth не настроен на сервере'}</p>${data.connected ? `<label><input type="checkbox" id="shiki-sync" ${data.sync_enabled ? 'checked' : ''}> Синхронизировать просмотренные серии</label><label><input type="checkbox" id="shiki-auto-complete" ${data.auto_complete ? 'checked' : ''}> После последней серии переводить в «Просмотрено»</label><p class="meta">Авто-перевод срабатывает только после завершения последней доступной серии и только при включённой синхронизации.</p><p class="meta">${esc(backgroundImportText(data.background_import))}</p><button class="action secondary" id="save-shiki-settings">Сохранить</button><button class="action" id="import">Импортировать список</button><button class="action secondary" id="disconnect">Отключить</button>` : data.configured ? '<button class="action" id="connect">Подключить Shikimori</button>' : ''}</section><button class="action secondary back">Назад</button>`; root.querySelector('.back').onclick = back; if (data.connected) { root.querySelector('#disconnect').onclick = async () => { await api('/api/shikimori', {method:'DELETE'}); settings(); }; root.querySelector('#save-shiki-settings').onclick = async () => { try { await api('/api/shikimori/settings', {method:'PATCH',body:JSON.stringify({sync_enabled:root.querySelector('#shiki-sync').checked,auto_complete:root.querySelector('#shiki-auto-complete').checked})}); settings(); } catch(error) { fail(error); } }; root.querySelector('#import').onclick = shikimoriImport; } if (data.configured && !data.connected) root.querySelector('#connect').onclick = async () => { const result = await api('/api/shikimori/connect', {method:'POST'}); location.href = result.authorization_url; }; } catch(error) { fail(error); } }
  async function shikimoriImport() {
    let current;
    try { current = await api('/api/shikimori/status'); } catch(error) { fail(error); return; }
    const selected = new Set(current.background_import?.statuses || ['watching', 'planned', 'completed', 'dropped']);
    root.innerHTML = `<h1>Импорт Shikimori</h1><p class="meta">Фоновый импорт сохранит список и продолжит работу после закрытия Mini App. Ручной вариант оставлен для немедленного обновления.</p><p class="meta">Статус берётся из Shikimori, а прогресс не уменьшается: используется максимум локального и Shikimori.</p><section class="panel">${[['watching','Смотрю'],['planned','Запланировано'],['rewatching','Пересматриваю'],['completed','Просмотрено'],['on_hold','Отложено'],['dropped','Брошено']].map(([value,label]) => `<label><input type="checkbox" value="${value}" ${selected.has(value) ? 'checked' : ''}> ${label}</label><br>`).join('')}</section><p class="meta" id="import-state">${esc(backgroundImportText(current.background_import))}</p><button class="action" id="start-background-import">Импортировать в фоне</button><button class="action secondary" id="start-import">Импортировать сейчас</button><button class="action secondary back">Назад</button>`;
    useBack(); root.querySelector('.back').onclick = settings;
    root.querySelector('#start-background-import').onclick = async () => {
      const button = root.querySelector('#start-background-import');
      try {
        const statuses = [...root.querySelectorAll('input:checked')].map(item => item.value);
        button.disabled = true; button.textContent = 'Ставим в очередь…';
        const data = await api('/api/shikimori/import/background', {method:'POST', body:JSON.stringify({statuses})});
        root.querySelector('#import-state').textContent = 'Фоновый импорт поставлен в очередь. Его можно не ждать.';
        button.textContent = data.background_import.state === 'running' ? 'Импорт выполняется' : 'Импорт в очереди';
      } catch(error) { fail(error); }
    };
    root.querySelector('#start-import').onclick = async () => {
      const button = root.querySelector('#start-import');
      try {
        const statuses = [...root.querySelectorAll('input:checked')].map(item => item.value);
        button.disabled = true; button.textContent = 'Импортируем список…';
        root.querySelector('#import-state').textContent = 'Сохраняем список и первые названия. Остальные будут догружены в фоне.';
        const data = await api('/api/shikimori/import', {method:'POST', body:JSON.stringify({statuses})});
        renderShikimoriImport(data);
      } catch(error) { fail(error); }
    };
  }
  function renderShikimoriImport(data) {
    const unmatched = data.unmatched || [];
    const total = Number(data.unmatched_total ?? unmatched.length);
    root.innerHTML = `<h1>Shikimori импортирован</h1><p class="meta">Импортировано: ${esc(data.imported)} · привязано: ${esc(data.linked)}</p>${data.metadata_refreshing ? '<p class="meta">Остальные названия и обложки догружаются в фоне. Обновите экран через несколько секунд.</p>' : ''}${unmatched.length ? `<button class="action" id="auto-link">Автопривязать по MAL ID</button><p class="meta">Осталось привязать: ${esc(total)}. Автоматически добавляются только точные совпадения ID.</p><div class="inline-actions"><input id="unmatched-query" value="${esc(data.query || '')}" placeholder="Название или Shikimori ID"><button class="action secondary" id="find-unmatched">Найти</button></div><h2>Нужно выбрать Anime365</h2>${unmatched.map(item => `<section class="panel"><strong>${esc(item.title)}</strong><p class="meta">${esc(shikimoriStatus(item.status))} · ${esc(item.episodes)} сер.</p><button class="action secondary" data-direct-link="${esc(item.external_rate_id)}">Проверить MAL ID</button><button class="action secondary" data-match="${esc(item.external_rate_id)}">Подобрать</button></section>`).join('')}${data.next_offset !== null && data.next_offset !== undefined ? '<button class="action secondary" id="more-unmatched">Показать ещё</button>' : ''}` : '<p class="empty">Все импортированные тайтлы привязаны.</p>'}<button class="action secondary back">К настройкам</button>`;
    root.querySelector('.back').onclick = settings;
    const autoLink = root.querySelector('#auto-link');
    if (autoLink) autoLink.onclick = async () => { try {
      autoLink.disabled = true; autoLink.textContent = 'Проверяем MAL ID…';
      const result = await api('/api/shikimori/imports/auto-link', {method:'POST'});
      const current = await api('/api/shikimori/imports?linked=false');
      renderShikimoriImport({imported:data.imported, linked:data.linked + result.linked,
                             unmatched:current.items, unmatched_total:current.total,
                             next_offset:current.next_offset});
    } catch(error) { fail(error); } };
    const findUnmatched = root.querySelector('#find-unmatched');
    if (findUnmatched) findUnmatched.onclick = async () => { try {
      const query = root.querySelector('#unmatched-query').value.trim();
      const found = await api('/api/shikimori/imports?linked=false&query=' + encodeURIComponent(query));
      renderShikimoriImport({...data, unmatched:found.items, unmatched_total:found.total,
                             next_offset:null, query:found.query || query});
    } catch(error) { fail(error); } };
    root.querySelectorAll('[data-direct-link]').forEach(button => button.onclick = async () => { try {
      button.disabled = true; button.textContent = 'Проверяем MAL ID…';
      await api('/api/shikimori/imports/' + encodeURIComponent(button.dataset.directLink) + '/auto-link', {method:'POST'});
      const current = await api('/api/shikimori/imports?linked=false');
      renderShikimoriImport({imported:data.imported, linked:data.linked + 1,
                             unmatched:current.items, unmatched_total:current.total,
                             next_offset:current.next_offset});
    } catch(error) { fail(error); } });
    const more = root.querySelector('#more-unmatched');
    if (more) more.onclick = async () => { try {
      const next = await api('/api/shikimori/imports?linked=false&offset=' + encodeURIComponent(data.next_offset));
      renderShikimoriImport({...data, unmatched:[...unmatched, ...next.items],
                             unmatched_total:next.total, next_offset:next.next_offset});
    } catch(error) { fail(error); } };
    root.querySelectorAll('[data-match]').forEach(button => button.onclick = () => shikimoriCandidates(button.dataset.match));
  }
  async function shikimoriCandidates(rateId) {
    try {
      const data = await api('/api/shikimori/imports/' + encodeURIComponent(rateId) + '/candidates');
      root.innerHTML = `<h1>Выберите Anime365</h1><p class="meta">${esc(data.rate.title)}${data.verified_mal_available ? ' · найдена надёжная привязка по MAL ID' : ' · выбор без MAL ID нужно подтвердить вручную'}</p>${data.candidates.length ? data.candidates.map(item => `<section class="panel"><strong>${esc(item.title)}</strong><p class="meta">${esc(item.year || '')} ${esc(item.type || '')} · ${esc(item.match_reason)}${item.verified_mal ? ' · проверено' : ''}</p><button class="action" data-link="${item.series_id}" data-verified="${item.verified_mal}">${item.verified_mal ? 'Привязать по MAL' : 'Выбрать вручную'}</button></section>`).join('') : '<p class="empty">Кандидатов не найдено. Добавьте тайтл через каталог и попробуйте позже.</p>'}<button class="action secondary back">Назад</button>`;
      root.querySelector('.back').onclick = () => shikimoriImport();
      root.querySelectorAll('[data-link]').forEach(button => button.onclick = async () => {
        try {
          await api('/api/shikimori/imports/' + encodeURIComponent(rateId) + '/link', {
            method:'POST', body:JSON.stringify({series_id:Number(button.dataset.link),
                                                 confirm_manual:button.dataset.verified !== 'true'}),
          });
          const imported = await api('/api/shikimori/imports?linked=false');
          renderShikimoriImport({imported:0, linked:0, unmatched:imported.items,
                                 unmatched_total:imported.total, next_offset:imported.next_offset});
        } catch(error) { fail(error); }
      });
    } catch(error) { fail(error); }
  }
  function episodePicker(data) {
    const episodes = data.episodes || [];
    const focusIndex = Math.max(0, episodes.findIndex(item => Number(item.id) === Number(state.episodeFocusId)));
    const all = state.episodesFullyOpen === true;
    const radius = Math.max(2, Number(state.episodeRadius) || 2);
    const windowSize = Math.min(episodes.length, radius * 2 + 1);
    const start = all ? 0 : Math.min(Math.max(0, focusIndex - radius), episodes.length - windowSize);
    const end = all ? episodes.length : start + windowSize;
    const visible = episodes.slice(start, end);
    const hasMore = start > 0 || end < episodes.length;
    return `<div id="episode-picker"><div class="episodes">${visible.map(ep => `<button class="action ${ep.watched ? 'secondary' : ''} ${Number(ep.id) === Number(state.episodeFocusId) ? 'current-episode' : ''}" data-episode="${ep.id}">${ep.watched ? '✓ ' : ''}${esc(ep.number)}</button>`).join('')}</div>${hasMore ? `<div class="episode-actions"><span class="meta">Показано ${visible.length} из ${episodes.length}</span><button class="action secondary" id="show-more-episodes">Показать ещё</button><button class="action secondary" id="show-all-episodes">Открыть полностью</button></div>` : ''}</div>`;
  }
  function bindEpisodePicker(data) {
    root.querySelectorAll('[data-episode]').forEach(button => button.onclick = () => selectEpisode(Number(button.dataset.episode)));
    const more = root.querySelector('#show-more-episodes');
    if (more) more.onclick = () => {
      state.episodeRadius = Math.min(data.episodes.length, (Number(state.episodeRadius) || 2) + 5);
      root.querySelector('#episode-picker').outerHTML = episodePicker(data);
      bindEpisodePicker(data);
    };
    const all = root.querySelector('#show-all-episodes');
    if (all) all.onclick = () => {
      state.episodesFullyOpen = true;
      root.querySelector('#episode-picker').outerHTML = episodePicker(data);
      bindEpisodePicker(data);
    };
  }
  async function details(seriesId) {
    try {
      state.view = 'details';
      const data = await api(`/api/library/${seriesId}`); state.selected = data;
      const p = data.playback;
      const nextEpisode = data.episodes.find(item => !item.watched) || data.episodes[data.episodes.length - 1];
      const resumeEpisode = data.episodes.find(item => Number(item.id) === Number(p?.episode_id));
      state.episodeFocusId = resumeEpisode?.id || nextEpisode?.id || null;
      state.episodeRadius = 2;
      state.episodesFullyOpen = false;
      const currentMode = data.item.notifications_enabled ? data.item.notification_mode : '';
      const isHentai = data.item.provider === 'hentai365';
      const shikimori = isHentai ? '' : data.item.shikimori_status
        ? `<section class="panel"><h2>Shikimori</h2><select id="shiki-status">${[['planned','Запланировано'],['watching','Смотрю'],['rewatching','Пересматриваю'],['completed','Просмотрено'],['on_hold','Отложено'],['dropped','Брошено']].map(([value,label]) => `<option value="${value}" ${data.item.shikimori_status === value ? 'selected' : ''}>${label}</option>`).join('')}</select><button class="action secondary" id="save-shiki-status">Сохранить статус</button><button class="action secondary" id="refresh-shikimori">Обновить обложку</button><button class="action secondary" id="choose-shikimori">Перепривязать Shikimori</button></section>`
        : `<section class="panel"><h2>Shikimori</h2><p class="meta">Привяжите тайтл из уже импортированного списка, чтобы добавить статус и обложку.</p><button class="action secondary" id="choose-shikimori">Привязать Shikimori</button></section>`;
      const notifications = isHentai ? '' : `<section class="panel"><h2>Уведомления</h2><select id="notification-mode"><option value="">Отключены</option><option value="any" ${currentMode === 'any' ? 'selected' : ''}>Любая новая серия</option><option value="subtitles" ${currentMode === 'subtitles' ? 'selected' : ''}>Русские субтитры</option><option value="voice" ${currentMode === 'voice' ? 'selected' : ''}>Русская озвучка</option></select><button class="action secondary" id="save-notifications">Сохранить уведомления</button></section>`;
      root.innerHTML = `<section class="title-header"><div><h1>${esc(data.item.title)}</h1><p class="meta">Просмотрено: ${esc(data.item.last_watched_episode_number || 0)} / ${data.episodes.length}${data.item.shikimori_status ? ` · Shikimori: ${esc(shikimoriStatus(data.item.shikimori_status))}` : ''}</p>${p ? `<button class="action" id="resume">Продолжить с ${Math.floor(p.position_seconds/60)}:${String(Math.floor(p.position_seconds%60)).padStart(2,'0')}</button>` : ''}</div>${data.item.poster_url ? `<img class="detail-poster" src="${esc(data.item.poster_url)}" alt="" loading="lazy">` : ''}</section>${notifications}${shikimori}<h2>Серии</h2>${episodePicker(data)}${isHentai ? '' : '<button class="action secondary" id="batch-download">Скачать серии</button>'}<button class="action secondary" id="remove-series">Удалить из «Смотрю»</button><button class="action secondary back">Назад</button>`;
      useBack(); root.querySelector('.back').onclick = back;
      const saveNotifications = root.querySelector('#save-notifications'); if (saveNotifications) saveNotifications.onclick = async () => { try {
        const mode = root.querySelector('#notification-mode').value;
        await api(`/api/library/${seriesId}/notifications`, {method:'PATCH', body:JSON.stringify({enabled:Boolean(mode), mode:mode || 'any'})});
        details(seriesId);
      } catch(error) { fail(error); } };
      root.querySelector('#remove-series').onclick = async () => { try { await api(`/api/library/${seriesId}`, {method:'DELETE'}); home(); } catch(error) { fail(error); } };
      const chooseShikimori = root.querySelector('#choose-shikimori'); if (chooseShikimori) chooseShikimori.onclick = () => shikimoriLibraryLinks(seriesId, data.item.title);
      const refresh = root.querySelector('#refresh-shikimori');
      if (refresh) refresh.onclick = async () => { try { refresh.disabled = true; refresh.textContent = 'Обновляем…'; await api(`/api/library/${seriesId}/shikimori-metadata`, {method:'POST'}); details(seriesId); } catch(error) { fail(error); } };
      if (p) root.querySelector('#resume').onclick = () => selectEpisode(p.episode_id);
      const batchDownload = root.querySelector('#batch-download'); if (batchDownload) batchDownload.onclick = startBatchDownload;
      bindEpisodePicker(data);
    } catch(error) { fail(error); }
  }

  async function shikimoriLibraryLinks(seriesId, initialQuery) {
    const render = async query => {
      try {
        const data = await api(`/api/library/${seriesId}/shikimori-rates?query=${encodeURIComponent(query)}`);
        root.innerHTML = `<h1>Привязать Shikimori</h1><p class="meta">Выберите тайтл из вашего импортированного списка. Это изменит только вашу привязку; общие MAL-сопоставления не меняются вручную.</p><input id="shiki-link-query" value="${esc(query)}" placeholder="Название или Shikimori ID"><button class="action" id="find-shiki-rate">Найти</button>${data.items.length ? data.items.map(item => `<section class="panel"><strong>${esc(item.title)}</strong><p class="meta">${esc(shikimoriStatus(item.status))} · ${esc(item.episodes)} сер. · Shikimori #${esc(item.external_anime_id)}</p><button class="action" data-shiki-rate="${esc(item.external_rate_id)}">Привязать</button></section>`).join('') : '<p class="empty">Совпадений нет. Сначала импортируйте этот статус в настройках Shikimori или уточните название.</p>'}<button class="action secondary back">Назад</button>`;
        useBack(); root.querySelector('.back').onclick = () => details(seriesId);
        root.querySelector('#find-shiki-rate').onclick = () => render(root.querySelector('#shiki-link-query').value);
        root.querySelectorAll('[data-shiki-rate]').forEach(button => button.onclick = async () => { try {
          await api(`/api/library/${seriesId}/shikimori-link`, {method:'POST', body:JSON.stringify({external_rate_id:button.dataset.shikiRate})});
          details(seriesId);
        } catch(error) { fail(error); } });
      } catch(error) { fail(error); }
    };
    await render(initialQuery);
  }
  async function selectEpisode(episodeId) { try { state.episode = state.selected.episodes.find(item => item.id === episodeId); const data = await api(`/api/episodes/${episodeId}/translations?series_id=${encodeURIComponent(state.selected.item.series_id)}`); root.innerHTML = `<h1>Серия ${esc(state.episode.number)}</h1>${state.episode.watched ? '<p class="meta">Серия отмечена просмотренной.</p>' : '<section class="panel"><button class="action secondary" id="mark-watched">Отметить просмотренной</button><p class="meta">Серия и предыдущие будут отмечены просмотренными.</p></section>'}${data.groups.map(group => `<section class="panel"><h2>${esc(group.label)}</h2>${group.items.map(item => `<button class="action secondary" data-translation="${item.id}">${esc(item.authorsSummary || item.title || 'Перевод')}</button>`).join('')}</section>`).join('')}<button class="action secondary back">Назад</button>`; useBack(); root.querySelector('.back').onclick = () => details(state.selected.item.series_id); const markWatched = root.querySelector('#mark-watched'); if (markWatched) markWatched.onclick = async () => { try { markWatched.disabled = true; markWatched.textContent = 'Отмечаем…'; await api(`/api/library/${state.selected.item.series_id}/episodes/${state.episode.id}/watched`, {method:'POST'}); details(state.selected.item.series_id); } catch(error) { fail(error); } }; root.querySelectorAll('[data-translation]').forEach(button => button.onclick = () => qualities(Number(button.dataset.translation))); } catch(error) { fail(error); } }
  async function qualities(translationId) { try { state.translation = translationId; const data = await api(`/api/translations/${translationId}/qualities?series_id=${encodeURIComponent(state.selected.item.series_id)}`); root.innerHTML = `<h1>Качество</h1>${data.items.map(value => `<section class="panel"><strong>${value}p</strong><div><button class="action" data-watch="${value}">Смотреть</button><button class="action secondary" data-download="${value}">Скачать</button></div></section>`).join('')}<button class="action secondary back">Назад</button>`; root.querySelector('.back').onclick = () => selectEpisode(state.episode.id); root.querySelectorAll('[data-watch]').forEach(button => button.onclick = () => player(Number(button.dataset.watch))); root.querySelectorAll('[data-download]').forEach(button => button.onclick = () => downloadOptions(Number(button.dataset.download))); } catch(error) { fail(error); } }
  function downloadOptions(quality) { root.innerHTML = `<h1>Скачать</h1><p class="meta">${esc(state.selected.item.title)} · серия ${esc(state.episode.number)} · ${quality}p</p><section class="panel"><strong>Эта серия</strong><p class="meta">Подготовим MKV без перекодирования.</p><button class="action" id="download-browser">Скачать в Mini App</button><button class="action secondary" id="download-telegram">Отправить в Telegram</button></section><section class="panel"><strong>В поездку</strong><p class="meta">Поставить в очередь следующую серию или несколько непросмотренных.</p><button class="action secondary" id="download-travel">Выбрать серии</button></section><button class="action secondary back">Назад</button>`; root.querySelector('#download-browser').onclick = () => queueDownload(quality, 'browser'); root.querySelector('#download-telegram').onclick = () => queueDownload(quality, 'telegram'); root.querySelector('#download-travel').onclick = () => travelMode(quality); root.querySelector('.back').onclick = () => qualities(state.translation); }
  async function queueDownload(quality, delivery) { try { const job = await api('/api/downloads', {method:'POST',body:JSON.stringify({series_id:state.selected.item.series_id,episode_id:state.episode.id,translation_id:state.translation,quality,delivery})}); root.innerHTML = `<h1>Подготовка поставлена в очередь</h1><p class="meta">Серия ${esc(state.episode.number)} · ${quality}p</p><p id="job-status">${esc(job.status)}</p><button class="action secondary" id="downloads">Все загрузки</button><button class="action secondary back">К аниме</button>`; root.querySelector('#downloads').onclick = downloads; root.querySelector('.back').onclick = () => details(state.selected.item.series_id); const poll = async () => { const current = await api('/api/downloads/' + encodeURIComponent(job.id)); const node = root.querySelector('#job-status'); if (!node) return; node.textContent = jobStatus(current.status); if (current.status === 'ready') { const ticket = await api('/api/downloads/' + encodeURIComponent(job.id) + '/ticket', {method:'POST'}); node.innerHTML = `<a class="action" href="${ticket.url}">Скачать готовый MKV</a>`; return; } if (current.status === 'queued' || current.status === 'preparing') setTimeout(() => poll().catch(()=>{}), 2500); }; setTimeout(() => poll().catch(()=>{}), 1500); } catch(error) { fail(error); } }
  const jobStatus = status => ({queued:'В очереди',preparing:'Подготавливается',ready:'Готово',sent:'Отправлено в Telegram',failed:'Не удалось подготовить',cancelled:'Отменено',expired:'Срок хранения истёк'}[status] || status);
  async function downloads() { try { state.view = 'downloads'; const data = await api('/api/downloads'); const groups = new Map(); data.items.forEach(job => { const name = job.series_title || `Аниме #${job.series_id}`; groups.set(name, [...(groups.get(name) || []), job]); }); const rows = [...groups.entries()].map(([name,jobs]) => `<section class="download-group"><h2>${esc(name)}</h2>${jobs.map(job => `<article class="panel"><strong>Серия ${esc(job.episode_number)} · ${esc(job.quality)}p</strong><p class="meta">${esc(jobStatus(job.status))}${job.delivery === 'telegram' ? ' · Telegram' : ''}</p>${job.status === 'ready' ? `<button class="action" data-file="${esc(job.id)}">Скачать MKV</button>` : ''}${['queued','preparing'].includes(job.status) ? `<button class="action secondary" data-cancel="${esc(job.id)}">Отменить</button>` : ''}${job.status === 'failed' ? '<p class="meta">Попробуйте другой перевод или качество.</p>' : ''}</article>`).join('')}</section>`).join(''); const hasFinished = data.items.some(job => !['queued','preparing'].includes(job.status)); root.innerHTML = `<section class="page-heading"><p class="eyebrow">Offline</p><h1>Загрузки</h1></section>${rows || '<p class="empty">Нет активных или готовых загрузок.</p>'}<button class="action secondary" id="refresh">Обновить</button>${hasFinished ? '<button class="action secondary" id="clear">Скрыть завершённые</button>' : ''}<button class="action secondary back">Назад</button>`; useBack(); root.querySelector('#refresh').onclick = downloads; root.querySelector('.back').onclick = home; if (hasFinished) root.querySelector('#clear').onclick = async () => { try { await api('/api/downloads/clear', {method:'POST'}); downloads(); } catch(error) { fail(error); } }; root.querySelectorAll('[data-cancel]').forEach(button => button.onclick = async () => { try { await api('/api/downloads/' + encodeURIComponent(button.dataset.cancel), {method:'DELETE'}); downloads(); } catch(error) { fail(error); } }); root.querySelectorAll('[data-file]').forEach(button => button.onclick = async () => { try { const ticket = await api('/api/downloads/' + encodeURIComponent(button.dataset.file) + '/ticket', {method:'POST'}); location.href = ticket.url; } catch(error) { fail(error); } }); } catch(error) { fail(error); } }
  async function travelMode(quality) { root.innerHTML = `<h1>В поездку</h1><p class="meta">${esc(state.selected.item.title)} · ${quality}p. В очередь попадёт текущая серия, если она ещё не просмотрена, и следующие. Будет использован выбранный перевод или доступный перевод того же типа и языка.</p><section class="panel"><label>Куда отправить<select id="travel-delivery"><option value="browser">Скачать через Mini App</option><option value="telegram">Отправить в Telegram</option></select></label></section><button class="action" data-travel-count="1">Текущую серию</button><button class="action" data-travel-count="3">Текущую и следующие 2</button><button class="action" data-travel-count="5">Текущую и следующие 4</button><button class="action secondary" data-travel-all="true">Все непросмотренные от текущей</button><button class="action secondary back">Назад</button>`; useBack(); root.querySelector('.back').onclick = () => qualities(state.translation); const start = async (count, allAvailable) => { try { const data = await api('/api/travel', {method:'POST',body:JSON.stringify({series_id:state.selected.item.series_id,anchor_episode_id:state.episode.id,translation_id:state.translation,quality,count,all_available:allAvailable,delivery:root.querySelector('#travel-delivery').value})}); root.innerHTML = `<h1>Подготовка поставлена в очередь</h1><p class="meta">Добавлено: ${esc(data.queued)} из ${esc(data.requested)}. ${esc(data.message)}</p>${data.skipped_episodes.length ? `<p class="meta">Нет подходящего перевода у серий: ${esc(data.skipped_episodes.join(', '))}</p>` : ''}${data.batch_limited ? '<p class="meta">За один запуск можно поставить не более 25 серий.</p>' : ''}<button class="action" id="downloads">Открыть загрузки</button><button class="action secondary back">К аниме</button>`; root.querySelector('#downloads').onclick = downloads; root.querySelector('.back').onclick = () => details(state.selected.item.series_id); } catch(error) { fail(error); } }; root.querySelectorAll('[data-travel-count]').forEach(button => button.onclick = () => start(Number(button.dataset.travelCount), false)); root.querySelector('[data-travel-all]').onclick = () => start(5, true); }
  function startBatchDownload() {
    state.batchDownload = {
      selected: new Set(), configurations: new Map(), query: '', delivery: 'browser',
    };
    batchEpisodeSelection();
  }
  function batchEpisodes() {
    return state.selected?.episodes || [];
  }
  function batchEpisodeSelection() {
    const batch = state.batchDownload;
    if (!batch) return details(state.selected.item.series_id);
    const render = () => {
      const query = batch.query.trim().toLocaleLowerCase();
      const matches = batchEpisodes().filter(episode => !query ||
        `${episode.number} ${episode.title} ${episode.type}`.toLocaleLowerCase().includes(query));
      const visible = matches.slice(0, 100);
      root.innerHTML = `<h1>Скачать серии</h1><p class="meta">Выберите любые серии, включая OVA и дубли. Для каждой затем отдельно выбираются перевод и качество. За один запуск можно подготовить до 50 серий.</p><div class="search-field"><span>⌕</span><input id="batch-episode-query" value="${esc(batch.query)}" placeholder="Номер, название или тип серии"></div><div class="episode-actions"><button class="action secondary" id="batch-select-found">Выбрать найденные</button><button class="action secondary" id="batch-clear">Очистить выбор</button></div><section class="panel"><strong>Выбрано: <span id="batch-selected-count">${batch.selected.size}</span> / 50</strong><div class="batch-episodes">${visible.map(episode => `<label class="batch-episode"><input type="checkbox" data-batch-episode="${episode.id}" ${batch.selected.has(episode.id) ? 'checked' : ''}><span>Серия ${esc(episode.number)}${episode.title ? ` · ${esc(episode.title)}` : ''}${episode.type && episode.type !== 'tv' ? ` · ${esc(episode.type)}` : ''}</span></label>`).join('') || '<p class="empty">Серии не найдены.</p>'}</div>${matches.length > visible.length ? `<p class="meta">Показаны первые ${visible.length} из ${matches.length}. Уточните поиск, чтобы выбрать остальные.</p>` : ''}</section><button class="action" id="batch-configure" ${batch.selected.size ? '' : 'disabled'}>Настроить выбранные (${batch.selected.size})</button><button class="action secondary back">Назад</button>`;
      useBack();
      root.querySelector('.back').onclick = () => details(state.selected.item.series_id);
      root.querySelector('#batch-episode-query').oninput = event => { batch.query = event.target.value; render(); };
      root.querySelector('#batch-select-found').onclick = () => { matches.slice(0, Math.max(0, 50 - batch.selected.size)).forEach(episode => batch.selected.add(episode.id)); render(); };
      root.querySelector('#batch-clear').onclick = () => { batch.selected.clear(); batch.configurations.clear(); render(); };
      root.querySelectorAll('[data-batch-episode]').forEach(input => input.onchange = () => {
        const id = Number(input.dataset.batchEpisode);
        if (input.checked && batch.selected.size >= 50) { input.checked = false; return; }
        if (input.checked) batch.selected.add(id); else { batch.selected.delete(id); batch.configurations.delete(id); }
        root.querySelector('#batch-selected-count').textContent = String(batch.selected.size);
        const configure = root.querySelector('#batch-configure'); configure.disabled = !batch.selected.size;
        configure.textContent = `Настроить выбранные (${batch.selected.size})`;
      });
      root.querySelector('#batch-configure').onclick = batchConfiguration;
    };
    render();
  }
  function batchConfiguration() {
    const batch = state.batchDownload;
    if (!batch) return details(state.selected.item.series_id);
    const episodes = batchEpisodes().filter(episode => batch.selected.has(episode.id));
    const ready = episodes.filter(episode => batch.configurations.has(episode.id));
    root.innerHTML = `<h1>Настройка серий</h1><p class="meta">Выберите перевод и качество отдельно для каждой серии. Ненастроенные серии в очередь не попадут.</p><section class="panel"><label>Куда отправить<select id="batch-delivery"><option value="browser" ${batch.delivery === 'browser' ? 'selected' : ''}>Скачать через Mini App</option><option value="telegram" ${batch.delivery === 'telegram' ? 'selected' : ''}>Отправить в Telegram</option></select></label></section>${episodes.map(episode => { const choice = batch.configurations.get(episode.id); return `<section class="panel"><strong>Серия ${esc(episode.number)}${episode.title ? ` · ${esc(episode.title)}` : ''}</strong><p class="meta">${choice ? `${esc(choice.label)} · ${esc(choice.quality)}p` : 'Перевод и качество не выбраны.'}</p><button class="action secondary" data-batch-configure="${episode.id}">${choice ? 'Изменить выбор' : 'Выбрать перевод и качество'}</button></section>`; }).join('')}<button class="action" id="batch-queue" ${ready.length ? '' : 'disabled'}>Добавить в очередь (${ready.length})</button><button class="action secondary back">К выбору серий</button>`;
    useBack();
    root.querySelector('#batch-delivery').onchange = event => { batch.delivery = event.target.value; };
    root.querySelector('.back').onclick = batchEpisodeSelection;
    root.querySelectorAll('[data-batch-configure]').forEach(button => button.onclick = () => batchTranslations(Number(button.dataset.batchConfigure)));
    root.querySelector('#batch-queue').onclick = queueBatchDownload;
  }
  async function batchTranslations(episodeId) {
    try {
      const episode = batchEpisodes().find(item => item.id === episodeId);
      const data = await api(`/api/episodes/${episodeId}/translations?series_id=${encodeURIComponent(state.selected.item.series_id)}`);
      root.innerHTML = `<h1>Серия ${esc(episode?.number || '?')}</h1><p class="meta">Выберите перевод для этой серии.</p>${data.groups.map(group => `<section class="panel"><h2>${esc(group.label)}</h2>${group.items.map(item => `<button class="action secondary" data-batch-translation="${item.id}" data-batch-label="${esc(item.authorsSummary || item.title || 'Перевод')}">${esc(item.authorsSummary || item.title || 'Перевод')}</button>`).join('')}</section>`).join('') || '<p class="empty">Для этой серии нет доступных переводов.</p>'}<button class="action secondary back">Назад</button>`;
      useBack();
      root.querySelector('.back').onclick = batchConfiguration;
      root.querySelectorAll('[data-batch-translation]').forEach(button => button.onclick = () => batchQualities(episodeId, Number(button.dataset.batchTranslation), button.dataset.batchLabel));
    } catch(error) { fail(error); }
  }
  async function batchQualities(episodeId, translationId, label) {
    try {
      const data = await api(`/api/translations/${translationId}/qualities?series_id=${encodeURIComponent(state.selected.item.series_id)}`);
      root.innerHTML = `<h1>Качество</h1><p class="meta">${esc(label)} · серия ${esc(batchEpisodes().find(item => item.id === episodeId)?.number || '?')}</p>${data.items.map(quality => `<button class="action" data-batch-quality="${quality}">${esc(quality)}p</button>`).join('') || '<p class="empty">У этого перевода нет доступного качества.</p>'}<button class="action secondary back">Назад</button>`;
      useBack();
      root.querySelector('.back').onclick = () => batchTranslations(episodeId);
      root.querySelectorAll('[data-batch-quality]').forEach(button => button.onclick = () => {
        state.batchDownload.configurations.set(episodeId, {translation_id: translationId, quality: Number(button.dataset.batchQuality), label});
        batchConfiguration();
      });
    } catch(error) { fail(error); }
  }
  async function queueBatchDownload() {
    try {
      const batch = state.batchDownload;
      const items = batchEpisodes().filter(episode => batch.selected.has(episode.id)).map(episode => {
        const choice = batch.configurations.get(episode.id);
        return choice && {episode_id: episode.id, translation_id: choice.translation_id, quality: choice.quality};
      }).filter(Boolean);
      if (!items.length) return batchConfiguration();
      const button = root.querySelector('#batch-queue'); button.disabled = true; button.textContent = 'Добавляем в очередь…';
      const data = await api('/api/downloads/batch', {method:'POST', body:JSON.stringify({series_id: state.selected.item.series_id, delivery: batch.delivery, items})});
      root.innerHTML = `<h1>Подготовка поставлена в очередь</h1><p class="meta">Добавлено: ${esc(data.queued)} из ${esc(data.requested)}. ${esc(data.message)}</p>${data.skipped_episodes.length ? `<p class="meta">Больше недоступны: ${esc(data.skipped_episodes.join(', '))}. Настройте их заново.</p>` : ''}<button class="action" id="downloads">Открыть загрузки</button><button class="action secondary back">К аниме</button>`;
      state.batchDownload = null;
      root.querySelector('#downloads').onclick = downloads;
      root.querySelector('.back').onclick = () => details(state.selected.item.series_id);
    } catch(error) { fail(error); }
  }
  const isHlsSource = value => {
    try { return new URL(String(value), location.href).pathname.toLowerCase().endsWith('.m3u8'); }
    catch (_) { return false; }
  };
  function destroyHls() {
    if (state.hls) state.hls.destroy();
    state.hls = null;
  }
  function loadHls() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (state.hlsLoad) return state.hlsLoad;
    state.hlsLoad = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/assets/hls-1.7.3.min.js'; script.async = true;
      script.onload = () => window.Hls ? resolve(window.Hls) : reject(new Error('HLS player is unavailable.'));
      script.onerror = () => reject(new Error('HLS player could not be loaded.'));
      document.head.appendChild(script);
    }).catch(error => { state.hlsLoad = null; throw error; });
    return state.hlsLoad;
  }
  async function setVideoSource(video, source, hls, onFatal) {
    destroyHls();
    if (!hls || video.canPlayType('application/vnd.apple.mpegurl') || video.canPlayType('application/x-mpegURL')) {
      video.src = source;
      return;
    }
    const Hls = await loadHls();
    if (!Hls.isSupported()) { video.src = source; return; }
    const instance = new Hls({enableWorker: true});
    state.hls = instance;
    instance.on(Hls.Events.MEDIA_ATTACHED, () => instance.loadSource(source));
    instance.on(Hls.Events.ERROR, (_event, data) => {
      if (!data?.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) { instance.recoverMediaError(); return; }
      onFatal?.();
    });
    instance.attachMedia(video);
  }
  async function player(quality) { try { state.view = 'player'; const result = await api('/api/play', {method:'POST', body:JSON.stringify({series_id:state.selected.item.series_id, episode_id:state.episode.id, translation_id:state.translation, quality})}); root.innerHTML = `<section class="player-page"><h1>${esc(state.selected.item.title)} <span>· Серия ${esc(state.episode.number)}</span></h1><video controls playsinline>${result.subtitle_url ? `<track kind="subtitles" srclang="ru" label="Субтитры" src="${esc(result.subtitle_url)}" default>` : ''}</video>${result.subtitle_url ? '<p class="meta">Субтитры загружаются вместе с плеером.</p>' : ''}<p><button class="action secondary" id="fallback">Использовать proxy-поток</button></p><button class="action secondary back">Назад</button></section>`; const video = root.querySelector('video'); const hls = isHlsSource(result.media_url); let usingProxy = false; const fallback = async () => { if (usingProxy) return; usingProxy = true; const button = root.querySelector('#fallback'); button.disabled = true; button.textContent = 'Подключаем proxy…'; await setVideoSource(video, result.proxy_url, hls); video.play().catch(()=>{}); button.textContent = 'Proxy-поток подключён'; }; const start = () => setVideoSource(video, result.media_url, hls, () => fallback().catch(()=>{})); await start(); video.addEventListener('error', () => { if (!usingProxy) fallback().catch(()=>{}); }); video.addEventListener('loadedmetadata', () => { for (const track of video.textTracks) track.mode = 'showing'; }); let last = 0; const save = (ended=false) => { if (!video.duration || Date.now()-last < 15000 && !ended) return; last=Date.now(); api('/api/progress',{method:'POST',body:JSON.stringify({series_id:state.selected.item.series_id,episode_id:state.episode.id,position_seconds:video.currentTime,duration_seconds:video.duration,ended})}).catch(()=>{}); }; video.addEventListener('timeupdate', () => save(false)); video.addEventListener('pause', () => save(false)); video.addEventListener('ended', () => save(true)); root.querySelector('#fallback').onclick = () => fallback().catch(error => fail(error)); root.querySelector('.back').onclick = () => { save(false); destroyHls(); details(state.selected.item.series_id); }; } catch(error) { destroyHls(); fail(error); } }
  api('/api/me').then(async () => { const params = new URLSearchParams(location.search); const seriesId = Number(params.get('series_id')); const episodeId = Number(params.get('episode_id')); if (seriesId) { await details(seriesId); if (episodeId && state.selected.episodes.some(item => item.id === episodeId)) await selectEpisode(episodeId); } else await home(); }).catch(error => { root.innerHTML = `<p class="error">${esc(error.message)}${initData ? '' : '<br>Откройте страницу из Telegram.'}</p>`; });
})();
