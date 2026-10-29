(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const csrf = $('meta[name="healthos-csrf"]')?.content || '';
  const draftKey = 'healthos.training.draft.v1';
  const pageSize = 50;
  const state = {
    exercises: [], sessions: [], aliases: [], catalog: [], analytics: null,
    offset: 0, total: 0, query: '', selectedSession: null, editingId: null,
    metric: 'e1rm', timerStartedAt: null, timerEndedAt: null, elapsedBase: 0,
  };
  let timerHandle = null;
  let searchHandle = null;

  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);

  function toast(message) {
    const node = $('#toast');
    node.textContent = message;
    node.classList.add('show');
    setTimeout(() => node.classList.remove('show'), 3000);
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      credentials: 'same-origin',
      cache: 'no-store',
      ...options,
      headers: { Accept: 'application/json', ...(options.headers || {}) },
    });
    const type = response.headers.get('content-type') || '';
    const body = type.includes('application/json') ? await response.json() : null;
    if (!response.ok) {
      const error = new Error(body?.detail || body?.error || `请求失败 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function changeView(name) {
    $$('.side-nav button').forEach(button => button.classList.toggle('active', button.dataset.view === name));
    $$('.view').forEach(view => view.classList.toggle('active', view.id === `view-${name}`));
    $('#sidebar').classList.remove('open');
    if (name === 'stats') loadAnalytics().catch(showError);
    if (name === 'aliases') loadAliases().catch(showError);
  }

  function newSet() { return { weight: '', reps: '', rpe: '', note: '', warmup: false, drop: false, segments: [{ weight: '', reps: '' }] }; }
  function newExercise(name = '') { return { name, last: null, sets: [newSet()] }; }

  function groupSessionRecords(records) {
    const groups = new Map();
    for (const record of records || []) {
      const key = `${record.exercise_order}:${record.exercise_id}`;
      if (!groups.has(key)) groups.set(key, { name: record.standard_name || record.raw_exercise_name || '', bodyPart: record.exercise_body_part || '', last: null, sets: [] });
      const group = groups.get(key);
      const extra = record.extra_data && typeof record.extra_data === 'object' ? record.extra_data : {};
      if (extra.structure === 'drop_set_group') {
        group.sets.push({
          weight: '', reps: '', rpe: '', note: record.note || '', warmup: false, drop: true,
          restSeconds: extra.rest_seconds || '',
          segments: (extra.segments || []).map(segment => ({ weight: segment.weight_kg ?? '', reps: segment.reps ?? '' })),
        });
        continue;
      }
      const reps = Array.isArray(record.reps) ? record.reps : [];
      const rpe = Array.isArray(record.rpe) ? record.rpe : [];
      if (reps.length) {
        reps.forEach((count, index) => group.sets.push({
          weight: record.weight_kg ?? '', reps: count ?? '', rpe: rpe[index] ?? '',
          note: record.note || '', warmup: Boolean(record.is_warmup), drop: false,
        }));
      } else {
        for (let index = 0; index < Math.max(1, Number(record.sets_count) || 1); index++) {
          group.sets.push({ weight: record.weight_kg ?? '', reps: '', rpe: '', note: record.note || '', warmup: Boolean(record.is_warmup), drop: false });
        }
      }
    }
    return [...groups.values()];
  }

  function normalSetMarkup(set) {
    return `<input data-field="weight" aria-label="重量 kg" type="number" min="0" step="0.5" inputmode="decimal" value="${escapeHtml(set.weight)}" placeholder="kg">
      <input data-field="reps" aria-label="次数" type="number" min="0" step="1" inputmode="numeric" value="${escapeHtml(set.reps)}" placeholder="次">
      <input data-field="rpe" aria-label="RPE" type="number" min="0" max="10" step="0.5" inputmode="decimal" value="${escapeHtml(set.rpe)}" placeholder="RPE">
      <input class="note-input" data-field="note" aria-label="组备注" value="${escapeHtml(set.note)}" maxlength="1000" placeholder="备注">`;
  }

  function dropSetMarkup(set) {
    const segments = Array.isArray(set.segments) && set.segments.length ? set.segments : [{ weight: '', reps: '' }];
    return `<div class="drop-segments">
      <div class="drop-segment"><span>主组</span><input aria-label="主组重量" type="number" min="0" step="0.5" data-field="weight" value="${escapeHtml(set.weight)}" placeholder="kg"><input aria-label="主组次数" type="number" min="0" step="1" data-field="reps" value="${escapeHtml(set.reps)}" placeholder="次"></div>
      ${segments.map((segment, index) => `<div class="drop-segment"><span>递减 ${index + 1}</span><input aria-label="递减段重量" type="number" min="0" step="0.5" data-segment="${index}" data-field="weight" value="${escapeHtml(segment.weight)}" placeholder="kg"><input aria-label="递减段次数" type="number" min="0" step="1" data-segment="${index}" data-field="reps" value="${escapeHtml(segment.reps)}" placeholder="次"></div>`).join('')}
      <button class="tiny-btn add-drop-segment" type="button">加递减段</button><label class="drop-rest">休息秒数<input data-field="restSeconds" type="number" min="0" step="1" value="${escapeHtml(set.restSeconds || '')}" placeholder="休息"></label></div>`;
  }

  function renderExercises() {
    const list = $('#exerciseList');
    list.innerHTML = '';
    state.exercises.forEach((exercise, exerciseIndex) => {
      const card = document.createElement('article');
      card.className = 'exercise-card';
      card.innerHTML = `<div class="exercise-head"><label class="exercise-name-wrap">动作<input class="exercise-name" list="exerciseHints" data-exercise="${exerciseIndex}" value="${escapeHtml(exercise.name)}" maxlength="128" placeholder="输入或选择动作" autocomplete="off"></label><button class="tiny-btn danger remove-exercise" type="button" title="删除动作">删除动作</button></div>
        <div class="last-training">${exercise.last ? `<b>上次训练：</b>${escapeHtml(exercise.last)}` : '选择动作后显示上次训练'}</div>
        <div class="set-header"><span>组次</span><span>重量 kg</span><span>次数</span><span>RPE</span><span>备注</span><span></span></div>
        <div class="set-list">${exercise.sets.map((set, setIndex) => `<div class="set-row${set.drop ? ' drop' : ''}" data-exercise-index="${exerciseIndex}" data-set-index="${setIndex}"><span class="set-no">${set.drop ? '递减组' : `${set.warmup ? '热身' : '第'} ${set.warmup ? '' : `${setIndex + 1} 组`}`}</span>${set.drop ? dropSetMarkup(set) : normalSetMarkup(set)}<button class="set-more" type="button" title="${set.drop ? '恢复普通组' : '设为递减组'}">${set.drop ? '↩' : '⋯'}</button></div>`).join('')}</div>
        <div class="exercise-bottom"><button class="tiny-btn add-set" type="button">加组</button></div>`;
      $('.remove-exercise', card).onclick = () => { state.exercises.splice(exerciseIndex, 1); renderExercises(); scheduleDraftSave(); };
      $('.add-set', card).onclick = () => { state.exercises[exerciseIndex].sets.push(newSet()); renderExercises(); scheduleDraftSave(); };
      $('.exercise-name', card).addEventListener('input', event => {
        const value = event.target.value;
        state.exercises[exerciseIndex].name = value;
        clearTimeout(searchHandle);
        searchHandle = setTimeout(() => refreshExerciseHints(value), 180);
        scheduleDraftSave();
      });
      list.append(card);
    });
    $('#recordCount').textContent = `${state.exercises.length} 个动作`;
    updateVolume();
    updateDraft();
  }

  function exerciseRowsFromDom() {
    $$('.exercise-card').forEach((card, exerciseIndex) => {
      const exercise = state.exercises[exerciseIndex];
      $$('.set-row', card).forEach(row => {
        const set = exercise.sets[Number(row.dataset.setIndex)];
        $$('[data-field]', row).forEach(input => {
          const field = input.dataset.field;
          const value = input.value === '' ? '' : (input.type === 'number' ? Number(input.value) : input.value);
          if (input.dataset.segment !== undefined) {
            const index = Number(input.dataset.segment);
            set.segments[index][field] = value;
          } else {
            set[field] = value;
          }
        });
      });
    });
  }

  function updateVolume() {
    exerciseRowsFromDom();
    const volume = state.exercises.reduce((total, exercise) => total + exercise.sets.reduce((sum, set) => {
      if (set.drop) return sum + (set.segments || []).reduce((segmentSum, segment) => segmentSum + (Number(segment.weight) || 0) * (Number(segment.reps) || 0), 0);
      return sum + (Number(set.weight) || 0) * (Number(set.reps) || 0);
    }, 0), 0);
    $('#totalVolume').textContent = `总训练量 ${Math.round(volume).toLocaleString()} kg`;
  }

  function updateDraft() {
    $('#draftStatus').textContent = state.editingId ? `编辑训练 #${state.editingId}` : '本地草稿自动保存';
  }

  function draftSnapshot() {
    exerciseRowsFromDom();
    return {
      session_date: $('#sessionDate').value, body_part: $('#bodyPart').value,
      feeling: $('#feeling').value, duration_minutes: $('#duration').value,
      exercises: state.exercises, timer_started_at: state.timerStartedAt,
      timer_ended_at: state.timerEndedAt, elapsed_base: state.elapsedBase,
    };
  }

  let draftSaveHandle = null;
  function scheduleDraftSave() {
    clearTimeout(draftSaveHandle);
    draftSaveHandle = setTimeout(() => {
      try { localStorage.setItem(draftKey, JSON.stringify(draftSnapshot())); } catch (_) { /* Browser storage may be unavailable. */ }
      updateDraft();
    }, 250);
  }

  function restoreDraft() {
    try {
      const value = JSON.parse(localStorage.getItem(draftKey) || 'null');
      if (!value || !Array.isArray(value.exercises)) return false;
      if (value.session_date) $('#sessionDate').value = value.session_date;
      if (value.body_part) $('#bodyPart').value = value.body_part;
      $('#feeling').value = value.feeling || '';
      $('#duration').value = value.duration_minutes || '';
      state.exercises = value.exercises;
      state.timerStartedAt = value.timer_started_at || null;
      state.timerEndedAt = value.timer_ended_at || null;
      state.elapsedBase = Number(value.elapsed_base) || 0;
      return true;
    } catch (_) { return false; }
  }

  function payloadRecords() {
    exerciseRowsFromDom();
    const records = [];
    state.exercises.forEach((exercise, exerciseIndex) => {
      const name = exercise.name.trim();
      if (!name) throw new Error(`第 ${exerciseIndex + 1} 个动作需要填写名称。`);
      if (!exercise.sets.length) throw new Error(`${name} 至少需要一组记录。`);
      exercise.sets.forEach((set, setIndex) => {
        if (set.drop) {
          const segments = (set.segments || []).filter(segment => segment.weight !== '' || segment.reps !== '');
          if (set.weight === '' || set.reps === '' || segments.length === 0 || segments.some(segment => segment.weight === '' || segment.reps === '')) {
            throw new Error(`${name} 第 ${setIndex + 1} 组的主组和每个递减段都要填写重量与次数。`);
          }
          const normalizedSegments = [{weight_kg: Number(set.weight), reps: Number(set.reps)}, ...segments.map(segment => ({weight_kg: Number(segment.weight), reps: Number(segment.reps)}))];
          const volume = normalizedSegments.reduce((sum, segment) => sum + segment.weight_kg * segment.reps, 0);
          records.push({name, raw_name: name, body_part: $('#bodyPart').value, weight_kg: null, reps: [], rpe: [], sets_count: 1,
            total_reps: normalizedSegments.reduce((sum, segment) => sum + segment.reps, 0), volume_kg: volume,
            exercise_order: exerciseIndex + 1, note: set.note || null,
            extra_data: {structure: 'drop_set_group', rest_seconds: set.restSeconds ? Number(set.restSeconds) : null, segments: normalizedSegments}});
          return;
        }
        if (set.weight === '' || set.reps === '') throw new Error(`${name} 第 ${setIndex + 1} 组需要填写重量和次数。`);
        const weight = Number(set.weight), reps = Number(set.reps), rpe = set.rpe === '' ? null : Number(set.rpe);
        records.push({name, raw_name: name, body_part: $('#bodyPart').value, weight_kg: weight,
          reps: [reps], rpe: rpe === null ? [] : [rpe], sets_count: 1, total_reps: reps, volume_kg: weight * reps,
          is_warmup: Boolean(set.warmup), exercise_order: exerciseIndex + 1, note: set.note || null, extra_data: {}});
      });
    });
    if (!records.length) throw new Error('请至少添加一组有效动作记录。');
    return records;
  }

  function trainingFormData() {
    const form = new FormData();
    form.set('csrf', csrf);
    form.set('request_id', crypto.randomUUID().replaceAll('-', ''));
    if (state.editingId) form.set('session_id', String(state.editingId));
    form.set('session_date', $('#sessionDate').value);
    form.set('body_part', $('#bodyPart').value);
    form.set('duration_minutes', $('#duration').value);
    form.set('feeling', $('#feeling').value);
    form.set('records_json', JSON.stringify(payloadRecords()));
    const now = new Date();
    form.set('training_timing_json', JSON.stringify(state.timerStartedAt ? {
      started_at: state.timerStartedAt,
      ended_at: state.timerEndedAt || (timerHandle ? null : now.toISOString()),
    } : {}));
    return form;
  }

  async function saveTraining() {
    const button = $('#saveBtn');
    button.disabled = true;
    try {
      const result = await request('/training', {method: 'POST', body: trainingFormData()});
      localStorage.removeItem(draftKey);
      state.editingId = null;
      state.timerStartedAt = null;
      state.timerEndedAt = null;
      state.elapsedBase = 0;
      stopTimer();
      $('#activeBar').hidden = true;
      $('#startBtn').hidden = false;
      $('#sessionDate').value = new Date().toISOString().slice(0, 10);
      $('#duration').value = '';
      $('#feeling').value = '';
      state.exercises = [];
      renderExercises();
      toast('训练已保存到 HealthOS');
      state.offset = 0;
      await loadSessions();
      await loadAnalytics();
      if (result?.item?.id) {
        changeView('history');
        await loadDetail(result.item.id);
      }
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; button.textContent = state.editingId ? '更新训练' : '保存训练'; }
  }

  function fmtDate(date) { return String(date || '').slice(0, 10); }
  function fmtNumber(value, digits = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString('zh-CN', {maximumFractionDigits: digits}) : '—';
  }

  async function loadSessions() {
    const params = new URLSearchParams({limit: String(pageSize), offset: String(state.offset)});
    if (state.query) params.set('q', state.query);
    const result = await request(`/owner/api/training/sessions?${params}`);
    state.sessions = result.items || [];
    state.total = result.total || 0;
    renderSessions();
  }

  function renderSessions() {
    const root = $('#sessions');
    const start = state.total ? state.offset + 1 : 0;
    const end = Math.min(state.offset + state.sessions.length, state.total);
    $('#historyCount').textContent = `显示 ${start}–${end} 条，共 ${state.total} 节`;
    $('#historyPrevious').disabled = state.offset <= 0;
    $('#historyNext').disabled = state.offset + pageSize >= state.total;
    root.innerHTML = state.sessions.map(session => `<button class="session-item${Number(state.selectedSession) === Number(session.id) ? ' selected' : ''}" data-session-id="${Number(session.id)}" type="button">
      <span class="session-top"><strong>${escapeHtml(session.body_part)} · ${escapeHtml(fmtDate(session.session_date))}</strong><time>${session.duration_minutes ? `${session.duration_minutes} 分钟` : '未记录时长'}</time></span>
      <span class="session-meta"><span class="session-volume">${fmtNumber(session.total_volume_kg)} kg</span>　${Number(session.record_count) || 0} 组　${Number(session.effective_sets) || 0} 有效组</span></button>`).join('') || '<div class="status-note">没有匹配的 HealthOS 训练记录。</div>';
    $$('.session-item', root).forEach(button => button.onclick = () => loadDetail(Number(button.dataset.sessionId)));
  }

  async function loadDetail(id) {
    const session = await request(`/owner/api/training/sessions/${id}`);
    state.selectedSession = id;
    renderSessions();
    const root = $('#sessionDetail');
    const groups = groupSessionRecords(session.records);
    root.innerHTML = `<div class="block-title"><h2>训练详情</h2><span class="sub">HealthOS 本地记录</span></div>
      <div class="detail-title"><div><h3>${escapeHtml(fmtDate(session.session_date))} · ${escapeHtml(session.body_part)}</h3><p>${escapeHtml(session.feeling || '未填写状态备注')}</p></div><div class="detail-actions"><button class="tiny-btn" id="editSession" type="button">编辑</button><button class="tiny-btn danger" id="deleteSession" type="button">删除</button></div></div>
      <div class="detail-kpis"><div class="detail-kpi"><small>时长</small><strong>${session.duration_minutes ? `${session.duration_minutes} 分钟` : '—'}</strong></div><div class="detail-kpi"><small>训练量</small><strong>${fmtNumber(session.total_volume_kg)} kg</strong></div><div class="detail-kpi"><small>动作组</small><strong>${session.records.length} 组</strong></div></div>
      ${groups.map(group => `<details class="exercise-block" open><summary>${escapeHtml(group.name)}<span>${group.sets.length} 组</span></summary><div class="table-wrap"><table class="records-table"><thead><tr><th>组次</th><th>重量</th><th>次数</th><th>RPE</th><th>组型</th></tr></thead><tbody>${group.sets.map((set, index) => `<tr><td>${index + 1}</td><td>${set.drop ? '递减组' : `${escapeHtml(set.weight)} kg`}</td><td>${set.drop ? (set.segments || []).map(segment => escapeHtml(segment.reps)).join(' → ') : escapeHtml(set.reps)}</td><td>${escapeHtml(set.rpe ?? '')}</td><td>${set.drop ? '递减组' : set.warmup ? '热身组' : '普通组'}</td></tr>`).join('')}</tbody></table></div></details>`).join('') || '<p class="status-note">此训练没有动作记录。</p>'}`;
    $('#editSession').onclick = () => editSession(session);
    $('#deleteSession').onclick = () => deleteSession(session.id);
  }

  function editSession(session) {
    state.editingId = Number(session.id);
    state.exercises = groupSessionRecords(session.records).map(group => ({...group, last: null}));
    $('#sessionDate').value = fmtDate(session.session_date);
    $('#bodyPart').value = session.body_part;
    $('#feeling').value = session.feeling || '';
    $('#duration').value = session.duration_minutes || '';
    const timing = session.extra_data?.training_timing || {};
    state.timerStartedAt = timing.started_at || null;
    state.timerEndedAt = timing.ended_at || null;
    state.elapsedBase = 0;
    renderExercises();
    $('#saveBtn').textContent = '更新训练';
    changeView('record');
    scheduleDraftSave();
  }

  async function deleteSession(id) {
    if (!confirm('删除这节训练及其所有动作记录？此操作无法撤销。')) return;
    const form = new FormData();
    form.set('csrf', csrf);
    form.set('session_id', String(id));
    try {
      await request('/training/delete', {method: 'POST', body: form});
      state.selectedSession = null;
      await loadSessions();
      await loadAnalytics();
      $('#sessionDetail').innerHTML = '<div class="block-title"><h2>训练详情</h2></div><p class="status-note">训练记录已删除。</p>';
      toast('训练已从 HealthOS 删除');
    } catch (error) { toast(error.message); }
  }

  async function refreshExerciseHints(query = '') {
    const params = new URLSearchParams({q: query, body_part: $('#bodyPart').value});
    try {
      const result = await request(`/owner/api/training/exercises?${params}`);
      $('#exerciseHints').innerHTML = (result.items || []).map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.body_part)}</option>`).join('');
    } catch (_) { /* The user can still enter an exercise name manually. */ }
  }

  async function loadLastExercise(exercise) {
    const result = await request(`/owner/api/training/exercises/last?name=${encodeURIComponent(exercise.name)}`);
    exercise.last = result.session_date
      ? `${fmtDate(result.session_date)} · ${(result.sets || []).map(set => set.extra_data?.structure === 'drop_set_group' ? '递减组' : `${set.weight_kg ?? '—'} kg × ${(set.reps || []).join('/')}`).join('，')}`
      : '暂无历史记录';
    const card = $$('.exercise-card').find(item => $('.exercise-name', item)?.value === exercise.name);
    if (card) $('.last-training', card).innerHTML = `<b>上次训练：</b>${escapeHtml(exercise.last)}`;
  }

  async function loadAnalytics() {
    state.analytics = await request('/owner/api/training/analytics');
    renderAnalytics();
  }

  function renderAnalytics() {
    const data = state.analytics;
    if (!data) return;
    const summary = data.summary || {};
    $('#nextHint').textContent = `建议：${data.suggested_body_part || '胸'}`;
    $('#statsRange').textContent = '根据 HealthOS 本地记录实时计算';
    $('#metricTotal').textContent = fmtNumber(summary.total_sessions);
    $('#metricRecent').textContent = `${fmtNumber(summary.recent_sessions)} 次`;
    $('#metricVolume').textContent = `${fmtNumber(summary.recent_volume / 1000, 1)} t`;
    const effectiveSets = (data.weeks || []).reduce((sum, week) => sum + Number(week.effective_sets || 0), 0);
    const weekSessions = (data.weeks || []).reduce((sum, week) => sum + Number(week.sessions || 0), 0);
    $('#metricEffective').textContent = weekSessions ? (effectiveSets / weekSessions).toFixed(1) : '0';
    const parts = data.body_parts_90d || [];
    const maxPart = Math.max(1, ...parts.map(item => Number(item[1]) || 0));
    $('#bodyPerformance').innerHTML = parts.map(([part, count]) => `<div class="performance-card"><small>${escapeHtml(part)}</small><strong>${fmtNumber(count)} 次</strong><div class="microbar"><i style="width:${Math.round(Number(count) / maxPart * 100)}%"></i></div></div>`).join('') || '<p class="status-note">近 90 天暂无训练记录。</p>';
    $('#bodyPartStats').innerHTML = parts.map(([part, count]) => `<div class="bar-item"><span>${escapeHtml(part)}</span><div class="bar-track"><i style="width:${Math.round(Number(count) / maxPart * 100)}%"></i></div><span>${fmtNumber(count)}</span></div>`).join('') || '<p class="status-note">暂无部位分布数据。</p>';
    const weeks = data.weeks || [];
    const maxWeekly = Math.max(1, ...weeks.map(week => Number(week.sessions) || 0));
    $('#weeklyStats').innerHTML = weeks.slice(-6).map(week => `<div class="bar-item"><span>${escapeHtml(week.week_start.slice(5))}</span><div class="bar-track"><i style="width:${Math.round(Number(week.sessions) / maxWeekly * 100)}%"></i></div><span>${fmtNumber(week.sessions)}</span></div>`).join('') || '<p class="status-note">暂无周训练频率数据。</p>';
    const progression = data.exercise_trends || [];
    const e1rm = data.e1rm_trends || [];
    $('#progressionList').innerHTML = progression.map(item => {
      const sessions = item.sessions || [];
      const latest = sessions[0] || {};
      const e1rmItem = e1rm.find(entry => entry.name === item.name);
      const latestE1rm = [...(e1rmItem?.points || [])].reverse().find(point => point.e1rm !== null)?.e1rm;
      return `<article class="lift-card"><h3>${escapeHtml(item.name)}</h3><div class="lift-grid"><div><small>最高重量</small><strong>${fmtNumber(latest.max_weight_kg, 1)} kg</strong></div><div><small>近期 e1RM</small><strong>${latestE1rm == null ? '—' : `${fmtNumber(latestE1rm, 1)} kg`}</strong></div><div><small>近期训练</small><strong>${sessions.length} 次</strong></div></div></article>`;
    }).join('') || '<p class="status-note">暂无动作表现记录。</p>';
    const select = $('#e1rmExercise');
    const current = select.value;
    select.innerHTML = e1rm.map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join('');
    if ([...select.options].some(option => option.value === current)) select.value = current;
    else if (data.e1rm_trends?.length) select.value = data.e1rm_trends[0].name;
    drawChart();
  }

  function drawChart() {
    const data = state.analytics;
    const canvas = $('#trendChart');
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !data) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    const context = canvas.getContext('2d');
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    const width = rect.width, height = rect.height, pad = {left: 40, right: 22, top: 14, bottom: 28};
    context.clearRect(0, 0, width, height);
    let labels = (data.weeks || []).map(week => week.week_start);
    let values = [];
    if (state.metric === 'e1rm') {
      const item = (data.e1rm_trends || []).find(entry => entry.name === $('#e1rmExercise').value);
      values = (item?.points || []).map(point => point.e1rm);
      $('#chartLegend').textContent = item?.name || '尚无 e1RM 记录';
    } else if (state.metric === 'sets') {
      values = (data.weeks || []).map(week => Number(week.effective_sets));
      $('#chartLegend').textContent = '每周有效组';
    } else {
      values = (data.weeks || []).map(week => Number(week.volume_kg));
      $('#chartLegend').textContent = '每周训练量（kg）';
    }
    const points = values.map((value, index) => ({value: Number(value), index})).filter(point => Number.isFinite(point.value));
    if (!points.length) {
      context.fillStyle = '#999'; context.font = '13px system-ui'; context.textAlign = 'center';
      context.fillText('当前时间范围内暂无数据', width / 2, height / 2); context.textAlign = 'start'; return;
    }
    let min = Math.min(...points.map(point => point.value)), max = Math.max(...points.map(point => point.value));
    if (min === max) { min -= 1; max += 1; }
    context.font = '10px system-ui'; context.strokeStyle = '#292929'; context.fillStyle = '#858585';
    for (let index = 0; index < 4; index++) {
      const y = pad.top + (height - pad.top - pad.bottom) * index / 3;
      context.beginPath(); context.moveTo(pad.left, y); context.lineTo(width - pad.right, y); context.stroke();
      context.fillText(fmtNumber(max - (max - min) * index / 3, 1), 3, y + 3);
    }
    const xAt = index => pad.left + (width - pad.left - pad.right) * index / Math.max(1, labels.length - 1);
    const yAt = value => pad.top + (height - pad.top - pad.bottom) * (max - value) / (max - min);
    context.strokeStyle = '#eee'; context.lineWidth = 2; context.beginPath();
    points.forEach((point, index) => index ? context.lineTo(xAt(point.index), yAt(point.value)) : context.moveTo(xAt(point.index), yAt(point.value)));
    context.stroke(); context.fillStyle = '#eee';
    points.forEach(point => { context.beginPath(); context.arc(xAt(point.index), yAt(point.value), 2.5, 0, Math.PI * 2); context.fill(); });
    context.fillStyle = '#777'; context.textAlign = 'center';
    const ticks = [0, Math.floor((labels.length - 1) / 3), Math.floor((labels.length - 1) * 2 / 3), labels.length - 1].filter((index, at, array) => array.indexOf(index) === at);
    ticks.forEach(index => context.fillText(labels[index].slice(5), xAt(index), height - 5));
    context.textAlign = 'start';
  }

  async function loadAliases() {
    const [aliases, catalog] = await Promise.all([
      request('/owner/api/training/aliases'), request('/owner/api/training/exercises?q='),
    ]);
    state.aliases = aliases.items || [];
    state.catalog = catalog.items || [];
    $('#aliasExercise').innerHTML = state.catalog.map(item => `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(item.body_part)}</option>`).join('');
    $('#aliases').innerHTML = state.aliases.map(item => `<div class="alias-item"><span>${escapeHtml(item.alias_name)}</span><span>${escapeHtml(item.standard_name)}</span></div>`).join('') || '<p class="status-note">暂无动作别名。</p>';
  }

  async function showSuggestions() {
    const query = $('#aliasQuery').value.trim();
    const result = await request(`/owner/api/training/exercises?q=${encodeURIComponent(query)}`);
    $('#suggestions').innerHTML = (result.items || []).map(item => `<div class="alias-item"><span>${escapeHtml(item.name)}${item.aliases?.length ? ` · ${item.aliases.map(escapeHtml).join('、')}` : ''}</span><span>${escapeHtml(item.body_part)}</span></div>`).join('') || '<p class="status-note">没有找到相符动作。</p>';
  }

  function padTime(value) { return String(value).padStart(2, '0'); }
  function elapsedSeconds() { return state.elapsedBase + (state.timerStartedAt && !state.timerEndedAt ? Math.floor((Date.now() - Date.parse(state.timerStartedAt)) / 1000) : 0); }
  function renderTimer() {
    const elapsed = Math.max(0, elapsedSeconds());
    $('#timer').textContent = `${padTime(Math.floor(elapsed / 60))}:${padTime(elapsed % 60)}`;
    if (state.timerStartedAt) $('#duration').value = String(Math.round(elapsed / 60));
  }
  function stopTimer() { clearInterval(timerHandle); timerHandle = null; }
  function startTimer() {
    if (!state.timerStartedAt) state.timerStartedAt = new Date().toISOString();
    state.timerEndedAt = null;
    $('#activePart').textContent = $('#bodyPart').value;
    $('#activeLabel').textContent = '进行中';
    $('#endBtn').textContent = '结束运动';
    $('#activeBar').hidden = false; $('#startBtn').hidden = true;
    stopTimer(); timerHandle = setInterval(renderTimer, 1000); renderTimer(); scheduleDraftSave();
  }
  function finishTimer() {
    state.elapsedBase += state.timerStartedAt && !state.timerEndedAt ? Math.floor((Date.now() - Date.parse(state.timerStartedAt)) / 1000) : 0;
    state.timerEndedAt = new Date().toISOString();
    stopTimer(); renderTimer();
  }

  function showError(error) {
    if (error.status === 401 || error.status === 403) { location.href = '/login'; return; }
    toast(error.message || 'HealthOS 数据暂不可用');
  }

  async function initialize() {
    $('#sessionDate').value = new Date().toISOString().slice(0, 10);
    $('#duration').value = '';
    $('#sidebar').classList.remove('open');
    $$('.side-nav button').forEach(button => button.onclick = () => changeView(button.dataset.view));
    $('#menuBtn').onclick = () => $('#sidebar').classList.toggle('open');
    $('#bodyPart').onchange = () => { $('#activePart').textContent = $('#bodyPart').value; refreshExerciseHints().catch(showError); scheduleDraftSave(); };
    $('#sessionDate').onchange = scheduleDraftSave;
    $('#feeling').oninput = scheduleDraftSave;
    $('#duration').oninput = scheduleDraftSave;
    $('#exerciseList').addEventListener('input', event => { if (event.target.matches('.set-row input')) { updateVolume(); scheduleDraftSave(); } });
    $('#exerciseList').addEventListener('change', event => {
      if (!event.target.matches('.exercise-name')) return;
      const exercise = state.exercises[Number(event.target.dataset.exercise)];
      if (exercise?.name) loadLastExercise(exercise).catch(showError);
    });
    $('#exerciseList').addEventListener('click', event => {
      const button = event.target.closest('.set-more');
      if (!button) return;
      const row = button.closest('.set-row');
      const exercise = state.exercises[Number(row.dataset.exerciseIndex)];
      const set = exercise.sets[Number(row.dataset.setIndex)];
      set.drop = !set.drop;
      if (set.drop && !set.segments?.length) set.segments = [{weight: '', reps: ''}];
      renderExercises(); scheduleDraftSave();
    });
    $('#exerciseList').addEventListener('click', event => {
      if (!event.target.closest('.add-drop-segment')) return;
      const row = event.target.closest('.set-row');
      const set = state.exercises[Number(row.dataset.exerciseIndex)].sets[Number(row.dataset.setIndex)];
      set.segments.push({weight: '', reps: ''}); renderExercises(); scheduleDraftSave();
    });
    $('#addExerciseBtn').onclick = () => { state.exercises.push(newExercise()); renderExercises(); $('.exercise-name', $('#exerciseList').lastElementChild)?.focus(); scheduleDraftSave(); };
    $('#clearBtn').onclick = () => { if (!confirm('清空当前未保存训练草稿？')) return; state.exercises = []; renderExercises(); scheduleDraftSave(); };
    $('#saveBtn').onclick = saveTraining;
    $('#startBtn').onclick = () => startTimer();
    $('#endBtn').onclick = () => {
      if (state.timerEndedAt) { startTimer(); return; }
      finishTimer(); $('#finishOverlay').classList.add('open'); scheduleDraftSave();
    };
    $('#continueBtn').onclick = () => { $('#finishOverlay').classList.remove('open'); startTimer(); };
    $('#finishLater').onclick = () => { $('#finishOverlay').classList.remove('open'); $('#activeLabel').textContent = '已结束 · 可稍后保存'; $('#endBtn').textContent = '继续计时'; scheduleDraftSave(); };
    $('#finishSave').onclick = saveTraining;
    $('#historyPrevious').onclick = async () => { state.offset = Math.max(0, state.offset - pageSize); await loadSessions(); };
    $('#historyNext').onclick = async () => { if (state.offset + pageSize < state.total) { state.offset += pageSize; await loadSessions(); } };
    $('#historySearch').oninput = () => { clearTimeout(searchHandle); searchHandle = setTimeout(() => { state.query = $('#historySearch').value.trim(); state.offset = 0; loadSessions().catch(showError); }, 220); };
    $('#suggestBtn').onclick = () => showSuggestions().catch(showError);
    $('#aliasQuery').onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); showSuggestions().catch(showError); } };
    $('#aliasForm').onsubmit = async event => {
      event.preventDefault(); const form = new FormData(event.currentTarget); form.set('csrf', csrf);
      try { await request('/training/aliases', {method: 'POST', body: form}); event.currentTarget.reset(); await loadAliases(); toast('动作别名已保存'); }
      catch (error) { showError(error); }
    };
    $$('.trend-controls [data-metric]').forEach(button => button.onclick = () => {
      state.metric = button.dataset.metric; $$('.trend-controls [data-metric]').forEach(item => item.classList.toggle('active', item === button));
      $('#chartTitle').textContent = state.metric === 'e1rm' ? 'e1RM 趋势' : state.metric === 'sets' ? '有效组趋势' : '训练容量趋势'; drawChart();
    });
    $('#e1rmExercise').onchange = drawChart;
    window.addEventListener('resize', drawChart);
    const restored = restoreDraft();
    if (!state.exercises.length) state.exercises = [];
    renderExercises();
    if (state.timerStartedAt) {
      $('#activeBar').hidden = false; $('#startBtn').hidden = true;
      if (state.timerEndedAt) { state.elapsedBase = Math.max(state.elapsedBase, Math.floor((Date.parse(state.timerEndedAt) - Date.parse(state.timerStartedAt)) / 1000)); $('#activeLabel').textContent = '已结束 · 可稍后保存'; $('#endBtn').textContent = '继续计时'; }
      else { $('#activeLabel').textContent = '已恢复计时'; timerHandle = setInterval(renderTimer, 1000); }
      renderTimer();
    }
    try {
      await Promise.all([loadSessions(), loadAnalytics(), refreshExerciseHints(), loadAliases()]);
      if (restored) toast('已恢复本地未保存草稿');
    } catch (error) { showError(error); }
  }

  initialize();
})();
