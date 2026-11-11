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

  // =========================================================================
  // 5. Verify Kanban Origin Selector, No Duplicate Subtitle, and ClickUp 'En Espera' Hiding
  // =========================================================================
  const vKanbanMatch = content.match(/<div class="view" id="v-kanban">[\s\S]*?<div class="kanban-grid" id="kanbanGrid"><\/div>\s*<\/div>/);
  assert(vKanbanMatch, '#v-kanban view block not found');
  const vKanbanHtml = vKanbanMatch[0];

  // Ensure subtitle is NOT duplicated inside #v-kanban
  const subMatches = vKanbanHtml.match(/Arrastra actividades entre columnas para sincronizar su\s*estado/g) || [];
  assert.strictEqual(subMatches.length, 1, `Expected exactly 1 subtitle in #v-kanban, found ${subMatches.length}`);
  assert(vKanbanHtml.includes('id="kanbanOriginSelector"'), '#kanbanOriginSelector missing in #v-kanban');
  assert(vKanbanHtml.includes('id="kanbanOriginBadge"'), '#kanbanOriginBadge missing in #v-kanban');
  assert(vKanbanHtml.includes('onclick="setOriginFilter(\'ClickUp\')"'), 'ClickUp origin button missing in #v-kanban');
  assert(vKanbanHtml.includes('onclick="setOriginFilter(\'GLPI\')"'), 'GLPI origin button missing in #v-kanban');

  // Extract setOriginFilter, syncKanbanOriginUI, renderKPIs, handleDragOver, handleDrop, renderKanban
  const setOriginFn = (content.match(/function setOriginFilter\(src\)[\s\S]*?\n    \}/) || [])[0];
  const syncOriginFn = (content.match(/function syncKanbanOriginUI\(srcVal\)[\s\S]*?\n    \}/) || [])[0];
  const renderKpisFn = (content.match(/function renderKPIs\(k\)[\s\S]*?\n    \}/) || [])[0];
  const handleDragOverFn = (content.match(/function handleDragOver\(e\)[\s\S]*?\n    \}/) || [])[0];
  const handleDropFn = (content.match(/async function handleDrop\(e, targetStatus\)[\s\S]*?\n    \}/) || [])[0];
  const renderKanbanFn = (content.match(/function renderKanban\(list\)[\s\S]*?\n    \}/) || [])[0];
  const parseUtcFn = (content.match(/function parseUtcDate\(dateStr\)[\s\S]*?\n    \}/) || [])[0];
  const isItemRecentClosedFn = (content.match(/function isItemRecentClosed\(i\)[\s\S]*?\n    \}/) || [])[0];

  assert(setOriginFn, 'setOriginFilter function not found');
  assert(syncOriginFn, 'syncKanbanOriginUI function not found');
  assert(renderKpisFn, 'renderKPIs function not found');
  assert(handleDropFn, 'handleDrop function not found');
  assert(renderKanbanFn, 'renderKanban function not found');
  assert(parseUtcFn, 'parseUtcDate function not found');
  assert(isItemRecentClosedFn, 'isItemRecentClosed function not found');

  const pillButtons = ['', 'ClickUp', 'GLPI'].map(origin => {
    const classes = new Set(origin === '' ? ['kanban-origin-pill', 'active'] : ['kanban-origin-pill']);
    return {
      getAttribute: (k) => k === 'data-origin' ? origin : null,
      classList: {
        toggle: (c, force) => { if (force) classes.add(c); else classes.delete(c); },
        has: (c) => classes.has(c)
      }
    };
  });

  const kanbanGridClasses = new Set();
  const mockKanbanEls = {
    fSrc: { value: '' },
    fPrio: { value: '' },
    fResp: { value: '' },
    fSearch: { value: '' },
    kanbanOriginSelector: {
      querySelectorAll: () => pillButtons
    },
    kanbanOriginBadge: { textContent: '', style: {} },
    kanbanSubText: { textContent: '' },
    kpisContainer: { innerHTML: '' },
    navOver: { textContent: '', style: {} },
    navEspera: { textContent: '', style: {} },
    kanbanGrid: {
      innerHTML: '',
      classList: {
        add: (c) => kanbanGridClasses.add(c),
        remove: (c) => kanbanGridClasses.delete(c),
        toggle: (c, force) => { if (force) kanbanGridClasses.add(c); else kanbanGridClasses.delete(c); },
        has: (c) => kanbanGridClasses.has(c)
      }
    }
  };

  const kanbanToasts = [];
  const kanbanItems = [
    { id: 1, source: 'ClickUp', external_id: 'cu-1', title: 'Tarea ClickUp 1', status: 'pendiente', priority: 'Alta' },
    { id: 2, source: 'ClickUp', external_id: 'cu-2', title: 'Tarea ClickUp 2', status: 'curso', priority: 'Media' },
    { id: 3, source: 'GLPI', external_id: '236', title: 'Ticket GLPI 236', status: 'espera', priority: 'Alta', client_name: 'SYCOM' },
    { id: 4, source: 'GLPI', external_id: '237', title: 'Ticket GLPI 237', status: 'cerrado', priority: 'Baja', client_name: 'Microfilms Center' }
  ];

  const kanbanRunner = new Function(
    'document', 'escapeHtml', 'showToast', 'ALL_ITEMS',
    `
    let ACTIVE_KPI_FILTER = 'espera';
    let DRAGGED_ITEM_ID = null;
    let IS_DRAGGING = false;
    let HIGHLIGHT_ITEM_ID = null;
    let TV_MODE_ACTIVE = false;
    const API_URL = 'http://localhost:8000/api';
    function canUserEditItem() { return true; }
    function isCoordinator() { return true; }
    function isSpecialist() { return false; }
    function getItemTimeLabel() { return '10m'; }
    function getItemAttentionTime() { return '10m'; }
    function formatDate(d) { return d || '—'; }
    function getAuthHeaders() { return {}; }
    function loadData() { return Promise.resolve(); }
    function renderAll() {
      syncKanbanOriginUI();
      renderKPIs({ pendientes: 1, en_curso: 1, en_espera: 1, cerrados: 1, total_registrados: 4, total_actividades: 4 });
      const src = document.getElementById('fSrc').value;
      const filtered = src ? ALL_ITEMS.filter(i => i.source === src) : ALL_ITEMS;
      renderKanban(filtered);
    }
    ${setOriginFn}
    ${syncOriginFn}
    ${renderKpisFn}
    ${handleDragOverFn}
    ${handleDropFn}
    const onDrop = handleDrop;
    ${parseUtcFn}
    ${isItemRecentClosedFn}
    ${renderKanbanFn}
    return {
      setOriginFilter,
      syncKanbanOriginUI,
      renderKPIs,
      renderKanban,
      handleDragOver,
      handleDrop,
      onDrop,
      isItemRecentClosed,
      getActiveKpi: () => ACTIVE_KPI_FILTER,
      setDraggedId: (id) => { DRAGGED_ITEM_ID = id; }
    };
    `
  )(
    { getElementById: (id) => mockKanbanEls[id] || null },
    (s) => String(s || ''),
    (title, message, type) => { kanbanToasts.push({ title, message, type }); },
    kanbanItems
  );

  // 5A: Switch origin to 'ClickUp' -> renders all 4 columns including 'espera', no .cols-3, renders 'espera' KPI
  kanbanRunner.setOriginFilter('ClickUp');
  assert.strictEqual(mockKanbanEls.fSrc.value, 'ClickUp', '#fSrc should be updated to ClickUp');
  assert.strictEqual(pillButtons[1].classList.has('active'), true, 'ClickUp pill should be active');
  assert.strictEqual(pillButtons[0].classList.has('active'), false, 'Todos pill should not be active');
  assert(mockKanbanEls.kanbanOriginBadge.textContent.includes('ClickUp'), 'Badge should show ClickUp origin');
  assert.strictEqual(kanbanGridClasses.has('cols-3'), false, '#kanbanGrid must NOT have cols-3 class for ClickUp');
  assert(mockKanbanEls.kanbanGrid.innerHTML.includes('data-col="espera"'), 'Kanban must ALWAYS render En espera column for ClickUp');
  assert(mockKanbanEls.kanbanGrid.innerHTML.includes('data-col="pendiente"'), 'Kanban must render Pendientes column');
  assert(mockKanbanEls.kanbanGrid.innerHTML.includes('data-col="curso"'), 'Kanban must render En curso column');
  assert(mockKanbanEls.kanbanGrid.innerHTML.includes('data-col="cerrado"'), 'Kanban must render Cerrados column');
  assert(mockKanbanEls.kpisContainer.innerHTML.includes('data-kpi="espera"'), 'KPIs must render En Espera card for ClickUp');
  assert(!mockKanbanEls.kpisContainer.innerHTML.includes('Cumplimiento SLA'), 'KPIs must not have SLA compliance card');
  assert(!mockKanbanEls.kpisContainer.innerHTML.includes('data-kpi="atrasados"'), 'KPIs must not have Atrasadas card');

  // 5B: Switch origin to 'GLPI' -> shows all 4 columns including 'espera' and renders 'espera' KPI
  kanbanRunner.setOriginFilter('GLPI');
  assert.strictEqual(mockKanbanEls.fSrc.value, 'GLPI');
  assert.strictEqual(pillButtons[2].classList.has('active'), true, 'GLPI pill should be active');
  assert.strictEqual(kanbanGridClasses.has('cols-3'), false, '#kanbanGrid should not have cols-3 for GLPI');
  assert(mockKanbanEls.kanbanGrid.innerHTML.includes('data-col="espera"'), 'Kanban must render En espera column for GLPI');
  assert(mockKanbanEls.kpisContainer.innerHTML.includes('data-kpi="espera"'), 'KPIs must render En Espera card for GLPI');

  // 5B2: Verify badge hides when 0 and shows when >= 1
  kanbanRunner.renderKPIs({ pendientes: 0, en_curso: 0, en_espera: 0, cerrados: 0, total_actividades: 0 });
  assert.strictEqual(mockKanbanEls.navEspera.style.display, 'none', 'navEspera must be hidden when en_espera is 0');
  assert.strictEqual(mockKanbanEls.navEspera.textContent, '', 'navEspera textContent must be empty when 0');

  kanbanRunner.renderKPIs({ pendientes: 0, en_curso: 0, en_espera: 2, cerrados: 0, total_actividades: 2 });
  assert.strictEqual(mockKanbanEls.navEspera.style.display, '', 'navEspera must be visible when en_espera > 0');
  assert.strictEqual(mockKanbanEls.navEspera.textContent, 2, 'navEspera textContent must be count when > 0');

  // 5C: Switch origin to '' (Todos) and test dragging a ClickUp item onto 'espera' column -> successfully moves to 'espera'
  kanbanRunner.setOriginFilter('');
  assert.strictEqual(mockKanbanEls.fSrc.value, '');
  assert(mockKanbanEls.kanbanGrid.innerHTML.includes('data-col="espera"'), 'Kanban must render 4 columns for Todos');

  global.fetch = async (url, opts) => ({ ok: true, json: async () => ({ id: 1, status: 'espera' }) });
  global.alert = () => {};
  kanbanRunner.setDraggedId(1); // Item 1 is ClickUp
  await kanbanRunner.onDrop({
    preventDefault: () => {},
    currentTarget: { classList: { remove: () => {} } },
    dataTransfer: { getData: () => '1' }
  }, 'espera');
  assert.strictEqual(kanbanItems[0].status, 'espera', 'ClickUp item status must be updated to espera when dropped on espera');
  assert(kanbanToasts.some(t => t.title.includes('Estado sincronizado')), 'Toast should confirm ClickUp moved to En espera');

  // 5D: Verify dropping a ClickUp item onto 'curso' while filtered by ClickUp preserves ClickUp filter (does not leak GLPI cards)
  kanbanRunner.setOriginFilter('ClickUp');
  kanbanRunner.setDraggedId(1);
  global.fetch = async () => ({ ok: false, json: async () => ({ detail: 'Simulated error' }) });
  await kanbanRunner.onDrop({
    preventDefault: () => {},
    currentTarget: { classList: { remove: () => {} } },
    dataTransfer: { getData: () => '1' }
  }, 'curso');
  assert(!mockKanbanEls.kanbanGrid.innerHTML.includes('Ticket GLPI 236'), 'GLPI items must not leak into ClickUp-filtered Kanban after drop/rollback');
  assert(!mockKanbanEls.kanbanGrid.innerHTML.includes('Ticket GLPI 237'), 'GLPI items must not leak into ClickUp-filtered Kanban after drop/rollback');

  // 5E: Verify openModal template contains 'espera' option for all items (ClickUp and GLPI)
  assert(
    content.includes('<option value="espera"'),
    'openModal must render the espera status option'
  );

  // 5F: Verify that both standard Kanban and TV mode Kanban filter out closed items older than 24h
  assert(content.includes('function isItemRecentClosed(i)'), 'isItemRecentClosed must be defined');
  const tvKanbanCodeMatch = content.match(/else if \(s\.type === 'kanban'\)[\s\S]*?tv-kanban-cards/);
  assert(tvKanbanCodeMatch, 'TV mode kanban slide block must exist');
  assert(tvKanbanCodeMatch[0].includes('isItemRecentClosed'), 'TV mode kanban must apply isItemRecentClosed filter to closed items');

  // 5G: Verify exact rolling 24h calculation for isItemRecentClosed
  const nowTs = Date.now();
  const item23h = { status: 'cerrado', closed_at: new Date(nowTs - 23 * 3600 * 1000).toISOString() };
  const item24h1m = { status: 'cerrado', closed_at: new Date(nowTs - (24 * 3600 + 60) * 1000).toISOString() };
  const item25h = { status: 'cerrado', closed_at: new Date(nowTs - 25 * 3600 * 1000).toISOString() };

  assert.strictEqual(kanbanRunner.isItemRecentClosed(item23h), true, 'Item closed 23h ago must be recent closed');
  assert.strictEqual(kanbanRunner.isItemRecentClosed(item24h1m), false, 'Item closed 24h 1m ago must NOT be recent closed');
  assert.strictEqual(kanbanRunner.isItemRecentClosed(item25h), false, 'Item closed 25h ago must NOT be recent closed');

  console.log('✓ Test 5: Kanban 4-column layout for ClickUp/GLPI, full espera drag-and-drop, and clean KPIs verified');

  // Test 6: Verify showItemDetailModal and clean chronological fields in modal
  assert(content.includes('function showItemDetailModal('), 'showItemDetailModal must be defined');
  assert(content.includes('window.showItemDetailModal = showItemDetailModal;'), 'showItemDetailModal must be exposed on window');
  assert(content.includes('Fecha Recibido en Sycom:'), 'Modal must contain Fecha Recibido en Sycom');
  assert(content.includes('Fecha Escalado a Infra & BD:'), 'Modal must contain Fecha Escalado a Infra & BD');
  assert(content.includes('Tiempo de Atención:'), 'Modal must contain Tiempo de Atención');
  console.log('✓ Test 6: showItemDetailModal and clean chronological labels verified');

  // Test 7: Zero emoji check
  const emojiPattern = /[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{2702}-\u{27B0}\u{24C2}-\u{1F251}\u{1F900}-\u{1F9FF}\u{1FA70}-\u{1FAFF}\u{2600}-\u{26FF}]/u;
  assert(!emojiPattern.test(content), 'index.html must contain 0 emojis');
  console.log('✓ Test 7: Strict 0 emojis constraint verified');

  // Test 8: Zero SLA traces in DOM and JS logic
  const lines = content.split('\n');
  const slaMatches = [];
  lines.forEach((line, idx) => {
    const l = line.toLowerCase();
    if (l.includes('sla') && !l.includes('translate') && !l.includes('transform') && !l.includes('slash')) {
      slaMatches.push(`Line ${idx + 1}: ${line.trim()}`);
    }
  });
  assert.strictEqual(slaMatches.length, 0, `Expected 0 SLA occurrences in index.html, found:\n${slaMatches.join('\n')}`);
  console.log('✓ Test 8: Complete removal of SLA metrics, badges, and containers verified');

  // Test 9: 10-item pagination and centered controls
  assert(content.includes('id="resumenPagination"'), 'resumenPagination container missing');
  assert(content.includes('id="esperaPagination"'), 'esperaPagination container missing');
  assert(content.includes('id="escalationsPagination"'), 'escalationsPagination container missing');
  assert(content.includes('id="reportsHistoryPagination"'), 'reportsHistoryPagination container missing');
  assert(content.includes('const PAGE_SIZE_DEFAULT = 10;'), 'PAGE_SIZE_DEFAULT must be 10');
  assert(content.includes('function getPaginationPageNumbers('), 'getPaginationPageNumbers must be defined');
  assert(content.includes('function renderPaginationControls('), 'renderPaginationControls must be defined');

  // Test pagination helper logic in isolation
  const getPageNumsMatch = content.match(/function getPaginationPageNumbers\(currentPage, totalPages\)[\s\S]*?\n    \}/);
  const renderPaginationMatch = content.match(/function renderPaginationControls\(containerId, currentPage, totalItems, pageSize, onPageChangeFnName\)[\s\S]*?\n    \}/);
  assert(getPageNumsMatch && renderPaginationMatch, 'Pagination functions extraction failed');

  const paginationRunner = new Function(`
    ${getPageNumsMatch[0]}
    ${renderPaginationMatch[0]}
    return { getPaginationPageNumbers, renderPaginationControls };
  `)();

  // Test getPaginationPageNumbers
  assert.deepStrictEqual(paginationRunner.getPaginationPageNumbers(1, 1), [1]);
  assert.deepStrictEqual(paginationRunner.getPaginationPageNumbers(1, 3), [1, 2, 3]);
  assert.deepStrictEqual(paginationRunner.getPaginationPageNumbers(1, 10), [1, 2, 3, 4, '...', 10]);
  assert.deepStrictEqual(paginationRunner.getPaginationPageNumbers(5, 10), [1, '...', 4, 5, 6, '...', 10]);

  // Test renderPaginationControls DOM output
  const mockContainer = { innerHTML: '', style: {} };
  global.document = {
    getElementById: (id) => (id === 'testPagination' ? mockContainer : null)
  };
  paginationRunner.renderPaginationControls('testPagination', 1, 25, 10, 'onTestChange');
  assert.strictEqual(mockContainer.style.display, 'flex');
  assert(mockContainer.innerHTML.includes('Mostrando 1–10 de 25'), 'Pagination info text incorrect');
  assert(mockContainer.innerHTML.includes('disabled onclick="onTestChange(0)"'), 'Previous button should be disabled on page 1');
  assert(mockContainer.innerHTML.includes('class="pagination-btn active"'), 'Page 1 button should be active');
  assert(mockContainer.innerHTML.includes('onclick="onTestChange(2)"'), 'Next button should point to page 2');
  console.log('✓ Test 9: 10-item pagination with centered page controls and slice boundaries verified');

  // Test 10: Accurate UTC parsing and getTimeAgo without local-time offset distortion
  const parseUtcMatch = content.match(/function parseUtcDate\(dateStr\)[\s\S]*?\n    \}/);
  const timeAgoMatch = content.match(/function getTimeAgo\(dateStr\)[\s\S]*?\n    \}/);
  assert(parseUtcMatch && timeAgoMatch, 'parseUtcDate / getTimeAgo extraction failed');

  const timeAgoRunner = new Function(`
    ${parseUtcMatch[0]}
    ${timeAgoMatch[0]}
    return { parseUtcDate, getTimeAgo };
  `)();

  // Test parseUtcDate
  const parsed1 = timeAgoRunner.parseUtcDate('2026-11-10T20:03:00');
  const parsed2 = timeAgoRunner.parseUtcDate('2026-11-10T20:03:00Z');
  assert.strictEqual(parsed1.getTime(), parsed2.getTime(), 'parseUtcDate must treat naive ISO as UTC');

  // Test 24 hours and 3 minutes ago
  const nowMs = Date.now();
  const past24h3m = new Date(nowMs - (24 * 3600 + 3 * 60) * 1000).toISOString();
  assert.strictEqual(timeAgoRunner.getTimeAgo(past24h3m), 'hace 1d', '24h3m ago should return hace 1d');

  // Test 20 hours ago
  const past20h = new Date(nowMs - 20 * 3600 * 1000).toISOString();
  assert.strictEqual(timeAgoRunner.getTimeAgo(past20h), 'hace 20h 0m', '20h ago should return hace 20h 0m');
  console.log('✓ Test 10: Accurate UTC timezone parsing and getTimeAgo verified');

  // Test 11: Overtime Records (Horas Extras) module and Heatmap validation
  assert(content.includes('data-view="horas_extras"'), 'Sidebar navigation for Horas Extras missing');
  assert(content.includes('id="v-horas_extras"'), 'View container #v-horas_extras missing');
  assert(content.includes('id="chartOvertimeMatrix"'), 'Analytics Overtime Heatmap Card #chartOvertimeMatrix missing');
  assert(content.includes('id="btnNewActivityHeader"'), 'Header action button #btnNewActivityHeader missing');
  assert(content.includes('updateHeaderActionButton'), 'updateHeaderActionButton missing');

  // Test calculateOvertimeDurationPreview function
  const calcDurationMatch = content.match(/function calculateOvertimeDurationPreview\(\)[\s\S]*?\n    \}/);
  assert(calcDurationMatch, 'calculateOvertimeDurationPreview function not found');

  const calcDurationRunner = new Function(`
    function escapeHtml(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
    const mockElements = {
      'otStartTime': { value: '' },
      'otEndTime': { value: '' },
      'otDurationPreview': { innerHTML: '', textContent: '' },
      'btnSubmitOvertime': { disabled: false }
    };
    const document = {
      getElementById: (id) => mockElements[id] || { value: '', innerHTML: '', textContent: '', disabled: false }
    };
    ${calcDurationMatch[0]}
    return {
      run: (start, end) => {
        mockElements['otStartTime'].value = start;
        mockElements['otEndTime'].value = end;
        calculateOvertimeDurationPreview();
        return mockElements['otDurationPreview'].innerHTML;
      }
    };
  `)();

  const previewValid = calcDurationRunner.run('2026-11-11T18:00', '2026-11-11T20:30');
  assert(previewValid.includes('2h 30m') && previewValid.includes('2.50 hrs'), 'Live preview for 18:00-20:30 should be 2h 30m (2.50 hrs)');

  const previewInvalid = calcDurationRunner.run('2026-11-11T21:00', '2026-11-11T18:00');
  assert(previewInvalid.includes('debe ser posterior'), 'Negative duration should indicate invalid range');

  console.log('✓ Test 11: Overtime UI elements, live duration preview, and heatmap matrix integration verified');

  console.log('ALL FRONTEND TESTS PASSED SUCCESSFULLY!');
})();
