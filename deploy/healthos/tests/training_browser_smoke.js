async page => {
  const base = '__BASE_URL__', output = '__OUTPUT_DIR__';
  const context = page.context(), results = [], writes = [], runtimeErrors = [];
  let stage = 'login';
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const frame = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  page.on('pageerror', error => runtimeErrors.push(error.message));
  page.on('request', request => {
    if (request.method() === 'POST' && request.url() === base + '/training') writes.push(request.postData());
  });
  const cards = () => page.locator('.exercise-card');
  const rows = card => card.locator('.set-row');
  async function fresh() {
    await page.unrouteAll({behavior: 'wait'});
    await page.goto(base + '/training');
    await page.evaluate(() => localStorage.clear());
    await page.reload();
    await page.locator('#entryStartBtn').click();
    await page.waitForFunction(() => document.querySelector('#aliasExercise').options.length >= 2);
  }
  async function add(name = 'Synthetic press', weight = '40', reps = '8') {
    await page.locator('#addExerciseBtn').click();
    const card = cards().last();
    await card.locator('.exercise-name').fill(name);
    if (weight !== null) await rows(card).first().locator('[data-field="weight"]').fill(weight);
    if (reps !== null) await rows(card).first().locator('[data-field="reps"]').fill(reps);
    return card;
  }
  async function menu(row, action) {
    const mode = page.viewportSize().width === 390 ? 'tap' : 'click';
    await row.locator('.set-more')[mode]();
    await page.getByRole('menuitem', {name: action, exact: true})[mode]();
  }
  async function submitStatus(status, modal = false) {
    await page.locator(modal ? '#decisionSave' : '#saveBtn').click();
    await page.waitForFunction(expected => document.querySelector('#saveFeedback').dataset.state === expected &&
      !document.querySelector('#saveBtn').disabled, status);
  }
  async function noSuccess() {
    assert(!['saved', 'saved_with_warning'].includes(await page.locator('#saveFeedback').getAttribute('data-state')), 'false success state');
    assert(!(await page.locator('#trainingNoticeText').innerText()).includes('训练已保存到'), 'stale global success');
    assert((await page.locator('#draftStatus').innerText()).includes('本机草稿已保留'), 'draft was not retained');
  }
  try {
    await context.clearCookies();
    await page.goto(base + '/login');
    await page.getByRole('textbox', {name: '管理员密码'}).fill('synthetic-probe-password');
    await page.getByRole('button', {name: '登录 HealthOS'}).click();
    await page.waitForURL(base + '/');
    for (const width of __VIEWPORTS__) {
      const label = width === 390 ? 'mobile' : 'desktop';
      await page.setViewportSize({width, height: 844});
      stage = label + ': inheritance';
      await fresh();
      let card = await add('Synthetic press', null, null);
      assert(await rows(card).first().locator('[data-field="weight"]').inputValue() === '', 'first weight was prefilled');
      await rows(card).first().locator('[data-field="weight"]').fill('50');
      await rows(card).first().locator('[data-field="reps"]').fill('8');
      await rows(card).first().locator('[data-field="rpe"]').fill('8');
      await rows(card).first().locator('[data-field="note"]').fill('private synthetic note');
      await card.locator('.add-set').click();
      assert(await rows(card).last().locator('[data-field="weight"]').inputValue() === '50', 'weight not inherited');
      for (const field of ['reps', 'rpe', 'note']) assert(await rows(card).last().locator(`[data-field="${field}"]`).inputValue() === '', field + ' copied');
      await rows(card).first().locator('[data-field="weight"]').fill('60');
      assert(await rows(card).last().locator('[data-field="weight"]').inputValue() === '50', 'inheritance stayed linked');
      await rows(card).last().locator('[data-field="weight"]').fill('0');
      await card.locator('.add-set').click();
      assert(await rows(card).last().locator('[data-field="weight"]').inputValue() === '0', 'zero weight lost');
      await rows(card).last().locator('[data-field="weight"]').fill('');
      await card.locator('.add-set').click();
      assert(await rows(card).last().locator('[data-field="weight"]').inputValue() === '', 'empty weight guessed');
      results.push(label + ': first/normal/zero/empty weight, no linked edits or copied reps/RPE/notes');

      stage = label + ': set menu and undo';
      await fresh();
      card = await add('Synthetic press', '60', '8');
      await rows(card).first().locator('.set-more').click();
      assert(!(await rows(card).first().getAttribute('class')).includes(' drop'), 'menu itself changed set type');
      await page.keyboard.press('ArrowDown');
      assert(await page.getByRole('menuitem', {name: '删除本组'}).evaluate(node => node === document.activeElement), 'menu arrow navigation');
      await page.keyboard.press('Escape');
      assert(await page.locator('#setMenu').isHidden(), 'Escape failed');
      assert(await rows(card).first().locator('.set-more').evaluate(node => node === document.activeElement), 'menu focus not restored');
      await menu(rows(card).first(), '设为递减组');
      await rows(card).first().locator('[data-field="weight"][data-segment="0"]').fill('25');
      await rows(card).first().locator('[data-field="reps"][data-segment="0"]').fill('10');
      assert((await page.locator('#totalVolume').innerText()).includes('730'), 'drop main volume omitted');
      await card.locator('.add-set').click();
      assert(await rows(card).last().locator('[data-field="weight"]').inputValue() === '60', 'copied final drop segment instead of main weight');
      await rows(card).last().locator('[data-field="reps"]').fill('6');
      await rows(card).first().locator('.set-more').click();
      const box = await page.locator('#setMenu').boundingBox();
      assert(box.x >= 0 && box.x + box.width <= width && box.y >= 0 && box.y + box.height <= 844, 'menu outside viewport');
      await page.screenshot({path: output + '/set-menu-' + label + '.png'});
      await page.getByRole('menuitem', {name: '删除本组'}).click();
      assert(await rows(card).count() === 1, 'drop group not removed');
      assert((await rows(card).first().locator('.set-no').innerText()).includes('1'), 'set not renumbered');
      assert((await page.locator('#totalVolume').innerText()).includes('360'), 'deleted volume remains');
      await page.locator('#undoDeletionBtn').click();
      assert(await rows(card).count() === 2 && await rows(card).first().locator('[data-segment="0"][data-field="weight"]').inputValue() === '25', 'undo lost drop data');
      await menu(rows(card).first(), '删除本组');
      await rows(card).first().locator('[data-field="weight"]').fill('65');
      assert(await page.locator('#deletionUndo').isHidden(), 'undo remained after later edit');
      await menu(rows(card).first(), '删除本组');
      assert(await cards().count() === 0, 'last group left an empty exercise');
      await page.locator('#undoDeletionBtn').click();
      assert(await cards().count() === 1 && await rows(cards().first()).first().locator('[data-field="weight"]').inputValue() === '65', 'last-exercise undo failed');
      await page.reload();
      assert(await cards().count() === 1 && await rows(cards().first()).count() === 1, 'draft restored deleted groups');
      await page.evaluate(() => {
        const key = localStorage.getItem('healthos.training.active-draft.v2');
        const draft = JSON.parse(localStorage.getItem(key));
        draft.timer_started_at = null; draft.timer_ended_at = null; draft.timer_running_since = null;
        localStorage.setItem(key, JSON.stringify(draft));
      });
      await page.reload();
      await menu(rows(cards().first()).first(), '删除本组');
      assert(await page.locator('#undoDeletionBtn').isVisible(), 'legacy untimed draft hid its last-group undo');
      await page.locator('#undoDeletionBtn').click();
      assert(await cards().count() === 1, 'untimed draft could not undo its last group');
      results.push(label + ': menu keyboard/touch layout, drop inheritance/volume, deletion, undo and draft reload');

      stage = label + ': suggestion filtering';
      await fresh();
      await page.locator('#addExerciseBtn').click();
      let first = cards().first();
      await first.locator('.exercise-name').fill('press');
      await first.getByRole('option').filter({hasText: 'Synthetic press'})[width === 390 ? 'tap' : 'click']();
      stage = label + ': current-card candidate';
      await page.locator('#feeling').focus();
      await first.locator('.exercise-name').focus();
      await first.getByRole('option').filter({hasText: 'Synthetic press'}).waitFor();
      await page.locator('#addExerciseBtn').click();
      let second = cards().last();
      await second.locator('.exercise-name').fill('press');
      await second.locator('.exercise-hint-status').filter({hasText: '已在本次训练'}).waitFor();
      assert(await second.getByRole('option').count() === 0, 'selected standard appeared again');
      await second.locator('.exercise-name').fill('Bench alias');
      await second.locator('.exercise-hint-status').filter({hasText: '已在本次训练'}).waitFor();
      assert(await second.getByRole('option').count() === 0, 'selected alias appeared again');
      await page.reload();
      second = cards().last();
      await second.locator('.exercise-name').fill('press');
      await second.locator('.exercise-hint-status').filter({hasText: '已在本次训练'}).waitFor();
      await cards().first().locator('.remove-exercise').click();
      stage = label + ': deleted candidate returns';
      await cards().first().locator('.exercise-name').fill('press');
      await cards().first().getByRole('option').filter({hasText: 'Synthetic press'}).waitFor();
      await fresh();
      await add(' bEnCh aLiAs ');
      await add('', null, null);
      await cards().last().locator('.exercise-name').fill('press');
      await cards().last().locator('.exercise-hint-status').filter({hasText: '已在本次训练'}).waitFor();
      await fresh();
      await add('', null, null);
      await add('', null, null);
      let delayedSuggestion;
      await page.route('**/owner/api/training/exercises?*', route => {
        if (new URL(route.request().url()).searchParams.get('q') === 'press') delayedSuggestion = route;
        else return route.continue();
      });
      await cards().last().locator('.exercise-name').fill('press');
      for (let i = 0; i < 100 && !delayedSuggestion; i++) await new Promise(resolve => setTimeout(resolve, 20));
      assert(delayedSuggestion, 'suggestion was not held');
      await cards().first().locator('.exercise-name').evaluate(input => {
        input.value = ' bEnCh aLiAs '; input.dispatchEvent(new Event('input', {bubbles: true}));
      });
      await delayedSuggestion.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: [
        {id: 1, name: 'Synthetic press', body_part: '胸', aliases: ['Bench alias']}
      ]})});
      await cards().last().locator('.exercise-hint-status').filter({hasText: '已在本次训练'}).waitFor();
      assert(await cards().last().getByRole('option').count() === 0, 'late suggestion reintroduced selected exercise');
      await page.unrouteAll({behavior: 'wait'});
      results.push(label + ': canonical/alias filtering, current selection, draft compatibility and reappearance after deletion');

      stage = label + ': local validation';
      for (const failure of ['name', 'weight', 'reps', 'drop', 'empty']) {
        await fresh();
        if (failure !== 'empty') {
          card = await add(failure === 'name' ? '' : 'Synthetic press');
          if (failure === 'weight' || failure === 'reps') await rows(card).first().locator(`[data-field="${failure}"]`).fill('');
          if (failure === 'drop') await menu(rows(card).first(), '设为递减组');
        }
        const before = writes.length;
        await submitStatus('error');
        await noSuccess();
        assert(writes.length === before, 'invalid data posted');
        if (failure === 'weight') await page.screenshot({path: output + '/save-error-footer-' + label + '.png'});
        await page.locator('#saveFeedback [data-locate-error]').click();
        assert(await page.locator('[aria-invalid="true"]').evaluate(node => node === document.activeElement), 'validation focus did not locate field');
      }
      await fresh();
      await add('Synthetic press', '', '8');
      await page.locator('#endBtn').click();
      await submitStatus('error', true);
      assert(await page.locator('#decisionSaveFeedback [data-save-message]').innerText() === await page.locator('#saveFeedback [data-save-message]').innerText(), 'modal and footer disagree');
      assert(await page.locator('#trainingDecisionOverlay').evaluate(node => node.classList.contains('open')), 'failure dismissed decision modal');
      await page.screenshot({path: output + '/save-error-' + label + '.png'});
      await page.locator('#decisionSaveFeedback [data-locate-error]').click();
      assert(!await page.locator('#trainingDecisionOverlay').evaluate(node => node.classList.contains('open')), 'locate did not close modal');
      await rows(cards().first()).first().locator('[data-field="weight"]').fill('40');
      assert(await page.locator('#saveFeedback').getAttribute('data-state') === 'idle', 'edit retained stale failure');
      results.push(label + ': no-write local validation, separate draft semantics, matching modal/footer and error focus');

      stage = label + ': server and network failures';
      for (const failure of [400, 401, 403, 500, 'network', 'html', 'ack']) {
        await fresh();
        await add();
        await page.route(base + '/training', route => {
          if (route.request().method() !== 'POST') return route.continue();
          if (failure === 'network') return route.abort('failed');
          if (failure === 'html') return route.fulfill({status: 200, contentType: 'text/html', body: '<p>Unexpected</p>'});
          if (failure === 'ack') return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({item: {id: 123, request_id: 'wrong'}})});
          return route.fulfill({status: failure, contentType: 'application/json', body: JSON.stringify({error: failure === 403 ? 'csrf_invalid' : 'synthetic_rejection', detail: 'Synthetic rejection'})});
        });
        const before = writes.length;
        await submitStatus([400, 401, 403].includes(failure) ? 'error' : 'uncertain');
        assert(writes.length === before + 1, 'write was retried automatically');
        await noSuccess();
        assert(await cards().count() === 1, 'failure cleared exercise');
      }
      results.push(label + ': 400/401/403/500, network, HTML and wrong acknowledgement never report success or discard input');

      stage = label + ': lost acknowledgement idempotency';
      await fresh();
      await add();
      const beforeCount = (await (await context.request.get(base + '/owner/api/training/sessions?limit=100')).json()).total;
      let lost = true;
      let committedId;
      await page.route(base + '/training', async route => {
        if (route.request().method() !== 'POST' || !lost) return route.continue();
        lost = false;
        committedId = (await (await route.fetch()).json()).item.id;
        await route.abort('failed');
      });
      await submitStatus('uncertain');
      const firstId = new URLSearchParams(writes.at(-1)).get('request_id');
      await submitStatus('saved');
      assert(new URLSearchParams(writes.at(-1)).get('request_id') === firstId, 'retry changed request ID');
      const saved = await (await context.request.get(base + '/owner/api/training/sessions?limit=100')).json();
      assert(saved.total === beforeCount + 1, 'lost response retry changed record count unexpectedly');
      assert((await (await context.request.get(base + '/owner/api/training/sessions/' + committedId)).json()).request_id === firstId, 'saved request ID differs');
      results.push(label + ': committed response loss retries the original ID without duplicate records');

      stage = label + ': uncertain save preserves later edits';
      await fresh();
      await add();
      let originalId;
      let loseFirst = true;
      await page.route(base + '/training', async route => {
        if (route.request().method() !== 'POST' || !loseFirst) return route.continue();
        loseFirst = false;
        originalId = (await (await route.fetch()).json()).item.id;
        await route.abort('failed');
      });
      await submitStatus('uncertain');
      await rows(cards().first()).first().locator('[data-field="weight"]').fill('55');
      await rows(cards().first()).first().locator('[data-field="reps"]').fill('');
      await submitStatus('error');
      await rows(cards().first()).first().locator('[data-field="reps"]').fill('9');
      await submitStatus('idle');
      assert((await page.locator('#saveFeedback').innerText()).includes('当前修改尚未提交'), 'confirmation claimed edited values were saved');
      assert(await rows(cards().first()).first().locator('[data-field="weight"]').inputValue() === '55', 'confirmation discarded edits');
      assert((await (await context.request.get(base + '/owner/api/training/sessions/' + originalId)).json()).records[0].weight_kg === 40, 'confirmation unexpectedly changed original write');
      await submitStatus('saved');
      assert((await (await context.request.get(base + '/owner/api/training/sessions/' + originalId)).json()).records[0].weight_kg === 55, 'explicit save did not update retained edits');
      results.push(label + ': confirming an uncertain earlier write retains newer edits for an explicit update');

      stage = label + ': manual duplicates and refresh warning';
      await fresh();
      await add('Synthetic press');
      await add('Bench alias', '45', '6');
      let committed = false;
      await page.route(base + '/training', async route => {
        if (route.request().method() !== 'POST') return route.continue();
        const response = await route.fetch();
        committed = true;
        await route.fulfill({response});
      });
      await page.route('**/owner/api/training/analytics', route => committed
        ? route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({error: 'synthetic_refresh_failure'})}) : route.continue());
      await submitStatus('saved_with_warning');
      assert((await page.locator('#trainingNoticeText').innerText()).includes('已保存到 HealthOS'), 'refresh error reclassified committed save');
      results.push(label + ': manually repeated exercise remains allowed; refresh failure keeps acknowledged success');

      stage = label + ': draft cleanup warning';
      await fresh();
      await add();
      await page.evaluate(() => {
        const remove = Storage.prototype.removeItem;
        Storage.prototype.removeItem = function(key) {
          if (key.startsWith('healthos.training.draft.v2.')) throw new Error('Synthetic cleanup failure');
          return remove.call(this, key);
        };
      });
      await submitStatus('saved_with_warning');
      assert((await page.locator('#trainingNoticeText').innerText()).includes('旧草稿未能清理'), 'cleanup warning missing');
      results.push(label + ': draft cleanup failure preserves confirmed success');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'horizontal overflow');
    }
    assert(runtimeErrors.length === 0, 'browser runtime errors: ' + runtimeErrors.join('; '));
    return {passed: results.length, scenarios: results, browserVersion: context.browser().version(), productionConnections: false};
  } catch (error) {
    throw new Error(stage + ': ' + error.message);
  }
}
