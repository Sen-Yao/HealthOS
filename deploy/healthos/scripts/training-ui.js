(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  let csrf = $('meta[name="healthos-csrf"]')?.content || '';
  const legacyDraftKey = 'healthos.training.draft.v1';
  const draftPrefix = 'healthos.training.draft.v2.';
  const activeDraftKey = 'healthos.training.active-draft.v2';
  const lifecycleKey = 'healthos.training.lifecycle.v1';
  const pageInstanceId = typeof crypto?.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const pageSize = 50;
  const uniqueId = () => typeof crypto?.randomUUID === 'function' ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const idleReminderSeconds = 10 * 60;
  const localDate = () => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  };
  const state = {
    exercises: [], sessions: [], aliases: [], catalog: [], analytics: null,
    offset: 0, total: 0, query: '', selectedSession: null, editingId: null,
    metric: 'e1rm', timerStartedAt: null, timerEndedAt: null, elapsedBase: 0,
    lastActivityAt: null, idleReminderShown: false,
    timerRunningSince: null,
    draftSavedAt: null, draftWriteError: null, draftDirty: false,
    draftId: uniqueId(), requestId: uniqueId(), draftRevision: 0, draftStorageKey: null,
    lastDraftText: null, draftConflict: false, saving: false, pendingSubmission: null,
    saveFeedback: {status: 'idle', message: '尚未提交到 HealthOS。', target: null},
  };
  let timerHandle = null;
  let idleCheckHandle = null;
  let searchHandle = null;
  let sessionCheck = null;
  let authEpoch = 0;
  let authSubmitting = false;
  const authUpdateKey = 'healthos.owner.auth.updated';
  let authChannel = null;
  try { if (typeof BroadcastChannel === 'function') authChannel = new BroadcastChannel('healthos.owner.auth'); }
  catch (_) { /* Restricted storage must not prevent training or local sign-in. */ }
  let authReturnFocus = null;
  let setMenuContext = null;
  let deletionUndo = null;
  const exerciseSearch = new Map();

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
    const epoch = authEpoch;
    const response = await fetch(path, {
      credentials: 'same-origin',
      cache: 'no-store',
      ...options,
      headers: { Accept: 'application/json', ...(options.headers || {}) },
    });
    if (response.redirected) {
      const error = new Error('请求被重定向，训练尚未确认保存。');
      error.status = new URL(response.url, location.href).pathname === '/login' ? 401 : 502;
      error.staleAuth = epoch !== authEpoch || authSubmitting;
      if (error.status === 401 && !error.staleAuth) requireReauthentication();
      throw error;
    }
    const type = response.headers.get('content-type') || '';
    if (!type.toLowerCase().includes('application/json')) throw new Error('服务器返回了非预期内容，训练尚未确认保存。');
    let body;
    try { body = await response.json(); }
    catch (_) { throw new Error('服务器响应无法解析，训练尚未确认保存。'); }
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('服务器响应不完整，训练尚未确认保存。');
    if (!response.ok) {
      const error = new Error(body?.detail || body?.error || `请求失败 (${response.status})`);
      error.status = response.status;
      error.code = body.error;
      error.staleAuth = path !== '/login' && (epoch !== authEpoch || authSubmitting);
      if (error.status === 401 && path !== '/login' && !error.staleAuth) requireReauthentication();
      throw error;
    }
    return body;
  }

  function setNotice(message = '', kind = 'error', source = 'general') {
    const node = $('#trainingNotice');
    node.hidden = !message;
    node.dataset.kind = kind;
    node.dataset.source = source;
    $('#trainingNoticeText').textContent = message;
  }

  function validationError(message, target) {
    const error = new Error(message);
    error.validation = true;
    error.target = target;
    return error;
  }

  function validationField(target) {
    if (!target) return null;
    if (target.id) return document.getElementById(target.id);
    const card = exerciseCard(target.exerciseId);
    if (!card) return null;
    if (target.field === 'name') return $('.exercise-name', card);
    const row = $$('.set-row', card)[target.setIndex];
    if (!row) return null;
    return target.segmentIndex === undefined
      ? $(`[data-field="${target.field}"]:not([data-segment])`, row)
      : $(`[data-field="${target.field}"][data-segment="${target.segmentIndex}"]`, row);
  }

  function renderSaveFeedback() {
    const {status, message, target} = state.saveFeedback;
    const modal = $('#trainingDecisionOverlay').classList.contains('open');
    for (const id of ['saveFeedback', 'decisionSaveFeedback']) {
      const node = document.getElementById(id);
      node.dataset.state = status;
      node.setAttribute('role', ['error', 'uncertain'].includes(status) ? 'alert' : 'status');
      node.setAttribute('aria-live', id === 'saveFeedback' && modal ? 'off' : ['error', 'uncertain'].includes(status) ? 'assertive' : 'polite');
      $('[data-save-message]', node).textContent = message;
      $('[data-locate-error]', node).hidden = !target;
      if (id === 'decisionSaveFeedback') node.hidden = !modal;
    }
  }

  function setSaveFeedback(status, message, target = null) {
    $$('#activeTraining [aria-invalid="true"], #trainingEntry [aria-invalid="true"]').forEach(node => node.removeAttribute('aria-invalid'));
    state.saveFeedback = {status, message, target};
    validationField(target)?.setAttribute('aria-invalid', 'true');
    renderSaveFeedback();
    if (status === 'saved' || status === 'saved_with_warning') setNotice(message, 'info', 'save');
  }

  function locateSaveError() {
    const field = validationField(state.saveFeedback.target);
    if (!field) return;
    closeTrainingDecision();
    field.scrollIntoView({block: 'center', behavior: 'smooth'});
    field.focus({preventScroll: true});
  }

  function showSaveFailure() {
    renderSaveFeedback();
    if (!$('#trainingDecisionOverlay').classList.contains('open') && !$('#authOverlay').classList.contains('open')) {
      $('#saveFeedback').focus({preventScroll: true});
      $('#saveFeedback').scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
  }

  function requireReauthentication() {
    if (state.draftDirty || hasTrainingDraft()) writeDraft('auth-required');
    setNotice('登录已失效。训练内容仍保留在本页，请重新登录后再次保存。');
    $('#reauthenticateBtn').hidden = false;
    openAuthentication();
  }

  function openAuthentication() {
    closeSetMenu();
    if ($('#authOverlay').classList.contains('open')) return;
    authReturnFocus = document.activeElement;
    $('#authError').textContent = '';
    $('#authOverlay').classList.add('open');
    $('#reauth-password').focus();
  }

  function closeAuthentication() {
    $('#authOverlay').classList.remove('open');
    $('#reauth-password').value = '';
    if (authReturnFocus?.isConnected) authReturnFocus.focus();
  }

  async function authenticate(event) {
    event.preventDefault();
    if (authSubmitting) return;
    authSubmitting = true;
    authEpoch += 1;
    sessionCheck = null;
    $('#authSubmit').disabled = true;
    try {
      const result = await request('/login', {method: 'POST', body: new URLSearchParams(new FormData(event.currentTarget))});
      if (typeof result.csrf !== 'string' || !result.csrf) throw new Error('登录响应不完整，请重试。');
      csrf = result.csrf;
      authEpoch += 1;
      sessionCheck = null;
      closeAuthentication();
      $('#reauthenticateBtn').hidden = true;
      setNotice('已重新登录，训练内容已保留。请再次点击保存训练。', 'info');
      try {
        if (authChannel) authChannel.postMessage('signed-in');
        else localStorage.setItem(authUpdateKey, uniqueId());
      } catch (_) { /* Local authentication still works without cross-tab notification. */ }
    } catch (error) { $('#authError').textContent = error.message || '登录失败，请重试。'; }
    finally { authSubmitting = false; $('#authSubmit').disabled = false; }
  }

  async function checkSession() {
    if (!sessionCheck) {
      const epoch = authEpoch;
      const pending = request('/owner/api/session').then(result => {
        if (typeof result.csrf !== 'string' || !result.csrf) throw new Error('无法确认登录状态，请重试。');
        if (epoch === authEpoch && !authSubmitting) csrf = result.csrf;
        return result;
      }).finally(() => { if (sessionCheck === pending) sessionCheck = null; });
      sessionCheck = pending;
    }
    return sessionCheck;
  }

  function refreshSharedAuthentication() {
    if (authSubmitting) return;
    authEpoch += 1;
    sessionCheck = null;
    checkSession().then(() => {
      if ($('#authOverlay').classList.contains('open')) {
        closeAuthentication();
        $('#reauthenticateBtn').hidden = true;
        setNotice('登录状态已更新，训练内容已保留。请再次点击保存训练。', 'info');
      }
    }).catch(showError);
  }

  if (authChannel) authChannel.onmessage = event => { if (event.data === 'signed-in') refreshSharedAuthentication(); };

  function changeView(name) {
    $$('.side-nav button').forEach(button => button.classList.toggle('active', button.dataset.view === name));
    $$('.view:not(#trainingEntry)').forEach(view => view.classList.toggle('active', view.id === `view-${name}`));
    $('#sidebar').classList.remove('open');
    if (name === 'record') {
      renderTrainingMode();
      return;
    }
    $('#trainingEntry').hidden = true;
    $('#activeTraining').hidden = true;
    if (name === 'stats') loadAnalytics().catch(showError);
    if (name === 'aliases') loadAliases().catch(showError);
  }

  function newSet(weight = '') { return { weight, reps: '', rpe: '', note: '', warmup: false, drop: false, segments: [{ weight: '', reps: '' }] }; }
  function newExercise(name = '') { return { id: uniqueId(), name, last: null, sets: [newSet()] }; }

  function closeSetMenu(restoreFocus = false) {
    const trigger = setMenuContext?.trigger;
    if (trigger?.isConnected) trigger.setAttribute('aria-expanded', 'false');
    $('#setMenu').hidden = true;
    setMenuContext = null;
    if (restoreFocus && trigger?.isConnected) trigger.focus();
  }

  function openSetMenu(trigger) {
    if (state.saving) return;
    if (setMenuContext?.trigger === trigger) { closeSetMenu(true); return; }
    closeSetMenu();
    closeExerciseHints();
    exerciseRowsFromDom();
    const row = trigger.closest('.set-row');
    const exercise = state.exercises[Number(row.dataset.exerciseIndex)];
    const setIndex = Number(row.dataset.setIndex);
    setMenuContext = {exerciseId: exercise.id, setIndex, trigger};
    $('#toggleSetType').textContent = exercise.sets[setIndex].drop ? '恢复普通组' : '设为递减组';
    const menu = $('#setMenu');
    menu.hidden = false;
    const rect = trigger.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, rect.bottom + menu.offsetHeight + 8 <= innerHeight ? rect.bottom + 4 : rect.top - menu.offsetHeight - 4)}px`;
    trigger.setAttribute('aria-expanded', 'true');
    $('#toggleSetType').focus({preventScroll: true});
  }

  function clearDeletionUndo() {
    deletionUndo = null;
    $('#deletionUndo').hidden = true;
  }

  function deleteDraftPart(exerciseId, setIndex = null) {
    if (state.saving) return;
    exerciseRowsFromDom();
    const index = state.exercises.findIndex(exercise => exercise.id === exerciseId);
    if (index < 0) return;
    const exercise = state.exercises[index];
    if (setIndex !== null && !exercise.sets[setIndex]) return;
    deletionUndo = {exercises: JSON.parse(JSON.stringify(state.exercises)), index, setIndex};
    const removeExercise = setIndex === null || exercise.sets.length === 1;
    if (removeExercise) {
      clearTimeout(exerciseSearch.get(exercise.id)?.timer);
      exerciseSearch.delete(exercise.id);
      state.exercises.splice(index, 1);
    } else exercise.sets.splice(setIndex, 1);
    renderExercises();
    renderTrainingMode();
    scheduleDraftSave(true);
    $('#deletionUndoText').textContent = removeExercise ? `已移除动作“${exercise.name || '未命名动作'}”。` : `已删除“${exercise.name || '未命名动作'}”第 ${setIndex + 1} 组。`;
    $('#deletionUndo').hidden = false;
    const nextCard = $$('.exercise-card')[Math.min(index, state.exercises.length - 1)];
    const nextRow = nextCard && $$('.set-row', nextCard)[Math.min(setIndex ?? 0, $$('.set-row', nextCard).length - 1)];
    (nextRow ? $('.set-more', nextRow) : $('#addExerciseBtn')).focus();
  }

  function undoDraftDeletion() {
    if (!deletionUndo || state.saving) return;
    const undo = deletionUndo;
    state.exercises = undo.exercises;
    exerciseSearch.clear();
    clearDeletionUndo();
    renderExercises();
    renderTrainingMode();
    scheduleDraftSave();
    const card = $$('.exercise-card')[undo.index];
    const row = card && $$('.set-row', card)[undo.setIndex ?? 0];
    (row ? $('.set-more', row) : $('.exercise-name', card))?.focus();
  }

  function hasTrainingDraft() {
    return Boolean(state.editingId || state.timerStartedAt || state.exercises.length);
  }

  function renderTrainingMode() {
    const active = hasTrainingDraft() || Boolean(deletionUndo);
    $('#trainingEntry').hidden = active;
    $('#activeTraining').hidden = !active;
    $('#startBtn').hidden = !active || Boolean(state.timerStartedAt);
    $('#activeBar').hidden = !state.timerStartedAt;
    $('#exportDraftBtn').hidden = !active && !state.draftWriteError && !state.draftConflict;
  }

  function groupSessionRecords(records) {
    const groups = new Map();
    for (const record of records || []) {
      const key = `${record.exercise_order}:${record.exercise_id}`;
      if (!groups.has(key)) groups.set(key, { name: record.standard_name || record.raw_exercise_name || '', canonicalId: record.exercise_id, bodyPart: record.exercise_body_part || '', last: null, sets: [] });
      const group = groups.get(key);
      const extra = record.extra_data && typeof record.extra_data === 'object' ? record.extra_data : {};
      if (extra.structure === 'drop_set_group') {
        const segments = extra.segments || [];
        group.sets.push({
          weight: segments[0]?.weight_kg ?? '', reps: segments[0]?.reps ?? '', rpe: '', note: record.note || '', warmup: false, drop: true,
          restSeconds: extra.rest_seconds || '',
          segments: segments.slice(1).map(segment => ({ weight: segment.weight_kg ?? '', reps: segment.reps ?? '' })),
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
    closeSetMenu();
    closeExerciseHints();
    const list = $('#exerciseList');
    list.innerHTML = '';
    state.exercises.forEach((exercise, exerciseIndex) => {
      exercise.id ||= uniqueId();
      const card = document.createElement('article');
      card.className = 'exercise-card';
      card.dataset.exerciseId = exercise.id;
      const hintsId = `exercise-hints-${exercise.id}`;
      card.innerHTML = `<div class="exercise-head"><div class="exercise-name-wrap"><label for="exercise-name-${escapeHtml(exercise.id)}">动作</label><input id="exercise-name-${escapeHtml(exercise.id)}" class="exercise-name" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${escapeHtml(hintsId)}" data-exercise="${exerciseIndex}" value="${escapeHtml(exercise.name)}" maxlength="128" placeholder="输入或选择动作" autocomplete="off"><div class="exercise-hints" id="${escapeHtml(hintsId)}" role="listbox" aria-label="动作建议" hidden></div><span class="exercise-hint-status" role="status" aria-live="polite"></span></div><button class="tiny-btn danger remove-exercise" type="button" title="删除动作">删除动作</button></div>
        <div class="last-training">${exercise.last ? `<b>上次训练：</b>${escapeHtml(exercise.last)}` : '选择动作后显示上次训练'}</div>
        <div class="set-header"><span>组次</span><span>重量 kg</span><span>次数</span><span>RPE</span><span>备注</span><span></span></div>
        <div class="set-list">${exercise.sets.map((set, setIndex) => `<div class="set-row${set.drop ? ' drop' : ''}" data-exercise-index="${exerciseIndex}" data-set-index="${setIndex}"><span class="set-no">${set.drop ? `第 ${setIndex + 1} 组 · 递减` : `${set.warmup ? '热身' : '第'} ${set.warmup ? '' : `${setIndex + 1} 组`}`}</span>${set.drop ? dropSetMarkup(set) : normalSetMarkup(set)}<button class="set-more" type="button" title="组操作" aria-label="第 ${setIndex + 1} 组操作" aria-haspopup="menu" aria-controls="setMenu" aria-expanded="false">⋯</button></div>`).join('')}</div>
        <div class="exercise-bottom"><button class="tiny-btn add-set" type="button">加组</button></div>`;
      $('.remove-exercise', card).onclick = () => deleteDraftPart(exercise.id);
      $('.add-set', card).onclick = () => {
        if (state.saving) return;
        exerciseRowsFromDom();
        const weight = exercise.sets.at(-1)?.weight;
        exercise.sets.push(newSet(weight !== '' && weight !== undefined && Number.isFinite(Number(weight)) && Number(weight) >= 0 ? weight : ''));
        renderExercises(); scheduleDraftSave();
        const lastRow = $$('.set-row', exerciseCard(exercise.id)).at(-1);
        $('[data-field="reps"]', lastRow).focus();
      };
      const input = $('.exercise-name', card);
      input.addEventListener('input', event => {
        const value = event.target.value;
        exercise.name = value;
        exercise.canonicalId = null;
        exercise.last = null;
        exercise.lastSequence = (exercise.lastSequence || 0) + 1;
        $('.last-training', card).textContent = '选择动作后显示上次训练';
        queueExerciseHints(exercise, event.isComposing);
        scheduleDraftSave();
      });
      input.addEventListener('focus', () => { closeExerciseHints(); refreshExerciseHints(exercise); });
      input.addEventListener('blur', () => { closeExerciseHints(exercise.id); if (exercise.name.trim()) loadLastExercise(exercise).catch(showError); });
      input.addEventListener('compositionstart', () => { const search = searchFor(exercise); search.composing = true; clearTimeout(search.timer); search.sequence += 1; closeExerciseHints(exercise.id); });
      input.addEventListener('compositionend', () => { searchFor(exercise).composing = false; queueExerciseHints(exercise); });
      input.addEventListener('keydown', event => {
        const search = searchFor(exercise);
        if (event.isComposing || search.composing) return;
        if (event.key === 'Escape') { closeExerciseHints(exercise.id); return; }
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          if (!search.open) { refreshExerciseHints(exercise); return; }
          if (!search.items.length) return;
          search.active = (search.active + (event.key === 'ArrowDown' ? 1 : -1) + search.items.length) % search.items.length;
          renderExerciseHints(exercise);
        } else if (event.key === 'Enter' && search.open && search.active >= 0) {
          event.preventDefault(); selectExerciseHint(exercise, search.items[search.active]);
        }
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
      if (set.drop) return sum + (Number(set.weight) || 0) * (Number(set.reps) || 0) + (set.segments || []).reduce((segmentSum, segment) => segmentSum + (Number(segment.weight) || 0) * (Number(segment.reps) || 0), 0);
      return sum + (Number(set.weight) || 0) * (Number(set.reps) || 0);
    }, 0), 0);
    $('#totalVolume').textContent = `总训练量 ${Math.round(volume).toLocaleString()} kg`;
  }

  function updateDraft() {
    const editing = state.editingId ? `编辑训练 #${state.editingId} · ` : '';
    if (state.draftConflict) {
      $('#draftStatus').textContent = `${editing}发现另一页面的更新，已停止覆盖`;
    } else if (state.draftWriteError) {
      $('#draftStatus').textContent = `${editing}尚未保存在本机，请导出草稿`;
    } else if (state.draftSavedAt) {
      const savedAt = new Date(state.draftSavedAt);
      const time = Number.isNaN(savedAt.getTime()) ? '' : savedAt.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'});
      $('#draftStatus').textContent = `${editing}本机草稿已保留${time ? ` ${time}` : ''}，不代表已提交`;
    } else {
      $('#draftStatus').textContent = `${editing}本机草稿尚未保留`;
    }
    $('#recoverDraftBtn').hidden = !state.draftConflict;
    $('#exportDraftBtn').hidden = !hasTrainingDraft() && !state.draftWriteError && !state.draftConflict;
  }

  function draftSnapshot() {
    exerciseRowsFromDom();
    return {
      session_date: $('#sessionDate').value, body_part: $('#bodyPart').value,
      feeling: $('#feeling').value, duration_minutes: $('#duration').value,
      exercises: state.exercises, timer_started_at: state.timerStartedAt,
      timer_ended_at: state.timerEndedAt, elapsed_base: state.elapsedBase,
      timer_running_since: state.timerRunningSince,
      last_activity_at: state.lastActivityAt,
      draft_id: state.draftId, request_id: state.requestId, editing_id: state.editingId,
      pending_submission: state.pendingSubmission,
    };
  }

  function recordLifecycle(eventName) {
    try {
      const navigation = performance.getEntriesByType('navigation')[0];
      const events = JSON.parse(localStorage.getItem(lifecycleKey) || '[]');
      events.push({
        at: new Date().toISOString(), event: eventName, page_instance_id: pageInstanceId,
        visibility: document.visibilityState, navigation_type: navigation?.type || 'unknown',
      });
      localStorage.setItem(lifecycleKey, JSON.stringify(events.slice(-40)));
    } catch (_) { /* Diagnostics must never block training entry. */ }
  }

  function currentDraftKey() {
    if (!state.draftStorageKey) state.draftStorageKey = `${draftPrefix}${state.editingId ? `edit.${state.editingId}` : `new.${state.draftId}`}`;
    return state.draftStorageKey;
  }

  function markDraftConflict() {
    state.draftConflict = true;
    setNotice('另一页面已更新或保存这份训练，已停止覆盖。可先导出本页内容，再恢复已保存草稿。');
    updateDraft();
  }

  function writeDraft(reason = 'change') {
    if (state.draftConflict) return false;
    try {
      const savedAt = new Date().toISOString();
      const key = currentDraftKey();
      const currentText = localStorage.getItem(key);
      if (currentText !== state.lastDraftText) {
        markDraftConflict();
        return false;
      }
      const snapshot = JSON.stringify({
        ...draftSnapshot(), schema_version: 2, saved_at: savedAt, revision: state.draftRevision + 1,
        save_reason: reason, page_instance_id: pageInstanceId,
      });
      // A tab-local recovery copy also protects simultaneous writes in separate tabs.
      localStorage.setItem(`${key}.tab.${pageInstanceId}`, snapshot);
      localStorage.setItem(key, snapshot);
      state.lastDraftText = snapshot;
      state.draftRevision += 1;
      localStorage.setItem(activeDraftKey, key);
      state.draftSavedAt = savedAt;
      state.draftWriteError = null;
    } catch (error) {
      state.draftWriteError = error;
      setNotice('草稿尚未保存在本机。请保持此页打开，并导出草稿备份。');
    }
    updateDraft();
    refreshDraftChoices();
    return !state.draftWriteError;
  }

  function scheduleDraftSave(preserveUndo = false) {
    if (preserveUndo !== true) clearDeletionUndo();
    if (!state.saving) setSaveFeedback('idle', state.pendingSubmission ? '尚未确认上次提交，当前修改仅在本机保留。请重试确认。' : '尚未提交到 HealthOS，请保存后确认。');
    if ($('#trainingNotice').dataset.kind === 'info') setNotice('');
    state.draftDirty = true;
    state.lastActivityAt = new Date().toISOString();
    state.idleReminderShown = false;
    // Keep this synchronous: iOS may suspend the page before a delayed callback runs.
    writeDraft('change');
  }

  function restoreDraft() {
    try {
      const key = localStorage.getItem(activeDraftKey);
      const text = key?.startsWith(draftPrefix) ? localStorage.getItem(key) : null;
      if (text) return applyDraft(JSON.parse(text), key, text);
      const legacyText = localStorage.getItem(legacyDraftKey);
      if (!legacyText) return false;
      const restored = applyDraft(JSON.parse(legacyText), null, null);
      // v1 is removed only after a verified v2 write; rendering must precede DOM capture.
      if (restored) {
        renderExercises();
        if (writeDraft('migrate-v1')) localStorage.removeItem(legacyDraftKey);
      }
      return restored;
    } catch (error) {
      state.draftWriteError = error;
      setNotice('本机草稿暂时无法读取，请勿清理浏览器数据。可继续在本页录入并导出备份。');
      return false;
    }
  }

  function applyDraft(value, key, text) {
      if (!value || !Array.isArray(value.exercises)) return false;
      if (![1, 2, undefined].includes(value.schema_version)) throw new Error('不支持的草稿版本');
      if (value.exercises.some(exercise => !exercise || typeof exercise.name !== 'string' || !Array.isArray(exercise.sets))) throw new Error('草稿内容不完整');
      state.draftId = value.draft_id || uniqueId();
      state.requestId = value.request_id || uniqueId();
      state.editingId = Number(value.editing_id) > 0 ? Number(value.editing_id) : null;
      state.draftStorageKey = key;
      state.draftRevision = Number(value.revision) || 0;
      state.lastDraftText = text;
      state.draftConflict = false;
      state.draftWriteError = null;
      state.pendingSubmission = value.pending_submission || null;
      if (value.session_date) $('#sessionDate').value = value.session_date;
      if (value.body_part) $('#bodyPart').value = value.body_part;
      $('#feeling').value = value.feeling || '';
      $('#duration').value = value.duration_minutes || '';
      state.exercises = value.exercises.map(exercise => ({...exercise, id: exercise.id || uniqueId(), last: null}));
      state.timerStartedAt = value.timer_started_at || null;
      state.timerEndedAt = value.timer_ended_at || null;
      state.timerRunningSince = state.timerEndedAt ? null : value.timer_running_since || state.timerStartedAt;
      state.elapsedBase = Number(value.elapsed_base) || 0;
      state.lastActivityAt = value.last_activity_at || value.saved_at || value.timer_started_at || null;
      state.draftSavedAt = value.saved_at || null;
      state.draftDirty = true;
      clearDeletionUndo();
      setSaveFeedback(state.pendingSubmission ? 'uncertain' : 'idle', state.pendingSubmission
        ? '上次提交尚未确认，请重试确认。当前内容已从本机草稿恢复。' : '尚未提交到 HealthOS，请保存后确认。');
      return true;
  }

  function recoverSavedDraft() {
    if (state.saving) return;
    if (!confirm('恢复已保存草稿将替换本页内容。请先导出需要保留的本页草稿。继续？')) return;
    try {
      const key = currentDraftKey();
      const text = localStorage.getItem(key);
      if (!text) { setNotice('这份训练已在另一页面保存或丢弃。请先导出本页内容，再丢弃本页草稿。'); return; }
      if (!applyDraft(JSON.parse(text), key, text)) throw new Error('草稿内容不完整');
      stopTimer();
      exerciseSearch.clear();
      renderExercises(); renderTrainingMode(); resumeDraftTimer();
      setNotice('已恢复另一页面保存的草稿。', 'info');
    } catch (error) { setNotice(`无法恢复草稿：${error.message}`); }
  }

  function exportDraft() {
    const snapshot = {...draftSnapshot(), schema_version: 2, revision: state.draftRevision, saved_at: new Date().toISOString()};
    const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a');
    link.href = url; link.download = `healthos-training-${state.draftId}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function refreshDraftChoices() {
    const select = $('#savedDrafts');
    try {
      const drafts = [];
      for (let index = 0; index < localStorage.length; index++) {
        const key = localStorage.key(index);
        if (!key?.startsWith(draftPrefix)) continue;
        try {
          const value = JSON.parse(localStorage.getItem(key));
          if (Array.isArray(value?.exercises)) drafts.push({key, value, tabCopy: key.includes('.tab.')});
        } catch (_) { /* Leave unreadable drafts untouched. */ }
      }
      drafts.sort((left, right) => String(right.value.saved_at).localeCompare(String(left.value.saved_at)));
      select.innerHTML = '<option value="">选择已保存草稿</option>' + drafts.map(({key, value, tabCopy}) => `<option value="${escapeHtml(key)}">${tabCopy ? '冲突恢复副本 · ' : ''}${value.editing_id ? `编辑 #${Number(value.editing_id)}` : '新训练'} · ${escapeHtml(value.session_date)} · ${escapeHtml(value.body_part)} · ${value.exercises.length} 个动作</option>`).join('');
      select.value = state.draftStorageKey || '';
    } catch (_) { select.innerHTML = '<option value="">本机草稿暂不可用</option>'; }
  }

  function switchDraft(key) {
    if (state.saving || !key || key === state.draftStorageKey) return;
    if (state.draftDirty && !writeDraft('switch-draft')) {
      setNotice('请先导出当前草稿并处理保存失败或冲突，再切换草稿。');
      refreshDraftChoices(); return;
    }
    try {
      const text = localStorage.getItem(key);
      if (!text || !applyDraft(JSON.parse(text), key, text)) throw new Error('草稿不存在或内容不完整');
      localStorage.setItem(activeDraftKey, key);
      exerciseSearch.clear();
      renderExercises(); renderTrainingMode(); resumeDraftTimer(); changeView('record');
      setNotice('已恢复所选草稿。', 'info');
    } catch (error) { setNotice(`无法切换草稿：${error.message}`); }
    refreshDraftChoices();
  }

  function removeCurrentDraft() {
    try {
      const key = currentDraftKey();
      if (localStorage.getItem(key) === state.lastDraftText) {
        localStorage.removeItem(key);
        if (localStorage.getItem(activeDraftKey) === key) localStorage.removeItem(activeDraftKey);
        for (let index = localStorage.length - 1; index >= 0; index--) {
          const copyKey = localStorage.key(index);
          if (!copyKey?.startsWith(`${key}.tab.`)) continue;
          try {
            const copy = JSON.parse(localStorage.getItem(copyKey));
            if (copy.draft_id === state.draftId) localStorage.removeItem(copyKey);
          } catch (_) { /* Leave unreadable recovery copies intact. */ }
        }
        return true;
      }
      return false;
    } catch (_) { return false; }
  }

  function payloadRecords() {
    exerciseRowsFromDom();
    const records = [];
    state.exercises.forEach((exercise, exerciseIndex) => {
      const name = exercise.name.trim();
      if (!name) throw validationError(`第 ${exerciseIndex + 1} 个动作需要填写名称。`, {exerciseId: exercise.id, field: 'name'});
      if (!exercise.sets.length) throw validationError(`${name} 至少需要一组记录。`, {exerciseId: exercise.id, field: 'name'});
      exercise.sets.forEach((set, setIndex) => {
        const target = {exerciseId: exercise.id, setIndex};
        const number = (value, field, label, {integer = false, max = Infinity, positive = false, segmentIndex} = {}) => {
          if (value === '' || value == null || !Number.isFinite(Number(value)) || Number(value) < 0 ||
              (integer && !Number.isInteger(Number(value))) || (positive && Number(value) <= 0) || Number(value) > max) {
            throw validationError(`${name} 第 ${setIndex + 1} 组${label}${positive ? '须为正整数' : '未填写或无效'}。`, {...target, field, segmentIndex});
          }
          return Number(value);
        };
        const weight = number(set.weight, 'weight', '重量');
        const reps = number(set.reps, 'reps', '次数', {integer: true, positive: true});
        if (set.drop) {
          const segments = (set.segments || []).map((segment, index) => ({...segment, index})).filter(segment => segment.weight !== '' || segment.reps !== '');
          if (!segments.length) throw validationError(`${name} 第 ${setIndex + 1} 组至少需要一个完整递减段。`, {...target, field: 'weight', segmentIndex: 0});
          const normalizedSegments = [{weight_kg: weight, reps}, ...segments.map(segment => ({
            weight_kg: number(segment.weight, 'weight', `递减段 ${segment.index + 1} 重量`, {segmentIndex: segment.index}),
            reps: number(segment.reps, 'reps', `递减段 ${segment.index + 1} 次数`, {integer: true, positive: true, segmentIndex: segment.index}),
          }))];
          const volume = normalizedSegments.reduce((sum, segment) => sum + segment.weight_kg * segment.reps, 0);
          records.push({name, raw_name: name, body_part: $('#bodyPart').value, weight_kg: null, reps: [], rpe: [], sets_count: 1,
            total_reps: normalizedSegments.reduce((sum, segment) => sum + segment.reps, 0), volume_kg: volume,
            exercise_order: exerciseIndex + 1, note: set.note || null,
            extra_data: {structure: 'drop_set_group', rest_seconds: set.restSeconds ? Number(set.restSeconds) : null, segments: normalizedSegments}});
          return;
        }
        const rpe = set.rpe === '' || set.rpe == null ? null : number(set.rpe, 'rpe', 'RPE（0–10）', {max: 10});
        records.push({name, raw_name: name, body_part: $('#bodyPart').value, weight_kg: weight,
          reps: [reps], rpe: rpe === null ? [] : [rpe], sets_count: 1, total_reps: reps, volume_kg: weight * reps,
          is_warmup: Boolean(set.warmup), exercise_order: exerciseIndex + 1, note: set.note || null, extra_data: {}});
      });
    });
    if (!records.length) throw validationError('请至少添加一组有效动作记录。', {id: 'addExerciseBtn'});
    return records;
  }

  function trainingFormData() {
    if (!$('#sessionDate').value) throw validationError('请选择训练日期。', {id: 'sessionDate'});
    const duration = $('#duration').value;
    if (duration !== '' && (!Number.isInteger(Number(duration)) || Number(duration) < 0)) {
      throw validationError('训练时长须为非负整数分钟。', {id: 'duration'});
    }
    const form = new URLSearchParams();
    form.set('csrf', csrf);
    form.set('request_id', state.requestId);
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

  function resetCurrentTraining() {
    if (state.saving) return;
    stopTimer();
    const removed = removeCurrentDraft();
    state.exercises = [];
    state.editingId = null;
    state.timerStartedAt = null;
    state.timerEndedAt = null;
    state.timerRunningSince = null;
    state.elapsedBase = 0;
    state.lastActivityAt = null;
    state.idleReminderShown = false;
    state.draftSavedAt = null;
    state.draftWriteError = null;
    state.draftDirty = false;
    state.draftId = uniqueId(); state.requestId = uniqueId();
    state.draftRevision = 0; state.draftStorageKey = null; state.lastDraftText = null;
    state.draftConflict = false; state.pendingSubmission = null;
    exerciseSearch.clear();
    clearDeletionUndo();
    setSaveFeedback('idle', '尚未提交到 HealthOS。');
    $('#sessionDate').value = localDate();
    $('#bodyPart').selectedIndex = 0;
    $('#duration').value = '';
    $('#feeling').value = '';
    closeTrainingDecision();
    renderExercises();
    renderTrainingMode();
    setNotice(removed ? '' : '本页已清空，但无法移除本机草稿。刷新后可能再次恢复旧内容。');
    toast('本次未保存训练已丢弃');
  }

  function discardCurrentTraining() {
    if (!confirm('丢弃本次未保存训练及计时？此操作无法撤销。')) return;
    resetCurrentTraining();
  }

  function closeTrainingDecision() {
    $('#trainingDecisionOverlay').classList.remove('open');
    renderSaveFeedback();
  }

  function openTrainingDecision(source) {
    closeSetMenu();
    const idle = source === 'idle';
    $('#trainingDecisionTitle').textContent = idle ? '还在训练吗？' : '结束本次训练';
    $('#trainingDecisionDescription').textContent = idle
      ? '已经有 10 分钟没有填写新的训练数据。计时仍在继续，请选择下一步。'
      : '计时已暂停。你可以继续训练、保存本次训练，或丢弃本次训练。';
    $('#trainingDecisionOverlay').classList.add('open');
    renderSaveFeedback();
  }

  function continueTrainingFromDecision() {
    const wasEnded = Boolean(state.timerEndedAt);
    closeTrainingDecision();
    if (wasEnded) startTimer();
    else scheduleDraftSave();
  }

  async function saveTrainingFromDecision() {
    if (!state.timerEndedAt && state.timerStartedAt) {
      finishTimer();
      scheduleDraftSave();
    }
    await saveTraining();
  }

  async function saveTraining() {
    if (state.saving) return;
    clearDeletionUndo();
    closeSetMenu();
    if ($('#trainingNotice').dataset.source === 'save') setNotice('');
    if (state.draftConflict) {
      markDraftConflict();
      setSaveFeedback('error', '未保存：另一页面已修改这份草稿，请先处理冲突。');
      showSaveFailure();
      return;
    }
    const button = $('#saveBtn');
    let savedItem = null;
    let cleanupFailed = false;
    let submitted = false;
    let preservedEdits = false;
    const previousPending = state.pendingSubmission;
    state.saving = true;
    const controls = $$('#trainingEntry input, #trainingEntry select, #trainingEntry button, #activeTraining input, #activeTraining textarea, #activeTraining button, #trainingDecisionOverlay button, #recoverDraftBtn');
    controls.forEach(control => { control.disabled = true; });
    try {
      setSaveFeedback('saving', '正在保存到 HealthOS，请稍候。');
      let form = trainingFormData();
      const currentPayload = Object.fromEntries([...form].filter(([key]) => key !== 'csrf'));
      const pendingPayload = state.pendingSubmission?.payload;
      const confirmingEarlier = pendingPayload && JSON.stringify(pendingPayload) !== JSON.stringify(currentPayload);
      if (pendingPayload) form = new URLSearchParams(pendingPayload);
      state.draftDirty = true;
      writeDraft('before-submit');
      if (state.draftConflict) throw validationError('另一页面已更新草稿，请先处理冲突。');
      await checkSession();
      form.set('csrf', csrf);
      state.pendingSubmission = {request_id: state.requestId, at: new Date().toISOString(), payload: pendingPayload || currentPayload};
      writeDraft('before-request');
      if (state.draftConflict) throw validationError('另一页面已更新草稿，请先处理冲突。');
      submitted = true;
      const result = await request('/training', {method: 'POST', body: form});
      const item = result.item;
      if (!item || !Number.isSafeInteger(item.id) || item.id <= 0 ||
          (state.editingId ? item.id !== state.editingId : item.request_id !== state.requestId)) {
        throw new Error('未收到有效的保存凭据，草稿已保留。请重试确认保存。');
      }
      savedItem = item;
      if (confirmingEarlier) {
        // Confirm the uncertain write without discarding changes made after that request.
        state.editingId = item.id;
        state.requestId = uniqueId();
        state.pendingSubmission = null;
        state.draftDirty = true;
        writeDraft('confirmed-earlier-submission');
        setSaveFeedback('idle', '上次提交已确认保存；当前修改尚未提交到 HealthOS，请再次保存。');
        preservedEdits = true;
      } else {
        cleanupFailed = !removeCurrentDraft();
        closeTrainingDecision();
        state.draftSavedAt = null;
        state.draftWriteError = null;
        state.draftDirty = false;
        state.editingId = null;
        state.timerStartedAt = null;
        state.timerEndedAt = null;
        state.timerRunningSince = null;
        state.elapsedBase = 0;
        state.draftId = uniqueId(); state.requestId = uniqueId();
        state.draftRevision = 0; state.draftStorageKey = null; state.lastDraftText = null;
        state.draftConflict = false; state.pendingSubmission = null;
        exerciseSearch.clear();
        stopTimer();
        $('#sessionDate').value = localDate();
        $('#duration').value = '';
        $('#feeling').value = '';
        state.exercises = [];
        renderExercises();
        renderTrainingMode();
        setSaveFeedback(cleanupFailed ? 'saved_with_warning' : 'saved', cleanupFailed
          ? '训练已保存到 HealthOS，但本机旧草稿未能清理，请勿再次提交旧草稿。' : '训练已保存到 HealthOS。');
      }
    } catch (error) {
      if (savedItem) {
        setSaveFeedback('saved_with_warning', `训练 #${savedItem.id} 已保存到 HealthOS，但页面更新未完成。请刷新查看，不要重复提交。`);
      } else {
        const uncertain = submitted && (!error.status || error.status >= 500);
        if (!uncertain) state.pendingSubmission = previousPending;
        writeDraft('submit-failed');
        let message = error.message || '保存未完成';
        if (error.status === 403 && error.code === 'csrf_invalid') {
          try { await checkSession(); message = '页面验证已更新，请再次点击保存。'; }
          catch (sessionError) { showError(sessionError); }
        } else if (error.status === 401) message = error.staleAuth ? '登录状态已更新，请再次点击保存。' : '请重新登录后再次保存。';
        setSaveFeedback(uncertain ? 'uncertain' : 'error', `${uncertain ? '尚未确认保存，请重试确认' : '未保存'}：${message} 训练内容仍保留在本页。${!uncertain && previousPending ? '上次提交仍待确认。' : ''}`, error.target);
        showSaveFailure();
      }
    }
    finally {
      state.saving = false;
      controls.forEach(control => { if (control.isConnected) control.disabled = false; });
      button.textContent = state.editingId ? '更新训练' : '保存训练';
    }
    if (!savedItem || preservedEdits) return;
    // Saving is already acknowledged; refresh errors must never look like a failed write.
    state.offset = 0;
    try {
      await Promise.all([loadSessions(), loadAnalytics()]);
      changeView('history');
      await loadDetail(savedItem.id);
    } catch (_) {
      setSaveFeedback('saved_with_warning', `训练 #${savedItem.id} 已保存到 HealthOS，${cleanupFailed ? '旧草稿清理及' : ''}历史或统计刷新暂时失败。请稍后刷新查看，不要重复提交。`);
    }
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
    root.innerHTML = `<div class="block-title"><h2>训练详情</h2></div>
      <div class="detail-title"><div><h3>${escapeHtml(fmtDate(session.session_date))} · ${escapeHtml(session.body_part)}</h3><p>${escapeHtml(session.feeling || '未填写状态备注')}</p></div><div class="detail-actions"><button class="tiny-btn" id="editSession" type="button">编辑</button><button class="tiny-btn danger" id="deleteSession" type="button">删除</button></div></div>
      <div class="detail-kpis"><div class="detail-kpi"><small>时长</small><strong>${session.duration_minutes ? `${session.duration_minutes} 分钟` : '—'}</strong></div><div class="detail-kpi"><small>训练量</small><strong>${fmtNumber(session.total_volume_kg)} kg</strong></div><div class="detail-kpi"><small>动作组</small><strong>${session.records.length} 组</strong></div></div>
      ${groups.map(group => `<details class="exercise-block" open><summary>${escapeHtml(group.name)}<span>${group.sets.length} 组</span></summary><div class="table-wrap"><table class="records-table"><thead><tr><th>组次</th><th>重量</th><th>次数</th><th>RPE</th><th>组型</th></tr></thead><tbody>${group.sets.map((set, index) => `<tr><td>${index + 1}</td><td>${set.drop ? '递减组' : `${escapeHtml(set.weight)} kg`}</td><td>${set.drop ? (set.segments || []).map(segment => escapeHtml(segment.reps)).join(' → ') : escapeHtml(set.reps)}</td><td>${escapeHtml(set.rpe ?? '')}</td><td>${set.drop ? '递减组' : set.warmup ? '热身组' : '普通组'}</td></tr>`).join('')}</tbody></table></div></details>`).join('') || '<p class="status-note">此训练没有动作记录。</p>'}`;
    $('#editSession').onclick = () => editSession(session);
    $('#deleteSession').onclick = () => deleteSession(session.id);
  }

  function editSession(session) {
    if (state.saving) return;
    if (state.draftDirty && !writeDraft('switch-to-edit')) {
      setNotice('当前草稿尚未安全保存。请先导出或处理冲突，再编辑历史训练。');
      return;
    }
    stopTimer();
    exerciseSearch.clear();
    const editKey = `${draftPrefix}edit.${session.id}`;
    let existing;
    try { existing = localStorage.getItem(editKey); }
    catch (_) { setNotice('无法读取编辑草稿，请先导出当前训练再重试。'); return; }
    if (existing) {
      try {
        if (!applyDraft(JSON.parse(existing), editKey, existing)) throw new Error('草稿内容不完整');
        renderExercises(); renderTrainingMode(); resumeDraftTimer();
        changeView('record');
        setNotice('已恢复这节训练的未保存编辑。', 'info');
      } catch (error) { setNotice(`编辑草稿无法恢复：${error.message}`); }
      return;
    }
    state.draftId = uniqueId(); state.requestId = uniqueId();
    state.draftRevision = 0; state.draftStorageKey = editKey; state.lastDraftText = null;
    state.draftConflict = false; state.draftSavedAt = null; state.draftWriteError = null; state.pendingSubmission = null;
    state.editingId = Number(session.id);
    state.exercises = groupSessionRecords(session.records).map(group => ({...group, id: uniqueId(), last: null}));
    $('#sessionDate').value = fmtDate(session.session_date);
    $('#bodyPart').value = session.body_part;
    $('#feeling').value = session.feeling || '';
    $('#duration').value = session.duration_minutes || '';
    const timing = session.extra_data?.training_timing || {};
    state.timerStartedAt = timing.started_at || null;
    state.timerEndedAt = timing.ended_at || null;
    state.elapsedBase = state.timerStartedAt && state.timerEndedAt ? Math.max(0, Math.floor((Date.parse(state.timerEndedAt) - Date.parse(state.timerStartedAt)) / 1000)) : 0;
    state.timerRunningSince = state.timerEndedAt ? null : state.timerStartedAt;
    renderExercises();
    renderTrainingMode();
    $('#saveBtn').textContent = '更新训练';
    changeView('record');
    scheduleDraftSave();
    resumeDraftTimer();
  }

  async function deleteSession(id) {
    if (!confirm('删除这节训练及其所有动作记录？此操作无法撤销。')) return;
    const form = new URLSearchParams();
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

  function exerciseCard(id) {
    return $$('.exercise-card').find(card => card.dataset.exerciseId === id);
  }

  function searchFor(exercise) {
    if (!exerciseSearch.has(exercise.id)) exerciseSearch.set(exercise.id, {sequence: 0, timer: null, items: [], active: -1, open: false, composing: false, message: ''});
    return exerciseSearch.get(exercise.id);
  }

  function closeExerciseHints(id = null) {
    for (const [exerciseId, search] of exerciseSearch) {
      if (id && id !== exerciseId) continue;
      search.open = false;
      search.sequence += 1;
      clearTimeout(search.timer);
      const card = exerciseCard(exerciseId);
      if (card) {
        $('.exercise-hints', card).hidden = true;
        $('.exercise-name', card).setAttribute('aria-expanded', 'false');
        $('.exercise-name', card).removeAttribute('aria-activedescendant');
      }
    }
  }

  function renderExerciseHints(exercise) {
    const card = exerciseCard(exercise.id);
    if (!card) return;
    const search = searchFor(exercise);
    search.items = search.items.filter(item => !exerciseHintUsed(exercise, item));
    if (search.active >= search.items.length) search.active = search.items.length - 1;
    const list = $('.exercise-hints', card);
    const input = $('.exercise-name', card);
    list.hidden = !search.open || !search.items.length;
    input.setAttribute('aria-expanded', String(!list.hidden));
    $('.exercise-hint-status', card).textContent = search.message;
    list.innerHTML = search.items.map((item, index) => `<div role="option" id="${escapeHtml(list.id)}-${index}" aria-selected="${index === search.active}" data-suggestion-index="${index}"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.body_part)}${item.aliases?.length ? ` · ${item.aliases.map(escapeHtml).join('、')}` : ''}</small></div>`).join('');
    $$('[role="option"]', list).forEach((option, index) => {
      let selectedByPointer = false;
      let pointerStart = null;
      option.onpointerdown = event => {
        if (event.pointerType === 'mouse' && event.button !== 0) return;
        event.preventDefault();
        pointerStart = {id: event.pointerId, x: event.clientX, y: event.clientY};
      };
      option.onpointerup = event => {
        if (!pointerStart || pointerStart.id !== event.pointerId) return;
        const moved = Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 8;
        pointerStart = null;
        if (moved) return;
        selectedByPointer = true;
        selectExerciseHint(exercise, search.items[index]);
      };
      option.onpointercancel = () => { pointerStart = null; };
      option.onclick = () => {
        if (selectedByPointer) { selectedByPointer = false; return; }
        selectExerciseHint(exercise, search.items[index]);
      };
    });
    if (!list.hidden && search.active >= 0) {
      input.setAttribute('aria-activedescendant', `${list.id}-${search.active}`);
      $('[aria-selected="true"]', list)?.scrollIntoView({block: 'nearest'});
    } else input.removeAttribute('aria-activedescendant');
  }

  function exerciseHintUsed(currentExercise, item) {
    const normalize = value => String(value || '').replace(/\s+/gu, '').toLowerCase();
    const names = new Set([item.name, ...(item.aliases || [])].map(normalize));
    return state.exercises.some(exercise => exercise !== currentExercise &&
      ((exercise.canonicalId != null && item.id != null && String(exercise.canonicalId) === String(item.id)) ||
       (normalize(exercise.name) && names.has(normalize(exercise.name)))));
  }

  function queueExerciseHints(exercise, composing = false) {
    const search = searchFor(exercise);
    clearTimeout(search.timer);
    search.sequence += 1;
    search.items = []; search.active = -1; search.open = false;
    search.message = '';
    renderExerciseHints(exercise);
    if (!composing && !search.composing) search.timer = setTimeout(() => refreshExerciseHints(exercise), 180);
  }

  async function refreshExerciseHints(exercise) {
    const search = searchFor(exercise);
    if (search.composing) return;
    const sequence = ++search.sequence;
    const query = exercise.name;
    const part = $('#bodyPart').value;
    const params = new URLSearchParams({q: query, body_part: part});
    const current = () => exerciseSearch.get(exercise.id) === search && search.sequence === sequence &&
      state.exercises.includes(exercise) && exercise.name === query && $('#bodyPart').value === part &&
      document.activeElement === $('.exercise-name', exerciseCard(exercise.id) || document.createElement('div'));
    search.items = []; search.active = -1; search.open = true; search.message = '正在加载动作…';
    renderExerciseHints(exercise);
    try {
      const result = await request(`/owner/api/training/exercises?${params}`);
      if (!Array.isArray(result.items)) throw new Error('动作列表格式无效');
      if (!current()) return;
      const candidates = result.items.filter(item => typeof item.name === 'string').map(item => ({...item, aliases: Array.isArray(item.aliases) ? item.aliases : []}));
      search.items = candidates.filter(item => !exerciseHintUsed(exercise, item));
      search.message = search.items.length ? `${search.items.length} 个匹配动作` : '没有匹配动作，可以保留手动输入。';
      if (candidates.length && !search.items.length) search.message = '匹配动作已在本次训练中，可回到原动作加组，也可继续手动输入。';
    } catch (error) {
      if (!current()) return;
      search.message = error.status === 401 ? '请重新登录后加载动作建议。' : '动作建议加载失败，可继续手动输入或重新聚焦重试。';
    }
    if (current()) renderExerciseHints(exercise);
  }

  function selectExerciseHint(exercise, item) {
    if (!item || state.saving) return;
    if (exerciseHintUsed(exercise, item)) { refreshExerciseHints(exercise); return; }
    exercise.name = item.name; exercise.last = null;
    exercise.canonicalId = item.id ?? null;
    const card = exerciseCard(exercise.id);
    if (!card) return;
    $('.exercise-name', card).value = item.name;
    $('.last-training', card).textContent = '正在加载上次训练…';
    closeExerciseHints(exercise.id);
    scheduleDraftSave();
    loadLastExercise(exercise).catch(showError);
  }

  function formatLastTraining(result) {
    if (!result.session_date) return '暂无历史记录';
    const sets = (result.sets || []).flatMap(set => {
      if (set.extra_data?.structure === 'drop_set_group') return [`递减组 ${(set.extra_data.segments || []).map(segment => `${segment.weight_kg ?? '未记录'} kg × ${segment.reps ?? '未记录'}`).join(' → ') || '未记录分段'}`];
      const reps = Array.isArray(set.reps) ? set.reps : [];
      const rpe = Array.isArray(set.rpe) ? set.rpe : [];
      return Array.from({length: Math.max(reps.length, rpe.length, Number(set.sets_count) || 1)}, (_, index) => `${set.weight_kg ?? '未记录'} kg × ${reps[index] ?? '未记录'} · RPE ${rpe[index] ?? '未记录'}`);
    });
    return `${fmtDate(result.session_date)} · ${sets.join('；') || '未记录组数据'}`;
  }

  async function loadLastExercise(exercise) {
    const name = exercise.name;
    const sequence = exercise.lastSequence = (exercise.lastSequence || 0) + 1;
    const result = await request(`/owner/api/training/exercises/last?name=${encodeURIComponent(name)}`);
    if (!state.exercises.includes(exercise) || exercise.name !== name || exercise.lastSequence !== sequence) return;
    exercise.last = formatLastTraining(result);
    const card = exerciseCard(exercise.id);
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
    $('#statsRange').textContent = '最近 30 天';
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
  function elapsedSeconds() { return state.elapsedBase + (state.timerRunningSince && !state.timerEndedAt ? Math.floor((Date.now() - Date.parse(state.timerRunningSince)) / 1000) : 0); }
  function renderTimer() {
    const elapsed = Math.max(0, elapsedSeconds());
    $('#timer').textContent = `${padTime(Math.floor(elapsed / 60))}:${padTime(elapsed % 60)}`;
    if (state.timerStartedAt) $('#duration').value = String(Math.round(elapsed / 60));
  }
  function stopTimer() {
    clearInterval(timerHandle); timerHandle = null;
    clearInterval(idleCheckHandle); idleCheckHandle = null;
  }
  function checkIdleTraining() {
    if (!state.timerStartedAt || state.timerEndedAt || state.idleReminderShown || !state.lastActivityAt) return;
    const idleSeconds = Math.floor((Date.now() - Date.parse(state.lastActivityAt)) / 1000);
    if (idleSeconds < idleReminderSeconds) return;
    state.idleReminderShown = true;
    writeDraft('idle-reminder');
    openTrainingDecision('idle');
  }
  function startTimer() {
    if (state.saving) return;
    if (!state.timerStartedAt) state.timerStartedAt = new Date().toISOString();
    if (!state.timerRunningSince) state.timerRunningSince = new Date().toISOString();
    state.timerEndedAt = null;
    state.idleReminderShown = false;
    $('#activePart').textContent = $('#bodyPart').value;
    $('#activeLabel').textContent = '进行中';
    $('#endBtn').textContent = '结束运动';
    $('#activeBar').hidden = false; $('#startBtn').hidden = true;
    stopTimer();
    timerHandle = setInterval(renderTimer, 1000);
    idleCheckHandle = setInterval(checkIdleTraining, 15000);
    renderTimer(); scheduleDraftSave();
  }
  function finishTimer() {
    state.elapsedBase = elapsedSeconds();
    state.timerEndedAt = new Date().toISOString();
    state.timerRunningSince = null;
    stopTimer(); renderTimer();
  }

  function showError(error) {
    if (error.staleAuth) return;
    if (error.status === 401) return;
    if (error.status === 403 && error.code === 'csrf_invalid') {
      checkSession().then(() => setNotice('页面验证已更新，请重试刚才的操作。')).catch(showError);
      return;
    }
    setNotice(error.message || 'HealthOS 数据暂不可用');
  }

  function resumeDraftTimer() {
    stopTimer();
    $('#saveBtn').textContent = state.editingId ? '更新训练' : '保存训练';
    $('#activePart').textContent = $('#bodyPart').value;
    if (!state.timerStartedAt) return;
    $('#activeBar').hidden = false; $('#startBtn').hidden = true;
    if (state.timerEndedAt) {
      $('#activeLabel').textContent = '已结束'; $('#endBtn').textContent = '继续计时';
    } else {
      $('#activeLabel').textContent = '已恢复计时'; $('#endBtn').textContent = '结束训练';
      timerHandle = setInterval(renderTimer, 1000);
      idleCheckHandle = setInterval(checkIdleTraining, 15000);
    }
    renderTimer();
  }

  async function initialize() {
    $('#sessionDate').value = localDate();
    $('#duration').value = '';
    $('#sidebar').classList.remove('open');
    $$('.side-nav button').forEach(button => button.onclick = () => changeView(button.dataset.view));
    $('#menuBtn').onclick = () => $('#sidebar').classList.toggle('open');
    $('#entryStartBtn').onclick = () => { startTimer(); renderTrainingMode(); };
    $('#bodyPart').onchange = () => { $('#activePart').textContent = $('#bodyPart').value; closeExerciseHints(); scheduleDraftSave(); renderTrainingMode(); };
    $('#sessionDate').onchange = scheduleDraftSave;
    $('#feeling').oninput = scheduleDraftSave;
    $('#duration').oninput = scheduleDraftSave;
    $('#exerciseList').addEventListener('input', event => { if (event.target.matches('.set-row input')) { updateVolume(); scheduleDraftSave(); } });
    $('#exerciseList').addEventListener('click', event => {
      const button = event.target.closest('.set-more');
      if (!button) return;
      openSetMenu(button);
    });
    $('#toggleSetType').onclick = () => {
      if (!setMenuContext || state.saving) return;
      exerciseRowsFromDom();
      const {exerciseId, setIndex} = setMenuContext;
      const exercise = state.exercises.find(item => item.id === exerciseId);
      const set = exercise?.sets[setIndex];
      if (!set) { closeSetMenu(); return; }
      set.drop = !set.drop;
      if (set.drop && !set.segments?.length) set.segments = [{weight: '', reps: ''}];
      renderExercises(); scheduleDraftSave();
      $('.set-more', $$('.set-row', exerciseCard(exerciseId))[setIndex]).focus();
    };
    $('#deleteSet').onclick = () => { if (setMenuContext) deleteDraftPart(setMenuContext.exerciseId, setMenuContext.setIndex); };
    $('#undoDeletionBtn').onclick = undoDraftDeletion;
    $$('[data-locate-error]').forEach(button => { button.onclick = locateSaveError; });
    document.addEventListener('pointerdown', event => {
      if (!event.target.closest('#setMenu, .set-more')) closeSetMenu();
    });
    document.addEventListener('keydown', event => {
      if (!setMenuContext) return;
      if (event.key === 'Escape') { event.preventDefault(); closeSetMenu(true); }
      else if (event.key === 'Tab') closeSetMenu(true);
    });
    $('#setMenu').onkeydown = event => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const buttons = $$('button', $('#setMenu'));
      const index = buttons.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
    };
    window.addEventListener('scroll', () => closeSetMenu(), {passive: true});
    $('#exerciseList').addEventListener('click', event => {
      if (!event.target.closest('.add-drop-segment')) return;
      const row = event.target.closest('.set-row');
      const set = state.exercises[Number(row.dataset.exerciseIndex)].sets[Number(row.dataset.setIndex)];
      exerciseRowsFromDom();
      set.segments.push({weight: '', reps: ''}); renderExercises(); scheduleDraftSave();
    });
    $('#addExerciseBtn').onclick = () => { state.exercises.push(newExercise()); renderExercises(); $('.exercise-name', $('#exerciseList').lastElementChild)?.focus(); scheduleDraftSave(); };
    $('#clearBtn').onclick = () => { if (!confirm('清空当前未保存训练的动作数据？')) return; state.exercises = []; renderExercises(); scheduleDraftSave(); };
    $('#discardBtn').onclick = discardCurrentTraining;
    $('#saveBtn').onclick = saveTraining;
    $('#exportDraftBtn').onclick = exportDraft;
    $('#recoverDraftBtn').onclick = recoverSavedDraft;
    $('#reauthenticateBtn').onclick = openAuthentication;
    $('#authForm').onsubmit = authenticate;
    $('#authCancel').onclick = closeAuthentication;
    $('#authOverlay').addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); closeAuthentication(); }
      if (event.key === 'Tab') {
        const controls = $$('input, button', $('#authOverlay')).filter(control => !control.disabled);
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    $('#startBtn').onclick = () => startTimer();
    $('#endBtn').onclick = () => {
      if (state.timerEndedAt) { startTimer(); return; }
      finishTimer(); scheduleDraftSave(); openTrainingDecision('manual');
    };
    $('#decisionContinue').onclick = continueTrainingFromDecision;
    $('#decisionSave').onclick = saveTrainingFromDecision;
    $('#decisionDiscard').onclick = resetCurrentTraining;
    $('#historyPrevious').onclick = async () => { state.offset = Math.max(0, state.offset - pageSize); await loadSessions(); };
    $('#historyNext').onclick = async () => { if (state.offset + pageSize < state.total) { state.offset += pageSize; await loadSessions(); } };
    $('#historySearch').oninput = () => { clearTimeout(searchHandle); searchHandle = setTimeout(() => { state.query = $('#historySearch').value.trim(); state.offset = 0; loadSessions().catch(showError); }, 220); };
    $('#suggestBtn').onclick = () => showSuggestions().catch(showError);
    $('#aliasQuery').onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); showSuggestions().catch(showError); } };
    $('#aliasForm').onsubmit = async event => {
      event.preventDefault(); const form = new URLSearchParams(new FormData(event.currentTarget)); form.set('csrf', csrf);
      try { await request('/training/aliases', {method: 'POST', body: form}); event.currentTarget.reset(); await loadAliases(); toast('动作别名已保存'); }
      catch (error) { showError(error); }
    };
    $$('.trend-controls [data-metric]').forEach(button => button.onclick = () => {
      state.metric = button.dataset.metric; $$('.trend-controls [data-metric]').forEach(item => item.classList.toggle('active', item === button));
      $('#chartTitle').textContent = state.metric === 'e1rm' ? 'e1RM 趋势' : state.metric === 'sets' ? '有效组趋势' : '训练容量趋势'; drawChart();
    });
    $('#e1rmExercise').onchange = drawChart;
    window.addEventListener('resize', () => { closeSetMenu(); drawChart(); });
    recordLifecycle('load');
    document.addEventListener('visibilitychange', () => {
      recordLifecycle(`visibility:${document.visibilityState}`);
      if (document.visibilityState === 'hidden' && state.draftDirty) writeDraft('visibility-hidden');
      if (document.visibilityState === 'visible') { checkIdleTraining(); checkSession().catch(showError); }
    });
    window.addEventListener('pagehide', () => {
      recordLifecycle('pagehide');
      if (state.draftDirty) writeDraft('pagehide');
    });
    window.addEventListener('pageshow', event => {
      recordLifecycle(event.persisted ? 'pageshow-bfcache' : 'pageshow');
      checkIdleTraining();
      checkSession().catch(showError);
    });
    window.addEventListener('storage', event => {
      if (event.key === authUpdateKey) { refreshSharedAuthentication(); return; }
      if (event.key === state.draftStorageKey && event.newValue !== state.lastDraftText && state.draftDirty) markDraftConflict();
      refreshDraftChoices();
    });
    $('#savedDrafts').onchange = () => switchDraft($('#savedDrafts').value);
    const restored = restoreDraft();
    if (!state.exercises.length) state.exercises = [];
    renderExercises();
    renderTrainingMode();
    resumeDraftTimer();
    refreshDraftChoices();
    try {
      await checkSession();
      await Promise.all([loadSessions(), loadAnalytics(), loadAliases()]);
      if (restored) toast('已恢复本地未保存草稿');
    } catch (error) { showError(error); }
  }

  initialize();
})();
