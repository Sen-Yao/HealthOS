async page => {
  const base = '__BASE_URL__';
  const source = '__SOURCE_URL__';
  const context = page.context();
  const results = [];
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const frame = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  let loginPosts = 0, businessPosts = 0;
  context.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && path === '/login') loginPosts += 1;
    if (request.method() === 'POST' && ['/training', '/training/aliases', '/training/delete'].includes(path)) businessPosts += 1;
  });
  const session = () => page.evaluate(async () => {
    const response = await fetch('/owner/api/session');
    return {status: response.status, body: await response.json()};
  });
  async function freshLogin() {
    await context.clearCookies();
    await page.goto(base + '/login');
    await page.getByRole('textbox', {name: '管理员密码'}).fill('synthetic-probe-password');
    await page.getByRole('button', {name: '登录 HealthOS'}).click();
    await page.waitForURL(base + '/');
  }
  async function gate() {
    await context.request.get(base + '/__auth_probe/reset');
    await page.evaluate(() => { window.pendingAuthProbe = fetch('/owner/api/session?hold=1').then(r => r.json()); });
    for (let i = 0; i < 100; i++) {
      if ((await (await context.request.get(base + '/__auth_probe/status')).json()).ready) return;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    throw new Error('The synthetic response did not reach its gate');
  }
  const loginFetch = () => page.evaluate(async () => {
    const response = await fetch('/login', {method: 'POST', headers: {Accept: 'application/json'},
      body: new URLSearchParams({username: 'Senyao', password: 'synthetic-probe-password'})});
    if (response.status !== 200) throw new Error('Synthetic login failed');
    return response.json();
  });

  await freshLogin();
  const cookie = (await context.cookies()).find(item => item.name === 'healthos_session_v2');
  assert(cookie && cookie.httpOnly && cookie.secure && cookie.sameSite === 'Lax', 'v2 cookie attributes');
  assert(cookie.expires - Date.now() / 1000 > 30 * 86400 - 60, 'v2 browser lifetime');
  const ordinary = await context.request.get(base + '/owner/api/session');
  assert(!ordinary.headers()['set-cookie'], 'ordinary v2 response rewrites cookies');
  await page.goto(source);
  await page.getByRole('link', {name: 'Open HealthOS'}).click();
  await page.waitForURL(base + '/training');
  assert((await session()).status === 200, 'cross-site Lax navigation');
  results.push('v2 Lax cross-site navigation and fixed browser expiry');

  await page.goto(source);
  const crossSiteWrite = page.waitForResponse(response => response.url() === base + '/training' && response.request().method() === 'POST');
  await page.getByRole('button', {name: 'Try cross-site write'}).click();
  const rejectedWrite = await crossSiteWrite;
  assert([303, 401, 403].includes(rejectedWrite.status()), 'cross-site write was accepted');
  assert(!(await rejectedWrite.request().allHeaders()).cookie, 'Lax cookie accompanied cross-site POST');
  await page.waitForURL(base + '/');
  results.push('cross-site POST is rejected and does not receive a Lax cookie');

  await context.clearCookies();
  await page.goto(base + '/__auth_probe/legacy');
  const postsBeforeRecovery = loginPosts;
  await page.goto(source);
  await page.getByRole('link', {name: 'Open HealthOS'}).click();
  await page.waitForURL(base + '/');
  assert(loginPosts === postsBeforeRecovery, 'legacy recovery submitted a password');
  const legacyCookies = await context.cookies();
  assert(legacyCookies.some(item => item.name === 'healthos_session' && item.sameSite === 'Lax'), 'legacy cookie not renewed as Lax');
  assert(!legacyCookies.some(item => item.name === 'healthos_session_v2'), 'ordinary legacy response promoted cookie');
  results.push('legacy Strict entry recovers automatically without password or forced migration');

  for (const kind of ['v2', 'legacy']) {
    if (kind === 'v2') await freshLogin();
    else {
      await context.clearCookies();
      await page.goto(base + '/__auth_probe/legacy');
      await page.goto(base + '/');
    }
    await gate();
    let newSession, newCookie;
    try {
      newSession = await loginFetch();
      newCookie = (await context.cookies()).find(item => item.name === 'healthos_session_v2');
    } finally {
      await context.request.get(base + '/__auth_probe/release');
    }
    await page.evaluate(() => window.pendingAuthProbe);
    assert((await context.cookies()).find(item => item.name === 'healthos_session_v2').value === newCookie.value,
      kind + ' late response overwrote v2');
    const next = await session();
    assert(next.status === 200 && next.body.csrf === newSession.csrf, kind + ' race lost authentication');
    results.push(kind + ' delayed response cannot replace a new login');
  }

  await freshLogin();
  const beforeLogout = await session();
  await gate();
  try {
    const status = await page.evaluate(async csrf => (await fetch('/logout', {
      method: 'POST', redirect: 'manual', body: new URLSearchParams({csrf})
    })).type, beforeLogout.body.csrf);
    assert(status === 'opaqueredirect', 'logout did not redirect');
  } finally {
    await context.request.get(base + '/__auth_probe/release');
  }
  await page.evaluate(() => window.pendingAuthProbe);
  assert((await session()).status === 401, 'late response undid logout');
  results.push('logout survives a delayed authenticated response');

  await context.clearCookies();
  await page.goto(base + '/login');
  await page.getByRole('textbox', {name: '管理员密码'}).fill('synthetic-probe-password');
  const beforeDouble = loginPosts;
  await page.evaluate(() => { const form = document.querySelector('form[action="/login"]'); form.requestSubmit(); form.requestSubmit(); });
  await page.waitForURL(base + '/');
  assert(loginPosts === beforeDouble + 1, 'normal form double submit');
  results.push('normal login form submits once');

  for (const mode of ['unauthenticated', 'malformed', 'timeout']) {
    await context.clearCookies();
    let failed;
    if (mode === 'malformed') {
      await page.route('**/owner/api/session', route => route.fulfill({status: 200, contentType: 'application/json', body: '{invalid'}));
    } else if (mode === 'timeout') {
      await page.route('**/owner/api/session', async route => {
        await new Promise(resolve => setTimeout(resolve, 4300));
        await route.fulfill({status: 401, contentType: 'application/json', body: '{}'}).catch(() => {});
      });
      failed = page.waitForEvent('requestfailed', {predicate: request => request.url().endsWith('/owner/api/session'), timeout: 6500});
    }
    await page.goto(base + '/login');
    if (failed) await failed;
    else await frame();
    assert(new URL(page.url()).pathname === '/login', mode + ' redirected unexpectedly');
    assert(await page.getByRole('button', {name: '登录 HealthOS'}).isEnabled(), mode + ' blocked manual login');
    await page.unrouteAll({behavior: 'wait'});
    results.push('login page stays usable on ' + mode);
  }

  await page.goto(base + '/login');
  await page.getByRole('textbox', {name: '管理员密码'}).fill('incorrect-synthetic-password');
  await page.getByRole('button', {name: '登录 HealthOS'}).click();
  await page.locator('form [role="alert"]').waitFor();
  assert(await page.getByRole('button', {name: '登录 HealthOS'}).isEnabled(), 'wrong password disabled retry');
  await page.getByRole('textbox', {name: '管理员密码'}).fill('synthetic-probe-password');
  await page.getByRole('button', {name: '登录 HealthOS'}).click();
  await page.waitForURL(base + '/');
  results.push('wrong password keeps the form usable and a corrected password succeeds');

  // Exercise actual training event handlers while an older session check is pending.
  for (const oldStatus of [200, 401]) {
    await freshLogin();
    await page.goto(base + '/training');
    await page.waitForFunction(() => document.querySelector('#aliasExercise').options.length > 0);
    await page.locator('#entryStartBtn').click();
    const draftText = 'Synthetic draft retained ' + oldStatus;
    await page.locator('#feeling').fill(draftText);
    await page.waitForFunction(text => Object.keys(localStorage).some(key => key.startsWith('healthos.training.draft.v2.') &&
      (localStorage.getItem(key) || '').includes(text)), draftText);
    const oldSession = await session();
    let held;
    await page.route('**/owner/api/session', route => { if (!held) held = route; else return route.continue(); });
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow')));
    for (let i = 0; i < 100 && !held; i++) await new Promise(resolve => setTimeout(resolve, 25));
    assert(held, 'training checkSession was not held');
    await page.route('**/owner/api/training/analytics', route => route.fulfill({status: 401,
      contentType: 'application/json', body: JSON.stringify({error: 'owner_auth_required'})}));
    await page.locator('[data-view="stats"]').click();
    await page.waitForFunction(() => document.querySelector('#authOverlay').classList.contains('open'));
    await page.locator('#reauth-password').fill('synthetic-probe-password');
    const beforePopup = loginPosts;
    const beforeBusiness = businessPosts;
    if (oldStatus === 200) {
      await page.evaluate(() => { const form = document.querySelector('#authForm'); form.requestSubmit(); form.requestSubmit(); });
    } else {
      // A second real tab signs in while the first tab has an older check in flight.
      const second = await context.newPage();
      await second.goto(base + '/__auth_probe/source');
      const loginHtml = await (await context.request.get(base + '/login', {headers: {Cookie: ''}})).text();
      await second.route('**/login', route => route.request().method() === 'GET'
        ? route.fulfill({status: 200, contentType: 'text/html', body: loginHtml}) : route.continue());
      // Delay the automatic recovery check so the stale login form can be submitted.
      await second.route('**/owner/api/session', route => route.fulfill({status: 401,
        contentType: 'application/json', body: JSON.stringify({error: 'owner_auth_required'})}));
      await second.goto(base + '/login');
      await second.getByRole('textbox', {name: '管理员密码'}).fill('synthetic-probe-password');
      await second.getByRole('button', {name: '登录 HealthOS'}).click();
      await second.waitForURL(base + '/');
      await second.close();
    }
    await page.waitForFunction(() => !document.querySelector('#authOverlay').classList.contains('open'));
    assert(loginPosts === beforePopup + 1, 'reauthentication double submit');
    const staleResponse = page.waitForResponse(response => response.url().endsWith('/owner/api/session'));
    await held.fulfill({status: oldStatus, contentType: 'application/json',
      body: JSON.stringify(oldStatus === 200 ? oldSession.body : {error: 'owner_auth_required'})});
    await (await staleResponse).finished();
    await page.unrouteAll({behavior: 'wait'});
    await frame();
    assert(!await page.locator('#authOverlay').evaluate(node => node.classList.contains('open')), 'stale response reopened login');
    assert(await page.locator('#feeling').inputValue() === draftText, 'reauthentication lost draft');
    assert(businessPosts === beforeBusiness, 'reauthentication automatically retried a write');
    await page.locator('[data-view="aliases"]').click();
    await page.locator('#aliasExercise').selectOption('1');
    await page.locator('#aliasForm input[name="alias_name"]').fill('Synthetic alias ' + oldStatus);
    const saved = page.waitForResponse(response => response.url().endsWith('/training/aliases') && response.request().method() === 'POST');
    await page.locator('#aliasForm').getByRole('button', {name: '添加别名'}).click();
    assert((await saved).status() === 200, 'stale response replaced new CSRF');
    results.push((oldStatus === 401 ? 'cross-tab sign-in: ' : '') + 'training ignores late ' + oldStatus +
      ', retains draft, preserves new CSRF, and does not retry writes');
    await page.evaluate(() => localStorage.clear());
  }
  return {passed: results.length, scenarios: results, browserVersion: context.browser().version(), productionConnections: false};
}
