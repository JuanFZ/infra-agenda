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

  // 5. Test Escalation Expandable Rows & Clean Table Headers
  const tblMatch = content.match(/<table id="tblEscalations">([\s\S]*?)<\/table>/);
  assert(tblMatch, '#tblEscalations table not found in index.html');
  const theadMatch = tblMatch[1].match(/<thead>([\s\S]*?)<\/thead>/);
  assert(theadMatch, '#tblEscalations thead not found');
  const thList = theadMatch[1].match(/<th\b[^>]*>[\s\S]*?<\/th>/g) || [];
  assert.strictEqual(thList.length, 7, `Expected 7 <th> columns in #tblEscalations, found ${thList.length}`);
  assert(!theadMatch[1].includes('Observaciones'), 'Observaciones column should not be in #tblEscalations thead');
  assert(!theadMatch[1].includes('Recomendaciones'), 'Recomendaciones column should not be in #tblEscalations thead');
  assert(tblMatch[1].includes('colspan="7"'), 'Initial #tblEscalationsBody state should use colspan="7"');

  // Verify no UTF-8 mojibake or emojis remain in index.html
  assert(!/[\u00c2\u00c3]/.test(content), 'UTF-8 mojibake (Ã / Â) found in frontend/index.html');
  assert(!content.includes('â€”'), 'Mojibake em-dash found in frontend/index.html');
  assert(!/[\u{1F300}-\u{1FAFF}]/u.test(content), 'Emoji characters found in frontend/index.html');

  // Extract escalation table functions and test DOM rendering + accordion toggle
  const escHtmlFnMatch = content.match(/function escapeHtml\(str\)[\s\S]*?\n    \}/);
  const catBadgeFnMatch = content.match(/function getCategoryBadge\(cat\)[\s\S]*?\n    \}/);
  const toggleFnMatch = content.match(/function toggleEscalationRow\(id, event\)[\s\S]*?\n    \}/);
  const renderFnMatch = content.match(/function renderEscalationsTable\(list\)[\s\S]*?\n    \}/);
  assert(escHtmlFnMatch && catBadgeFnMatch && toggleFnMatch && renderFnMatch, 'Escalation functions missing in index.html');

  const elementsById = {};
  const mockTbody = { innerHTML: '' };
  elementsById['tblEscalationsBody'] = mockTbody;

  const escRunner = new Function(
    'document',
    `
    const EXPANDED_ESCALATION_IDS = new Set();
    ${escHtmlFnMatch[0]}
    ${catBadgeFnMatch[0]}
    ${toggleFnMatch[0]}
    ${renderFnMatch[0]}
    return { renderEscalationsTable, toggleEscalationRow, EXPANDED_ESCALATION_IDS };
    `
  );

  const mockDoc = {
    getElementById: (id) => elementsById[id] || null
  };
  const escModule = escRunner(mockDoc);

  // Empty state check
  escModule.renderEscalationsTable([]);
  assert(mockTbody.innerHTML.includes('colspan="7"'), 'Empty state should use colspan="7"');
  assert(mockTbody.innerHTML.includes('No se encontraron escalamientos registrados.'), 'Empty state message missing');

  // Populated state check with edge cases (empty notes/recs, single quotes, multiline recommendations)
  const sampleEscalations = [
    {
      id: 101,
      title: "SIMF-ES-5260 Caso 'A'",
      category: 'PostgreSQL',
      ibp: '0134 - BANESCO',
      doc_url: 'https://docs.google.com/document/d/101',
      uploaded_by: 'Adonis Lozada',
      uploaded_by_role: 'Coordinador',
      created_at_caracas: '10/11/2026 09:00',
      notes: 'Observación detallada línea 1\nLínea 2',
      recommendations: 'Recomendación 1: Ajustar work_mem\nRecomendación 2: Vacuum'
    },
    {
      id: 102,
      title: 'SIMF-ES-5261 Sin Notas',
      category: 'CPU',
      ibp: '',
      doc_url: '',
      uploaded_by: '',
      uploaded_by_role: '',
      created_at_caracas: '',
      notes: '   ',
      recommendations: null
    }
  ];

  escModule.renderEscalationsTable(sampleEscalations);
  assert(mockTbody.innerHTML.includes('id="esc-row-101"'), 'Main row for 101 not rendered');
  assert(mockTbody.innerHTML.includes('id="esc-detail-101"'), 'Detail row for 101 not rendered');
  assert(mockTbody.innerHTML.includes('style="display:none"'), 'Detail row should be hidden by default');
  assert(mockTbody.innerHTML.includes('Observación detallada línea 1'), 'Notes should be inside detail row');
  assert(mockTbody.innerHTML.includes('Recomendación 1: Ajustar work_mem'), 'Recommendations should be inside detail row');
  assert(mockTbody.innerHTML.includes('Sin observaciones registradas.'), 'Fallback for empty notes missing');
  assert(mockTbody.innerHTML.includes('Sin recomendaciones registradas.'), 'Fallback for empty recommendations missing');

  // Set up mock DOM nodes for row 101 to test toggleEscalationRow
  function makeMockRow(initialDisplay) {
    const classes = new Set();
    const attrs = {};
    return {
      style: { display: initialDisplay },
      classList: {
        add: (c) => classes.add(c),
        remove: (c) => classes.delete(c),
        has: (c) => classes.has(c)
      },
      setAttribute: (k, v) => { attrs[k] = v; },
      getAttribute: (k) => attrs[k]
    };
  }
  elementsById['esc-row-101'] = makeMockRow('table-row');
  elementsById['esc-detail-101'] = makeMockRow('none');

  // Clicking an interactive button/link inside the row must NOT toggle
  escModule.toggleEscalationRow(101, {
    target: { closest: (sel) => sel.includes('button') ? {} : null }
  });
  assert.strictEqual(escModule.EXPANDED_ESCALATION_IDS.has(101), false, 'Interactive click should not expand row');
  assert.strictEqual(elementsById['esc-detail-101'].style.display, 'none');

  // Clicking non-interactive area of row expands detail downward
  escModule.toggleEscalationRow(101, {
    target: { closest: () => null }
  });
  assert.strictEqual(escModule.EXPANDED_ESCALATION_IDS.has(101), true, 'Row click should expand row');
  assert.strictEqual(elementsById['esc-detail-101'].style.display, 'table-row');
  assert.strictEqual(elementsById['esc-row-101'].classList.has('expanded'), true);
  assert.strictEqual(elementsById['esc-row-101'].getAttribute('aria-expanded'), 'true');

  // Re-rendering while expanded preserves expanded state
  escModule.renderEscalationsTable(sampleEscalations);
  assert(mockTbody.innerHTML.includes('id="esc-detail-101" class="escalation-detail-row expanded" style="display:table-row"'));

  // Clicking row again collapses detail
  escModule.toggleEscalationRow(101, {
    target: { closest: () => null }
  });
  assert.strictEqual(escModule.EXPANDED_ESCALATION_IDS.has(101), false, 'Second row click should collapse row');
  assert.strictEqual(elementsById['esc-detail-101'].style.display, 'none');
  assert.strictEqual(elementsById['esc-row-101'].classList.has('expanded'), false);

  // Keyboard navigation: non-Enter/Space keys do not toggle
  escModule.toggleEscalationRow(101, {
    type: 'keydown',
    key: 'Tab',
    target: { closest: () => null }
  });
  assert.strictEqual(escModule.EXPANDED_ESCALATION_IDS.has(101), false, 'Tab key should not expand row');

  // Keyboard navigation: Enter key expands row, Space key collapses row
  let prevented = false;
  escModule.toggleEscalationRow(101, {
    type: 'keydown',
    key: 'Enter',
    preventDefault: () => { prevented = true; },
    target: { closest: () => null }
  });
  assert.strictEqual(prevented, true, 'Enter key should preventDefault');
  assert.strictEqual(escModule.EXPANDED_ESCALATION_IDS.has(101), true, 'Enter key should expand row');
  assert.strictEqual(elementsById['esc-detail-101'].style.display, 'table-row');

  escModule.toggleEscalationRow(101, {
    type: 'keydown',
    key: ' ',
    preventDefault: () => {},
    target: { closest: () => null }
  });
  assert.strictEqual(escModule.EXPANDED_ESCALATION_IDS.has(101), false, 'Space key should collapse row');
  assert.strictEqual(elementsById['esc-detail-101'].style.display, 'none');

  console.log('✓ Test 4: Escalation table clean 7-column layout, expandable downward detail row, and UTF-8 integrity verified');
  console.log('ALL FRONTEND TESTS PASSED SUCCESSFULLY!');
})();
