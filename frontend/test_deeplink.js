const fs = require('fs');
const assert = require('assert');
const { URLSearchParams } = require('url');

console.log('--- RUNNING FRONTEND DEEP LINKING TESTS ---');

// 1. Read index.html
const htmlPath = 'frontend/index.html';
const content = fs.readFileSync(htmlPath, 'utf8');

// 2. Syntax Check
const scriptMatches = content.match(/<script[\s\S]*?>([\s\S]*?)<\/script>/gi);
assert(scriptMatches && scriptMatches.length > 0, 'No script tags found in index.html');
const code = scriptMatches[0].replace(/<script[\s\S]*?>/i, '').replace(/<\/script>/i, '');
try {
  new Function(code);
  console.log('✓ Test 1: Full inline script syntax valid');
} catch (e) {
  console.error('✗ Test 1 failed: Syntax error in index.html:', e.message);
  process.exit(1);
}

// 3. Extract getRouteParams function
const fnMatch = content.match(/function getRouteParams\(\)[\s\S]*?return params;\s*\}/);
assert(fnMatch, 'getRouteParams function not found in index.html');
const getRouteParamsCode = fnMatch[0];

function runGetRouteParams(search, hash) {
  const window = {
    location: {
      search: search || '',
      hash: hash || ''
    }
  };
  const evalFunc = new Function('window', 'URLSearchParams', getRouteParamsCode + '; return getRouteParams();');
  return evalFunc(window, URLSearchParams);
}

// Test URL formats
const cases = [
  { search: '?view=kanban&item_id=42', hash: '', expectedView: 'kanban', expectedItemId: '42' },
  { search: '?view=kaban&item_id=43', hash: '', expectedView: 'kanban', expectedItemId: '43' }, // Handles typo from task description
  { search: '?item_id=88', hash: '', expectedView: undefined, expectedItemId: '88' },
  { search: '?view=kanban&item=99', hash: '', expectedView: 'kanban', expectedItem: '99' },
  { search: '?task_id=77', hash: '', expectedView: undefined, expectedTaskId: '77' },
  { search: '?external_id=GLPI-500', hash: '', expectedView: undefined, expectedExtId: 'GLPI-500' },
  { search: '', hash: '#view=kanban&item_id=77', expectedView: 'kanban', expectedItemId: '77' },
  { search: '', hash: '#view=kaban&item_id=78', expectedView: 'kanban', expectedItemId: '78' },
  { search: '', hash: '#item=55', expectedView: undefined, expectedItem: '55' },
  { search: '', hash: '#/kanban?item_id=12', expectedView: 'kanban', expectedItemId: '12' },
  { search: '', hash: '#/item/42', expectedView: undefined, expectedItemId: '42' },
  { search: '', hash: '#item-55', expectedView: undefined, expectedItemId: '55' },
  { search: '', hash: '#task-90', expectedView: undefined, expectedItemId: '90' },
  { search: '', hash: '#kanban', expectedView: 'kanban', expectedItemId: undefined },
  { search: '', hash: '#kaban', expectedView: 'kanban', expectedItemId: undefined },
  { search: '', hash: '#123', expectedView: undefined, expectedItemId: '123' },
  { search: '?VIEW=KANBAN&ITEM_ID=200', hash: '', expectedView: 'kanban', expectedItemId: '200' },
  { search: '', hash: '', expectedView: undefined, expectedItemId: undefined }
];

cases.forEach((tc, idx) => {
  const res = runGetRouteParams(tc.search, tc.hash);
  if (tc.expectedView !== undefined) {
    assert.strictEqual(res.view, tc.expectedView, `Case ${idx} view mismatch`);
  }
  if (tc.expectedItemId !== undefined) {
    assert.strictEqual(res.item_id, tc.expectedItemId, `Case ${idx} item_id mismatch`);
  }
  if (tc.expectedItem !== undefined) {
    assert.strictEqual(res.item, tc.expectedItem, `Case ${idx} item mismatch`);
  }
  if (tc.expectedTaskId !== undefined) {
    assert.strictEqual(res.task_id, tc.expectedTaskId, `Case ${idx} task_id mismatch`);
  }
  if (tc.expectedExtId !== undefined) {
    assert.strictEqual(res.external_id, tc.expectedExtId, `Case ${idx} external_id mismatch`);
  }
});
console.log(`✓ Test 2: URL parsing handles all ${cases.length} query and hash formats`);

// 4. Test handleDeepLink simulated behavior
const deepLinkBlockMatch = content.match(/let DEEP_LINK_HANDLED = false;[\s\S]*?window\.addEventListener\('popstate'[\s\S]*?\n\s*\}\);/);
assert(deepLinkBlockMatch, 'handleDeepLink definition with hashchange and popstate not found');

function simulateDeepLink(opts) {
  let switchedTo = null;
  let openedModalId = null;
  let renderedAllCalls = 0;
  const toastMessages = [];

  const mockWindow = {
    location: {
      search: opts.search || '',
      hash: opts.hash || ''
    },
    addEventListener: () => {}
  };

  const fakeInputs = {
    fSrc: { value: 'ClickUp' },
    fPrio: { value: 'Alta' },
    fResp: { value: 'juan' },
    fSearch: { value: 'error' }
  };

  const scope = {
    window: mockWindow,
    URLSearchParams,
    CURRENT_USER: opts.currentUser || null,
    ALL_ITEMS: opts.items ? [...opts.items] : [],
    HIGHLIGHT_ITEM_ID: null,
    API_URL: 'http://localhost:8000/api',
    ACTIVE_KPI_FILTER: 'curso',
    getAuthHeaders: () => ({ 'X-User-Role': 'coordinador' }),
    switchView: (v) => { switchedTo = v; },
    openModal: async (id) => { openedModalId = id; },
    renderAll: () => { renderedAllCalls++; },
    showToast: (t, m, type) => { toastMessages.push({ t, m, type }); },
    fetch: opts.fetchImpl || (async () => ({ ok: false, status: 404 })),
    setTimeout: (fn, ms) => fn(),
    document: {
      getElementById: (id) => {
        if (fakeInputs[id]) return fakeInputs[id];
        return { scrollIntoView: () => {} };
      },
      querySelectorAll: () => []
    },
    console: { warn: () => {}, log: () => {} }
  };

  const runner = new Function(
    'window', 'URLSearchParams', 'CURRENT_USER', 'ALL_ITEMS', 'HIGHLIGHT_ITEM_ID',
    'API_URL', 'ACTIVE_KPI_FILTER', 'getAuthHeaders', 'switchView', 'openModal', 'renderAll',
    'showToast', 'fetch', 'setTimeout', 'document', 'console',
    getRouteParamsCode + '\n' + deepLinkBlockMatch[0] + '\n return { handleDeepLink, getHandled: () => DEEP_LINK_HANDLED };'
  );

  const instance = runner(
    scope.window, scope.URLSearchParams, scope.CURRENT_USER, scope.ALL_ITEMS, scope.HIGHLIGHT_ITEM_ID,
    scope.API_URL, scope.ACTIVE_KPI_FILTER, scope.getAuthHeaders, scope.switchView, scope.openModal,
    scope.renderAll, scope.showToast, scope.fetch, scope.setTimeout, scope.document, scope.console
  );

  return {
    instance,
    getInputs: () => fakeInputs,
    getToasts: () => toastMessages,
    getAllItems: () => scope.ALL_ITEMS,
    getResults: () => ({ switchedTo, openedModalId, renderedAllCalls }),
    resetResults: () => { switchedTo = null; openedModalId = null; renderedAllCalls = 0; }
  };
}

(async () => {
  // Subcase 4A: Not logged in -> Should not switch view or open modal
  const simA = simulateDeepLink({
    search: '?view=kanban&item_id=10',
    currentUser: null,
    items: [{ id: 10, title: 'Item 10' }]
  });
  await simA.instance.handleDeepLink();
  assert.strictEqual(simA.getResults().switchedTo, null);
  assert.strictEqual(simA.getResults().openedModalId, null);
  assert.strictEqual(simA.instance.getHandled(), false);

  // Subcase 4B: Logged in & items loaded -> Should switch to kanban, clear filters, and open modal
  const simB = simulateDeepLink({
    search: '?view=kanban&item_id=10',
    currentUser: { username: 'a.lozada', role: 'coordinador' },
    items: [{ id: 10, title: 'Item 10' }]
  });
  await simB.instance.handleDeepLink();
  assert.strictEqual(simB.getResults().switchedTo, 'kanban');
  assert.strictEqual(simB.getResults().openedModalId, 10);
  assert.strictEqual(simB.instance.getHandled(), true);
  // Verify UI filters were cleared so target item is visible
  assert.strictEqual(simB.getInputs().fSrc.value, '');
  assert.strictEqual(simB.getInputs().fPrio.value, '');
  assert.strictEqual(simB.getInputs().fResp.value, '');
  assert.strictEqual(simB.getInputs().fSearch.value, '');

  // Subcase 4C: Polling execution while already handled -> Should debounce and not re-trigger
  simB.resetResults();
  await simB.instance.handleDeepLink();
  assert.strictEqual(simB.getResults().openedModalId, null);
  assert.strictEqual(simB.getResults().switchedTo, null);

  // Subcase 4D: Hash deep link with view=kaban (typo tolerance)
  const simD = simulateDeepLink({
    hash: '#view=kaban&item_id=25',
    currentUser: { username: 'a.lozada', role: 'coordinador' },
    items: [{ id: 25, title: 'Item 25' }]
  });
  await simD.instance.handleDeepLink();
  assert.strictEqual(simD.getResults().switchedTo, 'kanban');
  assert.strictEqual(simD.getResults().openedModalId, 25);

  // Subcase 4E: Fallback fetch for item not in memory (e.g. closed older item)
  const simE = simulateDeepLink({
    search: '?view=kanban&item_id=300',
    currentUser: { username: 'a.lozada', role: 'coordinador' },
    items: [{ id: 1, title: 'Existing' }],
    fetchImpl: async (url) => {
      if (url.includes('/items/300')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ id: 300, title: 'Closed Past Task', status: 'cerrado' })
        };
      }
      return { ok: false, status: 404 };
    }
  });
  await simE.instance.handleDeepLink();
  assert.strictEqual(simE.getResults().switchedTo, 'kanban');
  assert.strictEqual(simE.getResults().openedModalId, 300);
  assert.strictEqual(simE.getAllItems().some(i => i.id === 300), true);

  // Subcase 4F: Non-existent item (404 from API) -> Displays user notification toast
  const simF = simulateDeepLink({
    search: '?view=kanban&item_id=99999',
    currentUser: { username: 'a.lozada', role: 'coordinador' },
    items: [],
    fetchImpl: async () => ({ ok: false, status: 404 })
  });
  await simF.instance.handleDeepLink();
  const toasts = simF.getToasts();
  assert.strictEqual(toasts.length, 1);
  assert.strictEqual(toasts[0].t, 'Actividad no encontrada');

  console.log('✓ Test 3: Deep link lifecycle (auth check, item lookup, fallback fetch, modal opening, debounce) verified');
  console.log('ALL FRONTEND TESTS PASSED SUCCESSFULLY!');
})();
