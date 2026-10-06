// Runs the planner's exact title/folder actions through cua, never the database.
// Carry the skill's snapshot token into the dependency URL as well.
const {elements, requireOne, assertReady, allRecordings, recordingElements} =
  await import(new URL('./collect.mjs', import.meta.url).href + new URL(import.meta.url).search);

const normalized = value => value.normalize('NFC').trim().replace(/\s+/g, ' ');
function seconds(value) {
  const clock = value.match(/^(?:(\d+):)?(\d+):(\d+)$/);
  if (clock) return Number(clock[1] ?? 0)*3600 + Number(clock[2])*60 + Number(clock[3]);
  const parts = ['시간', '분', '초'].map(unit => Number(value.match(new RegExp(`(\\d+)${unit}`))?.[1] ?? 0));
  if (!/[\d]+(?:시간|분|초)/.test(value)) throw new Error('Unsupported recording duration');
  return parts[0]*3600 + parts[1]*60 + parts[2];
}
function searchField(ax) {
  return requireOne(elements(ax).filter(e => /^텍스트 필드 검색(?: |$)/.test(e.text)), 'Search field');
}
function titleField(ax) {
  return requireOne(elements(ax).filter(e => /^텍스트 필드 \(settable\) Value: /.test(e.text)), 'Selected recording title field');
}
function exactRow(ax, title, duration) {
  return requireOne(recordingElements(ax).filter(r => normalized(r.title) === normalized(title) && seconds(r.duration) === duration), 'Exact recording title/duration');
}
function selectedRows(ax, action) {
  return recordingElements(ax).filter(r => r.selected &&
    [action.current_title, action.desired_title].some(title => normalized(r.title) === normalized(title)) &&
    seconds(r.duration) === action.duration_seconds);
}
function selectedRow(ax, action) {
  return requireOne(selectedRows(ax, action), 'Selected recording identity');
}
function assertTitle(ax, title) {
  const field = titleField(ax);
  if (normalized(field.text.match(/Value: (.*), Secondary Actions:/)[1]) !== normalized(title)) {
    throw new Error('Selected recording title does not match the plan');
  }
  return field;
}
function folderButton(ax, name) {
  return requireOne(elements(ax).filter(e => {
    const m = e.text.match(/^버튼(?: \(selected\))? Description: (.*), \d+개의 녹음 항목,/);
    return m && normalized(m[1]) === normalized(name) && !e.text.includes('흐리게 표시됨');
  }), 'Exact destination folder');
}

export async function applyActions(app, plan) {
  const completed = [];
  let current;
  try {
    if (plan.status !== 'ok' || !Array.isArray(plan.actions)) throw new Error('Invalid planner result');
    const observe = async () => {
      const ax = await app.getAXState({disableDiffing: true, emit: false});
      assertReady(ax);
      return ax;
    };
    const revealSelected = async (ax, action) => {
      if (selectedRows(ax, action).length) return ax;
      // Clearing search can put an older selected recording offscreen.
      // Scroll the real list to it; never substitute an unseen DB row.
      const initial = allRecordings(ax).text;
      const limit = Number(initial.match(/(\d+)개의 녹음 항목/)[1]) + 2;
      const page = state => JSON.stringify(recordingElements(state).map(r => [r.title, r.duration]));
      const scroll = async direction => {
        const list = requireOne(elements(ax).filter(e => e.text.includes('ID: RecordingsList,')), 'RecordingsList');
        const name = `Scroll ${direction}`;
        if (list.text.split('Secondary Actions: ')[1]?.split(', ').includes(name)) await app.performSecondaryAction(list.index, name);
        else await app.scroll(list.index, direction.toLowerCase(), 1);
        const next = await observe();
        if (allRecordings(next).text !== initial) throw new Error('All Recordings changed while locating selection');
        assertTitle(next, action.current_title);
        return next;
      };
      let top = false;
      for (let n = 0; !top && n < limit; n++) {
        const previous = page(ax); ax = await scroll('Up'); top = page(ax) === previous;
      }
      if (!top) throw new Error('Could not reach the top while locating selection');
      for (let n = 0; n < limit; n++) {
        if (selectedRows(ax, action).length) return ax;
        const previous = page(ax); ax = await scroll('Down');
        if (page(ax) === previous) break;
      }
      throw new Error('Selected recording could not be located after clearing search');
    };
    for (const action of plan.actions) {
      current = action.z_pk;
      if (!action.current_title || !action.desired_title || !Number.isInteger(action.duration_seconds) ||
          (!action.rename_to && !action.move_to) ||
          (action.rename_to && action.rename_to !== action.desired_title) ||
          (action.move_to && action.move_to !== action.course)) throw new Error('Invalid recording action');
      let ax = await observe();
      if (!allRecordings(ax).text.includes('(selected)')) throw new Error('All Recordings must be selected');
      await app.setValue(searchField(ax).index, action.current_title);
      ax = await observe();
      let row = exactRow(ax, action.current_title, action.duration_seconds);
      await app.click(row.index);
      ax = await observe();
      row = exactRow(ax, action.current_title, action.duration_seconds);
      if (!row.selected) {
        throw new Error('Selected recording does not match the plan');
      }
      assertTitle(ax, action.current_title);
      // Keep the selection out of a search that would hide it on rename.
      await app.setValue(searchField(ax).index, '');
      ax = await observe();
      ax = await revealSelected(ax, action);
      row = selectedRow(ax, action);
      if (action.rename_to) {
        await app.click(assertTitle(ax, action.current_title).index);
        ax = await observe();
        ax = await revealSelected(ax, action);
        selectedRow(ax, action);
        await app.setValue(assertTitle(ax, action.current_title).index, action.rename_to);
        await app.pressKey('Return');
        ax = await observe();
        assertTitle(ax, action.desired_title);
        // Voice Memos may retain the old AX row label until the list is
        // rebuilt. The selected title field must already show the saved value.
        row = selectedRow(ax, action);
      }
      if (action.move_to) {
        if (!row.actions.includes('이동')) throw new Error('Move action unavailable');
        await app.performSecondaryAction(row.index, '이동');
        const sheet = await app.getAXState({disableDiffing: true, emit: false});
        if (!sheet.includes('시트') || !sheet.includes('ID: FoldersList') || !sheet.includes('폴더 선택')) {
          throw new Error('Unexpected folder-selection sheet');
        }
        await app.click(folderButton(sheet, action.move_to).index);
        ax = await observe();
      }
      if (action.rename_to || action.move_to) {
        await app.click(folderButton(ax, action.course).index);
        ax = await observe();
        const folder = folderButton(ax, action.course);
        if (!folder.text.includes('(selected)')) throw new Error('Destination folder was not selected');
        await app.setValue(searchField(ax).index, action.desired_title);
        ax = await observe();
        exactRow(ax, action.desired_title, action.duration_seconds);
        await app.click(allRecordings(ax).index);
        ax = await observe();
      }
      await app.setValue(searchField(ax).index, '');
      ax = await observe();
      if (!allRecordings(ax).text.includes('(selected)')) throw new Error('All Recordings selection was lost');
      completed.push({z_pk: action.z_pk, renamed: Boolean(action.rename_to), moved: Boolean(action.move_to)});
    }
    return {status: 'ok', ui_completed: completed};
  } catch (error) {
    // Never continue to the next recording or retry a possibly saved change.
    return {status: 'failed', ui_completed: completed, failed_z_pk: current, error: error.message};
  }
}
