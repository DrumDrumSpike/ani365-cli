(() => {
  const telegram = window.Telegram && window.Telegram.WebApp;
  const root = document.getElementById('app');
  const initData = telegram ? telegram.initData : '';
  if (telegram) { telegram.ready(); telegram.expand(); }
  const state = { library: [], selected: null, episode: null, translation: null, quality: null, player: null, view: 'home', hls: null, hlsLoad: null, batchDownload: null, resumePosition: 0, resumeEpisodeId: null };
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
    ['home', '⌂', 'Главная'], ['search', '⌕', 'Каталог'], ['library', '▦', 'Моё'],
    ['downloads', '⇩', 'Загрузки'], ['more', '⋯', 'Ещё'],
  ];
  const moreSections = ['more', 'seasonal', 'recommendations', 'hentai', 'settings'];
  const nativeAvailable = version => Boolean(initData && telegram?.isVersionAtLeast?.(version));
  let adultPreferenceKey;
  function loadAdultPreference(userId) {
    adultPreferenceKey = `ani365:adult-catalog:${userId}`;
    try { state.adultCatalog = localStorage.getItem(adultPreferenceKey) === 'on'; }
    catch (_) { state.adultCatalog = false; }
  }
  function setAdultPreference(enabled) {
    state.adultCatalog = enabled;
    try { localStorage.setItem(adultPreferenceKey, enabled ? 'on' : 'off'); } catch (_) {}
  }
  function more() {
    state.view = 'more';
    const sections = [['seasonal', 'Новинки'], ['recommendations', 'Советы']];
    if (state.adultCatalog) sections.push(['hentai', 'Каталог 18+']);
    sections.push(['settings', 'Настройки']);
    root.innerHTML = `<section class="page-heading"><h1>Ещё</h1></section><div class="more-menu">${sections.map(([id, label]) => `<button data-nav="${id}">${label}<span aria-hidden="true">›</span></button>`).join('')}</div><button class="action secondary back">Назад</button>`;
    root.querySelector('.back').onclick = home;
  }
  function applyTelegramTheme() {
    document.documentElement.classList.toggle('telegram-theme', Boolean(initData && telegram?.themeParams?.bg_color));
    document.documentElement.dataset.telegramScheme = telegram?.colorScheme || 'dark';
  }
  applyTelegramTheme();
  telegram?.onEvent?.('themeChanged', applyTelegramTheme);
  let homeMainVisible = false;
  let nativeBackVisible = false;
  const resumeFromMainButton = () => root.querySelector('#continue-watching')?.click();
  const navigateBack = () => root.querySelector('.back')?.click();
  function releaseNavigationButtons() {
    if (homeMainVisible) {
      telegram.MainButton.offClick(resumeFromMainButton);
      telegram.MainButton.hide();
      homeMainVisible = false;
    }
    if (nativeBackVisible) {
      telegram.BackButton.offClick(navigateBack);
      telegram.BackButton.hide();
      nativeBackVisible = false;
    }
  }
  function syncNavigationButtons() {
    if (state.view === 'player') return;
    const resume = state.view === 'home' && root.querySelector('#continue-watching');
    const showMain = Boolean(resume && nativeAvailable('6.0') && telegram.MainButton);
    if (showMain !== homeMainVisible) {
      if (showMain) {
        telegram.MainButton.onClick(resumeFromMainButton);
        telegram.MainButton.setText('Продолжить просмотр');
        telegram.MainButton.show();
      } else {
        telegram.MainButton.offClick(resumeFromMainButton);
        telegram.MainButton.hide();
      }
      homeMainVisible = showMain;
    }
    const showBack = Boolean(root.querySelector('.back') && nativeAvailable('6.1') && telegram.BackButton);
    if (showBack !== nativeBackVisible) {
      if (showBack) {
        telegram.BackButton.onClick(navigateBack);
        telegram.BackButton.show();
      } else {
        telegram.BackButton.offClick(navigateBack);
        telegram.BackButton.hide();
      }
      nativeBackVisible = showBack;
    }
  }
  root.addEventListener('click', event => {
    if (event.isTrusted && nativeAvailable('6.1') && event.target.closest('button:not(:disabled)')) {
      telegram.HapticFeedback?.impactOccurred('light');
    }
  }, true);
  function updateAppFullscreen() {
    const active = Boolean(telegram?.isFullscreen || document.fullscreenElement);
    document.body.classList.toggle('app-fullscreen', active);
    const button = root.querySelector('#app-fullscreen');
    if (button) {
      button.textContent = active ? 'Обычный режим' : 'Во весь экран';
      button.setAttribute('aria-label', button.textContent);
    }
  }
  document.addEventListener('fullscreenchange', updateAppFullscreen);
  telegram?.onEvent?.('fullscreenChanged', updateAppFullscreen);
  function addChrome() {
    if (state.view === 'player' || root.querySelector('.app-topbar')) return;
    root.insertAdjacentHTML('afterbegin', `<header class="app-topbar"><button class="brand" data-nav="home" aria-label="На главную"><span class="brand-mark">▶</span><span>ani<b>365</b></span></button><div class="top-actions"><button class="top-fullscreen" id="app-fullscreen">Во весь экран</button><button class="top-settings" data-nav="settings" aria-label="Настройки">⚙</button></div></header>`);
    const fullscreenButton = root.querySelector('#app-fullscreen');
    fullscreenButton.onclick = async () => {
      try {
        if (telegram?.isFullscreen) telegram.exitFullscreen?.();
        else if (document.fullscreenElement) await document.exitFullscreen();
        else if (telegram?.requestFullscreen) telegram.requestFullscreen();
        else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen();
        else fullscreenButton.title = 'Полный экран недоступен в этом окне';
      } catch (_) { fullscreenButton.title = 'Полный экран недоступен в этом окне'; }
      updateAppFullscreen();
    };
    updateAppFullscreen();
    root.insertAdjacentHTML('beforeend', `<nav class="bottom-nav" aria-label="Основная навигация">${navItems.map(([id, icon, label]) => `<button class="${(moreSections.includes(state.view) ? 'more' : state.view) === id ? 'active' : ''}" data-nav="${id}"><span aria-hidden="true">${icon}</span>${label}</button>`).join('')}</nav>`);
    root.querySelectorAll('[data-nav]').forEach(button => {
      const target = button.dataset.nav;
      button.onclick = () => ({ home, seasonal, recommendations, search, hentai, library, downloads, more, settings }[target] || home)();
    });
  }
  const chromeObserver = new MutationObserver(() => {
    if (state.player && !root.contains(state.player.video)) {
      state.player.cleanup();
      state.player = null;
    }
    addChrome();
    syncNavigationButtons();
  });
  chromeObserver.observe(root, { childList: true });
  function back() { home(); }
  function useBack() { syncNavigationButtons(); }
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
      const data = await api('/api/library');
      if (state.view !== 'home') return;
      state.library = data.items;
      const resume = data.continue?.[0];
      const resumeText = resume?.playback ? `${Math.floor(resume.playback.position_seconds / 60)}:${String(Math.floor(resume.playback.position_seconds % 60)).padStart(2, '0')}` : '';
      root.innerHTML = `<div class="home-page"><div id="home-refresh-indicator" class="home-refresh-indicator" hidden>Потяните вниз для обновления</div>${resume ? `<section class="continue-hero"><img src="/assets/anime-night.webp" alt="" aria-hidden="true"><div class="hero-copy"><p class="eyebrow">Продолжить просмотр</p><h1>${esc(resume.title)}</h1><button class="action hero-action" id="continue-watching">▶ Продолжить${resumeText ? ` с ${esc(resumeText)}` : ''}</button><button class="text-link hero-all" id="continue-all">Все серии</button></div></section>` : `<section class="welcome-hero"><p class="eyebrow">Anime365</p><h1>Ваше аниме<br>в одном месте</h1><p>Найдите тайтл, добавьте его в библиотеку и продолжайте просмотр с того же места.</p><button class="action" id="search">Найти аниме</button></section>`}${data.new_episodes?.length ? `<section class="new-episodes"><span>✦</span><div><h2>Новые серии</h2><div class="new-episode-list">${data.new_episodes.slice(0, 3).map(item => `<button class="new-episode-item" data-new-series="${esc(item.series_id)}" data-new-episode="${esc(item.last_available_episode_id || '')}"><strong>${esc(item.title)}</strong><span>Вышла: ${esc(item.last_available_episode_number || '?')} ›</span></button>`).join('')}</div><button class="text-link" id="library-updates">Посмотреть обновления в библиотеке ›</button></div></section>` : '<section class="new-episodes"><div><h2>Новые серии</h2><p>Пока нет известных обновлений для начатых тайтлов.</p></div></section>'}<section class="section-heading"><div><p class="eyebrow">Ваша очередь</p><h2>Смотрю сейчас</h2></div><button class="text-link" id="library">Все ›</button></section>${homeCards(data.items, data.new_episodes || [])}</div>`;
      const searchButton = document.getElementById('search'); if (searchButton) searchButton.onclick = search;
      const libraryButton = document.getElementById('library'); if (libraryButton) libraryButton.onclick = () => library();
      const updatesButton = document.getElementById('library-updates'); if (updatesButton) updatesButton.onclick = () => library(true);
      root.querySelectorAll('[data-home-watch]').forEach(button => button.onclick = () => openHomeSeries(Number(button.dataset.homeWatch)));
      root.querySelectorAll('[data-new-series]').forEach(button => button.onclick = () => openNewEpisode(Number(button.dataset.newSeries), Number(button.dataset.newEpisode)));
      root.querySelectorAll('[data-series]').forEach(button => button.onclick = () => details(Number(button.dataset.series)));
      bindHomePullToRefresh();
      root.querySelectorAll('.home-poster img').forEach(image => { const mark = ok => { const poster = image.closest('.home-poster'); poster?.classList.add(ok ? 'poster-ready' : 'poster-failed'); if (ok) image.classList.add('loaded'); }; image.addEventListener('load', () => mark(true), {once:true}); image.addEventListener('error', () => mark(false), {once:true}); if (image.complete) mark(image.naturalWidth > 0); });
      if (resume) {
        const seriesId = Number(resume.series_id);
        root.querySelector('#continue-all').onclick = () => details(seriesId, true);
        root.querySelector('#continue-watching').onclick = async () => {
          if (!await details(seriesId)) return;
          const {playback, episodes} = state.selected;
          const savedEpisode = episodes.find(item => Number(item.id) === Number(playback?.episode_id) && !item.watched);
          if (savedEpisode) return resumeDirect(savedEpisode.id);
          const nextEpisode = episodes.find(item => !item.watched);
          if (nextEpisode) await selectEpisode(nextEpisode.id);
        };
      }
    } catch (error) { fail(error); }
  }
  function episodeNumberValue(value) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric;
    const match = String(value ?? '').match(/(?:^|\D)(\d+(?:[.,]\d+)?)/);
    return match ? Number(match[1].replace(',', '.')) : 0;
  }
  function homeCards(items, newEpisodes) {
    if (!items.length) return '<p class="empty">Пока здесь пусто.</p>';
    const newIds = new Set(newEpisodes.map(item => Number(item.series_id)));
    return `<div class="home-rail" aria-label="Смотрю сейчас">${items.map(item => {
      const watchedValue = episodeNumberValue(item.last_watched_episode_number);
      const availableValue = episodeNumberValue(item.last_available_episode_number);
      const watched = watchedValue > 0 ? watchedValue : 0;
      const available = availableValue > 0 ? availableValue : 0;
      const playbackIsNext = item.playback && Number(item.playback.episode_id) !== Number(item.last_watched_episode_id);
      const hasNew = newIds.has(Number(item.series_id));
      const isTrailer = /trailer|трейлер/i.test([item.series_type, item.last_watched_episode_number, item.last_available_episode_number].join(' '));
      const hasCount = !isTrailer && available > 0;
      const progress = hasCount ? Math.max(0, Math.min(100, Math.round(watched / available * 100))) : null;
      const progressMarkup = hasCount ? `<span class="home-card-progress" role="progressbar" aria-label="${watched} из ${available} серий" aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="100"><span style="width:${progress}%"></span></span>` : '';
      const badge = hasNew ? '<span class="home-new-dot" aria-label="Новая серия" title="Новая серия"></span>' : '';
      const poster = item.poster_url
        ? `<div class="home-poster"><div class="poster-skeleton" aria-hidden="true"></div><img src="${esc(item.poster_url)}" alt="" loading="lazy">${badge}${progressMarkup}</div>`
        : `<div class="home-poster poster-fallback">${badge}${progressMarkup}</div>`;
      const countText = isTrailer ? '' : hasCount ? `${watched} из ${available}` : watched > 0 ? `Просмотрено серий: ${watched}` : '';
      const reliableNext = hasCount && watched + 1 === available;
      const action = playbackIsNext ? 'Продолжить просмотр' : reliableNext ? `Серия ${watched + 1} →` : watched ? 'Смотреть дальше' : 'Смотреть';
      return `<article class="home-card"><button class="home-card-button" data-home-watch="${esc(item.series_id)}">${poster}<strong title="${esc(item.title)}">${esc(item.title)}</strong>${countText ? `<span class="home-card-meta">${countText}</span>` : ''}<span class="home-card-action">${action}</span></button></article>`;
    }).join('')}</div>`;
  }
  async function openHomeSeries(seriesId) {
    const loaded = await details(seriesId);
    if (!loaded || Number(state.selected?.item?.series_id) !== Number(seriesId)) return;
    const playback = state.selected.playback;
    const resumeEpisode = state.selected.episodes.find(item => Number(item.id) === Number(playback?.episode_id) && !item.watched);
    if (resumeEpisode) return resumeDirect(resumeEpisode.id);
    const nextEpisode = state.selected.episodes.find(item => !item.watched) || state.selected.episodes.find(item => Number(item.id) === Number(state.selected.item.last_available_episode_id)) || state.selected.episodes[state.selected.episodes.length - 1];
    if (nextEpisode) await selectEpisode(nextEpisode.id);
  }
  function bindHomePullToRefresh() {
    if (!root.addEventListener) return;
    state.homePullCleanup?.();
    const indicator = root.querySelector('#home-refresh-indicator');
    let startY = null, startX = null, pulling = false, busy = false;
    const start = event => {
      if (state.view !== 'home' || window.scrollY > 0 || busy) return;
      startY = event.touches?.[0]?.clientY ?? null;
      startX = event.touches?.[0]?.clientX ?? null;
      pulling = startY !== null && startX !== null;
    };
    const move = event => {
      if (!pulling || startY === null) return;
      const point = event.touches?.[0];
      const distance = Math.max(0, (point?.clientY ?? startY) - startY);
      const horizontal = Math.abs((point?.clientX ?? startX) - startX);
      if (horizontal > distance) { pulling = false; if (indicator) indicator.hidden = true; return; }
      if (indicator) { indicator.hidden = distance < 12; indicator.textContent = distance > 76 ? 'Отпустите, чтобы обновить' : 'Потяните вниз для обновления'; }
    };
    const end = async event => {
      if (!pulling) return;
      const distance = Math.max(0, (event.changedTouches?.[0]?.clientY ?? startY) - startY);
      pulling = false; startY = null; startX = null;
      if (indicator) indicator.hidden = true;
      if (distance < 76 || busy || state.view !== 'home') return;
      busy = true;
      if (indicator) { indicator.hidden = false; indicator.textContent = 'Обновляем…'; }
      await home();
      busy = false;
    };
    root.addEventListener('touchstart', start, {passive: true});
    root.addEventListener('touchmove', move, {passive: true});
    root.addEventListener('touchend', end, {passive: true});
    state.homePullCleanup = () => {
      root.removeEventListener?.('touchstart', start);
      root.removeEventListener?.('touchmove', move);
      root.removeEventListener?.('touchend', end);
    };
  }
  async function openNewEpisode(seriesId, episodeId) {
    const loaded = await details(seriesId);
    if (!loaded || Number(state.selected?.item?.series_id) !== Number(seriesId)) return;
    if (episodeId && state.selected.episodes.some(item => Number(item.id) === Number(episodeId))) await selectEpisode(episodeId);
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
  function formatTitleCount(count) { const n = Number(count) || 0; const tail = n % 10 === 1 && n % 100 !== 11 ? 'тайтл' : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? 'тайтла' : 'тайтлов'; return `${n} ${tail}`; }
  async function library(onlySeriesIds = null) {
    try {
      state.view = 'library';
      const data = await api('/api/library');
      const groups = [['watching', 'Смотрю'], ['rewatching', 'Пересматриваю'], ['planned', 'Запланировано'], ['on_hold', 'Отложено'], ['dropped', 'Брошено'], ['completed', 'Просмотрено']];
      const hentaiItems = state.adultCatalog ? (data.hentai_items || []) : [];
      const isUpdates = onlySeriesIds === true;
      const updateSeriesIds = isUpdates ? [...new Set((data.new_episodes || []).map(item => Number(item.series_id)))] : null;
      root.innerHTML = '<section class="page-heading"><p class="eyebrow">Личная коллекция</p><div><h1>' + (isUpdates ? 'Обновления' : 'Моё аниме') + '</h1><span class="count-badge">' + esc(formatTitleCount(isUpdates ? updateSeriesIds.length : Object.values(data.groups || {}).reduce((sum, items) => sum + items.length, hentaiItems.length))) + '</span></div></section><label class="search-field"><span>⌕</span><input id="library-search" placeholder="Поиск по названию"></label><div id="library-groups"></div><button class="action secondary back">Назад</button>';
      useBack(); root.querySelector('.back').onclick = home;
      const search = root.querySelector('#library-search');
      const render = () => {
        const query = search.value.trim().toLocaleLowerCase();
        const allowed = updateSeriesIds ? new Set(updateSeriesIds) : null;
        const sections = groups.map(([status, label]) => {
          const items = (data.groups?.[status] || []).filter(item => (!allowed || allowed.has(Number(item.series_id))) && (!query || String(item.title || '').toLocaleLowerCase().includes(query)));
          return items.length ? `<details class="library-group" open><summary>${label} · ${items.length}</summary>${cards(items)}</details>` : '';
        }).join('') + (() => { const items = hentaiItems.filter(item => (!allowed || allowed.has(Number(item.series_id))) && (!query || String(item.title || '').toLocaleLowerCase().includes(query))); return items.length ? `<details class="library-group"><summary>Hentai365 · ${items.length}</summary>${cards(items)}</details>` : ''; })();
        root.querySelector('#library-groups').innerHTML = sections || '<p class="empty">Ничего не найдено.</p>';
        root.querySelectorAll('[data-series]').forEach(button => button.onclick = () => details(Number(button.dataset.series)));
      };
      search.addEventListener('input', render); render();
    } catch (error) { fail(error); }
  }
  function recommendationCards(items) {
    return `<section class="grid">${items.map(item => {
      const poster = item.poster_url ? `<img class="poster" src="${esc(item.poster_url)}" alt="" loading="lazy">` : '<div class="poster catalog-placeholder">Anime365</div>';
      const heading = `<strong>${esc(item.title)}</strong><div class="meta">${esc(item.year || 'Год не указан')}${item.kind || item.series_type ? ` · ${esc(item.kind || item.series_type)}` : ''}</div>`;
      const opener = item.anime365_series_id ? `<button data-recommendation="${esc(item.anime365_series_id)}">${poster}${heading}</button>` : `${poster}${heading}`;
      const reason = item.reason ? `<p class="recommendation-reason">${esc(item.reason)}</p><button type="button" class="reason-toggle" data-reason-toggle>Подробнее</button>` : '';
      const missing = item.anime365_series_id ? '' : '<p class="meta">Нет карточки Anime365</p>';
      return `<article class="card">${opener}${reason}${missing}</article>`;
    }).join('')}</section>`;
  }
  function bindReasonToggles(scope) {
    scope.querySelectorAll('[data-reason-toggle]').forEach(button => {
      const reason = button.previousElementSibling;
      if (!reason || !reason.classList) return;
      if (reason.clientHeight && reason.scrollHeight <= reason.clientHeight + 1) {
        button.hidden = true;
        return;
      }
      button.onclick = event => {
        event?.preventDefault?.();
        event?.stopPropagation?.();
        const open = reason.classList.toggle('expanded');
        button.textContent = open ? 'Свернуть' : 'Подробнее';
      };
    });
  }
  const RECOMMENDATION_MOODS = [
    ['cyberpunk', 'Киберпанк'], ['isekai', 'Исекай'], ['magic', 'Магические битвы'],
    ['swords', 'Мечники/самураи'], ['dark', 'Мрачное'], ['light', 'Лёгкое/комедия'],
    ['romance', 'Романтика'], ['psychological', 'Психологическое'], ['sports', 'Спорт'],
    ['slice', 'Повседневность'], ['any', 'Любое'],
  ];
  function cleanMoodNote(value) {
    return String(value || '').replace(/[\u0000-\u001f\u007f<>`«»]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 80);
  }
  function recommendationMoodKey(chips, note) {
    const chosen = (chips || []).filter(id => id && id !== 'any');
    const ids = chosen.length ? [...chosen].sort() : ['any'];
    return ids.join(',') + '|' + cleanMoodNote(note).toLocaleLowerCase('ru').replace(/ё/g, 'е');
  }
  let recommendationTimer;
  function openRecommendation(items, seriesId) {
    const item = items.find(row => Number(row.anime365_series_id) === Number(seriesId));
    if (!item || !item.anime365_series_id) throw new Error('Карточка Anime365 для этого тайтла не найдена.');
    return api('/api/library', {method:'POST', body:JSON.stringify({
      series_id:item.anime365_series_id, title:item.title, year:item.year == null ? null : String(item.year),
      series_type:item.kind || item.series_type || null, provider:'anime365',
    })}).then(() => details(item.anime365_series_id));
  }
  async function recommendationHistory(source) {
    try {
      state.view = 'recommendations';
      const data = await api(`/api/recommendations/history?source=${encodeURIComponent(source)}`);
      const blocks = (data.items || []).map(pick => {
        const when = new Date(pick.created_at * 1000).toLocaleString('ru-RU');
        const verdict = pick.pair_skipped ? 'Пропущено' : pick.pair_rating ? `Оценка пары: ${pick.pair_rating}` : 'Без отзыва';
        const moodText = pick.mood_label && pick.mood_label !== 'Любое' ? ` · Настроение: ${esc(pick.mood_label)}` : '';
        const liked = (pick.items || []).filter(item => item.interested).map(item => item.title);
        const body = pick.items && pick.items.length ? recommendationCards(pick.items) : '<p class="empty">В этой подборке не было тайтлов.</p>';
        return `<section class="panel"><p class="meta">${esc(when)} · ${esc(verdict)}${moodText}${liked.length ? ` · Интересно: ${esc(liked.join(', '))}` : ''}</p>${body}</section>`;
      }).join('');
      root.innerHTML = `<section class="page-heading"><p class="eyebrow">${esc(source === 'xai' ? 'Grok' : 'ChatGPT')}</p><h1>История подборок</h1></section>${blocks || '<p class="empty">Пока нет прошлых подборок.</p>'}<button class="action secondary back">Назад</button>`;
      useBack(); root.querySelector('.back').onclick = () => recommendations(source);
      root.querySelectorAll('[data-recommendation]').forEach(button => button.onclick = async () => {
        try { await openRecommendation((data.items || []).flatMap(pick => pick.items || []), button.dataset.recommendation); }
        catch (error) { fail(error); }
      });
      bindReasonToggles(root);
    } catch (error) { fail(error); }
  }
  async function recommendations(preferredSource) {
    clearTimeout(recommendationTimer);
    try {
      state.view = 'recommendations';
      const data = await api('/api/recommendations');
      if (state.view !== 'recommendations') return;
      if (!data.shikimori_connected) {
        root.innerHTML = `<section class="page-heading"><p class="eyebrow">Для вас</p><h1>Рекомендации</h1><p class="meta">Подбор опирается на ваш список и оценки Shikimori.</p></section><section class="panel"><p>Сначала подключите Shikimori.</p>${data.shikimori_configured ? '<button class="action" id="connect-shikimori">Подключить Shikimori</button>' : '<p class="meta">OAuth Shikimori ещё не настроен на сервере.</p>'}</section><button class="action secondary back">Назад</button>`;
        useBack(); root.querySelector('.back').onclick = more;
        const connect = root.querySelector('#connect-shikimori');
        if (connect) connect.onclick = async () => { try { const result = await api('/api/shikimori/connect', {method:'POST'}); location.href = result.authorization_url; } catch (error) { fail(error); } };
        return;
      }
      const source = data.sources[preferredSource] ? preferredSource : (state.recommendationSource || 'openai');
      state.recommendationSource = source;
      const current = data.sources[source];
      const job = current.job;
      const pick = current.pick;
      const moodOptions = data.moods && data.moods.length ? data.moods : RECOMMENDATION_MOODS.map(([id, label]) => ({id, label}));
      const remembered = state.recommendationMood || data.mood || {};
      const selectedMoods = new Set(remembered.chips || ['any']);
      if (![...selectedMoods].some(id => id !== 'any')) { selectedMoods.clear(); selectedMoods.add('any'); }
      let moodNote = cleanMoodNote(remembered.note);
      const rememberMood = () => { state.recommendationMood = {chips: [...selectedMoods], note: moodNote}; };
      const moodKey = () => recommendationMoodKey([...selectedMoods], moodNote);
      const moodActive = () => Boolean(job && (job.state === 'queued' || job.state === 'running') && (job.mood_key || 'any|') === moodKey());
      const active = moodActive();
      const moodStatus = job && job.mood_label && job.mood_label !== 'Любое' ? ` Настроение: ${job.mood_label}.` : '';
      const statusText = !job ? '' : job.state === 'queued' ? `Подбор в очереди.${moodStatus} Можно закрыть приложение — запрос не продублируется.` : job.state === 'running' ? `Подбираем аниме.${moodStatus} Прошлая подборка остаётся на экране.` : job.state === 'failed' ? job.error_message || 'Не удалось подобрать аниме.' : '';
      const remaining = Math.max(0, Number(current.daily_limit) - Number(current.daily_used));
      const continuations = data.continuations || [];
      const continuationBlock = continuations.length ? `<section class="panel"><h2>Вышло продолжение</h2><p class="meta">Следующий сезон того, что вы уже досмотрели. Это не занимает место в подборке.</p>${recommendationCards(continuations.map(item => ({...item, reason: item.caption || (item.after_title ? `Продолжение «${item.after_title}» · у тебя просмотрено` : '')})) )}</section>` : '';
      let pickBlock = '';
      if (pick && pick.items && pick.items.length) {
        const saved = pick.pair_skipped ? 'Пара пропущена.' : pick.pair_rating ? `Ваша оценка пары: ${pick.pair_rating}.` : '';
        const pickMood = pick.mood_label && pick.mood_label !== 'Любое' ? `<p class="meta">Настроение: ${esc(pick.mood_label)}</p>` : '';
        pickBlock = `${pickMood}${recommendationCards(pick.items)}<section class="panel"><h2>Как вам пара?</h2><p class="meta">Оценка пары не меняет веса жанров. Она только передаётся в следующий запрос. Отметьте, что хочется посмотреть.</p><div class="score-row" id="pair-scores">${[1,2,3,4,5,6,7,8,9,10].map(score => `<button type="button" data-score="${score}" class="${pick.pair_rating === score ? 'selected' : ''}">${score}</button>`).join('')}</div><button class="action secondary" id="skip-pair">Пропустить</button><div>${pick.items.map(item => `<label><input type="checkbox" data-interest="${esc(item.shikimori_anime_id)}" ${item.interested ? 'checked' : ''}> ${esc(item.title)}</label><br>`).join('')}</div><button class="action" id="save-feedback">Сохранить отзыв</button><p class="meta" id="feedback-state">${esc(saved)}</p></section>`;
      } else if (pick) {
        pickBlock = '<p class="empty">Сейчас нет тайтла, который хорошо подходит. Можно попробовать позже или другой источник.</p>';
      } else if (!active) {
        pickBlock = '<p class="empty">Нажмите «Подобрать аниме». Пришлём не больше двух тайтлов и коротко объясним, почему они могут вам зайти.</p>';
      }
      const moodBlock = `<section class="panel"><h2>Настроение</h2><p class="meta">Можно выбрать несколько. «Любое» подбирает как раньше, по вкусу.</p><div class="mood-chips">${moodOptions.map(item => `<button type="button" data-mood="${esc(item.id)}" class="${selectedMoods.has(item.id) ? 'selected' : ''}">${esc(item.label)}</button>`).join('')}</div><label class="mood-note">Уточнение<input id="mood-note" maxlength="80" value="${esc(moodNote)}" placeholder="Необязательно, пара слов"></label></section>`;
      root.innerHTML = `<section class="page-heading"><p class="eyebrow">Для вас</p><h1>Рекомендации</h1><p class="meta">Оценки сравниваются с вашей обычной шкалой. «Брошено» учитывается отдельно и не штрафует жанры.</p></section><div class="source-switch"><button class="action ${source === 'openai' ? '' : 'secondary'}" data-source="openai">ChatGPT</button><button class="action ${source === 'xai' ? '' : 'secondary'}" data-source="xai">Grok</button></div><p class="meta">Сегодня осталось ${esc(remaining)} из ${esc(current.daily_limit)} для ${esc(current.label)}.</p>${current.configured ? '' : '<p class="meta">Ключ этого источника не настроен на сервере.</p>'}${moodBlock}<button class="action" id="recommend">${active ? 'Обновить статус' : 'Подобрать аниме'}</button><p class="${job && job.state === 'failed' ? 'error' : 'meta'}" id="recommendation-error">${esc(statusText)}</p>${continuationBlock}${pickBlock}<button class="action secondary" id="recommendation-history">История</button><button class="action secondary back">Назад</button>`;
      useBack(); root.querySelector('.back').onclick = more;
      root.querySelectorAll('[data-source]').forEach(button => button.onclick = () => recommendations(button.dataset.source));
      const paintMoods = () => {
        root.querySelectorAll('[data-mood]').forEach(button => button.classList.toggle('selected', selectedMoods.has(button.dataset.mood)));
        const recommend = root.querySelector('#recommend');
        if (recommend) recommend.textContent = moodActive() ? 'Обновить статус' : 'Подобрать аниме';
      };
      root.querySelectorAll('[data-mood]').forEach(button => button.onclick = () => {
        const id = button.dataset.mood;
        if (id === 'any') { selectedMoods.clear(); selectedMoods.add('any'); }
        else {
          selectedMoods.delete('any');
          if (selectedMoods.has(id)) selectedMoods.delete(id); else selectedMoods.add(id);
          if (!selectedMoods.size) selectedMoods.add('any');
        }
        rememberMood();
        paintMoods();
      });
      const noteInput = root.querySelector('#mood-note');
      if (noteInput) noteInput.oninput = () => { moodNote = cleanMoodNote(noteInput.value); rememberMood(); paintMoods(); };
      root.querySelector('#recommend').onclick = async () => {
        const button = root.querySelector('#recommend');
        try {
          button.disabled = true;
          moodNote = cleanMoodNote(noteInput ? noteInput.value : moodNote);
          await api(`/api/recommendations/${source}`, {method:'POST', body:JSON.stringify({chips:[...selectedMoods], note:moodNote})});
          await recommendations(source);
        } catch (error) {
          const slot = root.querySelector('#recommendation-error');
          if (slot && state.view === 'recommendations') { slot.className = 'error'; slot.textContent = error.message; button.disabled = false; }
          else fail(error);
        }
      };
      root.querySelector('#recommendation-history').onclick = () => recommendationHistory(source);
      const cards = [...(pick && pick.items || []), ...continuations];
      root.querySelectorAll('[data-recommendation]').forEach(button => button.onclick = async () => {
        try { await openRecommendation(cards, button.dataset.recommendation); }
        catch (error) { fail(error); }
      });
      bindReasonToggles(root);
      const save = root.querySelector('#save-feedback');
      if (save && pick) {
        let selected = pick.pair_rating || null;
        root.querySelectorAll('[data-score]').forEach(button => button.onclick = () => {
          selected = Number(button.dataset.score);
          root.querySelectorAll('[data-score]').forEach(item => item.classList.toggle('selected', Number(item.dataset.score) === selected));
        });
        const send = async skip => {
          const interested = [...root.querySelectorAll('[data-interest]:checked')].map(item => item.dataset.interest);
          const body = {pick_id: pick.id, skip, interested_ids: interested};
          if (!skip) {
            if (!selected) { root.querySelector('#feedback-state').textContent = 'Выберите оценку от 1 до 10 или пропуск.'; return; }
            body.pair_rating = selected;
          }
          await api(`/api/recommendations/${source}/feedback`, {method:'POST', body:JSON.stringify(body)});
          await recommendations(source);
        };
        save.onclick = async () => { try { await send(false); } catch (error) { fail(error); } };
        root.querySelector('#skip-pair').onclick = async () => { try { await send(true); } catch (error) { fail(error); } };
      }
      if (active || data.continuations_refreshing) recommendationTimer = setTimeout(() => { if (state.view === 'recommendations') recommendations(source); }, 3000);
    } catch (error) { if (state.view === 'recommendations') fail(error); }
  }
  let seasonalTimer;
  async function seasonal() {
    clearTimeout(seasonalTimer);
    state.view = 'seasonal';
    root.innerHTML = '<p class="status">Загружаем новинки сезона…</p>';
    try {
      const data = await api('/api/seasonal');
      if (state.view !== 'seasonal') return;
      const [season, year] = data.season.split('_');
      const label = {winter:'Зима', spring:'Весна', summer:'Лето', fall:'Осень'}[season];
      const items = data.items || [];
      const updated = data.updated_at ? new Date(data.updated_at * 1000).toLocaleString('ru-RU') : '';
      root.innerHTML = `<section class="page-heading"><p class="eyebrow">${esc(label)} ${esc(year)}</p><h1>Новинки сезона</h1><p class="meta">Свежие сезоны, которые сейчас выходят и доступны с русским переводом.</p>${updated ? `<p class="meta">Обновлено: ${esc(updated)}${data.stale ? '. Обновление задерживается.' : ''}</p>` : ''}</section>${items.length ? `<section class="grid">${items.map(item => `<article class="card"><button data-seasonal="${esc(item.series_id)}">${item.poster_url ? `<img class="poster" src="${esc(item.poster_url)}" alt="" loading="lazy">` : '<div class="poster catalog-placeholder">Anime365</div>'}<strong>${esc(item.title)}</strong><div class="meta">${Number(item.score) > 0 ? `★ ${esc(item.score)} · ` : ''}Доступно серий: ${esc(item.available_episodes)}</div><span class="text-link">Смотреть ›</span></button></article>`).join('')}</section>` : `<p class="empty">${data.updated_at ? 'Пока нет выходящих новинок с русским переводом.' : data.refreshing ? 'Собираем новинки сезона. Список появится автоматически.' : 'Не удалось загрузить новинки. Повторим обновление позже.'}</p>`}<button class="action secondary" id="reload-seasonal">Обновить страницу</button><button class="action secondary back">Назад</button>`;
      useBack(); root.querySelector('.back').onclick = more;
      root.querySelector('#reload-seasonal').onclick = seasonal;
      root.querySelectorAll('[data-seasonal]').forEach(button => button.onclick = async () => {
        try {
          button.disabled = true;
          const item = items.find(row => Number(row.series_id) === Number(button.dataset.seasonal));
          await api('/api/library', {method:'POST', body:JSON.stringify({
            series_id:item.series_id, title:item.title, year:item.year == null ? null : String(item.year),
            series_type:item.series_type, provider:'anime365',
          })});
          await details(item.series_id);
        } catch(error) { button.disabled = false; fail(error); }
      });
      if (data.refreshing) seasonalTimer = setTimeout(() => { if (state.view === 'seasonal') seasonal(); }, 5000);
    } catch(error) { if (state.view === 'seasonal') fail(error); }
  }
  async function search(provider = 'anime365') {
    if (provider === 'hentai365' && !state.adultCatalog) return more();
    state.view = provider === 'hentai365' ? 'hentai' : 'search';
    const label = provider === 'hentai365' ? 'Hentai365' : 'Anime365';
    root.innerHTML = `<section class="page-heading"><p class="eyebrow">${label}</p><h1>Каталог</h1></section><label class="search-field"><span>⌕</span><input id="query" placeholder="Название на русском или английском"></label><button class="action search-action" id="go">Найти</button><section id="results"></section><button class="action secondary back">Назад</button>`;
    useBack(); root.querySelector('.back').onclick = provider === 'hentai365' ? more : home;
    root.querySelector('#go').onclick = async () => { try { const endpoint = provider === 'hentai365' ? '/api/hentai/catalog' : '/api/catalog'; const data = await api(`${endpoint}?query=${encodeURIComponent(root.querySelector('#query').value)}`); root.querySelector('#results').innerHTML = catalogCards(data.items, provider); root.querySelectorAll('[data-catalog-series]').forEach(button => button.onclick = async () => { try { const item = data.items.find(row => Number(row.series_id) === Number(button.dataset.catalogSeries)); if (!item) throw new Error('Выбранный тайтл больше не найден. Повторите поиск.'); await api('/api/library', { method:'POST', body: JSON.stringify({ series_id:item.series_id, title:item.title, year:item.year == null ? null : String(item.year), series_type:item.series_type, provider }) }); await details(item.series_id); } catch(error) { fail(error); } }); } catch(error) { fail(error); } };
  }
  const hentai = () => search('hentai365');
  function backgroundImportText(item) {
    if (!item) return 'Фоновый импорт будет запущен после подключения.';
    const label = {queued:'в очереди',running:'выполняется',ready:'завершён',failed:'ожидает повторной попытки'}[item.state] || item.state;
    const count = item.imported_count ? ` · ${item.imported_count} тайтлов` : '';
    return `Фоновый импорт: ${label}${count}.`;
  }
  async function settings() { try { state.view = 'settings'; const data = await api('/api/shikimori/status'); root.innerHTML = `<section class="page-heading"><p class="eyebrow">Профиль</p><h1>Настройки</h1></section><section class="panel"><label class="adult-toggle"><span>Показывать каталог 18+ на этом устройстве</span><input id="adult-catalog" type="checkbox" ${state.adultCatalog ? 'checked' : ''}></label></section><section class="panel"><h2>Shikimori</h2><p class="meta">${data.connected ? 'Подключён' : data.configured ? 'Не подключён' : 'OAuth не настроен на сервере'}</p>${data.connected ? `<label><input type="checkbox" id="shiki-sync" ${data.sync_enabled ? 'checked' : ''}> Синхронизировать просмотренные серии</label><label><input type="checkbox" id="shiki-auto-complete" ${data.auto_complete ? 'checked' : ''}> После последней серии переводить в «Просмотрено»</label><p class="meta">Авто-перевод срабатывает только после завершения последней доступной серии и только при включённой синхронизации.</p><p class="meta">${esc(backgroundImportText(data.background_import))}</p><button class="action secondary" id="save-shiki-settings">Сохранить</button><button class="action" id="import">Импортировать список</button><button class="action secondary" id="disconnect">Отключить</button>` : data.configured ? '<button class="action" id="connect">Подключить Shikimori</button>' : ''}</section><button class="action secondary back">Назад</button>`; root.querySelector('.back').onclick = more; root.querySelector('#adult-catalog').onchange = event => setAdultPreference(event.target.checked); if (data.connected) { root.querySelector('#disconnect').onclick = async () => { await api('/api/shikimori', {method:'DELETE'}); settings(); }; root.querySelector('#save-shiki-settings').onclick = async () => { try { await api('/api/shikimori/settings', {method:'PATCH',body:JSON.stringify({sync_enabled:root.querySelector('#shiki-sync').checked,auto_complete:root.querySelector('#shiki-auto-complete').checked})}); settings(); } catch(error) { fail(error); } }; root.querySelector('#import').onclick = shikimoriImport; } if (data.configured && !data.connected) root.querySelector('#connect').onclick = async () => { const result = await api('/api/shikimori/connect', {method:'POST'}); location.href = result.authorization_url; }; } catch(error) { fail(error); } }
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
  async function details(seriesId, showAllEpisodes = false) {
    try {
      state.view = 'details';
      const data = await api(`/api/library/${seriesId}`);
      if (data.item.provider === 'hentai365' && !state.adultCatalog) { more(); return false; }
      state.selected = data;
      const p = data.playback;
      const nextEpisode = data.episodes.find(item => !item.watched) || data.episodes[data.episodes.length - 1];
      const resumeEpisode = data.episodes.find(item => Number(item.id) === Number(p?.episode_id));
      state.episodeFocusId = resumeEpisode?.id || nextEpisode?.id || null;
      state.episodeRadius = 2;
      state.episodesFullyOpen = showAllEpisodes;
      const currentMode = data.item.notifications_enabled ? data.item.notification_mode : '';
      const isHentai = data.item.provider === 'hentai365';
      const shikimori = isHentai ? '' : data.item.shikimori_status
        ? `<section class="panel"><h2>Shikimori</h2><select id="shiki-status">${[['planned','Запланировано'],['watching','Смотрю'],['rewatching','Пересматриваю'],['completed','Просмотрено'],['on_hold','Отложено'],['dropped','Брошено']].map(([value,label]) => `<option value="${value}" ${data.item.shikimori_status === value ? 'selected' : ''}>${label}</option>`).join('')}</select><button class="action secondary" id="save-shiki-status">Сохранить статус</button><button class="action secondary" id="refresh-shikimori">Обновить обложку</button><button class="action secondary" id="choose-shikimori">Перепривязать Shikimori</button></section>`
        : `<section class="panel"><h2>Shikimori</h2><p class="meta">Привяжите тайтл из уже импортированного списка, чтобы добавить статус и обложку.</p><button class="action secondary" id="choose-shikimori">Привязать Shikimori</button></section>`;
      const notifications = isHentai ? '' : `<section class="panel"><h2>Уведомления</h2><select id="notification-mode"><option value="">Отключены</option><option value="any" ${currentMode === 'any' ? 'selected' : ''}>Любая новая серия</option><option value="subtitles" ${currentMode === 'subtitles' ? 'selected' : ''}>Русские субтитры</option><option value="voice" ${currentMode === 'voice' ? 'selected' : ''}>Русская озвучка</option></select><button class="action secondary" id="save-notifications">Сохранить уведомления</button></section>`;
      const resumeAvailable = p && resumeEpisode;
      root.innerHTML = `<section class="title-header"><div><h1>${esc(data.item.title)}</h1><p class="meta">Просмотрено: ${esc(data.item.last_watched_episode_number || 0)} / ${data.episodes.length}${data.item.shikimori_status ? ` · Shikimori: ${esc(shikimoriStatus(data.item.shikimori_status))}` : ''}</p>${resumeAvailable ? `<button class="action" id="resume">Продолжить серию ${esc(resumeEpisode.number)} с ${Math.floor(p.position_seconds/60)}:${String(Math.floor(p.position_seconds%60)).padStart(2,'0')}</button>` : p ? '<p class="meta">Сохранённая серия больше недоступна. Выберите доступную серию ниже.</p>' : ''}</div>${data.item.poster_url ? `<img class="detail-poster" src="${esc(data.item.poster_url)}" alt="" loading="lazy">` : ''}</section>${notifications}${shikimori}<h2>Серии</h2>${episodePicker(data)}${isHentai ? '' : '<button class="action secondary" id="batch-download">Скачать серии</button>'}<button class="action secondary" id="remove-series">Удалить из «Смотрю»</button><button class="action secondary back">Назад</button>`;
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
      if (resumeAvailable) root.querySelector('#resume').onclick = () => resumeDirect(p.episode_id);
      const batchDownload = root.querySelector('#batch-download'); if (batchDownload) batchDownload.onclick = startBatchDownload;
      bindEpisodePicker(data);
      return true;
    } catch(error) { fail(error); return false; }
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
  async function resumeDirect(episodeId) {
    try {
      const playback = state.selected?.playback;
      state.episode = state.selected.episodes.find(item => Number(item.id) === Number(episodeId));
      state.resumePosition = Number(playback?.position_seconds) || 0;
      state.resumeEpisodeId = Number(episodeId);
      if (!playback?.translation_id || !playback?.quality) {
        await selectEpisode(episodeId);
        return;
      }
      const data = await api(`/api/episodes/${episodeId}/translations?series_id=${encodeURIComponent(state.selected.item.series_id)}`);
      const translation = data.groups?.flatMap(group => group.items || []).find(item => Number(item.id) === Number(playback.translation_id));
      if (!translation) { await selectEpisode(episodeId); return; }
      const qualities = await api(`/api/translations/${translation.id}/qualities?series_id=${encodeURIComponent(state.selected.item.series_id)}`);
      if (!qualities.items?.some(value => Number(value) === Number(playback.quality))) {
        await selectEpisode(episodeId);
        return;
      }
      state.translation = Number(translation.id);
      await player(Number(playback.quality));
    } catch (_) {
      // If metadata or the saved choice is stale, let the user choose a current source.
      await selectEpisode(episodeId);
    }
  }
  async function selectEpisode(episodeId) { try { state.episode = state.selected.episodes.find(item => item.id === episodeId); const data = await api(`/api/episodes/${episodeId}/translations?series_id=${encodeURIComponent(state.selected.item.series_id)}`); state.view = 'details'; root.innerHTML = `<h1>Серия ${esc(state.episode.number)}</h1>${state.episode.watched ? '<p class="meta">Серия отмечена просмотренной.</p>' : '<section class="panel"><button class="action secondary" id="mark-watched">Отметить просмотренной</button><p class="meta">Серия и предыдущие будут отмечены просмотренными.</p></section>'}${data.groups.map(group => `<section class="panel"><h2>${esc(group.label)}</h2>${group.items.map(item => `<button class="action secondary" data-translation="${item.id}">${esc(item.authorsSummary || item.title || 'Перевод')}</button>`).join('')}</section>`).join('')}<button class="action secondary back">Назад</button>`; useBack(); root.querySelector('.back').onclick = () => details(state.selected.item.series_id); const markWatched = root.querySelector('#mark-watched'); if (markWatched) markWatched.onclick = async () => { try { markWatched.disabled = true; markWatched.textContent = 'Отмечаем…'; await api(`/api/library/${state.selected.item.series_id}/episodes/${state.episode.id}/watched`, {method:'POST'}); details(state.selected.item.series_id); } catch(error) { fail(error); } }; root.querySelectorAll('[data-translation]').forEach(button => button.onclick = () => qualities(Number(button.dataset.translation))); } catch(error) { fail(error); } }
  async function qualities(translationId) { try { state.translation = translationId; const data = await api(`/api/translations/${translationId}/qualities?series_id=${encodeURIComponent(state.selected.item.series_id)}`); state.view = 'details'; root.innerHTML = `<h1>Качество</h1>${data.items.map(value => `<section class="panel"><strong>${value}p</strong><div><button class="action" data-watch="${value}">Смотреть</button><button class="action secondary" data-download="${value}">Скачать</button></div></section>`).join('')}<button class="action secondary back">Назад</button>`; root.querySelector('.back').onclick = () => selectEpisode(state.episode.id); root.querySelectorAll('[data-watch]').forEach(button => button.onclick = () => player(Number(button.dataset.watch))); root.querySelectorAll('[data-download]').forEach(button => button.onclick = () => downloadOptions(Number(button.dataset.download))); } catch(error) { fail(error); } }
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
  function playerExtrasMarkup(episodes, episode, subtitles) {
    return `<div class="video-tools" role="group" aria-label="Управление видео">
      <button type="button" class="video-skip" title="Вперёд на 85 секунд">+85 с</button>
      <label class="video-speed"><select aria-label="Скорость воспроизведения"><option value="1">1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option></select></label>
      ${subtitles ? `<details class="video-captions"><summary aria-label="Настройки субтитров" title="Субтитры">CC</summary>
        <div class="captions-panel"><p id="subtitle-status" role="status">Загружаем субтитры…</p>
        <label class="caption-size" hidden>Размер текста <select aria-label="Размер субтитров"><option value="default">Обычный</option><option value="large">Крупный</option><option value="larger">Очень крупный</option></select></label></div></details>` : ''}
      <button type="button" id="fullscreen" aria-label="На весь экран" title="На весь экран">⛶</button>
    </div>`;
  }
  function playerEpisodeList(episodes, current) {
    return `<section class="season-episodes"><h2>Серии</h2><div class="season-scroll">${episodes.map(ep =>
      `<button type="button" data-player-episode="${esc(ep.id)}"${ep.id === current.id ? ' aria-current="true" disabled' : ''}>
      ${ep.watched ? '<span aria-label="Просмотрено">✓</span> ' : ''}${esc(ep.number)}</button>`).join('')}</div></section>`;
  }
  function bindPlayerExtras(video, episodes, current, switchEpisode) {
    const surface = video.closest('.player-surface');
    const seek = delta => {
      if (Number.isFinite(video.duration)) {
        video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + delta));
      }
    };
    surface.querySelector('.video-skip').onclick = () => seek(85);
    surface.querySelector('.video-speed select').onchange = event => {
      video.playbackRate = Number(event.target.value);
    };
    const size = surface.querySelector('.caption-size select');
    if (size) size.onchange = () => { video.dataset.captionSize = size.value; };
    let previous;
    const tap = event => {
      if (event.pointerType !== 'touch' || !event.isPrimary) return;
      const rect = video.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width;
      const side = x < 0.32 ? -1 : x > 0.68 ? 1 : 0;
      if (!side || event.clientY > rect.bottom - 44) { previous = null; return; }
      if (previous && previous.side === side && event.timeStamp - previous.time < 350 &&
          Math.hypot(event.clientX - previous.x, event.clientY - previous.y) < 36) {
        event.preventDefault();
        seek(side * 10);
        previous = null;
      } else {
        previous = {side, time: event.timeStamp, x: event.clientX, y: event.clientY};
      }
    };
    video.addEventListener('pointerup', tap);
    const list = root.querySelector('.season-scroll');
    const active = list.querySelector('[aria-current]');
    if (active) list.scrollLeft = active.offsetLeft - (list.clientWidth - active.offsetWidth) / 2;
    list.onclick = event => {
      const target = event.target.closest('[data-player-episode]');
      if (target && !target.disabled) switchEpisode(Number(target.dataset.playerEpisode));
    };
    return () => { video.removeEventListener('pointerup', tap); list.onclick = null; };
  }
  async function requestVideoFullscreen(video) {
    // A video-only native fullscreen surface excludes the libass canvas.
    const target = video.closest?.('.player-surface') || video;
    const request = target.requestFullscreen || target.webkitRequestFullscreen;
    if (!request) return false;
    try { await request.call(target); return true; }
    catch (_) { return false; }
  }
  async function playerFullscreen(video) {
    await video.togglePlayerFullscreen?.();
  }
  function watchPlayerFullscreen(video, button) {
    const surface = video.closest('.player-surface');
    const exitButton = surface.querySelector('.player-exit');
    const backButton = telegram?.BackButton;
    let active = false, disposed = false, pending = false, timer;
    let nativeOwned = false, telegramOwned = false, backVisible = false;
    const nativeElement = () => document.fullscreenElement || document.webkitFullscreenElement;
    const reveal = () => {
      clearTimeout(timer);
      surface.classList.remove('controls-idle');
      if (active) timer = setTimeout(() => {
        // Native video controls manage their own idle state. Never disable them.
        if (!exitButton.matches(':focus-visible, :hover, :active')) {
          surface.classList.add('controls-idle');
        }
      }, 3000);
    };
    const render = () => {
      document.body.classList.toggle('player-expanded', active);
      button.title = active ? 'Выйти из полного экрана' : 'На весь экран';
      button.setAttribute?.('aria-label', button.title);
      if (active) { backButton?.hide(); reveal(); }
      else {
        clearTimeout(timer);
        surface.classList.remove('controls-idle');
        if (backVisible) backButton?.show();
      }
    };
    const exit = async () => {
      active = false;
      render();
      try {
        if (nativeOwned && nativeElement() === surface) {
          const leave = document.exitFullscreen || document.webkitExitFullscreen;
          await leave?.call(document);
        }
      } catch (_) {}
      nativeOwned = false;
      if (telegramOwned && telegram?.isFullscreen) {
        try { telegram.exitFullscreen?.(); } catch (_) {}
      }
      telegramOwned = false;
    };
    const toggle = async () => {
      if (active) { await exit(); return; }
      if (pending || disposed) return;
      pending = true;
      backVisible = Boolean(backButton?.isVisible);
      active = true;
      render();
      const entered = await requestVideoFullscreen(video);
      pending = false;
      nativeOwned = entered;
      if (disposed || !active) { await exit(); return; }
      if (!entered && !telegram?.isFullscreen && telegram?.requestFullscreen &&
          (!telegram.isVersionAtLeast || telegram.isVersionAtLeast('8.0'))) {
        telegramOwned = true;
        try { telegram.requestFullscreen(); } catch (_) { telegramOwned = false; }
      }
    };
    const updateNative = () => {
      if (nativeElement() === surface) nativeOwned = true;
      else if (nativeOwned) { nativeOwned = false; void exit(); }
    };
    const updateTelegram = () => {
      if (telegramOwned && !telegram.isFullscreen) { telegramOwned = false; void exit(); }
    };
    const failed = () => { button.title = 'Доступен режим на всё окно'; };
    const escape = event => { if (event.key === 'Escape' && active) void exit(); };
    const events = ['pointermove', 'pointerdown', 'pointerup', 'focusin', 'focusout', 'keydown'];
    events.forEach(name => surface.addEventListener(name, reveal));
    document.addEventListener('keydown', escape);
    document.addEventListener('fullscreenchange', updateNative);
    document.addEventListener('webkitfullscreenchange', updateNative);
    telegram?.onEvent?.('fullscreenChanged', updateTelegram);
    telegram?.onEvent?.('fullscreenFailed', failed);
    exitButton.onclick = exit;
    video.togglePlayerFullscreen = toggle;
    return () => {
      disposed = true;
      void exit();
      clearTimeout(timer);
      events.forEach(name => surface.removeEventListener(name, reveal));
      document.removeEventListener('keydown', escape);
      document.removeEventListener('fullscreenchange', updateNative);
      document.removeEventListener('webkitfullscreenchange', updateNative);
      telegram?.offEvent?.('fullscreenChanged', updateTelegram);
      telegram?.offEvent?.('fullscreenFailed', failed);
      exitButton.onclick = null;
      delete video.togglePlayerFullscreen;
    };
  }
  function loadPlayerSubtitles(video, url, status, availableFonts = {}) {
    const controller = new AbortController();
    let objectUrl;
    let renderer;
    let toggle;
    let resizeSelect;
    const track = document.createElement('track');
    track.kind = 'subtitles'; track.srclang = 'ru'; track.label = 'Субтитры'; track.default = true;
    track.addEventListener('load', () => {
      track.track.mode = 'showing';
      const size = status.parentElement?.querySelector('.caption-size');
      if (size) size.hidden = false;
      toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.textContent = 'Скрыть субтитры';
      toggle.onclick = () => {
        const show = track.track.mode !== 'showing';
        track.track.mode = show ? 'showing' : 'disabled';
        toggle.textContent = show ? 'Скрыть субтитры' : 'Показать субтитры';
      };
      status.after(toggle);
      status.textContent = 'Русские субтитры';
    });
    track.addEventListener('error', () => { status.textContent = 'Не удалось прочитать субтитры.'; });
    (async () => {
      try {
        const response = await fetch(url, {
          headers: initData ? {'X-Telegram-Init-Data': initData} : {},
          credentials: 'same-origin', signal: controller.signal,
        });
        if (!response.ok) throw new Error('Subtitle request failed');
        const body = await response.text();
        if (controller.signal.aborted) return;
        if (/^\s*\[Script Info\]/im.test(body)) {
          // Keep the entire script, including PlayRes, styles, layers, drawings,
          // embedded fonts and animations. libass owns layout and the video clock.
          if (typeof SubtitlesOctopus !== 'function') throw new Error('libass unavailable');
          // Load only families requested by this script, but include every face
          // so libass can choose real bold/italic instead of synthesizing them.
          const families = new Set();
          const familyKey = name => name.trim().replace(/^@/, '').toLowerCase();
          let section = '', fields = [];
          for (const raw of body.split(/\r?\n/)) {
            const line = raw.trim();
            if (line.startsWith('[')) section = line.toLowerCase();
            else if (section === '[v4+ styles]' || section === '[v4 styles]') {
              if (/^Format:/i.test(line)) fields = line.slice(line.indexOf(':') + 1).split(',').map(v => v.trim().toLowerCase());
              else if (/^Style:/i.test(line)) {
                const name = line.slice(line.indexOf(':') + 1).split(',')[fields.indexOf('fontname')];
                if (name) families.add(familyKey(name));
              }
            } else if (section === '[events]' && /^Dialogue:/i.test(line)) {
              for (const block of line.matchAll(/\{([^}]*)\}/g)) {
                for (const match of block[1].matchAll(/\\fn([^\\}]+)/g)) families.add(familyKey(match[1]));
              }
            }
          }
          const fonts = [...new Set([...families].flatMap(name => availableFonts[name] || []))];
          // Change declared font sizes only on request. Keep line layout tags,
          // drawings, timing and an untouched original for the default setting.
          const resizedScript = () => {
            const factor = {large: 1.25, larger: 1.5}[resizeSelect?.value];
            if (!factor) return body;
            let inStyles = false, sizeIndex = -1;
            return body.replace(/[^\r\n]+/g, line => {
              const trimmed = line.trim();
              if (trimmed.startsWith('[')) {
                inStyles = /^\[v4\+? styles\]$/i.test(trimmed);
                sizeIndex = -1;
              }
              if (!inStyles) return line;
              const colon = line.indexOf(':');
              if (colon < 0) return line;
              const values = line.slice(colon + 1).split(',');
              if (/^Format:/i.test(trimmed)) {
                sizeIndex = values.findIndex(key => key.trim().toLowerCase() === 'fontsize');
              } else if (/^Style:/i.test(trimmed) && sizeIndex >= 0 && Number(values[sizeIndex]) > 0) {
                values[sizeIndex] = String(Number(values[sizeIndex]) * factor);
                return line.slice(0, colon + 1) + values.join(',');
              }
              return line;
            });
          };
          let visible = true;
          video.setAttribute('controlslist', 'nofullscreen');
          video.disablePictureInPicture = true;
          renderer = new SubtitlesOctopus({
            video, subContent: body, fonts,
            workerUrl: '/assets/subtitles-octopus-worker.js',
            legacyWorkerUrl: '/assets/subtitles-octopus-worker-legacy.js',
            fallbackFont: '/assets/LiberationSans-Regular.ttf',
            targetFps: 60,
            onReady: () => {
              if (controller.signal.aborted) return;
              const size = status.parentElement?.querySelector('.caption-size');
              if (size) {
                resizeSelect = size.querySelector('select');
                resizeSelect.onchange = () => { if (visible) renderer.setTrack(resizedScript()); };
                size.hidden = false;
              }
              renderer.setRate(video.playbackRate);
              renderer.setIsPaused(video.paused, video.currentTime);
              status.textContent = 'Русские субтитры';
            },
            onError: () => { if (!controller.signal.aborted) status.textContent = 'Не удалось запустить ASS/SSA-субтитры.'; },
          });
          toggle = document.createElement('button');
          toggle.type = 'button';
          toggle.textContent = 'Скрыть субтитры';
          toggle.onclick = () => {
            if (!renderer?.worker) return;
            visible = !visible;
            if (visible) renderer.setTrack(resizedScript()); else renderer.freeTrack();
            toggle.textContent = visible ? 'Скрыть субтитры' : 'Показать субтитры';
          };
          status.after(toggle);
          return;
        }
        if (!/^\s*WEBVTT(?:\s|$)/.test(body)) throw new Error('Unsupported subtitle format');
        objectUrl = URL.createObjectURL(new Blob([body], {type: 'text/vtt'}));
        track.src = objectUrl;
        video.appendChild(track);
        track.track.mode = 'showing';
      } catch (error) {
        if (error.name !== 'AbortError') status.textContent = 'Не удалось загрузить субтитры. Откройте серию заново.';
      }
    })();
    return () => {
      if (controller.signal.aborted) return;
      controller.abort();
      track.remove();
      toggle?.remove();
      if (resizeSelect) resizeSelect.onchange = null;
      if (renderer?.worker) renderer.dispose();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }
  function matchingPlayerTranslation(currentGroups, nextGroups, translationId) {
    const group = currentGroups.find(group => group.items.some(item => Number(item.id) === Number(translationId)));
    if (!group) return null;
    const selected = group.items.find(item => Number(item.id) === Number(translationId));
    const author = item => String(item.authorsSummary || item.title || '').trim().toLowerCase();
    const candidates = nextGroups.filter(next => next.kind === group.kind && next.language === group.language)
      .flatMap(next => next.items);
    return candidates.find(item => author(item) === author(selected)) || null;
  }

  async function player(quality) {
    try {
      state.view = 'player';
      const seriesId = state.selected.item.series_id;
      const episode = state.episode;
      const translationId = state.translation;
      const episodes = state.selected.episodes;
      const nextEpisode = episodes[episodes.findIndex(item => item.id === episode.id) + 1];
      let active = true, disposed = false, pending = Promise.resolve(), countdownTimer = null;
      let last = 0, usingProxy = false, primaryAction = null;
      const result = await api('/api/play', {method:'POST', body:JSON.stringify({series_id:seriesId, episode_id:episode.id, translation_id:translationId, quality})});
      const episodeType = ({tv:'ТВ', ona:'ONA', ova:'OVA', movie:'Фильм', special:'Спецвыпуск'}[episode.type] || episode.type || 'Эпизод');
      const episodeIndex = String(episode.number).match(/^(?:(?:ONA|OVA|TV)\s+)?(\d+(?:[.,]\d+)?)(?:\s+серия)?$/i);
      const episodeLabel = episodeIndex ? `Серия ${episodeIndex[1]}` : episode.number;
      root.innerHTML = `<section class="player-page"><header class="player-heading"><div><h1>${esc(state.selected.item.title)}</h1><p class="meta">${esc(episodeLabel)} · ${esc(episodeType)}</p></div><button class="player-back action secondary" type="button">Назад</button></header><div class="player-surface"><video controls playsinline></video>${playerExtrasMarkup(episodes, episode, Boolean(result.subtitle_url))}<button class="player-exit" type="button">Выйти из полного экрана</button><section class="player-countdown" id="player-countdown" hidden aria-live="polite"><p>Серия закончена. Следующая начнётся через <strong id="countdown-seconds">10</strong> сек.</p><button class="action secondary" id="cancel-countdown">Отмена</button><button class="action" id="countdown-next">Следующая серия</button></section></div>${playerEpisodeList(episodes,episode)}<div class="player-controls"><button class="action" id="complete-episode" hidden></button><details class="player-more"><summary aria-label="Дополнительные действия">⋯</summary><button class="action secondary" id="fallback">Использовать резервный поток</button></details></div><p class="meta" id="player-progress-status" role="status" aria-live="polite"></p></section>`;
      const video = root.querySelector('video');
      const fullscreenButton = root.querySelector('#fullscreen');
      const stopWatchingFullscreen = watchPlayerFullscreen(video, fullscreenButton);
      const stopSubtitles = result.subtitle_url
        ? loadPlayerSubtitles(video, result.subtitle_url, root.querySelector('#subtitle-status'), result.subtitle_fonts)
        : () => {};
      const stopExtras = bindPlayerExtras(video, episodes, episode, async episodeId => {
        await save(true);
        state.player?.cleanup(); state.player = null;
        await selectEpisode(Number(episodeId));
      });
      const telegramBack = telegram?.BackButton;
      const domBack = root.querySelector('.player-back');
      const leavePlayer = async () => {
        await save(true);
        state.player?.cleanup(); state.player = null;
        await details(seriesId);
      };
      if (typeof releaseNavigationButtons === 'function') releaseNavigationButtons();
      if (initData && telegramBack?.show && telegramBack?.onClick) {
        domBack.hidden = true;
        telegramBack.show(); telegramBack.onClick(leavePlayer);
      } else domBack.onclick = leavePlayer;
      const telegramMain = telegram?.MainButton;
      const domAction = root.querySelector('#complete-episode');
      const mainAvailable = Boolean(initData && telegramMain?.show && telegramMain?.onClick && telegramMain?.setText);
      telegramMain?.enable?.();
      telegramMain?.hide?.();
      const hideMain = () => { telegramMain?.hide?.(); };
      let primaryVisible;
      const refreshMainVisibility = () => {
        const fullscreen = Boolean(telegram?.isFullscreen || (typeof document !== 'undefined' &&
          (document.fullscreenElement || document.body?.classList.contains('player-expanded'))));
        if (mainAvailable && primaryVisible && !fullscreen) telegramMain.show();
        else hideMain();
      };
      const onFullscreenChange = () => refreshMainVisibility();
      if (typeof document !== 'undefined') document.addEventListener?.('fullscreenchange', onFullscreenChange);
      telegram?.onEvent?.('fullscreenChanged', onFullscreenChange);
      const updatePrimaryAction = visible => {
        if (primaryVisible === visible) return;
        primaryVisible = visible;
        const label = nextEpisode ? 'Следующая серия' : 'Завершить серию';
        domAction.textContent = label;
        domAction.hidden = !visible || mainAvailable;
        if (mainAvailable) {
          telegramMain.setText(label);
          refreshMainVisibility();
        }
      };
      const clickPrimary = async () => {
        if (primaryAction) await primaryAction();
      };
      telegramMain?.onClick?.(clickPrimary);
      const hls = isHlsSource(result.media_url);
      const fallback = async () => {
        if (!active || usingProxy) return;
        usingProxy = true;
        const resumeSourceAt = Number(video.currentTime) || 0;
        const button = root.querySelector('#fallback');
        button.disabled = true;
        button.textContent = 'Подключаем резервный поток…';
        const status = root.querySelector('#player-progress-status');
        status.textContent = 'Не удалось открыть основной поток. Подключаем резервный…';
        try {
          await save(true).catch(() => {});
          video.addEventListener('loadedmetadata', () => {
            if (resumeSourceAt > 0 && Number.isFinite(video.duration)) video.currentTime = Math.min(resumeSourceAt, Math.max(0, video.duration - 1));
          }, {once:true});
          await setVideoSource(video, result.proxy_url, hls);
          video.play().catch(() => {});
          button.textContent = 'Резервный поток подключён';
          status.textContent = 'Воспроизведение продолжено через резервный поток.';
        } catch (_) {
          button.disabled = false;
          button.textContent = 'Повторить резервный поток';
          status.textContent = 'Резервный поток сейчас недоступен.';
          usingProxy = false;
        }
      };
      const shouldResume = Number(state.resumeEpisodeId) === Number(episode.id);
      const resumeAt = shouldResume ? Number(state.resumePosition) || 0 : 0;
      if (shouldResume) video.addEventListener('loadedmetadata', () => {
        if (resumeAt > 0 && Number.isFinite(video.duration)) video.currentTime = Math.min(resumeAt, Math.max(0, video.duration - 1));
        video.play().catch(() => {});
        state.resumeEpisodeId = null; state.resumePosition = 0;
      }, {once:true});
      const start = () => setVideoSource(video, result.media_url, hls, () => fallback().catch(() => {}));
      const save = (force = false, ended = false) => {
        if ((!active && !force) || (!force && (!Number.isFinite(video.duration) || !video.duration || Date.now() - last < 15000))) return pending;
        last = Date.now();
        const body = JSON.stringify({series_id:seriesId, episode_id:episode.id,
          position_seconds:video.currentTime || 0,
          duration_seconds:Number.isFinite(video.duration) ? video.duration : 0, ended});
        pending = pending.catch(() => {}).then(() => api('/api/progress', {method:'POST', body}));
        return pending;
      };
      const countdownNode = root.querySelector('#player-countdown');
      const cancelCountdown = () => {
        if (countdownTimer) clearInterval(countdownTimer);
        countdownTimer = null;
        countdownNode.hidden = true;
      };
      const completeCurrent = async () => {
        if (!active) return;
        cancelCountdown();
        domAction.disabled = true;
        if (mainAvailable) telegramMain.disable?.();
        const status = root.querySelector('#player-progress-status');
        active = false;
        video.pause();
        try {
          await save(true);
          await api(`/api/library/${seriesId}/episodes/${episode.id}/watched`, {method:'POST'});
          if (disposed) return;
          episode.watched = true;
          if (!nextEpisode) {
            domAction.textContent = '✓ Серия просмотрена';
            domAction.hidden = mainAvailable;
            hideMain();
            status.textContent = 'Серия отмечена просмотренной.';
            return;
          }
          status.textContent = 'Подбираем перевод следующей серии…';
          const [current, next] = await Promise.all([
            api(`/api/episodes/${episode.id}/translations?series_id=${seriesId}`),
            api(`/api/episodes/${nextEpisode.id}/translations?series_id=${seriesId}`),
          ]);
          if (disposed) return;
          const translation = matchingPlayerTranslation(current.groups, next.groups, translationId);
          if (!translation) { state.player?.cleanup(); state.player = null; await selectEpisode(nextEpisode.id); return; }
          const available = await api(`/api/translations/${translation.id}/qualities?series_id=${seriesId}`);
          if (disposed) return;
          state.episode = nextEpisode;
          state.translation = translation.id;
          state.player?.cleanup(); state.player = null;
          if (available.items.some(value => Number(value) === Number(quality))) { await player(quality); state.player?.video.play().catch(() => {}); }
          else await qualities(translation.id);
        } catch (error) {
          if (disposed) return;
          status.textContent = error.message || 'Не удалось открыть следующую серию.';
          active = true;
          domAction.disabled = false;
          if (mainAvailable) telegramMain.enable?.();
        }
      };
      primaryAction = completeCurrent;
      domAction.onclick = clickPrimary;
      root.querySelector('#fallback').onclick = () => fallback();
      root.querySelector('#cancel-countdown').onclick = cancelCountdown;
      root.querySelector('#countdown-next').onclick = completeCurrent;
      const startCountdown = () => {
        if (!nextEpisode || countdownTimer || disposed) return;
        countdownNode.hidden = false;
        let remaining = 10;
        const seconds = root.querySelector('#countdown-seconds');
        seconds.textContent = String(remaining);
        countdownTimer = setInterval(() => {
          remaining -= 1;
          seconds.textContent = String(Math.max(0, remaining));
          if (remaining <= 0) { cancelCountdown(); completeCurrent(); }
        }, 1000);
      };
      video.addEventListener('seeking', () => {
        if (countdownTimer && Number.isFinite(video.duration) && video.currentTime < video.duration - 1) cancelCountdown();
      });
      video.addEventListener('timeupdate', () => {
        save(false).catch(() => {});
        if (!Number.isFinite(video.duration) || !video.duration) return;
        if (countdownTimer && video.currentTime < video.duration - 1) cancelCountdown();
        updatePrimaryAction(video.duration - video.currentTime <= 120);
      });
      video.addEventListener('pause', () => save(false).catch(() => {}));
      video.addEventListener('ended', () => { save(true, true).catch(() => {}); startCountdown(); updatePrimaryAction(true); });
      fullscreenButton.onclick = () => playerFullscreen(video, fullscreenButton);
      domBack.onclick ||= leavePlayer;
      state.player = {video, flushProgress: () => save(true), cleanup: () => {
        disposed = true; active = false; cancelCountdown(); hideMain();
        if (typeof document !== 'undefined') document.removeEventListener?.('fullscreenchange', onFullscreenChange);
        telegram?.offEvent?.('fullscreenChanged', onFullscreenChange);
        telegramBack?.offClick?.(leavePlayer); telegramBack?.hide?.();
        telegramMain?.offClick?.(clickPrimary);
        stopWatchingFullscreen(); stopSubtitles(); stopExtras(); video.pause(); destroyHls();
      }};
      await start();
      video.addEventListener('error', () => { if (!usingProxy) fallback().catch(() => {}); });
    } catch (error) { destroyHls(); fail(error); }
  }
  api('/api/me').then(async user => { loadAdultPreference(user.user_id); const params = new URLSearchParams(location.search); const seriesId = Number(params.get('series_id')); const episodeId = Number(params.get('episode_id')); if (seriesId) { const opened = await details(seriesId); if (opened && episodeId && state.selected.episodes.some(item => item.id === episodeId)) await selectEpisode(episodeId); } else await home(); }).catch(error => { root.innerHTML = `<p class="error">${esc(error.message)}${initData ? '' : '<br>Откройте страницу из Telegram.'}</p>`; });
})();
