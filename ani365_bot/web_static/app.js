(() => {
  const telegram = window.Telegram && window.Telegram.WebApp;
  const root = document.getElementById('app');
  const initData = telegram ? telegram.initData : '';
  if (telegram) { telegram.ready(); telegram.expand(); }
  const state = { library: [], selected: null, episode: null, translation: null, quality: null, player: null };
  const esc = value => String(value ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (initData) headers['X-Telegram-Init-Data'] = initData;
    if (options.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
    if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.detail || 'Не удалось выполнить запрос.'); }
    return response.status === 204 ? null : response.json();
  }
  const fail = error => { root.innerHTML = `<p class="error">${esc(error.message || error)}</p>`; };
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
      telegram?.BackButton.hide();
      const data = await api('/api/library'); state.library = data.items;
      root.innerHTML = `<h1>Anime365</h1><button class="action" id="search">Найти аниме</button><button class="action secondary" id="library">Библиотека</button><button class="action secondary" id="downloads">Загрузки</button><button class="action secondary" id="settings">Настройки</button><h2>Продолжить просмотр</h2>${cards(data.continue, true)}${data.new_episodes?.length ? `<h2>Новые серии</h2>${cards(data.new_episodes)}` : ''}<h2>Смотрю</h2>${cards(data.items)}`;
      document.getElementById('search').onclick = search;
      document.getElementById('library').onclick = library;
      document.getElementById('downloads').onclick = downloads;
      document.getElementById('settings').onclick = settings;
      root.querySelectorAll('[data-series]').forEach(button => button.onclick = () => details(Number(button.dataset.series)));
    } catch (error) { fail(error); }
  }
  function cards(items, resume = false) {
    if (!items.length) return '<p class="empty">Пока здесь пусто.</p>';
    return `<section class="grid">${items.map(item => `<article class="card"><button data-series="${item.series_id}">${item.poster_url ? `<img class="poster" src="${esc(item.poster_url)}" alt="" loading="lazy">` : ''}<strong>${esc(item.title)}</strong><div class="meta">${esc(item.last_watched_episode_number || 0)} / ${esc(item.last_available_episode_number || '?')}${resume && item.playback ? ` · ${Math.floor(item.playback.position_seconds / 60)}:${String(Math.floor(item.playback.position_seconds % 60)).padStart(2,'0')}` : ''}${item.shikimori_status ? ` · ${esc(shikimoriStatus(item.shikimori_status))}` : ''}</div></button></article>`).join('')}</section>`;
  }
  const shikimoriStatus = status => ({planned:'Запланировано',watching:'Смотрю',rewatching:'Пересматриваю',completed:'Просмотрено',on_hold:'Отложено',dropped:'Брошено'}[status] || status);
  async function library() {
    try {
      const data = await api('/api/library');
      const groups = [['watching', 'Смотрю'], ['rewatching', 'Пересматриваю'], ['planned', 'Запланировано'], ['on_hold', 'Отложено'], ['dropped', 'Брошено'], ['completed', 'Просмотрено']];
      root.innerHTML = '<h1>Библиотека</h1><input id="library-search" placeholder="Поиск по названию"><div id="library-groups"></div><button class="action secondary back">Назад</button>';
      useBack(); root.querySelector('.back').onclick = home;
      const search = root.querySelector('#library-search');
      const render = () => {
        const query = search.value.trim().toLocaleLowerCase();
        const sections = groups.map(([status, label]) => {
          const items = (data.groups?.[status] || []).filter(item => !query || String(item.title || '').toLocaleLowerCase().includes(query));
          return items.length ? `<details class="library-group" open><summary>${label} · ${items.length}</summary>${cards(items)}</details>` : '';
        }).join('');
        root.querySelector('#library-groups').innerHTML = sections || '<p class="empty">Ничего не найдено.</p>';
        root.querySelectorAll('[data-series]').forEach(button => button.onclick = () => details(Number(button.dataset.series)));
      };
      search.addEventListener('input', render); render();
    } catch (error) { fail(error); }
  }
  async function search() {
    root.innerHTML = '<h1>Каталог</h1><input id="query" placeholder="Название аниме"><button class="action" id="go">Найти</button><section id="results"></section><button class="action secondary back">Назад</button>';
    useBack(); root.querySelector('.back').onclick = back;
    root.querySelector('#go').onclick = async () => { try { const data = await api(`/api/catalog?query=${encodeURIComponent(root.querySelector('#query').value)}`); root.querySelector('#results').innerHTML = cards(data.items); root.querySelectorAll('[data-series]').forEach(button => button.onclick = async () => { const item = data.items.find(row => Number(row.id) === Number(button.dataset.series)); await api('/api/library', { method:'POST', body: JSON.stringify({ series_id:item.id, title:item.titles?.ru || item.titles?.romaji || item.titles?.en || 'Без названия', year:item.year, series_type:item.typeTitle || item.type }) }); details(item.id); }); } catch(error) { fail(error); } };
  }
  function backgroundImportText(item) {
    if (!item) return 'Фоновый импорт будет запущен после подключения.';
    const label = {queued:'в очереди',running:'выполняется',ready:'завершён',failed:'ожидает повторной попытки'}[item.state] || item.state;
    const count = item.imported_count ? ` · ${item.imported_count} тайтлов` : '';
    return `Фоновый импорт: ${label}${count}.`;
  }
  async function settings() { try { const data = await api('/api/shikimori/status'); root.innerHTML = `<h1>Настройки</h1><section class="panel"><h2>Shikimori</h2><p class="meta">${data.connected ? 'Подключён' : data.configured ? 'Не подключён' : 'OAuth не настроен на сервере'}</p>${data.connected ? `<label><input type="checkbox" id="shiki-sync" ${data.sync_enabled ? 'checked' : ''}> Синхронизировать просмотренные серии</label><p class="meta">После просмотра серии обновляется только число серий. Статус «Просмотрено» автоматически не меняется.</p><p class="meta">${esc(backgroundImportText(data.background_import))}</p><button class="action secondary" id="save-shiki-settings">Сохранить</button><button class="action" id="import">Импортировать список</button><button class="action secondary" id="disconnect">Отключить</button>` : data.configured ? '<button class="action" id="connect">Подключить Shikimori</button>' : ''}</section><button class="action secondary back">Назад</button>`; root.querySelector('.back').onclick = back; if (data.connected) { root.querySelector('#disconnect').onclick = async () => { await api('/api/shikimori', {method:'DELETE'}); settings(); }; root.querySelector('#save-shiki-settings').onclick = async () => { try { await api('/api/shikimori/settings', {method:'PATCH',body:JSON.stringify({sync_enabled:root.querySelector('#shiki-sync').checked})}); settings(); } catch(error) { fail(error); } }; root.querySelector('#import').onclick = shikimoriImport; } if (data.configured && !data.connected) root.querySelector('#connect').onclick = async () => { const result = await api('/api/shikimori/connect', {method:'POST'}); location.href = result.authorization_url; }; } catch(error) { fail(error); } }
  async function shikimoriImport() {
    let current;
    try { current = await api('/api/shikimori/status'); } catch(error) { fail(error); return; }
    const selected = new Set(current.background_import?.statuses || ['watching', 'planned']);
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
  async function details(seriesId) {
    try {
      const data = await api(`/api/library/${seriesId}`); state.selected = data;
      const p = data.playback;
      const currentMode = data.item.notifications_enabled ? data.item.notification_mode : '';
      const shikimori = data.item.shikimori_status
        ? `<section class="panel"><h2>Shikimori</h2><select id="shiki-status">${[['planned','Запланировано'],['watching','Смотрю'],['rewatching','Пересматриваю'],['completed','Просмотрено'],['on_hold','Отложено'],['dropped','Брошено']].map(([value,label]) => `<option value="${value}" ${data.item.shikimori_status === value ? 'selected' : ''}>${label}</option>`).join('')}</select><button class="action secondary" id="save-shiki-status">Сохранить статус</button><button class="action secondary" id="refresh-shikimori">Обновить обложку</button><button class="action secondary" id="choose-shikimori">Перепривязать Shikimori</button></section>`
        : `<section class="panel"><h2>Shikimori</h2><p class="meta">Привяжите тайтл из уже импортированного списка, чтобы добавить статус и обложку.</p><button class="action secondary" id="choose-shikimori">Привязать Shikimori</button></section>`;
      root.innerHTML = `<section class="title-header"><div><h1>${esc(data.item.title)}</h1><p class="meta">Просмотрено: ${esc(data.item.last_watched_episode_number || 0)} / ${data.episodes.length}${data.item.shikimori_status ? ` · Shikimori: ${esc(shikimoriStatus(data.item.shikimori_status))}` : ''}</p>${p ? `<button class="action" id="resume">Продолжить с ${Math.floor(p.position_seconds/60)}:${String(Math.floor(p.position_seconds%60)).padStart(2,'0')}</button>` : ''}</div>${data.item.poster_url ? `<img class="detail-poster" src="${esc(data.item.poster_url)}" alt="" loading="lazy">` : ''}</section><section class="panel"><h2>Уведомления</h2><select id="notification-mode"><option value="">Отключены</option><option value="any" ${currentMode === 'any' ? 'selected' : ''}>Любая новая серия</option><option value="subtitles" ${currentMode === 'subtitles' ? 'selected' : ''}>Русские субтитры</option><option value="voice" ${currentMode === 'voice' ? 'selected' : ''}>Русская озвучка</option></select><button class="action secondary" id="save-notifications">Сохранить уведомления</button></section>${shikimori}<h2>Серии</h2><div class="episodes">${data.episodes.map(ep => `<button class="action ${ep.watched ? 'secondary' : ''}" data-episode="${ep.id}">${ep.watched ? '✓ ' : ''}${esc(ep.number)}</button>`).join('')}</div><button class="action secondary" id="remove-series">Удалить из «Смотрю»</button><button class="action secondary back">Назад</button>`;
      useBack(); root.querySelector('.back').onclick = back;
      root.querySelector('#save-notifications').onclick = async () => { try {
        const mode = root.querySelector('#notification-mode').value;
        await api(`/api/library/${seriesId}/notifications`, {method:'PATCH', body:JSON.stringify({enabled:Boolean(mode), mode:mode || 'any'})});
        details(seriesId);
      } catch(error) { fail(error); } };
      root.querySelector('#remove-series').onclick = async () => { try { await api(`/api/library/${seriesId}`, {method:'DELETE'}); home(); } catch(error) { fail(error); } };
      root.querySelector('#choose-shikimori').onclick = () => shikimoriLibraryLinks(seriesId, data.item.title);
      const refresh = root.querySelector('#refresh-shikimori');
      if (refresh) refresh.onclick = async () => { try { refresh.disabled = true; refresh.textContent = 'Обновляем…'; await api(`/api/library/${seriesId}/shikimori-metadata`, {method:'POST'}); details(seriesId); } catch(error) { fail(error); } };
      if (p) root.querySelector('#resume').onclick = () => selectEpisode(p.episode_id);
      root.querySelectorAll('[data-episode]').forEach(button => button.onclick = () => selectEpisode(Number(button.dataset.episode)));
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
  async function selectEpisode(episodeId) { try { state.episode = state.selected.episodes.find(item => item.id === episodeId); const data = await api(`/api/episodes/${episodeId}/translations`); root.innerHTML = `<h1>Серия ${esc(state.episode.number)}</h1>${data.groups.map(group => `<section class="panel"><h2>${esc(group.label)}</h2>${group.items.map(item => `<button class="action secondary" data-translation="${item.id}">${esc(item.authorsSummary || item.title || 'Перевод')}</button>`).join('')}</section>`).join('')}<button class="action secondary back">Назад</button>`; useBack(); root.querySelector('.back').onclick = () => details(state.selected.item.series_id); root.querySelectorAll('[data-translation]').forEach(button => button.onclick = () => qualities(Number(button.dataset.translation))); } catch(error) { fail(error); } }
  async function qualities(translationId) { try { state.translation = translationId; const data = await api(`/api/translations/${translationId}/qualities`); root.innerHTML = `<h1>Качество</h1>${data.items.map(value => `<section class="panel"><strong>${value}p</strong><div><button class="action" data-watch="${value}">Смотреть</button><button class="action secondary" data-browser="${value}">Скачать</button><button class="action secondary" data-telegram="${value}">В Telegram</button><button class="action secondary" data-travel="${value}">В поездку</button></div></section>`).join('')}<button class="action secondary back">Назад</button>`; root.querySelector('.back').onclick = () => selectEpisode(state.episode.id); root.querySelectorAll('[data-watch]').forEach(button => button.onclick = () => player(Number(button.dataset.watch))); root.querySelectorAll('[data-browser]').forEach(button => button.onclick = () => queueDownload(Number(button.dataset.browser), 'browser')); root.querySelectorAll('[data-telegram]').forEach(button => button.onclick = () => queueDownload(Number(button.dataset.telegram), 'telegram')); root.querySelectorAll('[data-travel]').forEach(button => button.onclick = () => travelMode(Number(button.dataset.travel))); } catch(error) { fail(error); } }
  async function queueDownload(quality, delivery) { try { const job = await api('/api/downloads', {method:'POST',body:JSON.stringify({series_id:state.selected.item.series_id,episode_id:state.episode.id,translation_id:state.translation,quality,delivery})}); root.innerHTML = `<h1>Подготовка поставлена в очередь</h1><p class="meta">Серия ${esc(state.episode.number)} · ${quality}p</p><p id="job-status">${esc(job.status)}</p><button class="action secondary" id="downloads">Все загрузки</button><button class="action secondary back">К аниме</button>`; root.querySelector('#downloads').onclick = downloads; root.querySelector('.back').onclick = () => details(state.selected.item.series_id); const poll = async () => { const current = await api('/api/downloads/' + encodeURIComponent(job.id)); const node = root.querySelector('#job-status'); if (!node) return; node.textContent = jobStatus(current.status); if (current.status === 'ready') { const ticket = await api('/api/downloads/' + encodeURIComponent(job.id) + '/ticket', {method:'POST'}); node.innerHTML = `<a class="action" href="${ticket.url}">Скачать готовый MKV</a>`; return; } if (current.status === 'queued' || current.status === 'preparing') setTimeout(() => poll().catch(()=>{}), 2500); }; setTimeout(() => poll().catch(()=>{}), 1500); } catch(error) { fail(error); } }
  const jobStatus = status => ({queued:'В очереди',preparing:'Подготавливается',ready:'Готово',sent:'Отправлено в Telegram',failed:'Не удалось подготовить',cancelled:'Отменено',expired:'Срок хранения истёк'}[status] || status);
  async function downloads() { try { const data = await api('/api/downloads'); const groups = new Map(); data.items.forEach(job => { const name = job.series_title || `Аниме #${job.series_id}`; groups.set(name, [...(groups.get(name) || []), job]); }); const rows = [...groups.entries()].map(([name,jobs]) => `<section class="download-group"><h2>${esc(name)}</h2>${jobs.map(job => `<article class="panel"><strong>Серия ${esc(job.episode_number)} · ${esc(job.quality)}p</strong><p class="meta">${esc(jobStatus(job.status))}${job.delivery === 'telegram' ? ' · Telegram' : ''}</p>${job.status === 'ready' ? `<button class="action" data-file="${esc(job.id)}">Скачать MKV</button>` : ''}${['queued','preparing'].includes(job.status) ? `<button class="action secondary" data-cancel="${esc(job.id)}">Отменить</button>` : ''}${job.status === 'failed' ? '<p class="meta">Попробуйте другой перевод или качество.</p>' : ''}</article>`).join('')}</section>`).join(''); const hasFinished = data.items.some(job => !['queued','preparing'].includes(job.status)); root.innerHTML = `<h1>Загрузки</h1>${rows || '<p class="empty">Нет активных или готовых загрузок.</p>'}<button class="action secondary" id="refresh">Обновить</button>${hasFinished ? '<button class="action secondary" id="clear">Скрыть завершённые</button>' : ''}<button class="action secondary back">Назад</button>`; useBack(); root.querySelector('#refresh').onclick = downloads; root.querySelector('.back').onclick = home; if (hasFinished) root.querySelector('#clear').onclick = async () => { try { await api('/api/downloads/clear', {method:'POST'}); downloads(); } catch(error) { fail(error); } }; root.querySelectorAll('[data-cancel]').forEach(button => button.onclick = async () => { try { await api('/api/downloads/' + encodeURIComponent(button.dataset.cancel), {method:'DELETE'}); downloads(); } catch(error) { fail(error); } }); root.querySelectorAll('[data-file]').forEach(button => button.onclick = async () => { try { const ticket = await api('/api/downloads/' + encodeURIComponent(button.dataset.file) + '/ticket', {method:'POST'}); location.href = ticket.url; } catch(error) { fail(error); } }); } catch(error) { fail(error); } }
  async function travelMode(quality) { root.innerHTML = `<h1>В поездку</h1><p class="meta">${esc(state.selected.item.title)} · ${quality}p. Будет использован выбранный перевод или доступный перевод того же типа и языка.</p><section class="panel"><label>Куда отправить<select id="travel-delivery"><option value="browser">Скачать через Mini App</option><option value="telegram">Отправить в Telegram</option></select></label></section><button class="action" data-travel-count="1">Следующую серию</button><button class="action" data-travel-count="3">Следующие 3</button><button class="action" data-travel-count="5">Следующие 5</button><button class="action secondary" data-travel-all="true">Все непросмотренные</button><button class="action secondary back">Назад</button>`; useBack(); root.querySelector('.back').onclick = () => qualities(state.translation); const start = async (count, allAvailable) => { try { const data = await api('/api/travel', {method:'POST',body:JSON.stringify({series_id:state.selected.item.series_id,anchor_episode_id:state.episode.id,translation_id:state.translation,quality,count,all_available:allAvailable,delivery:root.querySelector('#travel-delivery').value})}); root.innerHTML = `<h1>Подготовка поставлена в очередь</h1><p class="meta">Добавлено: ${esc(data.queued)} из ${esc(data.requested)}. ${esc(data.message)}</p>${data.skipped_episodes.length ? `<p class="meta">Нет подходящего перевода у серий: ${esc(data.skipped_episodes.join(', '))}</p>` : ''}${data.batch_limited ? '<p class="meta">За один запуск можно поставить не более 25 серий.</p>' : ''}<button class="action" id="downloads">Открыть загрузки</button><button class="action secondary back">К аниме</button>`; root.querySelector('#downloads').onclick = downloads; root.querySelector('.back').onclick = () => details(state.selected.item.series_id); } catch(error) { fail(error); } }; root.querySelectorAll('[data-travel-count]').forEach(button => button.onclick = () => start(Number(button.dataset.travelCount), false)); root.querySelector('[data-travel-all]').onclick = () => start(5, true); }
  async function player(quality) { try { const result = await api('/api/play', {method:'POST', body:JSON.stringify({series_id:state.selected.item.series_id, episode_id:state.episode.id, translation_id:state.translation, quality})}); root.innerHTML = `<h1>${esc(state.selected.item.title)} · ${esc(state.episode.number)}</h1><video controls playsinline>${result.subtitle_url ? `<track kind="subtitles" srclang="ru" label="Субтитры" src="${esc(result.subtitle_url)}" default>` : ''}</video>${result.subtitle_url ? '<p class="meta">Субтитры загружаются вместе с плеером.</p>' : ''}<p><button class="action secondary" id="fallback">Если видео не запустилось, использовать proxy</button></p><button class="action secondary back">Назад</button>`; const video = root.querySelector('video'); video.src = result.media_url; video.addEventListener('loadedmetadata', () => { for (const track of video.textTracks) track.mode = 'showing'; }); let last = 0; const save = (ended=false) => { if (!video.duration || Date.now()-last < 15000 && !ended) return; last=Date.now(); api('/api/progress',{method:'POST',body:JSON.stringify({series_id:state.selected.item.series_id,episode_id:state.episode.id,position_seconds:video.currentTime,duration_seconds:video.duration,ended})}).catch(()=>{}); }; video.addEventListener('timeupdate', () => save(false)); video.addEventListener('pause', () => save(false)); video.addEventListener('ended', () => save(true)); root.querySelector('#fallback').onclick = () => { video.src = result.proxy_url; video.play().catch(()=>{}); }; root.querySelector('.back').onclick = () => { save(false); details(state.selected.item.series_id); }; } catch(error) { fail(error); } }
  api('/api/me').then(async () => { const params = new URLSearchParams(location.search); const seriesId = Number(params.get('series_id')); const episodeId = Number(params.get('episode_id')); if (seriesId) { await details(seriesId); if (episodeId && state.selected.episodes.some(item => item.id === episodeId)) await selectEpisode(episodeId); } else await home(); }).catch(error => { root.innerHTML = `<p class="error">${esc(error.message)}${initData ? '' : '<br>Откройте страницу из Telegram.'}</p>`; });
})();
