// Loaded inside cua_repl. UI operations use only the supplied cua App API.
// No DB writes, recording edits, playback, sharing, or deletion.
export function elements(ax) {
  return ax.split('\n').flatMap(line => {
    const m = line.match(/^(\s*)(\d+) (.*)$/);
    return m ? [{index: Number(m[2]), depth: m[1].length, text: m[3]}] : [];
  });
}

export function requireOne(items, label) {
  if (items.length !== 1) throw new Error(`${label}: expected one element, found ${items.length}`);
  return items[0];
}

export function assertReady(ax) {
  if (!ax.includes('ID: SceneWindow') || !ax.includes('ID: Main Window')) {
    throw new Error('Voice Memos main window is unavailable; check lock/permissions');
  }
  // '완료' is also the ordinary playback-view Done button. Row and folder
  // names are data, even when they match a recording-control label.
  const controls = elements(ax).filter(e =>
    !/Value: .*Secondary Actions: .*이동/.test(e.text) &&
    !/Description: .*, \d+개의 녹음 항목,/.test(e.text));
  if (controls.some(e => /^(?:버튼|button) (?:Description: )?(?:녹음 일시 정지|녹음 중지|일시 정지)(?:,|$)/.test(e.text))) {
    throw new Error('Recording or editing controls are active; stop without changes');
  }
}

export function allRecordings(ax) {
  return requireOne(elements(ax).filter(e =>
    /^(?:버튼|button)(?: \(selected\))? Description: 모든 녹음 항목, \d+개의 녹음 항목,/.test(e.text)
  ), 'All Recordings sidebar row');
}

function sidebar(ax) {
  const all = allRecordings(ax);
  const total = Number(all.text.match(/모든 녹음 항목, (\d+)개의 녹음 항목,/)[1]);
  const folders = elements(ax).flatMap(e => {
    const m = e.text.match(/^(?:버튼|button)(?: \(selected\))? Description: (.*), \d+개의 녹음 항목,/);
    return m && !['모든 녹음 항목', '최근 삭제된 항목', '즐겨찾기'].includes(m[1]) ? [m[1]] : [];
  });
  return {total, folders};
}

export function recordingElements(ax) {
  const entries = elements(ax);
  const list = requireOne(entries.filter(e => e.text.includes('ID: RecordingsList,')), 'RecordingsList');
  const start = entries.indexOf(list) + 1;
  const result = [];
  for (const e of entries.slice(start)) {
    if (e.depth <= list.depth) break;
    const m = e.text.match(/^(?:버튼|button)(?: \(selected\))? Description: (.*), Value: (.*), Secondary Actions: (.*)$/);
    if (!m || !m[3].split(', ').includes('이동')) continue;
    const title = m[1].match(/^(.*), (?:어제|월요일|화요일|수요일|목요일|금요일|토요일|일요일|(?:오전|오후) \d{1,2}:\d{2}|\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.)(?:,.*)?$/);
    if (!title) throw new Error('Unsupported recording date description; do not guess its title');
    result.push({...e, title: title[1], duration: m[2], selected: e.text.includes('(selected)'), actions: m[3].split(', ')});
  }
  const keys = result.map(row => JSON.stringify([row.title, row.duration]));
  if (new Set(keys).size !== keys.length) throw new Error('Ambiguous duplicate title/duration on screen');
  return result;
}

export function recordingRows(ax) {
  return recordingElements(ax).map(({title, duration}) => ({title, duration}));
}

export async function collectInventory(app) {
  const observe = async () => {
    const ax = await app.getAXState({disableDiffing: true, emit: false});
    assertReady(ax);
    return ax;
  };
  let ax = await observe();
  if (!elements(ax).some(e => e.text.includes('Description: 모든 녹음 항목,'))) {
    const toggle = requireOne(elements(ax).filter(e => /^(?:버튼|button) 사이드바$/.test(e.text)), 'Sidebar button');
    await app.click(toggle.index);
    ax = await observe();
  }
  let all = allRecordings(ax);
  if (!all.text.includes('(selected)')) {
    await app.click(all.index);
    ax = await observe();
  }
  const search = requireOne(elements(ax).filter(e => /^텍스트 필드 검색(?: |$)/.test(e.text)), 'Search field');
  const searchValue = search.text.match(/Value: (.*?)(?:, Secondary Actions:|$)/)?.[1] ?? '';
  if (searchValue) {
    await app.setValue(search.index, '');
    ax = await observe();
  }
  const initial = sidebar(ax);
  const guard = current => {
    const all = allRecordings(current);
    const now = sidebar(current);
    const search = requireOne(elements(current).filter(e => /^텍스트 필드 검색(?: |$)/.test(e.text)), 'Search field');
    if (!all.text.includes('(selected)') || now.total !== initial.total ||
        JSON.stringify([...now.folders].sort()) !== JSON.stringify([...initial.folders].sort()) ||
        /Value: (?!,|$)\S/.test(search.text)) {
      throw new Error('Selection, search, count, or folders changed during collection');
    }
  };
  guard(ax);
  const pageKey = current => JSON.stringify(recordingRows(current));
  const scroll = async direction => {
    const list = requireOne(elements(ax).filter(e => e.text.includes('ID: RecordingsList,')), 'RecordingsList');
    const action = `Scroll ${direction}`;
    const actions = list.text.split('Secondary Actions: ')[1]?.split(', ') ?? [];
    if (actions.includes(action)) await app.performSecondaryAction(list.index, action);
    else await app.scroll(list.index, direction === 'Up' ? 'up' : 'down', 1);
    const next = await observe();
    guard(next);
    return next;
  };
  // Start from the top, even after an earlier run left the list at the bottom.
  const limit = Math.max(initial.total + 2, 4);
  let atTop = initial.total === 0;
  for (let n = 0; !atTop && n < limit; n++) {
    const before = pageKey(ax);
    const next = await scroll('Up');
    atTop = pageKey(next) === before;
    ax = next;
  }
  if (!atTop) throw new Error('Could not reach the top of All Recordings');
  const rows = new Map();
  for (let n = 0; n < limit; n++) {
    for (const row of recordingRows(ax)) rows.set(JSON.stringify([row.title, row.duration]), row);
    if (rows.size === initial.total) return {total_count: initial.total, folders: initial.folders, rows: [...rows.values()]};
    if (rows.size > initial.total) throw new Error('Collected more rows than the sidebar count');
    const before = pageKey(ax);
    const next = await scroll('Down');
    if (pageKey(next) === before) break;
    ax = next;
  }
  throw new Error(`Incomplete or ambiguous inventory: ${rows.size}/${initial.total}`);
}
